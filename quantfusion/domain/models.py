"""Stable data structures shared by engine boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

@dataclass
class Position:
    """Track one symbol and one strategy sub-position."""

    symbol: str
    strategy_name: str
    shares: int
    entry_price: float
    entry_date: str
    stop_loss: float = 0.0
    highest_since_entry: float = 0.0
    highest_close_since_entry: float = 0.0
    units: int = 1
    last_buy_date: str = ""
    last_add_price: float = 0.0

    @property
    def cost(self) -> float:
        """Return the remaining position's fee-inclusive cost basis."""
        return self.shares * self.entry_price

    def market_value_at(self, price: float) -> float:
        """Mark the position at the supplied price."""
        return self.shares * price


@dataclass
class TradeRecord:
    """Store one executed trade and its audited cash effects."""

    symbol: str
    strategy_name: str
    direction: str
    shares: int
    price: float
    date: str
    reason: str = ""
    pnl: float = 0.0
    pnl_pct: float = 0.0
    signal_date: str = ""
    gross_value: float = 0.0
    commission: float = 0.0
    stamp_duty_cost: float = 0.0
    net_cash_flow: float = 0.0
    cash_after: float = 0.0
    peak_close: float = 0.0
    exit_from_peak_pct: float = 0.0


def date_symbol_side_count(
    trades: list[TradeRecord], *, direction: str | None = None
) -> int:
    """Count unique date/symbol/side buckets in executed trade records.

    This diagnostic does not represent broker orders or broker-side netting.
    Multiple sleeve fills may share a bucket while remaining separate executed
    ``TradeRecord`` objects with separate modeled costs.
    """
    return len(
        {
            (trade.date, trade.symbol, trade.direction)
            for trade in trades
            if direction is None or trade.direction == direction
        }
    )


@dataclass(frozen=True)
class Signal:
    """Represent a close-generated instruction pending T+1 execution."""

    symbol: str
    strategy_name: str
    direction: str
    target_shares: int = 0
    price: float = 0.0
    stop_loss: float = 0.0
    reason: str = ""
    signal_date: str = ""
    atr: float = 0.0
    fusion_votes: int = 1
    fusion_label: str = "single_strategy"


@dataclass
class BarContext:
    """Provide immutable per-bar inputs to a strategy."""

    i: int
    df: pd.DataFrame
    current_assets: float
    indicators: dict
    symbol: str
    date: str


@dataclass(frozen=True)
class SectorObservation:
    """Aggregate equal-weight return and breadth from fully observed symbols."""

    symbol_count: int
    equal_return: float
    shock_breadth: float
    recovery_breadth: float
    normalized_series: tuple[pd.Series, ...]


@dataclass
class AccountState:
    """Real portfolio state from a live account snapshot.

    Used by the daily signal scanner to derive correct action labels
    without relying on a simulated replay from scratch.
    """
    cash: float
    position_value: float
    total_equity: float
    peak_equity: float
    positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    risk_state: dict[str, Any] = field(default_factory=dict)


@dataclass
class EngineState:
    """Cross-day state that survives between successive daily runs.

    Mirrors the fields that BacktestEngine.run() serialises into
    risk_state.json.
    """
    terminal_risk_lock: bool = False
    sector_guard_active: bool = False
    cycle_lock_count: int = 0
    persistent_risk_lock: bool = False
    run_id: str = ""


@dataclass(frozen=True)
class MarketRegimeObservation:
    """Snapshot the five basket-level regime indicators at one close.

    Each field is computed from the fixed ``regime_symbols`` basket using only
    data on or before the scored date. ``raw_score`` sums the per-indicator
    votes (+1 trend / -1 choppy / 0 neutral) and ``candidate_state`` is the
    unconfirmed regime implied by that score before the state machine applies
    its confirmation and minimum-hold gates.
    """

    ewi_slope: float
    breadth_above_ma: float
    adx_median: float
    hurst: float
    volatility_percentile: float
    raw_score: int
    candidate_state: str

def account_order_count(
    trades: list[TradeRecord], *, direction: str | None = None
) -> int:
    """Deprecated compatibility alias for :func:`date_symbol_side_count`.

    The returned value is a date/symbol/side bucket count, not a broker-level
    order count. New code must use the canonical name.
    """
    return date_symbol_side_count(trades, direction=direction)
