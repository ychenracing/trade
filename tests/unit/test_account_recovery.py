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
    canonical_gross_cap: float | None = None,
    state: AB5RecoveryState | None = None,
    recovery_cap: float | None = None,
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
    if canonical_gross_cap is not None:
        event["canonical_gross_cap"] = canonical_gross_cap
    if state is not None:
        event["ab5_recovery_state"] = state.value
    if recovery_cap is not None:
        event["ab5_recovery_gross_cap"] = recovery_cap
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


def test_planner_recovery_ceiling_only_tightens_canonical_gross_cap() -> None:
    cfg = default_engine_config()
    receipt, _ = plan_account_risk_budget(
        100_000.0,
        100_000.0,
        cfg,
        [],
        [],
        lambda _: 0.0,
        date_str="2026-01-05",
        gross_cap_ceiling=30_000.0,
    )

    assert receipt["canonical_gross_cap"] > 30_000.0
    assert receipt["gross_cap"] == 30_000.0
    assert receipt["recovery_gross_cap_ceiling"] == 30_000.0


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

    assert "canonical_gross_cap" not in receipt
    assert "recovery_gross_cap_ceiling" not in receipt


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


def test_filled_trim_enters_reduced_and_holds_source_cap() -> None:
    decision = next_ab5_recovery_decision(
        previous_envelope=_envelope(),
        new_reduction_caps=[60_000.0],
    )

    assert decision.state is AB5RecoveryState.AB5_REDUCED
    assert decision.gross_cap_ceiling == 60_000.0


def test_reduced_requires_one_canonically_safe_close_before_pending() -> None:
    previous = _envelope(
        canonical_gross_cap=80_000.0,
        state=AB5RecoveryState.AB5_REDUCED,
        recovery_cap=60_000.0,
    )

    decision = next_ab5_recovery_decision(
        previous_envelope=previous,
        new_reduction_caps=[],
    )

    assert decision.state is AB5RecoveryState.AB5_RECOVERY_PENDING
    assert decision.gross_cap_ceiling == 60_000.0


def test_recovery_pending_requires_second_canonically_safe_close() -> None:
    previous = _envelope(
        canonical_gross_cap=80_000.0,
        state=AB5RecoveryState.AB5_RECOVERY_PENDING,
        recovery_cap=60_000.0,
    )

    decision = next_ab5_recovery_decision(
        previous_envelope=previous,
        new_reduction_caps=[],
    )

    assert decision.state is AB5RecoveryState.NORMAL
    assert decision.gross_cap_ceiling is None


def test_underlying_risk_deterioration_returns_pending_to_reduced() -> None:
    previous = _envelope(
        gross_before=70_000.0,
        canonical_gross_cap=65_000.0,
        state=AB5RecoveryState.AB5_RECOVERY_PENDING,
        recovery_cap=60_000.0,
    )

    decision = next_ab5_recovery_decision(
        previous_envelope=previous,
        new_reduction_caps=[],
    )

    assert decision.state is AB5RecoveryState.AB5_REDUCED
    assert decision.gross_cap_ceiling == 60_000.0


def test_active_alert_or_shock_is_not_recovery_safe() -> None:
    for flag in ("risk_alert_active", "shock_episode_active"):
        kwargs = {flag: True}
        previous = _envelope(
            canonical_gross_cap=80_000.0,
            state=AB5RecoveryState.AB5_RECOVERY_PENDING,
            recovery_cap=60_000.0,
            **kwargs,
        )
        decision = next_ab5_recovery_decision(
            previous_envelope=previous,
            new_reduction_caps=[],
        )
        assert decision.state is AB5RecoveryState.AB5_REDUCED


def test_adapter_holds_budget_without_mutating_strategy_queue(monkeypatch) -> None:
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
        assert kwargs == {"shock_floor": 0.1, "gross_cap_ceiling": 60_000.0}
        events.append(
            _envelope(
                date=current,
                gross_cap=55_000.0,
                gross_before=50_000.0,
                canonical_gross_cap=55_000.0,
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
    assert envelope["ab5_recovery_gross_cap"] == 55_000.0


def test_adapter_releases_ceiling_after_second_safe_close(monkeypatch) -> None:
    state = SimpleNamespace(sleeve=SimpleNamespace(trades=[]), pending=[])
    events = [
        _envelope(
            canonical_gross_cap=80_000.0,
            state=AB5RecoveryState.AB5_RECOVERY_PENDING,
            recovery_cap=60_000.0,
        )
    ]

    def fake_account_budget(states, date, assets, peak, cfg, score, events, **kwargs):
        assert "gross_cap_ceiling" not in kwargs
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
    assert "ab5_recovery_gross_cap" not in events[-1]


def test_all_production_replay_paths_use_the_recovery_adapter() -> None:
    import quantfusion.engine.replay_loop as replay_loop
    from quantfusion.engine.universe import BacktestEngine

    assert BacktestEngine._apply_account_risk_budget.__module__ == "quantfusion.engine.universe"
    assert replay_loop.apply_account_risk_budget is apply_account_risk_budget_with_recovery
