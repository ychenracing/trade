"""C6 F0 contracts for state-local defensive sell consolidation."""
from types import SimpleNamespace
from quantfusion.risk.overlay.adapter import (
    apply_risk_actions,
    consolidate_risk_sells,
    make_sell_signal,
)
from quantfusion.risk.overlay.models import RiskAction
def _state(name: str, *signals: object) -> SimpleNamespace:
    return SimpleNamespace(
        sleeve=SimpleNamespace(sleeve_name=name, positions={}, _c6_action_lifecycle=[], _c6_action_by_signal={}),
        pending=[(signal, None) for signal in signals],
    )
def _sell(reason: str, shares: int = 100) -> object:
    return make_sell_signal(
        "AAA", "real-strategy", shares, 10.0, "2026-08-17", reason
    )
def test_same_real_book_in_three_states_keeps_one_sell_per_state() -> None:
    states = [_state(name, _sell("cost_stop")) for name in ("fast", "base", "slow")]
    events: list[dict[str, object]] = []
    consolidate_risk_sells(states, "2026-08-17", events)
    assert [len(state.pending) for state in states] == [1, 1, 1]
    assert events == []
def test_f0_off_reproduces_legacy_cross_state_book_collapse() -> None:
    states = [_state(name, _sell("cost_stop")) for name in ("fast", "base", "slow")]
    consolidate_risk_sells(
        states,
        "2026-08-17",
        [],
        state_local_books=False,
    )
    assert sum(len(state.pending) for state in states) == 1
def test_same_book_preserves_priority_target_and_reason_winner_order() -> None:
    low_priority = _sell("low-priority", 999)
    smaller_target = _sell("smaller-target", 80)
    later_reason = _sell("z-reason", 100)
    winner = _sell("a-reason", 100)
    state = _state("fast", low_priority, smaller_target, later_reason, winner)
    consolidate_risk_sells(
        [state],
        "2026-08-17",
        [],
        action_priorities={
            id(low_priority): 50,
            id(smaller_target): 60,
            id(later_reason): 60,
            id(winner): 60,
        },
    )
    assert [signal for signal, _ in state.pending] == [winner]
def test_carried_higher_priority_sell_remains_the_winner() -> None:
    carried = _sell("catastrophe_stop")
    state = _state("fast", carried)
    events: list[dict[str, object]] = []
    action = RiskAction(
        symbol="AAA",
        strategy_name="real-strategy",
        shares=999,
        price=10.0,
        signal_date="2026-08-17",
        reason="concentration_trim",
        priority=50,
    )
    apply_risk_actions(
        [action], [state], date_str="2026-08-17", events=events
    )
    assert [signal for signal, _ in state.pending] == [carried]
    assert events[0]["winner_reason"] == "catastrophe_stop"
    assert state.sleeve._c6_action_lifecycle[0]["release_reason"] == "CANCELLED"
def test_suppression_audit_has_complete_book_identity_and_stable_order() -> None:
    loser = _sell("concentration_trim", 50)
    winner = _sell("cost_stop", 100)
    unrelated = make_sell_signal(
        "BBB", "other-strategy", 25, 10.0, "2026-08-17", "cost_stop"
    )
    state = _state("fast", loser, unrelated, winner)
    events: list[dict[str, object]] = []
    consolidate_risk_sells([state], "2026-08-17", events)
    assert [signal for signal, _ in state.pending] == [unrelated, winner]
    assert events == [
        {
            "date": "2026-08-17",
            "event": "risk_action_suppressed",
            "symbol": "AAA",
            "strategy": "real-strategy",
            "reason": "concentration_trim",
            "target_shares": 50,
            "loser_state_index": 0,
            "loser_sleeve_name": "fast",
            "winner_state_index": 0,
            "winner_sleeve_name": "fast",
            "strategy_name": "real-strategy",
            "loser_reason": "concentration_trim",
            "winner_reason": "cost_stop",
            "loser_target_shares": 50,
            "winner_target_shares": 100,
            "loser_priority": 50,
            "winner_priority": 90,
        }
    ]
