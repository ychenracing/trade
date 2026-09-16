"""Causal recovery hysteresis for exposure reduced by the AB5 account budget."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from enum import Enum
from typing import Any

import pandas as pd

from quantfusion.execution.c6_receipts import reconcile_close_queue
from quantfusion.risk.account_budget import (
    apply_account_risk_budget as _apply_account_risk_budget,
)


BookId = tuple[int, str, str]
PendingItem = tuple[Any, Any]


class AB5RecoveryState(str, Enum):
    """Track whether AB5-reduced exposure is eligible to recover."""

    NORMAL = "NORMAL"
    AB5_REDUCED = "AB5_REDUCED"
    AB5_RECOVERY_PENDING = "AB5_RECOVERY_PENDING"


def _book_id(state_index: int, symbol: Any, strategy_name: Any) -> BookId:
    return state_index, str(symbol), str(strategy_name).split(":")[-1]


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
    new_reduction_shares: Mapping[BookId, int],
) -> AB5RecoveryState:
    """Advance recovery using only filled reductions and the prior completed close."""
    state = AB5RecoveryState(previous_state)
    if new_reduction_shares:
        return AB5RecoveryState.AB5_REDUCED
    if state is AB5RecoveryState.NORMAL:
        return state
    if not _recovery_safe(previous_envelope):
        return AB5RecoveryState.AB5_REDUCED
    if state is AB5RecoveryState.AB5_REDUCED:
        return AB5RecoveryState.AB5_RECOVERY_PENDING
    return AB5RecoveryState.NORMAL


def filled_ab5_reduction_shares(
    states: Sequence[Any], date_str: str
) -> dict[BookId, int]:
    """Return shares actually reduced by AB5 on this trading day, by book."""
    filled: dict[BookId, int] = {}
    for state_index, state in enumerate(states):
        sleeve = getattr(state, "sleeve", None)
        for trade in getattr(sleeve, "trades", ()):
            if str(getattr(trade, "date", "")) != date_str:
                continue
            if str(getattr(trade, "direction", "")) != "sell":
                continue
            if (
                str(getattr(trade, "reason", "")).split(":", 1)[0]
                != "account_budget_trim"
            ):
                continue
            shares = getattr(trade, "shares", None)
            if isinstance(shares, bool) or not isinstance(shares, int) or shares <= 0:
                raise ValueError("filled AB5 reduction shares must be a positive integer")
            book = _book_id(
                state_index,
                getattr(trade, "symbol", ""),
                getattr(trade, "strategy_name", ""),
            )
            filled[book] = filled.get(book, 0) + shares
    return filled


def apply_ab5_recovery_buy_ownership(
    pending: Sequence[PendingItem],
    *,
    state_index: int,
    owned_shares: Mapping[BookId, int],
) -> tuple[list[PendingItem], int, int]:
    """Defer at most the AB5-owned share deficit while preserving excess buys."""
    remaining = dict(owned_shares)
    retained: list[PendingItem] = []
    deferred_orders = 0
    deferred_shares = 0
    for signal, strategy in pending:
        book = _book_id(
            state_index,
            getattr(signal, "symbol", ""),
            getattr(signal, "strategy_name", ""),
        )
        owned = remaining.get(book, 0)
        if str(getattr(signal, "direction", "")) != "buy" or owned <= 0:
            retained.append((signal, strategy))
            continue
        requested = int(getattr(signal, "target_shares", 0))
        deferred = min(requested, owned)
        if deferred <= 0:
            retained.append((signal, strategy))
            continue
        deferred_orders += 1
        deferred_shares += deferred
        remaining[book] = owned - deferred
        authorized = requested - deferred
        if authorized > 0:
            retained.append((replace(signal, target_shares=authorized), strategy))
    return retained, deferred_orders, deferred_shares


def _latest_account_budget_envelope(
    events: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    for event in reversed(events):
        if event.get("event") == "account_budget_envelope":
            return event
    return None


def _recovery_owned_shares(
    envelope: Mapping[str, Any] | None,
) -> dict[BookId, int]:
    if envelope is None:
        return {}
    raw = envelope.get("ab5_recovery_owned_shares", ())
    if raw is None:
        return {}
    if not isinstance(raw, (list, tuple)):
        raise ValueError("ab5_recovery_owned_shares must be a sequence")
    owned: dict[BookId, int] = {}
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 4:
            raise ValueError("invalid AB5 recovery ownership entry")
        state_index, symbol, strategy_name, shares = item
        if isinstance(state_index, bool) or not isinstance(state_index, int):
            raise ValueError("AB5 recovery state_index must be an integer")
        if state_index < 0 or not isinstance(symbol, str) or not symbol:
            raise ValueError("invalid AB5 recovery book identity")
        if not isinstance(strategy_name, str) or not strategy_name:
            raise ValueError("invalid AB5 recovery book identity")
        if isinstance(shares, bool) or not isinstance(shares, int) or shares <= 0:
            raise ValueError("AB5 recovery owned shares must be a positive integer")
        book = _book_id(state_index, symbol, strategy_name)
        if book in owned:
            raise ValueError("duplicate AB5 recovery book identity")
        owned[book] = shares
    return owned


def _merge_owned_shares(
    carried: Mapping[BookId, int],
    newly_reduced: Mapping[BookId, int],
) -> dict[BookId, int]:
    merged = dict(carried)
    for book, shares in newly_reduced.items():
        merged[book] = merged.get(book, 0) + shares
    return merged


def apply_ab5_recovery_hysteresis(
    states: Sequence[Any],
    date_str: str,
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Preserve only the exposure gap created by filled AB5 reductions."""
    previous = _latest_account_budget_envelope(events)
    previous_state = (
        previous.get("ab5_recovery_state", AB5RecoveryState.NORMAL.value)
        if previous is not None
        else AB5RecoveryState.NORMAL.value
    )
    carried = _recovery_owned_shares(previous)
    newly_reduced = filled_ab5_reduction_shares(states, date_str)
    recovery_state = next_ab5_recovery_state(
        previous_state,
        previous_envelope=previous,
        new_reduction_shares=newly_reduced,
    )
    active = (
        _merge_owned_shares(carried, newly_reduced)
        if recovery_state is not AB5RecoveryState.NORMAL
        else {}
    )

    deferred_orders = 0
    deferred_shares = 0
    if active:
        for state_index, state in enumerate(states):
            before = list(state.pending)
            retained, order_count, share_count = apply_ab5_recovery_buy_ownership(
                before,
                state_index=state_index,
                owned_shares=active,
            )
            if order_count == 0:
                continue
            state.pending = retained
            deferred_orders += order_count
            deferred_shares += share_count
            reconcile_close_queue(
                getattr(state, "sleeve", None),
                before,
                retained,
                date_str,
                "ab5_recovery_hysteresis",
            )

    return {
        "state": recovery_state.value,
        "owned_shares": [
            [state_index, symbol, strategy_name, shares]
            for (state_index, symbol, strategy_name), shares in sorted(active.items())
        ],
        "deferred_orders": deferred_orders,
        "deferred_shares": deferred_shares,
    }


