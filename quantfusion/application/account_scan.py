"""Real-account point-in-time decision-support application."""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any, cast

import pandas as pd

from quantfusion.application.daily_report import publish_daily_report
from quantfusion.account.models import (
    AccountPosition,
    AccountSnapshot,
    PointInTimeSignal,
)
from quantfusion.account.service import (
    compute_target_shares,
    target_weight_for,
    trend_candidate_score,
    trend_candidate_components,
)
from quantfusion.account.snapshot import (
    EXPECTED_DATA_ERRORS,
    load_account_snapshot_with_sha256,
)
from quantfusion.config.engine import default_engine_config
from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.domain.models import Signal
from quantfusion.domain.rules import floor_to_lot, require_finite
from quantfusion.engine.universe import SleeveBacktestEngine
from quantfusion.risk.account_budget import plan_account_risk_budget
from quantfusion.config.profiles import config_for_symbol, get_symbol_profile
from quantfusion.config.regime import MAX_EVIDENCE_STALENESS_DAYS
from quantfusion.config.weak import weak_regime_config
from quantfusion.data import contracts as data_contracts
from quantfusion.data.providers import DataFetcher
from quantfusion.data.sessions import DEFAULT_CALENDAR_FILE, resolve_scan_dates, require_frame_coverage, index_coverage
from quantfusion.domain.models import BarContext
from quantfusion.engine.replay import RegimeAdaptiveBacktestEngine
from quantfusion.indicators.technical import Indicators
from quantfusion.io.artifacts import atomic_json
from quantfusion.research.fingerprints import account_source_sha, canonical_sequence_sha
from quantfusion.strategy.trend import (
    ATRChannelStrategy,
    DualMAStrategy,
    TurtleBreakoutStrategy,
)

_PreparedMarket = tuple[
    pd.DataFrame,
    str,
    dict[str, Any],
    dict[str, pd.Series],
]


