from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from quantfusion.config.engine import default_engine_config
from quantfusion.domain.models import Signal, TradeRecord
from quantfusion.risk import exposure_recovery as recovery
from quantfusion.risk.account_budget import plan_account_risk_budget
from quantfusion.risk.exposure_recovery import (
    AB5RecoveryState,
    apply_account_risk_budget_with_recovery,
    filled_ab5_reduction_caps,
    next_ab5_recovery_decision,
)


def _envelope(
    *,
    date: str = "2026-01-05",
    gross_cap: float = 60_000.0,
    gross_before: float = 50_000.0,
    state: AB5RecoveryState | None = None,
    recovery_buy_cap: float | None = None,
    risk_alert_active: bool = False,
    shock_episode_active: bool = False,
) -> dict:
    event = {
        "date": date,
        "event": "account_budget_envelope",
        "gross_before": gross_before,
        "gross_cap": gross_cap,
        "risk_alert_active": risk_alert_active,
        "shock_episode_active": shock_episode_active,
    }
    if state is not None:
        event["ab5_recovery_state"] = state.value
    if recovery_buy_cap is not None:
        event["ab5_recovery_buy_gross_cap"] = recovery_buy_cap
    return event


def _filled_trim(
    *,
    date: str = "2026-01-06",
    signal_date: str = "2026-01-05",
) -> TradeRecord:
    return TradeRecord(
        "300308",
        "atr_channel",
        "sell",
        200,
        10.0,
        date,
        reason="account_budget_trim",
        signal_date=signal_date,
    )


def _buy(shares: int = 2_000) -> Signal:
    return Signal(
        "300502",
        "turtle_breakout",
        "buy",
        target_shares=shares,
        price=10.0,
        signal_date="2026-01-05",
        reason="strategy entry",
    )


def _action_identity(actions: list) -> list[tuple]:
    return [
        (
            action.state_index,
            action.symbol,
            action.strategy_name,
            action.shares,
            action.reason,
        )
        for action in actions
    ]


def test_recovery_buy_ceiling_limits_new_risk_without_creating_sell_relief() -> None:
    cfg = default_engine_config()
    books = [(0, "300308", "atr_channel", 5_000, 10.0)]
    buys = [(0, _buy(), 20_000.0)]

    receipt, actions = plan_account_risk_budget(
        100_000.0,
        100_000.0,
        cfg,
        books,
        buys,
        lambda _: 0.0,
        date_str="2026-01-05",
        buy_gross_cap_ceiling=30_000.0,
    )

    assert receipt["gross_cap"] > 50_000.0
    assert actions == []
    assert receipt["recovery_buy_gross_cap"] == 30_000.0
    assert receipt["recovery_buy_gross_scale"] == 0.0
    assert receipt["buy_scales"] == [0.0]


def test_recovery_buy_ceiling_never_weakens_or_strengthens_canonical_sells() -> None:
    cfg = default_engine_config()
    books = [(0, "300308", "atr_channel", 8_000, 10.0)]

    baseline, baseline_actions = plan_account_risk_budget(
        90_000.0,
        100_000.0,
        cfg,
        books,
        [],
        lambda _: 0.0,
        date_str="2026-01-05",
    )
    recovery, recovery_actions = plan_account_risk_budget(
        90_000.0,
        100_000.0,
        cfg,
        books,
        [],
        lambda _: 0.0,
        date_str="2026-01-05",
        buy_gross_cap_ceiling=10_000.0,
    )

    assert baseline["gross_cap"] < 80_000.0
    assert recovery["gross_cap"] == baseline["gross_cap"]
    assert _action_identity(recovery_actions) == _action_identity(baseline_actions)


def test_planner_without_recovery_ceiling_keeps_original_receipt_shape() -> None:
    cfg = default_engine_config()
    receipt, _ = plan_account_risk_budget(
        100_000.0,
        100_000.0,
        cfg,
        [],
        [],
        lambda _: 0.0,
        date_str="2026-01-05",
    )

    assert "recovery_buy_gross_cap" not in receipt
    assert "recovery_buy_gross_scale" not in receipt


def test_filled_trim_owns_the_cap_from_its_signal_close() -> None:
    state = SimpleNamespace(sleeve=SimpleNamespace(trades=[_filled_trim()]))
    events = [
        _envelope(date="2026-01-04", gross_cap=70_000.0),
        _envelope(date="2026-01-05", gross_cap=60_000.0),
    ]

    assert filled_ab5_reduction_caps([state], "2026-01-06", events) == [60_000.0]


def test_filled_trim_without_source_envelope_fails_closed() -> None:
    state = SimpleNamespace(sleeve=SimpleNamespace(trades=[_filled_trim()]))

    with pytest.raises(ValueError, match="source account budget envelope"):
        filled_ab5_reduction_caps([state], "2026-01-06", [])


def test_filled_trim_enters_reduced_and_holds_source_buy_cap() -> None:
    decision = next_ab5_recovery_decision(
        previous_envelope=_envelope(),
        new_reduction_caps=[60_000.0],
    )

    assert decision.state is AB5RecoveryState.AB5_REDUCED
    assert decision.buy_gross_cap_ceiling == 60_000.0