def apply_account_risk_budget_with_recovery(
    states: Sequence[Any],
    date: pd.Timestamp,
    assets: float,
    peak: float,
    cfg: Mapping[str, Any],
    score: Callable[[str], float],
    events: list[dict[str, Any]],
    *,
    shock_floor: float = 0.0,
    preserve_strategy_valid_holdings: bool = False,
    risk_alert_active: bool | None = None,
    portfolio_evidence_buy_symbols: set[str] | None = None,
) -> None:
    """Apply recovery hysteresis, then the unchanged canonical AB5 policy."""
    date_str = date.strftime("%Y-%m-%d")
    decision = apply_ab5_recovery_hysteresis(states, date_str, events)
    event_start = len(events)
    options: dict[str, Any] = {}
    if shock_floor != 0.0:
        options["shock_floor"] = shock_floor
    if preserve_strategy_valid_holdings:
        options["preserve_strategy_valid_holdings"] = True
    if risk_alert_active is not None:
        options["risk_alert_active"] = risk_alert_active
    if portfolio_evidence_buy_symbols is not None:
        options["portfolio_evidence_buy_symbols"] = portfolio_evidence_buy_symbols
    _apply_account_risk_budget(
        states,
        date,
        assets,
        peak,
        cfg,
        score,
        events,
        **options,
    )
    envelope = next(
        (
            event
            for event in reversed(events[event_start:])
            if event.get("event") == "account_budget_envelope"
            and event.get("date") == date_str
        ),
        None,
    )
    if envelope is None:
        raise RuntimeError("canonical AB5 did not publish its account budget envelope")
    envelope.update(
        ab5_recovery_state=decision["state"],
        ab5_recovery_owned_shares=decision["owned_shares"],
        ab5_recovery_deferred_buy_orders=decision["deferred_orders"],
        ab5_recovery_deferred_buy_shares=decision["deferred_shares"],
    )
