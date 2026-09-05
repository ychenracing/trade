"""Translate immutable overlay decisions into engine pending queues."""

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
from quantfusion.execution.c6_receipts import (
    action_receipt, next_action_ordinal, order_receipt, link_order,
    prepare_action_consolidation,
)
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
    """Normalize one-state and multi-state adapter calls."""
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
    state_local_books: bool = True,
) -> None:
    """Consolidate new and carried overlay sells across the full queue.

    Existing unfilled risk orders remain part of the comparison. New immutable
    actions use their explicit priority; carried pending signals fall back to
    the stable reason-to-priority mapping.
    """
    explicit = action_priorities or {}
    suppressed: list[dict[str, Any]] = []
    winner_by_book: dict[tuple[object, ...], tuple[Any, int, int, str]] = {}
    for state_index, state in enumerate(states):
        sleeve_name = str(getattr(getattr(state, "sleeve", None), "sleeve_name", ""))
        for signal, strategy in state.pending:
            if strategy is not None or signal.direction != "sell":
                continue
            prepare_action_consolidation(getattr(state, "sleeve", None), signal, date_str)
            book = (
                (state_index, str(signal.symbol), str(signal.strategy_name))
                if state_local_books
                else (str(signal.symbol), str(signal.strategy_name))
            )
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
                winner_by_book[book] = (signal, priority, state_index, sleeve_name)

    for state_index, state in enumerate(states):
        sleeve_name = str(getattr(getattr(state, "sleeve", None), "sleeve_name", ""))
        retained: list[tuple[Any, Any]] = []
        for signal, strategy in state.pending:
            if strategy is not None or signal.direction != "sell":
                retained.append((signal, strategy))
                continue
            book = (
                (state_index, str(signal.symbol), str(signal.strategy_name))
                if state_local_books
                else (str(signal.symbol), str(signal.strategy_name))
            )
            loser_priority = explicit.get(
                id(signal),
                RISK_ACTION_PRIORITY.get(
                    signal.reason.split(":")[0], RISK_ACTION_DEFAULT_PRIORITY
                ),
            )
            winner, winner_priority, winner_state_index, winner_sleeve_name = (
                winner_by_book[book]
            )
            if signal is winner:
                retained.append((signal, strategy))
            else:
                record = action_receipt(getattr(state, "sleeve", None), signal)
                winner_record = action_receipt(getattr(states[winner_state_index], "sleeve", None), winner)
                loser_order = order_receipt(getattr(state, "sleeve", None), signal, date_str)
                winner_order = order_receipt(getattr(states[winner_state_index], "sleeve", None), winner, date_str)
                if loser_order is not None and winner_order is not None:
                    loser_order.update(status="suppressed", suppression_winner_order_ordinal=winner_order["order_ordinal"])
                if record is not None:
                    record.update({"winner_order_ordinal": winner_record["winner_order_ordinal"] if winner_record else record["winner_order_ordinal"], "retained_shares": 0, "suppressed_shares": record["planned_shares"], "remainder_shares": 0, "terminal_for_current_batch": True, "carry_to_next_batch": False, "release_reason": "CANCELLED"})
                    if winner_record is not None:
                        if loser_order is not None:
                            winner_record["loser_order_ordinals"].append(loser_order["order_ordinal"])
                suppressed.append(
                    {
                        "date": date_str,
                        "event": "risk_action_suppressed",
                        "symbol": str(signal.symbol),
                        "strategy": str(signal.strategy_name),
                        "reason": signal.reason,
                        "target_shares": int(signal.target_shares),
                        "loser_state_index": state_index,
                        "loser_sleeve_name": sleeve_name,
                        "winner_state_index": winner_state_index,
                        "winner_sleeve_name": winner_sleeve_name,
                        "strategy_name": str(signal.strategy_name),
                        "loser_reason": signal.reason,
                        "winner_reason": winner.reason,
                        "loser_target_shares": int(signal.target_shares),
                        "winner_target_shares": int(winner.target_shares),
                        "loser_priority": loser_priority,
                        "winner_priority": winner_priority,
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
    state_local_books: bool = True,
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
        sleeve = getattr(states[action.state_index], "sleeve", None)
        records = getattr(sleeve, "_c6_action_lifecycle", None)
        if records is not None and sleeve is not None:
            ordinal = next_action_ordinal(sleeve)
            position = sleeve.positions.get(action.symbol, {}).get(action.strategy_name)
            current = int(position.shares) if position is not None else 0
            record = {"emission_ordinal": ordinal, "timestamp": action.signal_date, "action_id": f"risk:{action.signal_date}:{ordinal}:{action.state_index}:{action.symbol}:{action.strategy_name}", "winner_order_ordinal": ordinal, "loser_order_ordinals": [], "state_index": action.state_index, "sleeve_name": sleeve.sleeve_name, "strategy_name": action.strategy_name, "symbol": action.symbol, "reason": action.reason, "priority": action.priority, "planned_shares": action.shares, "retained_shares": action.shares, "suppressed_shares": 0, "filled_shares": 0, "current_shares": current, "remainder_shares": action.shares, "scope_kind": "book_symbol", "scope_key": f"{action.state_index}:{action.symbol}:{action.strategy_name}", "post_action_target_shares": max(current - action.shares, 0), "terminal_for_current_batch": False, "carry_to_next_batch": True, "release_reason": "STILL_LIVE"}
            record["execution_timestamp"] = None
            record["observation_timestamp"] = action.signal_date
            record["observation_phase"] = "decision_close"
            record["reference_price"] = float(action.price)
            records.append(record)
            sleeve._c6_action_by_signal[id(signal)] = (signal, record)
            order = order_receipt(sleeve, signal, action.signal_date)
            if order is not None:
                record["winner_order_ordinal"] = order["order_ordinal"]
    if date_str is not None:
        consolidate_risk_sells(
            states,
            date_str,
            events if events is not None else [],
            action_priorities=priorities,
            state_local_books=state_local_books,
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
                    adjusted = replace(signal, target_shares=scaled_shares)
                    link_order(state.sleeve, signal, adjusted)
                    retained.append(
                        (adjusted, strategy)
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
