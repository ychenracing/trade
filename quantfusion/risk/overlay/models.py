"""Immutable actions emitted by cross-market risk policy."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RiskAction:
    """One prioritized sell intent before it enters an execution queue."""

    symbol: str
    strategy_name: str
    shares: int
    price: float
    signal_date: str
    reason: str
    priority: int
    state_index: int = 0
    extra: str = ""
