"""Pure account candidate scoring and target sizing."""

from __future__ import annotations

import math

import pandas as pd

from quantfusion.account.models import AccountSnapshot, PointInTimeSignal
from quantfusion.domain.rules import floor_to_lot


def _trend_candidate_score(
    frame: pd.DataFrame,
    indicators: dict[str, pd.Series],
    index: int,
    close: float,
    trigger_count: int,
    industry_relative_strength: float = 0.0,
) -> float:
    """计算买入排序分：多确认优先，辅以风险调整动量与趋势持续性。

    权重与报告一致：35% 多策略确认、25% 风险调整动量、10% 突破质量、
    10% 趋势持续性、5% 流动性（其余给行业相对强度留白，由调用方补充）。
    分数用于决定有限现金与持仓槽位分配顺序，不直接放大仓位。
    """
    if not math.isfinite(close) or close <= 0:
        return 0.0
    confirmation = trigger_count / 3.0
    atr_series = indicators.get("atr")
    atr = float(atr_series.iloc[index]) if atr_series is not None else float("nan")
    momentum = 0.0
    if index >= 20:
        prior = float(frame["close"].iloc[index - 20])
        if math.isfinite(prior) and prior > 0:
            momentum = close / prior - 1.0
    risk_adjusted_momentum = (
        momentum / (atr / close) if math.isfinite(atr) and atr > 0 else momentum
    )
    ma_short_series = indicators.get("ma_short")
    ma_long_series = indicators.get("ma_long")
    ma_short = (
        float(ma_short_series.iloc[index])
        if ma_short_series is not None
        else float("nan")
    )
    ma_long = (
        float(ma_long_series.iloc[index])
        if ma_long_series is not None
        else float("nan")
    )
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
        + 0.15 * max(0.0, min(1.0, industry_relative_strength))
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
    """为排序后的候选估算收盘价数量与权重，供人工复核。

    估算仍尊重总仓位、单票 60% 上限和 A 股整手；现金不足一手时为零。结果
    不是账户净订单，也不复现 fast/base/slow 的完整历史状态。
    """
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
    shares = floor_to_lot(alloc / close)
    if shares <= 0:
        return 0, 0.0
    locked = min(target_weight, 0.60)
    return shares, locked


trend_candidate_score = _trend_candidate_score
target_weight_for = _target_weight_for
compute_target_shares = _compute_target_shares

__all__ = [
    "compute_target_shares",
    "floor_to_lot",
    "target_weight_for",
    "trend_candidate_score",
]
