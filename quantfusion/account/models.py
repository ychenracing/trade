"""Immutable real-account snapshots and point-in-time signals."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AccountPosition:
    """账户建议当前能够严格解释的一只实际持仓。"""

    symbol: str
    shares: int
    sellable_shares: int
    avg_cost: float
    entry_date: str
    highest_close: float | None = None


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """单一 v3 schema 已验证的不可变同日账户快照。"""

    schema_version: int
    account_id: str
    snapshot_date: str
    cash: float
    peak_equity: float
    positions: tuple[AccountPosition, ...]


@dataclass(frozen=True, slots=True)
class PointInTimeSignal:
    """一只股票在指定时点的策略触发结果。

    由历史引擎的同一策略逻辑在 ``as_of`` 当日收盘后生成，方向为买入或卖出，
    并携带仅供收盘后人工复核的估算权重、数量、保护止损和拒绝原因。该结构不
    注入任何伪造历史持仓，也不表示券商订单或完整历史袖套状态。
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
