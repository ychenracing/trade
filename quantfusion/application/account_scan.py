"""Real-account point-in-time decision-support application."""

from __future__ import annotations

import math
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import pandas as pd

from quantfusion.account.models import AccountPosition, AccountSnapshot, PointInTimeSignal
from quantfusion.account.service import (
    compute_target_shares,
    target_weight_for,
    trend_candidate_score,
)
from quantfusion.account.snapshot import EXPECTED_DATA_ERRORS, load_account_snapshot
from quantfusion.config.regime import MAX_EVIDENCE_STALENESS_DAYS
from quantfusion.config.engine import default_engine_config
from quantfusion.data import contracts as data_contracts
from quantfusion.data.providers import DataFetcher
from quantfusion.domain.models import BarContext
from quantfusion.engine.replay import RegimeAdaptiveBacktestEngine
from quantfusion.engine.universe import BacktestEngine
from quantfusion.indicators.technical import Indicators
from quantfusion.io.artifacts import atomic_json
from quantfusion.strategy.trend import (
    ATRChannelStrategy,
    DualMAStrategy,
    TurtleBreakoutStrategy,
)

class AccountSignalEngine:
    """基于真实持仓生成时点建议，不伪造实盘历史收益曲线。"""

    def __init__(self, *, cache_dir: str, regime_data_dir: str) -> None:
        """绑定股票缓存目录和固定指数证据目录。"""
        self.cache_dir = cache_dir
        self.regime_data_dir = regime_data_dir

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
    ) -> tuple[list[PointInTimeSignal], dict[str, str], list[str]]:
        """为未持有标的生成趋势路线的新买入候选，并返回候选集/被阻断原因。

        复用历史引擎的同一策略逻辑（唐奇安突破、双均线、ATR 通道）在
        ``as_of`` 当日收盘后做时点判断，不注入伪造历史持仓。只有至少一个
        策略给出买入信号、且数据完整（无未来观测、无陈旧数据）的标的才会
        被纳入候选。返回 (候选信号列表, 代码->被阻断原因字典, 数据不足或
        陈旧的标的代码列表)。
        """
        candidates: list[PointInTimeSignal] = []
        blocked: dict[str, str] = {}
        unpriced: list[str] = []
        as_of_timestamp = pd.Timestamp(as_of)
        if as_of_timestamp is pd.NaT:
            raise ValueError("as_of must resolve to a valid date")
        as_of_timestamp = cast(pd.Timestamp, as_of_timestamp).normalize()
        prepared: dict[
            str, tuple[pd.DataFrame, dict[str, Any], dict[str, pd.Series]]
        ] = {}
        momentum_by_symbol: dict[str, tuple[float, float]] = {}
        for code, name in symbols.items():
            try:
                frame = self._frame(code, as_of)
            except EXPECTED_DATA_ERRORS as exc:
                if code not in held:
                    unpriced.append(code)
                    blocked[code] = str(exc)
                continue
            cfg = BacktestEngine.config_for_symbol(code, name=name)
            indicators = Indicators.compute_all(frame, cfg)
            prepared[code] = (frame, cfg, indicators)
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
            profile_name = BacktestEngine.get_symbol_profile(code, "unmapped")
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
            frame, cfg, indicators = prepared[code]
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
                    date=str(as_of_timestamp.date()),
                )
                signal = strategy.on_bar(ctx)
                if signal is None or signal.direction != "buy":
                    continue
                triggers.append(strategy.name)
                if signal.stop_loss is not None and math.isfinite(signal.stop_loss):
                    stop_prices.append(float(signal.stop_loss))
            if not triggers:
                continue
            # 多袖套确认数量作为置信度的主成分。
            stop_price = min(stop_prices) if stop_prices else None
            profile_name = BacktestEngine.get_symbol_profile(code, "unmapped")
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
                )
            )
        return candidates, blocked, unpriced

    def run(
        self,
        snapshot: AccountSnapshot,
        symbols: dict[str, str],
        *,
        as_of: str,
    ) -> dict[str, Any]:
        """生成持仓处置建议、候选买入和可审计估值完整性字段。"""
        data_contracts.refresh_regime_indices(
            self.regime_data_dir,
            end_date=as_of,
            strict=False,
        )
        decision = RegimeAdaptiveBacktestEngine().decide_current(
            symbols,
            as_of=as_of,
            data_dir=self.regime_data_dir,
            leader_data_dir=self.cache_dir,
        )

        held = {position.symbol: position for position in snapshot.positions}
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
                frame = self._frame(code, as_of)
                cfg = BacktestEngine.config_for_symbol(code, name=name)
                indicators = Indicators.compute_all(frame, cfg)
                i = len(frame) - 1
                close = float(frame["close"].iloc[i])
                if not math.isfinite(close) or close <= 0:
                    raise ValueError("latest close must be finite and positive")
                priced_market_value += position.shares * close

                atr = self._latest_value(indicators.get("atr"), i)
                ma_short = self._latest_value(indicators.get("ma_short"), i)
                ma_long = self._latest_value(indicators.get("ma_long"), i)
                if position.entry_date:
                    parsed_entry = pd.Timestamp(position.entry_date)
                    if parsed_entry is pd.NaT:
                        raise ValueError("entry_date must resolve to a valid date")
                    entry_timestamp = cast(pd.Timestamp, parsed_entry).normalize()
                    if entry_timestamp > as_of_timestamp:
                        raise ValueError(
                            f"position {code} entry_date is later than as_of"
                        )
                    since_entry = frame.loc[
                        frame.index >= entry_timestamp,
                        "close",
                    ]
                else:
                    since_entry = frame["close"]
                observed_peak = (
                    float(cast(Any, since_entry.max()))
                    if not since_entry.empty
                    else close
                )
                if not math.isfinite(observed_peak) or observed_peak <= 0:
                    observed_peak = close
                peak = max(observed_peak, position.highest_close or 0.0, close)

                hard_stop = position.avg_cost * (
                    1.0 - float(cfg.get("hard_stop", 0.15))
                )
                active_stop = hard_stop
                peak_gain = peak / position.avg_cost - 1.0
                if (
                    math.isfinite(atr)
                    and atr > 0
                    and peak_gain
                    >= float(cfg.get("profit_lock_activation", 0.2))
                ):
                    active_stop = max(
                        active_stop,
                        peak - float(cfg.get("trail_atr_mult", 4.0)) * atr,
                    )

                reasons: list[str] = []
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
                elif position.sellable_shares is None:
                    execution_fields.update(
                        recommended_shares=None,
                        blocked_shares=None,
                        execution_status="SELLABLE_UNKNOWN",
                    )
                    reasons.append(
                        "T+1 可卖数量未知，必须人工核验，"
                        "不得把当前总持仓当作可执行卖出数量。"
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
        selected = (
            decision.leaders.selected_symbols
            if decision.leaders is not None
            else ()
        )
        if (
            valuation_complete
            and decision.name == "positive_momentum_hold"
            and snapshot.cash > 0
        ):
            for code in selected:
                if code not in held:
                    actions.append(
                        {
                            "symbol": code,
                            "name": symbols.get(code, code),
                            "action": "BUY_CANDIDATE",
                            "shares": 0,
                            "reason": "current weak-regime leader",
                        }
                    )

        # 趋势路线：补齐新买入候选、目标股数与买入排序（P0-2）。
        trend_candidates: list[PointInTimeSignal] = []
        trend_blocked: dict[str, str] = {}
        if (
            valuation_complete
            and decision.name == "frozen_trend_engine"
            and snapshot.cash > 0
        ):
            trend_candidates, trend_blocked, _ = (
                self._evaluate_trend_candidates(
                    symbols,
                    held,
                    as_of=as_of,
                    snapshot=snapshot,
                )
            )
            # 统一买入排序：分数降序，分数相同按代码字典序保证确定性。
            ranked = sorted(
                trend_candidates,
                key=lambda item: (-item.score, item.symbol),
            )
            held_codes = set(held)
            default_cfg = default_engine_config()
            slots_left = max(
                int(default_cfg.get("max_positions", 6)) - len(held_codes), 0
            )
            # 只对实际将要买入的候选子集计算现金分配：分母必须只由被选中的
            # 候选构成，否则 total_weight 会包含未选中候选的权重，稀释每只
            # 选中股票的分配额（account_signal_engine.compute_target_shares）。
            selected_candidates = ranked[:max(slots_left, 0)]
            for candidate in selected_candidates:
                try:
                    frame = self._frame(candidate.symbol, as_of)
                    close = float(frame["close"].iloc[-1])
                except EXPECTED_DATA_ERRORS:
                    continue
                if not math.isfinite(close) or close <= 0:
                    continue
                target_shares, locked_weight = compute_target_shares(
                    candidate.symbol,
                    close,
                    candidate.target_weight,
                    snapshot,
                    selected_candidates,
                    total_equity=(
                        snapshot.cash + priced_market_value
                        if valuation_complete
                        else None
                    ),
                )
                actions.append(
                    {
                        "symbol": candidate.symbol,
                        "name": symbols.get(candidate.symbol, candidate.symbol),
                        "action": "BUY_CANDIDATE",
                        "shares": target_shares,
                        "close": close,
                        "target_weight": locked_weight,
                        "target_shares": target_shares,
                        "confidence": candidate.score,
                        "sleeves": candidate.strategy_name,
                        "protective_stop": candidate.stop_price,
                        "reason": (
                            "trend entry confirmed by "
                            + candidate.strategy_name
                            + f" (score {candidate.score:.3f})"
                        ),
                    }
                )
            for code, why in trend_blocked.items():
                if code not in held_codes:
                    actions.append(
                        {
                            "symbol": code,
                            "name": symbols.get(code, code),
                            "action": "BLOCKED",
                            "shares": 0,
                            "reason": why,
                        }
                    )

        estimated_market_value: float | None = (
            priced_market_value if valuation_complete else None
        )
        estimated_equity: float | None = (
            snapshot.cash + priced_market_value if valuation_complete else None
        )
        return {
            "as_of": as_of,
            "mode": "account_decision_support",
            "cash": snapshot.cash,
            "priced_market_value": priced_market_value,
            "estimated_market_value": estimated_market_value,
            "estimated_equity": estimated_equity,
            "valuation_complete": valuation_complete,
            "unpriced_symbols": sorted(unpriced_symbols),
            "buys_suppressed": not valuation_complete,
            "peak_equity": snapshot.peak_equity,
            "deployment_decision": asdict(decision),
            "actions": actions,
            "disclaimer": (
                "Decision support only. Orders are not sent to a broker and "
                "share quantities require manual confirmation."
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
) -> int:
    """运行账户扫描，写入工件并返回适合脚本调用的退出码。"""
    try:
        snapshot = load_account_snapshot(account_path)
        result = AccountSignalEngine(
            cache_dir=cache_dir,
            regime_data_dir=regime_data_dir,
        ).run(snapshot, symbols, as_of=end_date)
        output = Path(output_dir) / f"account_signals_{end_date}.json"
        atomic_json(result, output)
    except EXPECTED_DATA_ERRORS as exc:
        print(f"Account signal scan failed: {exc}")
        return 1

    print("=" * 72)
    print("  Real-account decision support")
    print("=" * 72)
    print(f"  As of: {end_date}")
    if result["estimated_equity"] is None:
        print(
            "  Estimated equity: unavailable "
            f"(unpriced holdings: {', '.join(result['unpriced_symbols'])})"
        )
    else:
        print(f"  Estimated equity: {result['estimated_equity']:,.0f}")
    print(f"  Route: {result['deployment_decision']['name']}")
    for action in result["actions"]:
        summary = f"  {action['symbol']} {action['name']}: {action['action']}"
        if "execution_status" in action:
            recommended = action.get("recommended_shares")
            displayed = "UNKNOWN" if recommended is None else str(recommended)
            summary += (
                f" | recommended_shares={displayed}"
                f" | execution_status={action['execution_status']}"
            )
        print(f"{summary} | {action['reason']}")
    print(f"  Artifact: {output}")
    return 0
