"""Immutable real-account snapshots, signals, and targets."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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
