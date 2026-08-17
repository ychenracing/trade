"""Translate immutable overlay decisions into legacy engine pending queues."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from typing import Any

import pandas as pd

from quantfusion.config.overlay import (
    CATASTROPHE_COOLDOWN_DAYS,
    RISK_ACTION_DEFAULT_PRIORITY,
    RISK_ACTION_PRIORITY,
)
from quantfusion.domain.models import Signal
from quantfusion.risk.overlay.models import RiskAction


def make_sell_signal(
    symbol: str,
    strategy_name: str,
    shares: int,
    price: float,
    signal_date: str,
    reason: str,
    extra: str = "",
) -> Signal:
    """Build the exact T+1 sell signal consumed by ensemble sleeves."""
    full_reason = f"{reason}:{extra}" if extra else reason
    return Signal(
        symbol=symbol,
        strategy_name=strategy_name,
        direction="sell",
        target_shares=shares,
        price=price,
        reason=full_reason,
        signal_date=signal_date,
    )


def _state_sequence(state_or_states: Any) -> list[Any]:
    """Normalize the legacy one-state call and canonical multi-state call."""
    if hasattr(state_or_states, "pending"):
        return [state_or_states]
    if isinstance(state_or_states, Sequence):
        return list(state_or_states)
    return list(state_or_states)


def _risk_action_beats(
    candidate: Any,
    candidate_priority: int,
    current: Any,
    current_priority: int,
) -> bool:
    """Return whether a candidate is the deterministic defensive winner."""
    if candidate_priority != current_priority:
        return candidate_priority > current_priority
    if candidate.target_shares != current.target_shares:
        return candidate.target_shares > current.target_shares
    return candidate.reason < current.reason


def consolidate_risk_sells(
    states: Sequence[Any],
    date_str: str,
    events: list[dict[str, Any]],
    *,
    action_priorities: dict[int, int] | None = None,
) -> None:
    """Consolidate new and carried overlay sells across the full queue.

    Existing unfilled risk orders remain part of the comparison. New immutable
    actions use their explicit priority; carried legacy signals fall back to
    the stable reason-to-priority mapping.
    """
    explicit = action_priorities or {}
    suppressed: list[dict[str, Any]] = []
    winner_by_book: dict[tuple[str, str], tuple[Any, int]] = {}
    for state in states:
        for signal, strategy in state.pending:
            if strategy is not None or signal.direction != "sell":
                continue
            book = (str(signal.symbol), str(signal.strategy_name))
            priority = explicit.get(
                id(signal),
                RISK_ACTION_PRIORITY.get(
                    signal.reason.split(":")[0], RISK_ACTION_DEFAULT_PRIORITY
                ),
            )
            current = winner_by_book.get(book)
            if current is None or _risk_action_beats(
                signal, priority, current[0], current[1]
            ):
                winner_by_book[book] = (signal, priority)

    for state in states:
        retained: list[tuple[Any, Any]] = []
        for signal, strategy in state.pending:
            if strategy is not None or signal.direction != "sell":
                retained.append((signal, strategy))
                continue
            book = (str(signal.symbol), str(signal.strategy_name))
            winner, _ = winner_by_book[book]
            if signal is winner:
                retained.append((signal, strategy))
            else:
                suppressed.append(
                    {
                        "date": date_str,
                        "event": "risk_action_suppressed",
                        "symbol": str(signal.symbol),
                        "strategy": str(signal.strategy_name),
                        "reason": signal.reason,
                        "target_shares": int(signal.target_shares),
                        "winner_reason": winner.reason,
                    }
                )
        state.pending = retained
    if suppressed:
        events.extend(suppressed)


def apply_risk_actions(
    actions: Iterable[RiskAction],
    state_or_states: Any,
    *,
    date_str: str | None = None,
    events: list[dict[str, Any]] | None = None,
) -> None:
    """Apply ordered actions, then consolidate against every pending risk sell."""
    states = _state_sequence(state_or_states)
    priorities: dict[int, int] = {}
    for action in actions:
        if action.state_index < 0 or action.state_index >= len(states):
            raise IndexError(f"RiskAction state_index is out of range: {action.state_index}")
        signal = make_sell_signal(
            action.symbol,
            action.strategy_name,
            action.shares,
            action.price,
            action.signal_date,
            action.reason,
            action.extra,
        )
        states[action.state_index].pending.append((signal, None))
        priorities[id(signal)] = action.priority
    if date_str is not None:
        consolidate_risk_sells(
            states,
            date_str,
            events if events is not None else [],
            action_priorities=priorities,
        )


def apply_cooldown_buy_gate(
    overlay: Any,
    states: Sequence[Any],
    date: pd.Timestamp,
    date_pos: int,
) -> None:
    """Filter pending buys using the overlay's catastrophe cooldown decision."""
    if overlay._outer_defensive_mode:
        return
    date_str = date.strftime("%Y-%m-%d")
    for state in states:
        retained: list[tuple[Any, Any]] = []
        for signal, strategy in state.pending:
            in_cooldown = date_pos < overlay._catastrophe_cooldown.get(
                str(signal.symbol), -1
            )
            if signal.direction == "buy" and in_cooldown:
                sleeve = getattr(state, "sleeve", None)
                record = (
                    sleeve._record_order_event
                    if sleeve is not None and hasattr(sleeve, "_record_order_event")
                    else None
                )
                if record is not None:
                    try:
                        record(
                            date=date_str,
                            signal=signal,
                            event="blocked_catastrophe_cooldown",
                            cooldown_days=CATASTROPHE_COOLDOWN_DAYS,
                        )
                    except TypeError:
                        pass
                overlay.events.append(
                    {
                        "date": date_str,
                        "event": "cooldown_blocked_buy",
                        "symbol": str(signal.symbol),
                        "reason": "catastrophe_cooldown",
                    }
                )
                continue
            retained.append((signal, strategy))
        state.pending = retained