class AccountSignalEngine:
    """基于真实持仓生成时点建议，不伪造实盘历史收益曲线。"""

    def __init__(self, *, cache_dir: str, regime_data_dir: str, calendar_file: str | Path = DEFAULT_CALENDAR_FILE) -> None:
        """绑定股票缓存目录和固定指数证据目录。"""
        self.cache_dir = cache_dir
        self.regime_data_dir = regime_data_dir
        self.calendar_file = calendar_file

    def _frame(self, code: str, as_of: str) -> pd.DataFrame:
        """加载截至指定日期的新鲜行情，并拒绝未来或陈旧观测。"""
        boundary = pd.Timestamp(as_of)
        if boundary is pd.NaT:
            raise ValueError("as_of must resolve to a valid date")
        boundary = cast(pd.Timestamp, boundary).normalize()
        start_boundary = cast(
            pd.Timestamp, boundary - pd.Timedelta(days=700)
        )
        start = start_boundary.strftime("%Y-%m-%d")
        frame = DataFetcher.load_stock_data(
            code,
            start,
            boundary.strftime("%Y-%m-%d"),
            data_dir=None,
            cache_dir=self.cache_dir,
        )
        if bool(frame.attrs.get("_stale", False)):
            cache_last_date = frame.attrs.get("_cache_last_date", "unknown")
            raise ValueError(
                f"{code} market data is provider-marked stale: "
                f"cache_last_date={cache_last_date}"
            )
        if frame.empty:
            raise ValueError("no market data")
        observed = pd.Timestamp(cast(Any, frame.index[-1]))
        if observed is pd.NaT:
            raise ValueError("latest market-data date is invalid")
        observed = cast(pd.Timestamp, observed).normalize()
        if observed > boundary:
            raise ValueError("market data contains a future observation")
        if (boundary - observed).days > MAX_EVIDENCE_STALENESS_DAYS:
            raise ValueError(
                f"latest market data is stale: {observed.date()} "
                f"(as of {boundary.date()})"
            )
        require_frame_coverage(frame, resolve_scan_dates(as_of, calendar_file=self.calendar_file), code)
        return frame

    @staticmethod
    def _latest_value(series: pd.Series | None, index: int) -> float:
        """读取指标末值；缺失或非有限值返回 NaN 供调用者显式判断。"""
        if series is None:
            return float("nan")
        value = float(series.iloc[index])
        return value if math.isfinite(value) else float("nan")

    def _evaluate_trend_candidates(
        self,
        symbols: dict[str, str],
        held: dict[str, AccountPosition],
        *,
        as_of: str,
        snapshot: AccountSnapshot,
        prepared: dict[str, _PreparedMarket],
    ) -> list[PointInTimeSignal]:
        """基于一次扫描已经冻结的完整横截面生成趋势候选。

        复用历史引擎的同一策略逻辑（唐奇安突破、双均线、ATR 通道）在
        ``as_of`` 当日收盘后做时点判断，不注入伪造历史持仓。调用方只有在
        全部预期候选的数据完整且实际截止日一致时才调用本方法，因此行业和
        市场相对强度不会在静默缩小后的股票池上重算。
        """
        candidates: list[PointInTimeSignal] = []
        as_of_timestamp = pd.Timestamp(as_of)
        if as_of_timestamp is pd.NaT:
            raise ValueError("as_of must resolve to a valid date")
        as_of_timestamp = cast(pd.Timestamp, as_of_timestamp).normalize()
        momentum_by_symbol: dict[str, tuple[float, float]] = {}
        for code, name in symbols.items():
            market = prepared.get(code)
            if market is None:
                continue
            frame, _, _, _ = market
            i = len(frame) - 1
            close = float(frame["close"].iloc[i])
            ret20 = (
                close / float(frame["close"].iloc[i - 20]) - 1.0
                if i >= 20 and float(frame["close"].iloc[i - 20]) > 0
                else 0.0
            )
            ret60 = (
                close / float(frame["close"].iloc[i - 60]) - 1.0
                if i >= 60 and float(frame["close"].iloc[i - 60]) > 0
                else ret20
            )
            momentum_by_symbol[code] = (ret20, ret60)

        group_samples: dict[str, list[tuple[float, float]]] = {}
        for code, returns in momentum_by_symbol.items():
            profile_name = get_symbol_profile(code, "unmapped")
            group_samples.setdefault(profile_name, []).append(returns)
        group_mean = {
            profile_name: (
                sum(value[0] for value in values) / len(values),
                sum(value[1] for value in values) / len(values),
            )
            for profile_name, values in group_samples.items()
        }
        market20 = (
            sum(value[0] for value in momentum_by_symbol.values())
            / len(momentum_by_symbol)
            if momentum_by_symbol else 0.0
        )
        market60 = (
            sum(value[1] for value in momentum_by_symbol.values())
            / len(momentum_by_symbol)
            if momentum_by_symbol else 0.0
        )

        for code, name in symbols.items():
            if code in held or code not in prepared:
                continue
            frame, evidence_date, cfg, indicators = prepared[code]
            i = len(frame) - 1
            close = float(frame["close"].iloc[i])
            strategies = [
                cls(cfg)
                for cls in (
                    TurtleBreakoutStrategy,
                    DualMAStrategy,
                    ATRChannelStrategy,
                )
            ]
            triggers: list[str] = []
            stop_prices: list[float] = []
            for strategy in strategies:
                ctx = BarContext(
                    i=i,
                    df=frame,
                    current_assets=snapshot.cash,
                    indicators=indicators,
                    symbol=code,
                    date=evidence_date,
                )
                signal = strategy.on_bar(ctx)
                if signal is None or signal.direction != "buy":
                    continue
                triggers.append(strategy.name)
                if signal.stop_loss is not None and math.isfinite(signal.stop_loss):
                    stop_prices.append(float(signal.stop_loss))
            if not triggers:
                continue
            # 多策略确认数量作为置信度的主成分。
            stop_price = min(stop_prices) if stop_prices else None
            profile_name = get_symbol_profile(code, "unmapped")
            group20, group60 = group_mean.get(profile_name, (market20, market60))
            symbol20, symbol60 = momentum_by_symbol.get(code, (0.0, 0.0))
            relative_raw = (
                0.5 * (symbol20 - group20)
                + 0.3 * (symbol60 - group60)
                + 0.2 * (group20 - market20)
            )
            industry_rs = max(0.0, min(1.0, 0.5 + relative_raw / 0.40))
            score = trend_candidate_score(
                frame,
                indicators,
                i,
                close,
                len(triggers),
                industry_relative_strength=industry_rs,
            )
            candidates.append(
                PointInTimeSignal(
                    symbol=code,
                    strategy_name="+".join(sorted(triggers)),
                    direction="buy",
                    score=score,
                    target_weight=target_weight_for(len(triggers)),
                    target_shares=0,
                    stop_price=stop_price,
                    reasons=tuple(triggers),
                    score_components=tuple(trend_candidate_components(
                        frame, indicators, i, close, len(triggers), industry_rs,
                    ).items()),
                )
            )
        return candidates

    @staticmethod
    def _apply_account_budget(
        snapshot: AccountSnapshot, prepared: dict[str, _PreparedMarket],
        actions: list[dict[str, Any]], *, equity: float, as_of: str,
    ) -> dict[str, Any]:
        """Apply AB5 to actual aggregate holdings, never invent historical sleeves.

        The required snapshot peak and today's complete valuation supply the
        high-water mark. Only a newly observed high can increase that mark.
        Recommendations remain indicative and bounded by known T+1 sellability.
        """
        cfg = default_engine_config()
        for _, _, symbol_cfg, _ in prepared.values():
            for key in ("slippage", "commission_rate", "stamp_duty", "min_commission"):
                cfg[key] = max(cfg[key], symbol_cfg.get(key, cfg[key]))
        peak = max(require_finite("peak_equity", snapshot.peak_equity, min_value=0.01), equity)
        by_symbol = {a["symbol"]: a for a in actions if a.get("shares", 0) > 0}
        books = [(0, p.symbol, "account_position", p.shares, by_symbol[p.symbol]["close"])
                 for p in snapshot.positions]
        buy_rows = [a for a in actions if a["action"] == "BUY_CANDIDATE"]
        buys = [(0, Signal(a["symbol"], "account_candidate", "buy",
                         target_shares=a["indicative_target_shares"], price=a["close"],
                         signal_date=as_of), a["indicative_target_shares"]*a["close"])
                for a in buy_rows]
        frames = {symbol: market[0] for symbol, market in prepared.items()}
        # Reuse the replay's causal laggard scores, without running a portfolio
        # or assigning fictitious sleeve ownership to real positions.
        scores = [SleeveBacktestEngine(peak, cfg=cfg, policy=PortfolioPolicy(),
                                      allocation_lookbacks=lookbacks, sleeve_name="account_risk_score")
                  ._allocation_scores(frames, pd.Timestamp(as_of))
                  for lookbacks in PortfolioPolicy().allocation_horizons]

        def score(symbol: str) -> float:
            return sum(values.get(symbol, 0.) for values in scores)/len(scores)

        receipt, reductions = plan_account_risk_budget(
            equity, peak, cfg, books, buys, score, date_str=as_of,
        )
        for reduction in reductions:
            row = by_symbol[reduction.symbol]
            existing = int(row.get("recommended_shares", 0))+int(row.get("blocked_shares", 0))
            desired = max(existing, reduction.shares)
            executable = min(desired, row["sellable_shares"])
            # Existing full-position advisories keep their original T+1
            # semantics; a new partial reduction uses executable board lots.
            if desired < row["shares"]:
                executable = floor_to_lot(executable)
            row.update(action="SELL" if row["action"] == "SELL" else "REDUCE_REVIEW",
                       recommended_shares=executable, blocked_shares=desired-executable,
                       execution_status=("EXECUTABLE" if executable == desired else
                                         "PARTIALLY_T1_BLOCKED" if executable else "T1_BLOCKED"),
                       reason=row["reason"]+"; account_budget_trim (close-known plan, not a fill)")
        for row in buy_rows:
            if receipt["buy_scale"] >= 1.:
                continue
            original = row["indicative_target_shares"]
            quantity = floor_to_lot(original*receipt["buy_scale"])
            row.update(indicative_target_shares=quantity,
                       original_indicative_target_shares=original,
                       target_weight=quantity*row["close"]/equity if equity else 0.,
                       reason=row["reason"]+"; account_budget_buy_reduced; queued sells give no credit")
            if not quantity:
                row.update(action="BLOCKED", execution_status="RISK_BUDGET_BLOCKED")
        return {"enabled": True, "mechanism": "AB5", "status": "APPLIED",
                "scope": "actual_account_snapshot_books", "planned_not_filled": True,
                "input_peak_equity": snapshot.peak_equity, **receipt,
                "planned_reductions": [asdict(a) for a in reductions]}

    def run(
        self,
        snapshot: AccountSnapshot,
        symbols: dict[str, str],
        *,
        as_of: str,
        expected_account_id: str = "main",
    ) -> dict[str, Any]:
        """生成持仓处置建议、候选买入和可审计估值完整性字段。"""
        if not isinstance(expected_account_id, str) or not expected_account_id:
            raise ValueError("expected account_id must be a non-empty string")
        if snapshot.account_id != expected_account_id:
            raise ValueError(
                f"expected account_id={expected_account_id!r}, "
                f"actual={snapshot.account_id!r}"
            )
        try:
            parsed_as_of = date.fromisoformat(as_of)
        except (TypeError, ValueError) as exc:
            raise ValueError("requested as_of must be an exact YYYY-MM-DD date") from exc
        if parsed_as_of.isoformat() != as_of:
            raise ValueError("requested as_of must use exact YYYY-MM-DD format")
        if snapshot.snapshot_date != as_of:
            raise ValueError(
                f"snapshot_date={snapshot.snapshot_date!r} does not match "
                f"requested_as_of={as_of!r}"
            )

        # The route decision and all later account advice share this one local
        # market snapshot.  In particular, weak-route leader selection must not
        # read a second, independently changing view of the same symbols.
        scan_dates = resolve_scan_dates(as_of, calendar_file=self.calendar_file)
        prepared: dict[str, _PreparedMarket] = {}
        market_errors: dict[str, str] = {}

        def prepare_market(code: str) -> _PreparedMarket:
            market = prepared.get(code)
            if market is not None:
                return market
            if code in market_errors:
                raise ValueError(market_errors[code])
            try:
                frame = self._frame(code, as_of)
                name = symbols.get(code, code)
                cfg = config_for_symbol(code, name=name)
                indicators = Indicators.compute_all(frame, cfg)
                close = float(frame["close"].iloc[-1])
                if not math.isfinite(close) or close <= 0:
                    raise ValueError("latest close must be finite and positive")
                observed = pd.Timestamp(cast(Any, frame.index[-1]))
                if observed is pd.NaT:
                    raise ValueError("latest market-data date is invalid")
                evidence_date = (
                    cast(pd.Timestamp, observed)
                    .normalize()
                    .date()
                    .isoformat()
                )
                market = (frame, evidence_date, cfg, indicators)
                prepared[code] = market
                return market
            except EXPECTED_DATA_ERRORS as exc:
                market_errors[code] = str(exc)
                raise

        def leader_frame_loader(code: str, boundary: str) -> pd.DataFrame:
            if boundary != as_of:
                raise ValueError(
                    f"leader boundary={boundary!r} does not match "
                    f"requested_as_of={as_of!r}"
                )
            return prepare_market(code)[0]

        data_contracts.refresh_regime_indices(
            self.regime_data_dir,
            end_date=as_of,
            strict=False,
        )
        checked_indices = index_coverage(self.regime_data_dir, scan_dates)
        decision = RegimeAdaptiveBacktestEngine().decide_current(
            symbols,
            as_of=as_of,
            data_dir=self.regime_data_dir,
            leader_data_dir=self.cache_dir,
            leader_frame_loader=leader_frame_loader,
        )

        held = {position.symbol: position for position in snapshot.positions}
        selected = (
            decision.leaders.selected_symbols
            if decision.leaders is not None
            else ()
        )
        buy_route = snapshot.cash > 0 and decision.name in {
            "frozen_trend_engine",
            "positive_momentum_hold",
        }
        required_codes = set(held)
        if buy_route and decision.name == "frozen_trend_engine":
            required_codes.update(symbols)
        elif decision.leaders is not None:
            # Completeness is defined by the caller's full candidate universe,
            # not only by whatever survived leader selection.  Include every
            # fixed reference symbol that the real selector already requested.
            required_codes.update(symbols)
            required_codes.update(prepared)
            required_codes.update(market_errors)

        # 单次扫描只冻结一份局部行情快照。持仓处置、横截面评分和目标数量
        # 都复用这份数据，避免同一次决策前后读取到不同状态。
        for code in sorted(required_codes):
            try:
                prepare_market(code)
            except EXPECTED_DATA_ERRORS:
                pass

        evidence_by_symbol = {
            code: prepared[code][1]
            for code in sorted(required_codes)
            if code in prepared
        }
        evidence_dates = set(evidence_by_symbol.values())
        evidence_date = (
            next(iter(evidence_dates)) if len(evidence_dates) == 1 else None
        )
        mixed_evidence_dates = len(evidence_dates) > 1
        required_market_errors = {
            code: market_errors[code]
            for code in sorted(required_codes)
            if code in market_errors
        }
        leader_unavailable: set[str] = set()
        leader_metadata_reasons: list[str] = []
        if decision.name == "positive_momentum_hold" or decision.leaders is not None:
            leaders = decision.leaders
            if leaders is None:
                leader_metadata_reasons.append("LEADER_EVIDENCE_UNAVAILABLE")
            else:
                expected_requested = tuple(sorted(symbols))
                actual_requested = tuple(sorted(leaders.requested_symbols))
                if actual_requested != expected_requested:
                    leader_metadata_reasons.append(
                        "LEADER_REQUESTED_SYMBOLS_MISMATCH:"
                        f"expected={','.join(expected_requested)}:"
                        f"actual={','.join(actual_requested)}"
                    )
                    leader_unavailable.update(
                        set(expected_requested) - set(actual_requested)
                    )
                leader_unavailable.update(leaders.unavailable_symbols)
                if (
                    leaders.observed_symbols != len(leaders.requested_symbols)
                    and not leaders.unavailable_symbols
                ):
                    leader_metadata_reasons.append(
                        "LEADER_EVIDENCE_INCOMPLETE:"
                        f"observed={leaders.observed_symbols}:"
                        f"requested={len(leaders.requested_symbols)}"
                    )
        data_complete = (
            not required_market_errors
            and not mixed_evidence_dates
            and not leader_metadata_reasons
            and not leader_unavailable
        )
        if mixed_evidence_dates:
            unavailable_symbols = sorted(required_codes)
        else:
            unavailable_symbols = sorted(
                set(required_market_errors) | leader_unavailable
            )
        buy_suppression_reasons = [
            f"MARKET_DATA_UNAVAILABLE:{code}"
            for code in sorted(required_market_errors)
        ]
        buy_suppression_reasons.extend(leader_metadata_reasons)
        buy_suppression_reasons.extend(
            f"LEADER_DATA_UNAVAILABLE:{code}"
            for code in sorted(leader_unavailable)
            if code not in required_market_errors
        )
        if mixed_evidence_dates:
            buy_suppression_reasons.extend(
                f"INCONSISTENT_EVIDENCE_DATE:{code}={evidence_by_symbol[code]}"
                for code in sorted(evidence_by_symbol)
            )

        actions: list[dict[str, Any]] = []
        priced_market_value = 0.0
        unpriced_symbols: list[str] = []
        as_of_timestamp = pd.Timestamp(as_of)
        if as_of_timestamp is pd.NaT:
            raise ValueError("as_of must resolve to a valid date")
        as_of_timestamp = cast(pd.Timestamp, as_of_timestamp).normalize()

        for position in snapshot.positions:
            code = position.symbol
            name = symbols.get(code, code)
            try:
                market = prepared.get(code)
                if market is None:
                    raise ValueError(
                        market_errors.get(code, "market data is unavailable")
                    )
                frame, position_evidence_date, cfg, indicators = market
                i = len(frame) - 1
                close = float(frame["close"].iloc[i])
                priced_market_value += position.shares * close

                atr = self._latest_value(indicators.get("atr"), i)
                ma_short = self._latest_value(indicators.get("ma_short"), i)
                ma_long = self._latest_value(indicators.get("ma_long"), i)
                parsed_entry = pd.Timestamp(position.entry_date)
                if parsed_entry is pd.NaT:
                    raise ValueError("entry_date must resolve to a valid date")
                entry_timestamp = cast(pd.Timestamp, parsed_entry).normalize()
                if entry_timestamp > as_of_timestamp:
                    raise ValueError(
                        f"position {code} entry_date is later than as_of"
                    )
                last_observed = pd.Timestamp(
                    cast(Any, frame.index[-1])
                ).normalize()
                if entry_timestamp > last_observed:
                    raise ValueError(
                        f"position {code} entry_date is later than "
                        f"market evidence_date={last_observed.date()}"
                    )
                first_observed = pd.Timestamp(cast(Any, frame.index[0])).normalize()
                peak_evidence_incomplete = (
                    entry_timestamp < first_observed
                    and position.highest_close is None
                )
                if peak_evidence_incomplete:
                    # 窗口早于建仓事实不完整时，不能把局部窗口峰值冒充持仓峰值。
                    observed_peak = close
                    peak = close
                    peak_evidence_status = "PEAK_EVIDENCE_INCOMPLETE"
                else:
                    since_entry = frame.loc[
                        frame.index >= entry_timestamp,
                        "close",
                    ]
                    observed_peak = (
                        float(cast(Any, since_entry.max()))
                        if not since_entry.empty
                        else close
                    )
                    if not math.isfinite(observed_peak) or observed_peak <= 0:
                        observed_peak = close
                    peak = max(
                        observed_peak,
                        position.highest_close or 0.0,
                        close,
                    )
                    peak_evidence_status = "COMPLETE"

                hard_stop = position.avg_cost * (
                    1.0 - float(cfg.get("hard_stop", 0.15))
                )
                active_stop = hard_stop
                peak_gain = peak / position.avg_cost - 1.0
                if (
                    not peak_evidence_incomplete
                    and math.isfinite(atr)
                    and atr > 0
                    and peak_gain
                    >= float(cfg.get("profit_lock_activation", 0.2))
                ):
                    active_stop = max(
                        active_stop,
                        peak - float(cfg.get("trail_atr_mult", 4.0)) * atr,
                    )

                reasons: list[str] = []
                if peak_evidence_incomplete:
                    reasons.append(
                        "PEAK_EVIDENCE_INCOMPLETE: entry predates the loaded "
                        "market window and highest_close is absent; peak-based "
                        "protection is disabled"
                    )
                action = "HOLD"
                if close <= active_stop:
                    action = "SELL"
                    reasons.append(
                        f"close {close:.2f} <= protective stop {active_stop:.2f}"
                    )
                elif (
                    math.isfinite(ma_short)
                    and math.isfinite(ma_long)
                    and ma_short < ma_long
                    and close < ma_short
                ):
                    action = "SELL"
                    reasons.append("short trend is below the long trend")
                elif decision.name == "cash_preservation":
                    action = "REDUCE_REVIEW"
                    reasons.append("current route is cash preservation")
                elif (
                    decision.name == "positive_momentum_hold"
                    and decision.leaders is not None
                    and code not in decision.leaders.selected_symbols
                ):
                    action = "REDUCE_REVIEW"
                    reasons.append(
                        "holding is outside the current weak-regime leaders"
                    )
                else:
                    reasons.append("no account-specific exit condition")

                execution_fields: dict[str, Any] = {
                    "sellable_shares": position.sellable_shares,
                }
                if action == "HOLD":
                    execution_fields.update(
                        recommended_shares=0,
                        blocked_shares=0,
                        execution_status="NO_ACTION",
                    )
                else:
                    executable = min(position.shares, position.sellable_shares)
                    blocked = position.shares - executable
                    if executable == position.shares:
                        execution_status = "EXECUTABLE"
                    elif executable > 0:
                        execution_status = "PARTIALLY_T1_BLOCKED"
                    else:
                        execution_status = "T1_BLOCKED"
                    execution_fields.update(
                        recommended_shares=executable,
                        blocked_shares=blocked,
                        execution_status=execution_status,
                    )

                actions.append(
                    {
                        "symbol": code,
                        "name": name,
                        "action": action,
                        "shares": position.shares,
                        "avg_cost": position.avg_cost,
                        "close": close,
                        "protective_stop": active_stop,
                        "peak_close": peak,
                        "peak_evidence_status": peak_evidence_status,
                        "evidence_date": position_evidence_date,
                        "reason": "; ".join(reasons),
                        **execution_fields,
                    }
                )
            except EXPECTED_DATA_ERRORS as exc:
                unpriced_symbols.append(code)
                actions.append(
                    {
                        "symbol": code,
                        "name": name,
                        "action": "DATA_ERROR",
                        "shares": position.shares,
                        "sellable_shares": position.sellable_shares,
                        "recommended_shares": None,
                        "blocked_shares": None,
                        "execution_status": "DATA_UNAVAILABLE",
                        "reason": str(exc),
                    }
                )

        valuation_complete = not unpriced_symbols
        buys_suppressed = not data_complete or not valuation_complete
        selected_candidates: list[PointInTimeSignal] = []
        ranked: list[PointInTimeSignal] = []
        slots_left = 0
        held_codes = set(held)
        route_candidates = (
            tuple(symbols)
            if decision.name == "frozen_trend_engine"
            else tuple(selected)
        )

        if buy_route and buys_suppressed:
            for code in route_candidates:
                if code in held_codes:
                    continue
                detail = market_errors.get(code)
                if detail is None and mixed_evidence_dates:
                    detail = (
                        "actual market evidence dates are inconsistent across "
                        "the required account universe"
                    )
                if detail is None:
                    detail = "required account evidence is incomplete"
                actions.append(
                    {
                        "symbol": code,
                        "name": symbols.get(code, code),
                        "action": "BLOCKED",
                        "shares": 0,
                        "reason": detail,
                    }
                )
        elif buy_route and decision.name == "frozen_trend_engine":
            trend_candidates = self._evaluate_trend_candidates(
                symbols,
                held,
                as_of=as_of,
                snapshot=snapshot,
                prepared=prepared,
            )
            ranked = sorted(
                trend_candidates,
                key=lambda item: (-item.score, item.symbol),
            )
            slots_left = max(
                int(default_engine_config().get("max_positions", 6))
                - len(held_codes),
                0,
            )
            selected_candidates = ranked[:slots_left]
        elif buy_route:
            strategy_weight = float(
                weak_regime_config(len(selected)).get("strategy_weight", 0.0)
            )
            selected_candidates = [
                PointInTimeSignal(
                    symbol=code,
                    strategy_name="positive_momentum_hold",
                    direction="buy",
                    score=1.0,
                    target_weight=strategy_weight,
                    target_shares=0,
                    stop_price=None,
                    reasons=(),
                )
                for code in selected
                if code not in held_codes
            ]

        for candidate in selected_candidates:
            frame, candidate_evidence_date, _, _ = prepared[candidate.symbol]
            close = float(frame["close"].iloc[-1])
            indicative_shares, locked_weight = compute_target_shares(
                candidate.symbol,
                close,
                candidate.target_weight,
                snapshot,
                selected_candidates,
                total_equity=snapshot.cash + priced_market_value,
            )
            strategies = sorted(candidate.reasons)
            trigger_description = (
                "+".join(strategies)
                if strategies
                else "positive_momentum_hold selection"
            )
            actions.append(
                {
                    "symbol": candidate.symbol,
                    "name": symbols.get(candidate.symbol, candidate.symbol),
                    "action": "BUY_CANDIDATE",
                    "shares": 0,
                    "indicative_target_shares": indicative_shares,
                    "execution_status": "INDICATIVE_REVIEW_ONLY",
                    "close": close,
                    "evidence_date": candidate_evidence_date,
                    "target_weight": locked_weight,
                    "confidence": candidate.score,
                    "strategies": strategies,
                    "protective_stop": candidate.stop_price,
                    "reason": (
                        f"{trigger_description}; 数量仅使用收盘价估算；"
                        "下一可交易日开盘价格、现金、涨跌停和人工状态仍需确认。"
                    ),
                }
            )

        estimated_market_value: float | None = (
            priced_market_value if valuation_complete else None
        )
        estimated_equity: float | None = (
            snapshot.cash + priced_market_value if valuation_complete else None
        )
        if valuation_complete and data_complete:
            account_budget = self._apply_account_budget(
                snapshot, prepared, actions, equity=snapshot.cash+priced_market_value, as_of=as_of,
            )
            if account_budget["buy_scale"] == 0.:
                buys_suppressed = True
                buy_suppression_reasons.append("ACCOUNT_RISK_BUDGET")
        else:
            account_budget = {"enabled": True, "mechanism": "AB5", "status": "NOT_READY",
                              "reason": "incomplete account valuation or market evidence"}

        advice_by_symbol = {row["symbol"]: row for row in actions}
        selected_codes = {candidate.symbol for candidate in selected_candidates}
        candidate_diagnostics = []
        for rank, candidate in enumerate(ranked, 1):
            advice = advice_by_symbol.get(candidate.symbol, {})
            candidate_diagnostics.append({
                "symbol": candidate.symbol, "rank": rank, "score": candidate.score,
                "score_components": dict(candidate.score_components),
                "confirmation_count": len(candidate.reasons),
                "selected_for_slots": candidate.symbol in selected_codes,
                "slot_constraint": "SELECTED" if candidate.symbol in selected_codes else "POSITION_SLOTS",
                "indicative_target_shares": advice.get("indicative_target_shares", 0),
                "constraint_reason": advice.get("reason", "POSITION_SLOTS"),
                "execution_status": advice.get("execution_status", "POSITION_SLOTS"),
            })
        market_identity = {
            code: {"evidence_date": value[1],
                   "frame_sha256": hashlib.sha256(value[0].to_csv(index=True).encode()).hexdigest(),
                   "config_sha256": canonical_sequence_sha(value[2])}
            for code, value in sorted(prepared.items())
        }
        if index_coverage(self.regime_data_dir, scan_dates) != checked_indices:
            raise ValueError("INDEX_EVIDENCE_CHANGED: account input changed during scan")
        return {
            "scan_dates": scan_dates,
            "candidate_diagnostics": candidate_diagnostics,
            "candidate_slots": slots_left,
            "account_code_sha256": account_source_sha(),
            "engine_config_sha256": canonical_sequence_sha(default_engine_config()),
            "market_evidence": market_identity,
            "index_evidence": checked_indices,
            "account_risk_budget": account_budget,
            "as_of": as_of,
            "mode": "account_decision_support",
            "account_id": snapshot.account_id,
            "snapshot_date": snapshot.snapshot_date,
            "requested_as_of": as_of,
            "cash": snapshot.cash,
            "priced_market_value": priced_market_value,
            "estimated_market_value": estimated_market_value,
            "estimated_equity": estimated_equity,
            "valuation_complete": valuation_complete,
            "unpriced_symbols": sorted(unpriced_symbols),
            "evidence_date": evidence_date,
            "data_complete": data_complete,
            "unavailable_symbols": unavailable_symbols,
            "market_data_errors": dict(sorted(required_market_errors.items())),
            "buys_suppressed": buys_suppressed,
            "buy_suppression_reasons": buy_suppression_reasons,
            "peak_equity": snapshot.peak_equity,
            "deployment_decision": asdict(decision),
            "actions": actions,
            "disclaimer": (
                "Decision support from a strict same-date account snapshot and "
                "consistent point-in-time market evidence. T+1 sellable shares "
                "constrain sell recommendations; buy quantities are close-price "
                "estimates for manual review, not broker-ready orders."
            ),
        }


def run_account_scan(
    *,
    account_path: str,
    symbols: dict[str, str],
    end_date: str,
    cache_dir: str,
    regime_data_dir: str,
    output_dir: str,
    expected_account_id: str = "main",
    calendar_file: str | Path = DEFAULT_CALENDAR_FILE,
) -> int:
    """运行账户扫描，写入工件并返回适合脚本调用的退出码。"""
    try:
        snapshot, snapshot_sha256 = load_account_snapshot_with_sha256(account_path)
        result = AccountSignalEngine(
            cache_dir=cache_dir,
            regime_data_dir=regime_data_dir,
            calendar_file=calendar_file,
        ).run(
            snapshot,
            symbols,
            as_of=end_date,
            expected_account_id=expected_account_id,
        )
        result["account_snapshot_sha256"] = snapshot_sha256
        output = Path(output_dir) / f"account_signals_{end_date}.json"
        atomic_json(result, output)
    except EXPECTED_DATA_ERRORS as exc:
        print(f"Account signal scan failed: {exc}")
        return 1

    publish_daily_report(output, symbols=symbols,
                         expected_identity=("account_snapshot_sha256", snapshot_sha256))
    print(f"机器结果已保存：{output}")
    return 0
