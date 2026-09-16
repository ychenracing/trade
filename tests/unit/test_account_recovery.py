from __future__ import annotations

from quantfusion.risk.exposure_recovery import (
    AB5RecoveryState,
    next_ab5_recovery_state,
)


def _safe_envelope() -> dict:
    return {
        "event": "account_budget_envelope",
        "gross_before": 900_000.0,
        "gross_cap": 1_000_000.0,
        "buy_scale": 1.0,
        "new_reduction_orders": 0,
        "risk_alert_active": False,
        "shock_episode_active": False,
    }


def test_new_filled_ab5_reduction_enters_reduced_state() -> None:
    state = next_ab5_recovery_state(
        AB5RecoveryState.NORMAL,
        previous_envelope=_safe_envelope(),
        new_reduction_book_ids={(0, "300308", "atr_channel")},
    )

    assert state is AB5RecoveryState.AB5_REDUCED


def test_reduced_state_requires_one_safe_close_before_recovery_pending() -> None:
    state = next_ab5_recovery_state(
        AB5RecoveryState.AB5_REDUCED,
        previous_envelope=_safe_envelope(),
        new_reduction_book_ids=set(),
    )

    assert state is AB5RecoveryState.AB5_RECOVERY_PENDING


def test_recovery_pending_requires_second_safe_close_before_normal() -> None:
    state = next_ab5_recovery_state(
        AB5RecoveryState.AB5_RECOVERY_PENDING,
        previous_envelope=_safe_envelope(),
        new_reduction_book_ids=set(),
    )

    assert state is AB5RecoveryState.NORMAL


def test_unsafe_close_returns_recovery_pending_to_reduced() -> None:
    unsafe = _safe_envelope()
    unsafe["buy_scale"] = 0.75

    state = next_ab5_recovery_state(
        AB5RecoveryState.AB5_RECOVERY_PENDING,
        previous_envelope=unsafe,
        new_reduction_book_ids=set(),
    )

    assert state is AB5RecoveryState.AB5_REDUCED


def test_safe_state_requires_no_planned_reduction_or_active_shock() -> None:
    planned = _safe_envelope()
    planned["new_reduction_orders"] = 1
    shock = _safe_envelope()
    shock["shock_episode_active"] = True

    assert (
        next_ab5_recovery_state(
            AB5RecoveryState.AB5_REDUCED,
            previous_envelope=planned,
            new_reduction_book_ids=set(),
        )
        is AB5RecoveryState.AB5_REDUCED
    )
    assert (
        next_ab5_recovery_state(
            AB5RecoveryState.AB5_REDUCED,
            previous_envelope=shock,
            new_reduction_book_ids=set(),
        )
        is AB5RecoveryState.AB5_REDUCED
    )
