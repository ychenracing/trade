"""Causal primitives for Trade-native winner lifecycle research.

The helpers deliberately reuse the strategy's existing profit-lock contract.
They introduce no new economic threshold and do not alter production behavior.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class WinnerLifecycleState:
    """Close-known winner state derived from the existing profit-lock contract."""

    peak_gain: float
    current_gain: float
    giveback_from_peak: float
    profit_lock_active: bool
    profit_lock_intact: bool
    strategic_winner: bool


def classify_winner_lifecycle(
    position: Any,
    *,
    close: float,
    cfg: Mapping[str, Any],
) -> WinnerLifecycleState:
    """Classify a held book using only state known at the current close.

    ``profit_lock_activation`` and ``profit_lock_giveback`` are existing Trade
    strategy controls.  The research state does not add another threshold.
    """
    entry = float(position.entry_price)
    observed_peak = float(position.highest_close_since_entry)
    close = float(close)
    activation = float(cfg.get("profit_lock_activation", 0.30))
    giveback = float(cfg.get("profit_lock_giveback", 0.18))
    if (
        not all(math.isfinite(value) for value in (entry, observed_peak, close, activation, giveback))
        or entry <= 0.0
        or close <= 0.0
        or activation < 0.0
        or not 0.0 <= giveback < 1.0
    ):
        return WinnerLifecycleState(0.0, 0.0, 0.0, False, False, False)

    peak = max(observed_peak, entry, close)
    peak_gain = peak / entry - 1.0
    current_gain = close / entry - 1.0
    giveback_from_peak = close / peak - 1.0
    profit_lock_active = peak_gain >= activation
    profit_lock_intact = close > peak * (1.0 - giveback)
    return WinnerLifecycleState(
        peak_gain=peak_gain,
        current_gain=current_gain,
        giveback_from_peak=giveback_from_peak,
        profit_lock_active=profit_lock_active,
        profit_lock_intact=profit_lock_intact,
        strategic_winner=profit_lock_active and current_gain > 0.0,
    )


def should_defer_atr_trailing_exit(
    position: Any,
    *,
    close: float,
    reason: str,
    cfg: Mapping[str, Any],
) -> bool:
    """Identify the narrow soft-exit counterfactual used by Phase 1 research.

    Only an ATR trailing exit may be deferred, and only while the existing
    profit-lock is both active and unbreached.  Profit protection, hard stops,
    structural strategy exits and account-level risk actions stay authoritative.
    """
    if not str(reason).startswith("ATR trailing stop"):
        return False
    state = classify_winner_lifecycle(position, close=close, cfg=cfg)
    return state.strategic_winner and state.profit_lock_intact


__all__ = [
    "WinnerLifecycleState",
    "classify_winner_lifecycle",
    "should_defer_atr_trailing_exit",
]
