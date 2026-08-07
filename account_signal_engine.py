"""真实账户的时点决策支持，不向历史回测状态机注入实盘持仓。"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from numbers import Real
from pathlib import Path
from typing import Any, cast

import pandas as pd

import market_data_contracts
import quant_fusion as qf
import regime_adaptive as ra


_SYMBOL_RE = re.compile(r"^\d{6}$")
_EXPECTED_DATA_ERRORS = (
    ImportError,
    IndexError,
    KeyError,
    OSError,
    RuntimeError,
    ValueError,
)


@dataclass(frozen=True, slots=True)
class AccountPosition:
    """描述账户快照中的一只实际持仓。

    除持仓数量与成本外，还保留 T+1 可卖股数、建仓日期、持仓来源与最近加仓
    日期，使账户引擎能真实复现 T+1 卖出约束、来源审计与加仓节奏。
    """

    symbol: str
    shares: int
    avg_cost: float
    entry_date: str
    highest_close: float | None = None
    sellable_shares: int | None = None
    position_source: str | None = None
    last_add_date: str | None = None


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """保存现金、历史权益峰值和实际持仓的不可变快照。

    跨日状态（冷却、路线、风险锁、待执行订单、上次执行报告、权益历史）是
    可选字段，向后兼容仅含现金/峰值/持仓的最小快照；提供时用于复现 T+1、
    冷却、风险锁与订单连续性的真实约束。
    """

    cash: float
    peak_equity: float
    positions: tuple[AccountPosition, ...]
    cooldowns: dict[str, Any] = field(default_factory=dict)
    route_state: dict[str, Any] = field(default_factory=dict)
    risk_state: dict[str, Any] = field(default_factory=dict)
    pending_orders: tuple[Any, ...] = ()
    last_execution_report: dict[str, Any] = field(default_factory=dict)
    equity_history: tuple[dict[str, Any], ...] = ()
    account_id: str = "main"
    schema_version: int = 2


@dataclass(frozen=True, slots=True)
class PointInTimeSignal:
    """一只股票在指定时点的单策略趋势信号。

    由历史引擎的同一策略逻辑在 ``as_of`` 当日收盘后生成，方向为买入或卖出，
    并携带目标权重、目标股数、保护止损和拒绝原因。该结构不注入任何伪造历史
    持仓，只基于真实账户与截至 ``as_of`` 的行情。
    """

    symbol: str
    strategy_name: str
    direction: str
    score: float
    target_weight: float
    target_shares: int
    stop_price: float | None
    reasons: tuple[str, ...]
    blocked_reason: str | None = None


@dataclass(frozen=True, slots=True)
class AccountTarget:
    """把三袖套信号汇总为单一账户净目标。

    账户模式不以"fast/base/slow 各自买多少"示人，而是输出净账户目标：
    当前持股、理论三袖套目标、账户约束后目标、建议增减股数与目标权重。
    """

    symbol: str
    current_shares: int
    target_shares: int
    delta_shares: int
    target_weight: float
    confidence: float
    contributing_sleeves: tuple[str, ...]
    reasons: tuple[str, ...]
    blocked_reasons: tuple[str, ...] = ()


def _trend_candidate_score(
    frame: pd.DataFrame,
    indicators: dict[str, pd.Series],
    index: int,
    close: float,
    trigger_count: int,
) -> float:
    """计算买入排序分：多确认优先，辅以风险调整动量与趋势持续性。

    权重与报告一致：35% 多袖套确认、25% 风险调整动量、10% 突破质量、
    10% 趋势持续性、5% 流动性（其余给行业相对强度留白，由调用方补充）。
    分数用于决定有限现金与持仓槽位分配顺序，不直接放大仓位。
    """
    if not math.isfinite(close) or close <= 0:
        return 0.0
    confirmation = trigger_count / 3.0
    atr = float(indicators.get("atr").iloc[index]) if indicators.get("atr") is not None else float("nan")
    momentum = 0.0
    if index >= 20:
        prior = float(frame["close"].iloc[index - 20])
        if math.isfinite(prior) and prior > 0:
            momentum = close / prior - 1.0
    risk_adjusted_momentum = (
        momentum / (atr / close) if math.isfinite(atr) and atr > 0 else momentum
    )
    ma_short = float(indicators.get("ma_short").iloc[index]) if indicators.get("ma_short") is not None else float("nan")
    ma_long = float(indicators.get("ma_long").iloc[index]) if indicators.get("ma_long") is not None else float("nan")
    trend_persistence = 0.0
    if math.isfinite(ma_short) and math.isfinite(ma_long) and ma_long > 0:
        trend_persistence = max(0.0, min(1.0, (ma_short - ma_long) / ma_long))
    quality = 0.0
    if index >= 5:
        high5 = float(frame["high"].iloc[max(0, index - 5): index + 1].max())
        if high5 > 0:
            quality = max(0.0, min(1.0, close / high5))
    volume_quality = 0.0
    if "volume" in frame.columns and index >= 20:
        cur = float(frame["volume"].iloc[index])
        avg = float(frame["volume"].iloc[index - 20: index].mean())
        if math.isfinite(cur) and math.isfinite(avg) and avg > 0:
            volume_quality = max(0.0, min(1.0, cur / avg))
    score = (
        0.35 * confirmation
        + 0.25 * max(0.0, min(1.0, risk_adjusted_momentum / 0.5))
        + 0.10 * quality
        + 0.10 * trend_persistence
        + 0.05 * volume_quality
    )
    return score if math.isfinite(score) else 0.0


def _target_weight_for(trigger_count: int) -> float:
    """按确认强度给出建议目标权重（三确认 -> 更高，单确认 -> 试探）。"""
    if trigger_count >= 3:
        return 0.60
    if trigger_count == 2:
        return 0.40
    return 0.25


def _compute_target_shares(
    symbol: str,
    close: float,
    target_weight: float,
    snapshot: AccountSnapshot,
    ranked_candidates: list[PointInTimeSignal],
    *,
    total_equity: float | None = None,
) -> tuple[int, float]:
    """在总仓位上限内把现金分配到排序后的候选，返回（目标股数, 目标权重）。

    总仓位不超过 100%，单票不超过 60%；股数按 A 股整手向下取整。现金不足以
    覆盖一手时不产生买入。这是账户级净目标，不放大三袖套仓位。
    """
    from quant_fusion import _floor_to_lot

    if not math.isfinite(close) or close <= 0:
        return 0, 0.0
    cash = max(snapshot.cash, 0.0)
    # 单票上限以账户净权益为基准（现金 + 已持仓市值），而非仅现金：否则一个
    # 已有大量持仓的账户会把全部现金投入同一标的，使该票占总权益比例失控。
    # 未提供总权益时退化为现金（既有行为）。
    equity = total_equity if total_equity is not None and total_equity > 0 else cash
    total_weight = sum(item.target_weight for item in ranked_candidates) or 1.0
    # 现金分配取两个约束的较小者：一是按候选权重把可用现金拆分的份额
    # （`cash * target_weight / total_weight`，保证所有候选合计不超过现金，
    # 避免多候选时每只都顶格吃现金导致超配）；二是该候选建议的目标权重占净
    # 权益的份额（`equity * target_weight`，保留单/双/三确认的 0.25/0.40/0.60
    # 试探意图，不被归一化抹平）。
    alloc = min(
        cash * target_weight / total_weight,
        equity * target_weight,
    )
    # 单票硬上限：任何一只被选中的候选最多动用账户净权益的 60%，即使
    # 归一化后它原本会吃下全部现金（单候选时 target_weight/total_weight=1）。
    # 否则单确认（0.25）/双确认（0.40）会被放大成满仓，违反"单票不超过 60%"。
    alloc = min(alloc, 0.60 * equity)
    shares = _floor_to_lot(alloc / close)
    if shares <= 0:
        return 0, 0.0
    locked = min(target_weight, 0.60)
    return shares, locked


def _require_real(name: str, value: object, *, minimum: float = 0.0) -> float:
    """拒绝布尔值和字符串，并返回满足下界的有限实数。"""
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a real number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < minimum:
        raise ValueError(f"{name} must be finite and >= {minimum}")
    return normalized


def _require_positive_int(name: str, value: object) -> int:
    """只接受严格正整数，避免 JSON 布尔值或小数被静默转换。"""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _require_non_negative_int(name: str, value: object) -> int:
    """只接受非负整数（T+1 可卖股数允许为 0）。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _optional_positive_real(name: str, value: object) -> float | None:
    """校验可选的正有限实数。"""
    if value is None:
        return None
    normalized = _require_real(name, value, minimum=0.0)
    if normalized <= 0:
        raise ValueError(f"{name} must be > 0 when provided")
    return normalized


