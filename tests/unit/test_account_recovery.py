from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from quantfusion.domain.models import Signal, TradeRecord
from quantfusion.risk import exposure_recovery as recovery
from quantfusion.risk.exposure_recovery import (
    AB5RecoveryState,
    apply_ab5_recovery_hysteresis,
    apply_account_risk_budget_with_recovery,
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


def _buy(symbol: str = "300308", strategy_name: str = "atr_channel") -> Signal:
    return Signal(
        symbol,
        strategy_name,
        "buy",
        target_shares=500,
        price=10.0,
        signal_date="2026-01-06",
        reason="strategy entry",
    )


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
    owned_buy = _buy()
    unrelated_buy = _buy("300502")
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


def test_hysteresis_enters_reduced_on_filled_trim_and_blocks_same_book_rebuy() -> None:
    current = "2026-01-06"
    owned_buy = _buy()
    unrelated_buy = _buy("300502")
    strategy = SimpleNamespace(name="atr_channel")
    sleeve = SimpleNamespace(
        trades=[
            TradeRecord(
                "300308",
                "atr_channel",
                "sell",
                200,
                10.0,
                current,
                reason="account_budget_trim",
            )
        ],
        _c6_orders=None,
    )
    state = SimpleNamespace(
        sleeve=sleeve,
        pending=[(owned_buy, strategy), (unrelated_buy, strategy)],
    )
    previous = _safe_envelope()
    previous.update(
        date="2026-01-05",
        ab5_recovery_state=AB5RecoveryState.NORMAL.value,
        ab5_recovery_book_ids=[],
    )

    decision = apply_ab5_recovery_hysteresis([state], current, [previous])

    assert decision["state"] == AB5RecoveryState.AB5_REDUCED.value
    assert decision["book_ids"] == [[0, "300308", "atr_channel"]]
    assert decision["blocked_orders"] == 1
    assert decision["blocked_shares"] == 500
    assert state.pending == [(unrelated_buy, strategy)]


def test_recovery_pending_keeps_owned_rebuy_blocked_for_second_safe_close() -> None:
    owned_buy = _buy()
    strategy = SimpleNamespace(name="atr_channel")
    state = SimpleNamespace(
        sleeve=SimpleNamespace(trades=[], _c6_orders=None),
        pending=[(owned_buy, strategy)],
    )
    previous = _safe_envelope()
    previous.update(
        date="2026-01-05",
        ab5_recovery_state=AB5RecoveryState.AB5_REDUCED.value,
        ab5_recovery_book_ids=[[0, "300308", "atr_channel"]],
    )

    decision = apply_ab5_recovery_hysteresis([state], "2026-01-06", [previous])

    assert decision["state"] == AB5RecoveryState.AB5_RECOVERY_PENDING.value
    assert state.pending == []


def test_second_safe_close_returns_normal_and_releases_owned_rebuy() -> None:
    owned_buy = _buy()
    strategy = SimpleNamespace(name="atr_channel")
    state = SimpleNamespace(
        sleeve=SimpleNamespace(trades=[], _c6_orders=None),
        pending=[(owned_buy, strategy)],
    )
    previous = _safe_envelope()
    previous.update(
        date="2026-01-05",
        ab5_recovery_state=AB5RecoveryState.AB5_RECOVERY_PENDING.value,
        ab5_recovery_book_ids=[[0, "300308", "atr_channel"]],
    )

    decision = apply_ab5_recovery_hysteresis([state], "2026-01-06", [previous])

    assert decision["state"] == AB5RecoveryState.NORMAL.value
    assert decision["book_ids"] == []
    assert decision["blocked_orders"] == 0
    assert state.pending == [(owned_buy, strategy)]


def test_account_budget_adapter_gates_before_canonical_and_annotates_envelope(
    monkeypatch,
) -> None:
    current = "2026-01-06"
    owned_buy = _buy()
    strategy = SimpleNamespace(name="atr_channel")
    sleeve = SimpleNamespace(
        trades=[
            TradeRecord(
                "300308",
                "atr_channel",
                "sell",
                200,
                10.0,
                current,
                reason="account_budget_trim",
            )
        ],
        _c6_orders=None,
    )
    state = SimpleNamespace(sleeve=sleeve, pending=[(owned_buy, strategy)])
    previous = _safe_envelope()
    previous.update(
        date="2026-01-05",
        ab5_recovery_state=AB5RecoveryState.NORMAL.value,
        ab5_recovery_book_ids=[],
    )
    events = [previous]

    def fake_account_budget(states, date, assets, peak, cfg, score, events, **kwargs):
        assert states[0].pending == []
        assert date == pd.Timestamp(current)
        assert assets == 100_000.0
        assert peak == 120_000.0
        assert cfg == {"sentinel": True}
        assert score("300308") == 0.5
        assert kwargs == {"shock_floor": 0.1}
        events.append({
            "date": current,
            "event": "account_budget_envelope",
            "gross_before": 80_000.0,
            "gross_cap": 90_000.0,
            "buy_scale": 1.0,
            "new_reduction_orders": 0,
        })

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

    envelope = events[-1]
    assert envelope["ab5_recovery_state"] == AB5RecoveryState.AB5_REDUCED.value
    assert envelope["ab5_recovery_book_ids"] == [[0, "300308", "atr_channel"]]
    assert envelope["ab5_recovery_blocked_orders"] == 1
    assert envelope["ab5_recovery_buy_shares_blocked"] == 500
