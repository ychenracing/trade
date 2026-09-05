"""Observation-only receipts for C6 defensive actions at execution boundaries."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from quantfusion.config.overlay import RISK_ACTION_DEFAULT_PRIORITY, RISK_ACTION_PRIORITY


def action_receipt(sleeve: Any, signal: Any) -> dict[str, Any] | None:
    """Keep the actual signal alive so recycled object IDs cannot join actions."""
    entry = getattr(sleeve, "_c6_action_by_signal", {}).get(id(signal))
    return entry[1] if entry is not None and entry[0] is signal else None


def link_action(sleeve: Any, original: Any, replacement: Any) -> None:
    record = action_receipt(sleeve, original)
    if record is not None:
        sleeve._c6_action_by_signal[id(replacement)] = (replacement, record)


def order_receipt(sleeve: Any, signal: Any, date: str, *, queued: bool = False) -> dict[str, Any] | None:
    records = getattr(sleeve, "_c6_orders", None)
    if records is None:
        return None
    entry = sleeve._c6_order_by_signal.get(id(signal))
    previous = entry[1] if entry is not None and entry[0] is signal else None
    if previous is not None and not (queued and previous["execution_timestamp"] is not None) and (previous["execution_timestamp"] in {None, date}
                                 or previous["status"] == "pending"):
        return previous
    action = action_receipt(sleeve, signal)
    record = {
        "order_ordinal": len(records), "decision_timestamp": signal.signal_date,
        "execution_timestamp": None, "state_index": sleeve._c6_state_index,
        "sleeve_name": sleeve.sleeve_name, "strategy_name": signal.strategy_name,
        "symbol": signal.symbol, "side": signal.direction.upper(),
        "requested_shares": int(signal.target_shares), "authorized_shares": 0,
        "filled_shares": 0, "reason": signal.reason,
        "priority": action["priority"] if action is not None else None,
        "action_id": action["action_id"] if action is not None else None,
        "scope_kind": action["scope_kind"] if action is not None else None,
        "scope_key": action["scope_key"] if action is not None else None,
        "status": "pending", "suppression_winner_order_ordinal": None,
        "carried_from_order_ordinal": previous["order_ordinal"] if previous else None,
        "blocked_reason": None, "events": [],
    }
    records.append(record)
    sleeve._c6_order_by_signal[id(signal)] = (signal, record)
    return record


def prepare_action_consolidation(sleeve: Any, signal: Any, date: str) -> None:
    previous = action_receipt(sleeve, signal)
    if previous is not None and previous.get("execution_timestamp") is not None:
        record = begin_action_batch(sleeve, signal, date)
        assert record is not None
        record.update(execution_timestamp=None, observation_timestamp=date,
                      observation_phase="decision_close")
    order_receipt(sleeve, signal, date, queued=True)


def link_order(sleeve: Any, original: Any, replacement: Any) -> None:
    entry = getattr(sleeve, "_c6_order_by_signal", {}).get(id(original))
    if entry is not None and entry[0] is original:
        sleeve._c6_order_by_signal[id(replacement)] = (replacement, entry[1])
    link_action(sleeve, original, replacement)


def begin_order(sleeve: Any, signal: Any, date: str, *, defensive: bool) -> dict[str, Any] | None:
    record = order_receipt(sleeve, signal, date)
    if record is not None:
        record["execution_timestamp"] = date
        if defensive and record["priority"] is None:
            record["priority"] = RISK_ACTION_PRIORITY.get(signal.reason.split(":")[0], RISK_ACTION_DEFAULT_PRIORITY)
    return record


def note_order(sleeve: Any, signal: Any, date: str, event: str, **details: Any) -> None:
    record = order_receipt(sleeve, signal, date)
    if record is None:
        return
    record["events"].append({"timestamp": date, "event": event, **details})
    if "adjusted_shares" in details:
        record["authorized_shares"] = int(details["adjusted_shares"])
    if event.startswith(("blocked_", "rejected_", "deferred_", "expired_")):
        record.update(status="blocked", blocked_reason=event, authorized_shares=0)


def record_fill(sleeve: Any, signal: Any, trade: Any) -> None:
    record = order_receipt(sleeve, signal, trade.date)
    if record is None:
        return
    record["execution_timestamp"] = trade.date
    record["filled_shares"] += int(trade.shares)
    record["authorized_shares"] = int(signal.target_shares)
    record["status"] = "filled" if record["filled_shares"] == record["requested_shares"] else "partial"
    sleeve._c6_fills.append({
        "fill_ordinal": len(sleeve._c6_fills), "order_ordinal": record["order_ordinal"],
        "timestamp": trade.date, "state_index": sleeve._c6_state_index,
        "sleeve_name": sleeve.sleeve_name, "strategy_name": signal.strategy_name,
        "symbol": trade.symbol, "side": trade.direction.upper(), "shares": int(trade.shares),
        "price": float(trade.price), "notional": abs(float(trade.gross_value)),
        "fee": float(trade.commission + trade.stamp_duty_cost),
        "slippage": abs(float(trade.price) - float(signal.price)) * int(trade.shares),
        "status": "filled", "blocked_reason": None,
    })


def next_action_ordinal(sleeve: Any) -> int:
    counter = getattr(sleeve, "_c6_action_sequence", None)
    if counter is None:
        counter = [len(sleeve._c6_action_lifecycle)]
        sleeve._c6_action_sequence = counter
    ordinal = counter[0]
    counter[0] += 1
    return ordinal


def begin_action_batch(sleeve: Any, signal: Any, date: str) -> dict[str, Any] | None:
    previous = action_receipt(sleeve, signal)
    if previous is None:
        return None
    record = deepcopy(previous)
    position = sleeve.positions.get(signal.symbol, {}).get(signal.strategy_name)
    current = int(position.shares) if position is not None else 0
    record.update(
        emission_ordinal=next_action_ordinal(sleeve),
        execution_timestamp=date,
        observation_timestamp=date,
        observation_phase="execution_open",
        planned_shares=int(signal.target_shares),
        retained_shares=int(signal.target_shares),
        suppressed_shares=0,
        filled_shares=0,
        current_shares=current,
        remainder_shares=int(signal.target_shares),
        post_action_target_shares=max(current - int(signal.target_shares), 0),
        terminal_for_current_batch=False,
        carry_to_next_batch=False,
        release_reason="STILL_LIVE",
        loser_order_ordinals=[],
    )
    sleeve._c6_action_lifecycle.append(record)
    sleeve._c6_action_by_signal[id(signal)] = (signal, record)
    return record


def finish_action_batch(record: dict[str, Any] | None, *, filled: int = 0,
                        remainder: int = 0, carry: bool = False,
                        release: str) -> None:
    if record is not None:
        record.update(filled_shares=int(filled), remainder_shares=int(remainder),
                      terminal_for_current_batch=True, carry_to_next_batch=carry,
                      release_reason=release)