def _validate_entry_date(value: object, *, symbol: str) -> str:
    """校验可选建仓日期并规范为 YYYY-MM-DD。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        timestamp = pd.Timestamp(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"position {symbol} has invalid entry_date") from exc
    if timestamp is pd.NaT:
        raise ValueError(f"position {symbol} has invalid entry_date")
    return cast(pd.Timestamp, timestamp).strftime("%Y-%m-%d")


def _optional_text(value: object) -> str | None:
    """把可选标量规范为去空白字符串；缺失或空串返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_mapping(value: object, *, name: str = "mapping") -> dict[str, Any]:
    """校验可选对象字段必须为字典，缺失时返回空字典。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object when provided")
    return dict(value)


def _optional_int(value: object, *, default: int) -> int:
    """校验可选正整数；缺失或非正整数时返回默认值。"""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("optional integer must be a non-negative integer")
    return value


def load_account_snapshot(path: str | Path) -> AccountSnapshot:
    """从严格 JSON 文件加载并验证真实账户快照。"""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read account snapshot: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("account snapshot root must be an object")

    cash = _require_real("account cash", payload.get("cash", 0.0))
    peak = _require_real("peak_equity", payload.get("peak_equity", cash))

    raw_positions = payload.get("positions", {})
    if not isinstance(raw_positions, dict):
        raise ValueError("positions must be an object keyed by stock code")

    positions: list[AccountPosition] = []
    for raw_code, raw in raw_positions.items():
        code = str(raw_code)
        if _SYMBOL_RE.fullmatch(code) is None:
            raise ValueError(f"invalid stock code in account snapshot: {code!r}")
        if not isinstance(raw, dict):
            raise ValueError(f"position {code} must be an object")

        shares = _require_positive_int(f"position {code} shares", raw.get("shares", 0))
        avg_cost = _require_real(
            f"position {code} avg_cost",
            raw.get("avg_cost", raw.get("price", 0.0)),
        )
        if avg_cost <= 0:
            raise ValueError(f"position {code} avg_cost must be > 0")
        highest_close = _optional_positive_real(
            f"position {code} highest_close",
            raw.get("highest_close"),
        )
        sellable = raw.get("sellable_shares")
        if sellable is not None:
            sellable = _require_non_negative_int(
                f"position {code} sellable_shares", sellable
            )
            if sellable > shares:
                raise ValueError(
                    f"position {code} sellable_shares must not exceed shares"
                )
        last_add = _validate_entry_date(raw.get("last_add_date", ""), symbol=code)
        positions.append(
            AccountPosition(
                symbol=code,
                shares=shares,
                avg_cost=avg_cost,
                entry_date=_validate_entry_date(raw.get("entry_date", ""), symbol=code),
                highest_close=highest_close,
                sellable_shares=sellable,
                position_source=_optional_text(raw.get("position_source")),
                last_add_date=last_add or None,
            )
        )

    positions.sort(key=lambda item: item.symbol)
    return AccountSnapshot(
        cash=cash,
        peak_equity=peak,
        positions=tuple(positions),
        cooldowns=_optional_mapping(payload.get("cooldowns")),
        route_state=_optional_mapping(payload.get("route_state")),
        risk_state=_optional_mapping(payload.get("risk_state")),
        pending_orders=tuple(payload.get("pending_orders", ())),
        last_execution_report=_optional_mapping(
            payload.get("last_execution_report")
        ),
        equity_history=tuple(payload.get("equity_history", ())),
        account_id=_optional_text(payload.get("account_id")) or "main",
        schema_version=_optional_int(payload.get("schema_version"), default=2),
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
        start = (boundary - pd.Timedelta(days=700)).strftime("%Y-%m-%d")
        frame = qf.DataFetcher.load_stock_data(
            code,
            start,
            boundary.strftime("%Y-%m-%d"),
            data_dir=None,
        )
        if frame.empty:
            raise ValueError("no market data")
        observed = pd.Timestamp(frame.index[-1])
        if observed is pd.NaT:
            raise ValueError("latest market-data date is invalid")
        observed = cast(pd.Timestamp, observed).normalize()
        if observed > boundary:
            raise ValueError("market data contains a future observation")
        if (boundary - observed).days > ra.MAX_EVIDENCE_STALENESS_DAYS:
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
        被纳入候选。前两个返回值是候选信号列表与（代码->策略比例）说明，
        第三个是数据不足或陈旧的标的代码列表。
        """
        candidates: list[PointInTimeSignal] = []
        blocked: dict[str, str] = {}
        unpriced: list[str] = []
        as_of_timestamp = pd.Timestamp(as_of)
        if as_of_timestamp is pd.NaT:
            raise ValueError("as_of must resolve to a valid date")
        as_of_timestamp = cast(pd.Timestamp, as_of_timestamp).normalize()
        for code, name in symbols.items():
            if code in held:
                continue
            try:
                frame = self._frame(code, as_of)
            except _EXPECTED_DATA_ERRORS as exc:
                unpriced.append(code)
                blocked[code] = str(exc)
                continue
            cfg = qf.BacktestEngine.config_for_symbol(code, name=name)
            indicators = qf.Indicators.compute_all(frame, cfg)
            i = len(frame) - 1
            close = float(frame["close"].iloc[i])
            strategies = [
                cls(cfg)
                for cls in (
                    qf.TurtleBreakoutStrategy,
                    qf.DualMAStrategy,
                    qf.ATRChannelStrategy,
                )
            ]
            triggers: list[str] = []
            stop_prices: list[float] = []
            for strategy in strategies:
                ctx = qf.BarContext(
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
            score = _trend_candidate_score(frame, indicators, i, close, len(triggers))
            candidates.append(
                PointInTimeSignal(
                    symbol=code,
                    strategy_name="+".join(sorted(triggers)),
                    direction="buy",
                    score=score,
                    target_weight=_target_weight_for(len(triggers)),
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
        qf.DataFetcher._cache_dir = self.cache_dir
        market_data_contracts.refresh_regime_indices(
            self.regime_data_dir,
            end_date=as_of,
            strict=False,
        )
        decision = ra.RegimeAdaptiveBacktestEngine().decide_current(
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
                cfg = qf.BacktestEngine.config_for_symbol(code, name=name)
                indicators = qf.Indicators.compute_all(frame, cfg)
                i = len(frame) - 1
                close = float(frame["close"].iloc[i])
                if not math.isfinite(close) or close <= 0:
                    raise ValueError("latest close must be finite and positive")
                priced_market_value += position.shares * close

                atr = self._latest_value(indicators.get("atr"), i)
                ma_short = self._latest_value(indicators.get("ma_short"), i)
                ma_long = self._latest_value(indicators.get("ma_long"), i)
                if position.entry_date:
                    entry_timestamp = pd.Timestamp(position.entry_date).normalize()
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
                    }
                )
            except _EXPECTED_DATA_ERRORS as exc:
                unpriced_symbols.append(code)
                actions.append(
                    {
                        "symbol": code,
                        "name": name,
                        "action": "DATA_ERROR",
                        "shares": position.shares,
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
                            "reason": (
                                "current positive-240-session weak-regime leader"
                            ),
                        }
                    )

        # 趋势路线：补齐新买入候选、目标股数与买入排序（P0-2）。
        trend_candidates: list[PointInTimeSignal] = []
        trend_blocked: dict[str, str] = {}
        trend_unpriced: list[str] = []
        if (
            valuation_complete
            and decision.name == "frozen_trend_engine"
            and snapshot.cash > 0
        ):
            trend_candidates, trend_blocked, trend_unpriced = (
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
            default_cfg = qf.BacktestEngine._default_config()
            slots_left = max(
                int(default_cfg.get("max_positions", 6)) - len(held_codes), 0
            )
            # 只对实际将要买入的候选子集计算现金分配：分母必须只由被选中的
            # 候选构成，否则 total_weight 会包含未选中候选的权重，稀释每只
            # 选中股票的分配额（account_signal_engine._compute_target_shares）。
            selected_candidates = ranked[:max(slots_left, 0)]
            for candidate in selected_candidates:
                try:
                    frame = self._frame(candidate.symbol, as_of)
                    close = float(frame["close"].iloc[-1])
                except _EXPECTED_DATA_ERRORS:
                    continue
                if not math.isfinite(close) or close <= 0:
                    continue
                target_shares, locked_weight = _compute_target_shares(
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


def _atomic_json(payload: dict[str, Any], path: Path) -> None:
    """以临时文件、刷盘和原子替换方式写入严格 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=".account_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
        _atomic_json(result, output)
    except _EXPECTED_DATA_ERRORS as exc:
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
        print(
            f"  {action['symbol']} {action['name']}: {action['action']} | "
            f"{action['reason']}"
        )
    print(f"  Artifact: {output}")
    return 0