def apply_risk_buy_gate(
    overlay: Any,
    states: Sequence[Any],
    date: pd.Timestamp,
    held_symbols: set[str],
) -> None:
    """Adapt overlay admission decisions to pending buy signals."""
    if overlay._outer_defensive_mode or not overlay.blocks_pyramiding:
        return
    date_str = date.strftime("%Y-%m-%d")
    transition_confirmed = any(
        getattr(state.sleeve, "_regime_state", "TREND") == "TRANSITION"
        and int(getattr(state.sleeve, "_regime_transition_days", 0)) >= 5
        for state in states
    )
    for state in states:
        retained: list[tuple[Any, Any]] = []
        for signal, strategy in state.pending:
            is_buy = signal.direction == "buy"
            is_held = str(signal.symbol) in held_symbols
            blocked = is_buy and (
                (overlay.blocks_new_positions and not is_held)
                or (overlay.blocks_pyramiding and is_held)
            )
            if (
                is_buy
                and not is_held
                and not overlay.blocks_new_positions
                and transition_confirmed
            ):
                scaled_shares = int(signal.target_shares * 0.75 // 100 * 100)
                if scaled_shares > 0:
                    retained.append(
                        (replace(signal, target_shares=scaled_shares), strategy)
                    )
                    continue
                blocked = True
            if not blocked:
                retained.append((signal, strategy))
                continue
            event = (
                "blocked_confirmed_market_risk"
                if overlay.blocks_new_positions
                else "blocked_market_risk_pyramid"
            )
            sleeve = getattr(state, "sleeve", None)
            if sleeve is not None and hasattr(sleeve, "_record_order_event"):
                sleeve._record_order_event(
                    date=date_str,
                    signal=signal,
                    event=event,
                    market_risk_level=int(overlay._risk_level),
                )
            overlay.events.append(
                {
                    "date": date_str,
                    "event": event,
                    "symbol": str(signal.symbol),
                    "level": int(overlay._risk_level),
                }
            )
        state.pending = retained


__all__ = [
    "apply_cooldown_buy_gate",
    "apply_risk_actions",
    "apply_risk_buy_gate",
    "consolidate_risk_sells",
    "make_sell_signal",
]
