"""Weak-market engine and portfolio policy factories."""

from __future__ import annotations

from typing import Any

from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.config.regime import (
    MAX_SYMBOL_WEIGHT,
    WEAK_CONFIRMED_DRAWDOWN,
    WEAK_DAILY_LOSS_LIMIT,
    WEAK_DRAWDOWN_ALERT,
    WEAK_EMERGENCY_DRAWDOWN,
    WEAK_TERMINAL_DRAWDOWN,
)


def weak_regime_policy() -> PortfolioPolicy:
    """Apply independent weak-market portfolio drawdown protection."""
    return PortfolioPolicy(
        allocation_mode="single",
        drawdown_alert=WEAK_DRAWDOWN_ALERT,
        confirmed_drawdown=WEAK_CONFIRMED_DRAWDOWN,
        emergency_drawdown=WEAK_EMERGENCY_DRAWDOWN,
        terminal_drawdown=WEAK_TERMINAL_DRAWDOWN,
        concentration_drawdown_adjustment=0.01,
        candidate_reference_percentile=0.0,
        market_regime_enabled=False,
    )


def weak_regime_config(symbol_count: int) -> dict[str, Any]:
    """Return the frozen single-sleeve weak-market engine overrides."""
    slots = max(1, symbol_count)
    target_weight = min(MAX_SYMBOL_WEIGHT, 1.0 / slots)
    return {
        "strategy_weight": target_weight,
        "max_symbol_weight": target_weight,
        "max_total_weight": 1.0,
        "max_positions": slots,
        "max_units": 1,
        "group_min_slots": 0,
        "daily_loss_limit": WEAK_DAILY_LOSS_LIMIT,
        "sector_guard_enabled": False,
        "market_regime_enabled": False,
        "fusion_single_scale": 1.0,
        "combined_group_weight_limits": {
            "overseas_compute": 1.0,
            "domestic_semiconductor": 1.0,
        },
    }
