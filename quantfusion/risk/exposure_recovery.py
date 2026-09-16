"""Causal recovery hysteresis for exposure reduced by the AB5 account budget."""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from enum import Enum
from typing import Any


class AB5RecoveryState(str, Enum):
    """Track whether AB5-reduced exposure is eligible to recover."""

    NORMAL = "NORMAL"
    AB5_REDUCED = "AB5_REDUCED"
    AB5_RECOVERY_PENDING = "AB5_RECOVERY_PENDING"


def _recovery_safe(envelope: Mapping[str, Any] | None) -> bool:
    """Return whether one completed close is safe for recovery progression."""
    if not envelope or envelope.get("event") != "account_budget_envelope":
        return False
    if envelope.get("new_reduction_orders"):
        return False
    if envelope.get("risk_alert_active") or envelope.get("shock_episode_active"):
        return False
    try:
        gross = float(envelope["gross_before"])
        cap = float(envelope["gross_cap"])
        buy_scale = float(envelope["buy_scale"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        math.isfinite(gross)
        and math.isfinite(cap)
        and math.isfinite(buy_scale)
        and gross <= cap + 1e-8
        and buy_scale >= 1.0 - 1e-12
    )


def next_ab5_recovery_state(
    previous_state: AB5RecoveryState | str,
    *,
    previous_envelope: Mapping[str, Any] | None,
    new_reduction_book_ids: Collection[tuple[int, str, str]],
) -> AB5RecoveryState:
    """Advance recovery using only filled reductions and the prior completed close."""
    state = AB5RecoveryState(previous_state)
    if new_reduction_book_ids:
        return AB5RecoveryState.AB5_REDUCED
    if state is AB5RecoveryState.NORMAL:
        return state
    if not _recovery_safe(previous_envelope):
        return AB5RecoveryState.AB5_REDUCED
    if state is AB5RecoveryState.AB5_REDUCED:
        return AB5RecoveryState.AB5_RECOVERY_PENDING
    return AB5RecoveryState.NORMAL
