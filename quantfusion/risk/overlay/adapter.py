"""Translate immutable risk actions into the legacy pending queue."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

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
    priority: int | None = None,
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
        risk_priority=priority,
    )


def _action_beats(candidate: RiskAction, current: RiskAction) -> bool:
    if candidate.priority != current.priority:
        return candidate.priority > current.priority
    if candidate.shares != current.shares:
        return candidate.shares > current.shares
    candidate_reason = f"{candidate.reason}:{candidate.extra}"
    current_reason = f"{current.reason}:{current.extra}"
    return candidate_reason < current_reason


def resolve_risk_actions(
    actions: Iterable[RiskAction],
) -> tuple[tuple[RiskAction, ...], tuple[RiskAction, ...]]:
    """Resolve one winning immutable action per strategy book using priority."""
    ordered = tuple(actions)
    winner_index: dict[tuple[str, str], int] = {}
    for index, action in enumerate(ordered):
        book = (action.symbol, action.strategy_name)
        current_index = winner_index.get(book)
        if current_index is None or _action_beats(action, ordered[current_index]):
            winner_index[book] = index
    selected = frozenset(winner_index.values())
    winners = tuple(action for index, action in enumerate(ordered) if index in selected)
    suppressed = tuple(
        action for index, action in enumerate(ordered) if index not in selected
    )
    return winners, suppressed


def apply_risk_actions(
    actions: Iterable[RiskAction], state_or_states: Any
) -> tuple[tuple[RiskAction, ...], tuple[RiskAction, ...]]:
    """Adapt actions and reconcile them with carried risk-pending signals.

    A risk sell can remain pending across trading days.  The historical engine
    resolved a newly emitted action against those carried entries as well as
    against same-day actions.  ``Signal.risk_priority`` preserves the immutable
    action's priority at the adapter boundary, so this reconciliation never
    parses the display-oriented reason string.
    """
    is_state_sequence = isinstance(state_or_states, (list, tuple))
    states = tuple(state_or_states) if is_state_sequence else (state_or_states,)
    for action in actions:
        state = states[action.state_index] if is_state_sequence else states[0]
        signal = make_sell_signal(
            action.symbol,
            action.strategy_name,
            action.shares,
            action.price,
            action.signal_date,
            action.reason,
            action.extra,
            action.priority,
        )
        state.pending.append((signal, None))

    action_by_signal_id: dict[int, RiskAction] = {}
    ordered_actions: list[RiskAction] = []
    for state_index, state in enumerate(states):
        for signal, strategy in state.pending:
            priority = getattr(signal, "risk_priority", None)
            if (
                strategy is not None
                or getattr(signal, "direction", None) != "sell"
                or priority is None
            ):
                continue
            pending_action = RiskAction(
                symbol=str(signal.symbol),
                strategy_name=str(signal.strategy_name),
                shares=int(signal.target_shares),
                price=float(signal.price),
                signal_date=str(signal.signal_date),
                reason=str(signal.reason),
                priority=int(priority),
                state_index=state_index,
            )
            action_by_signal_id[id(signal)] = pending_action
            ordered_actions.append(pending_action)

    winners, suppressed = resolve_risk_actions(ordered_actions)
    winner_ids = {id(action) for action in winners}
    for state in states:
        retained: list[tuple[Any, Any]] = []
        for entry in state.pending:
            signal, strategy = entry
            pending_action = action_by_signal_id.get(id(signal))
            if pending_action is None or id(pending_action) in winner_ids:
                retained.append(entry)
        state.pending = retained
    return winners, suppressed