def test_reduced_requires_one_canonically_safe_close_before_pending() -> None:
    previous = _envelope(
        gross_cap=80_000.0,
        state=AB5RecoveryState.AB5_REDUCED,
        recovery_buy_cap=60_000.0,
    )

    decision = next_ab5_recovery_decision(
        previous_envelope=previous,
        new_reduction_caps=[],
    )

    assert decision.state is AB5RecoveryState.AB5_RECOVERY_PENDING
    assert decision.buy_gross_cap_ceiling == 60_000.0


def test_recovery_pending_requires_second_canonically_safe_close() -> None:
    previous = _envelope(
        gross_cap=80_000.0,
        state=AB5RecoveryState.AB5_RECOVERY_PENDING,
        recovery_buy_cap=60_000.0,
    )

    decision = next_ab5_recovery_decision(
        previous_envelope=previous,
        new_reduction_caps=[],
    )

    assert decision.state is AB5RecoveryState.NORMAL
    assert decision.buy_gross_cap_ceiling is None


def test_underlying_risk_deterioration_returns_pending_to_reduced() -> None:
    previous = _envelope(
        gross_before=70_000.0,
        gross_cap=65_000.0,
        state=AB5RecoveryState.AB5_RECOVERY_PENDING,
        recovery_buy_cap=60_000.0,
    )

    decision = next_ab5_recovery_decision(
        previous_envelope=previous,
        new_reduction_caps=[],
    )

    assert decision.state is AB5RecoveryState.AB5_REDUCED
    assert decision.buy_gross_cap_ceiling == 60_000.0


def test_active_alert_or_shock_is_not_recovery_safe() -> None:
    for flag in ("risk_alert_active", "shock_episode_active"):
        kwargs = {flag: True}
        previous = _envelope(
            gross_cap=80_000.0,
            state=AB5RecoveryState.AB5_RECOVERY_PENDING,
            recovery_buy_cap=60_000.0,
            **kwargs,
        )
        decision = next_ab5_recovery_decision(
            previous_envelope=previous,
            new_reduction_caps=[],
        )
        assert decision.state is AB5RecoveryState.AB5_REDUCED


def test_adapter_holds_buy_budget_without_mutating_strategy_queue(monkeypatch) -> None:
    current = "2026-01-06"
    buy = Signal(
        "300308",
        "atr_channel",
        "buy",
        target_shares=500,
        price=10.0,
        signal_date="2026-01-05",
        reason="strategy entry",
    )
    pending = [(buy, SimpleNamespace(name="atr_channel"))]
    state = SimpleNamespace(
        sleeve=SimpleNamespace(trades=[_filled_trim()]),
        pending=pending,
    )
    events = [_envelope()]

    def fake_account_budget(states, date, assets, peak, cfg, score, events, **kwargs):
        assert states[0].pending == pending
        assert date == pd.Timestamp(current)
        assert assets == 100_000.0
        assert peak == 120_000.0
        assert cfg == {"sentinel": True}
        assert score("300308") == 0.5
        assert kwargs == {
            "shock_floor": 0.1,
            "buy_gross_cap_ceiling": 60_000.0,
        }
        events.append(
            _envelope(
                date=current,
                gross_cap=55_000.0,
                gross_before=50_000.0,
            )
        )

    monkeypatch.setattr(recovery, "_apply_account_risk_budget", fake_account_budget)

    apply_account_risk_budget_with_recovery(
        [state],
        pd.Timestamp(current),
        100_000.0,
        120_000.0,
        {"sentinel": True},
        lambda _: 0.5,
        events,
        shock_floor=0.1,
    )

    assert state.pending == pending
    envelope = events[-1]
    assert envelope["ab5_recovery_state"] == AB5RecoveryState.AB5_REDUCED.value
    assert envelope["ab5_recovery_buy_gross_cap"] == 55_000.0


def test_adapter_releases_buy_ceiling_after_second_safe_close(monkeypatch) -> None:
    state = SimpleNamespace(sleeve=SimpleNamespace(trades=[]), pending=[])
    events = [
        _envelope(
            gross_cap=80_000.0,
            state=AB5RecoveryState.AB5_RECOVERY_PENDING,
            recovery_buy_cap=60_000.0,
        )
    ]

    def fake_account_budget(states, date, assets, peak, cfg, score, events, **kwargs):
        assert "buy_gross_cap_ceiling" not in kwargs
        events.append(
            _envelope(
                date="2026-01-06",
                gross_cap=80_000.0,
                gross_before=50_000.0,
            )
        )

    monkeypatch.setattr(recovery, "_apply_account_risk_budget", fake_account_budget)

    apply_account_risk_budget_with_recovery(
        [state],
        pd.Timestamp("2026-01-06"),
        100_000.0,
        120_000.0,
        {"sentinel": True},
        lambda _: 0.5,
        events,
    )

    assert "ab5_recovery_state" not in events[-1]
    assert "ab5_recovery_buy_gross_cap" not in events[-1]


def test_all_production_replay_paths_use_the_recovery_adapter() -> None:
    import quantfusion.engine.replay_loop as replay_loop
    from quantfusion.engine.universe import BacktestEngine

    assert BacktestEngine._apply_account_risk_budget.__module__ == "quantfusion.engine.universe"
    assert replay_loop.apply_account_risk_budget is apply_account_risk_budget_with_recovery
