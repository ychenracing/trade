"""Causal recovery hysteresis for exposure reduced by the AB5 account budget."""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from enum import Enum
from typing import Any


BookId = tuple[int, str, str]
PendingItem = tuple[Any, Any]


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
    new_reduction_book_ids: Collection[BookId],
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


def filled_ab5_reduction_book_ids(
    states: Sequence[Any], date_str: str
) -> set[BookId]:
    """Return books whose AB5 reduction actually filled on this trading day."""
    filled: set[BookId] = set()
    for state_index, state in enumerate(states):
        sleeve = getattr(state, "sleeve", None)
        for trade in getattr(sleeve, "trades", ()):
            if str(getattr(trade, "date", "")) != date_str:
                continue
            if str(getattr(trade, "direction", "")) != "sell":
                continue
            if str(getattr(trade, "reason", "")).split(":", 1)[0] != "account_budget_trim":
                continue
            filled.add(
                (
                    state_index,
                    str(getattr(trade, "symbol", "")),
                    str(getattr(trade, "strategy_name", "")),
                )
            )
    return filled


def filter_ab5_recovery_buys(
    pending: Sequence[PendingItem],
    *,
    state_index: int,
    blocked_book_ids: Collection[BookId],
) -> tuple[list[PendingItem], list[PendingItem]]:
    """Partition pending orders, blocking only buys owned by reduced AB5 books."""
    blocked_books = set(blocked_book_ids)
    retained: list[PendingItem] = []
    blocked: list[PendingItem] = []
    for item in pending:
        signal, _ = item
        book = (
            state_index,
            str(getattr(signal, "symbol", "")),
            str(getattr(signal, "strategy_name", "")),
        )
        if str(getattr(signal, "direction", "")) == "buy" and book in blocked_books:
            blocked.append(item)
        else:
            retained.append(item)
    return retained, blocked
