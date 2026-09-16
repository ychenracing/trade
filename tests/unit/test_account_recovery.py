from __future__ import annotations

from types import SimpleNamespace

from quantfusion.domain.models import Signal, TradeRecord
from quantfusion.risk.exposure_recovery import (
    AB5RecoveryState,
    filled_ab5_reduction_book_ids,
    filter_ab5_recovery_buys,
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


def test_recovery_ownership_comes_only_from_same_day_filled_ab5_sells() -> None:
    current = "2026-01-06"
    sleeve_a = SimpleNamespace(
        trades=[
            TradeRecord(
                "300308",
                "atr_channel",
                "sell",
                200,
                10.0,
                current,
                reason="account_budget_trim",
            ),
            TradeRecord(
                "300308",
                "dual_ma",
                "sell",
                100,
                10.0,
                current,
                reason="strategy exit",
            ),
            TradeRecord(
                "300502",
                "turtle_breakout",
                "sell",
                100,
                10.0,
                "2026-01-05",
                reason="account_budget_trim",
            ),
        ]
    )
    sleeve_b = SimpleNamespace(
        trades=[
            TradeRecord(
                "300394",
                "dual_ma",
                "sell",
                300,
                20.0,
                current,
                reason="account_budget_trim",
            )
        ]
    )
    states = [SimpleNamespace(sleeve=sleeve_a), SimpleNamespace(sleeve=sleeve_b)]

    assert filled_ab5_reduction_book_ids(states, current) == {
        (0, "300308", "atr_channel"),
        (1, "300394", "dual_ma"),
    }


def test_recovery_filter_blocks_only_owned_buy_books_and_never_sells() -> None:
    owned_buy = Signal(
        "300308",
        "atr_channel",
        "buy",
        target_shares=500,
        price=10.0,
        signal_date="2026-01-06",
        reason="strategy entry",
    )
    unrelated_buy = Signal(
        "300502",
        "atr_channel",
        "buy",
        target_shares=600,
        price=20.0,
        signal_date="2026-01-06",
        reason="strategy entry",
    )
    owned_sell = Signal(
        "300308",
        "atr_channel",
        "sell",
        target_shares=200,
        price=10.0,
        signal_date="2026-01-06",
        reason="risk exit",
    )
    strategy = SimpleNamespace(name="atr_channel")
    pending = [(owned_buy, strategy), (unrelated_buy, strategy), (owned_sell, None)]

    retained, blocked = filter_ab5_recovery_buys(
        pending,
        state_index=0,
        blocked_book_ids={(0, "300308", "atr_channel")},
    )

    assert retained == [(unrelated_buy, strategy), (owned_sell, None)]
    assert blocked == [(owned_buy, strategy)]
