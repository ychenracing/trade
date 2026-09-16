from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from quantfusion.domain.models import Signal, TradeRecord
from quantfusion.risk import exposure_recovery as recovery
from quantfusion.risk.exposure_recovery import (
    AB5RecoveryState,
    apply_ab5_recovery_buy_ownership,
    apply_ab5_recovery_hysteresis,
    apply_account_risk_budget_with_recovery,
    filled_ab5_reduction_shares,
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


def _buy(
    symbol: str = "300308",
    strategy_name: str = "atr_channel",
    shares: int = 500,
) -> Signal:
    return Signal(
        symbol,
        strategy_name,
        "buy",
        target_shares=shares,
        price=10.0,
        signal_date="2026-01-06",
        reason="strategy entry",
    )


def test_new_filled_ab5_reduction_enters_reduced_state() -> None:
    state = next_ab5_recovery_state(
        AB5RecoveryState.NORMAL,
        previous_envelope=_safe_envelope(),
        new_reduction_shares={(0, "300308", "atr_channel"): 200},
    )

    assert state is AB5RecoveryState.AB5_REDUCED


def test_reduced_state_requires_one_safe_close_before_recovery_pending() -> None:
    state = next_ab5_recovery_state(
        AB5RecoveryState.AB5_REDUCED,
        previous_envelope=_safe_envelope(),
        new_reduction_shares={},
    )

    assert state is AB5RecoveryState.AB5_RECOVERY_PENDING


def test_recovery_pending_requires_second_safe_close_before_normal() -> None:
    state = next_ab5_recovery_state(
        AB5RecoveryState.AB5_RECOVERY_PENDING,
        previous_envelope=_safe_envelope(),
        new_reduction_shares={},
    )

    assert state is AB5RecoveryState.NORMAL


def test_unsafe_close_returns_recovery_pending_to_reduced() -> None:
    unsafe = _safe_envelope()
    unsafe["buy_scale"] = 0.75

    state = next_ab5_recovery_state(
        AB5RecoveryState.AB5_RECOVERY_PENDING,
        previous_envelope=unsafe,
        new_reduction_shares={},
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
            new_reduction_shares={},
        )
        is AB5RecoveryState.AB5_REDUCED
    )
    assert (
        next_ab5_recovery_state(
            AB5RecoveryState.AB5_REDUCED,
            previous_envelope=shock,
            new_reduction_shares={},
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
                "atr_channel",
                "sell",
                100,
                10.0,
                current,
                reason="account_budget_trim:continued",
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

    assert filled_ab5_reduction_shares(states, current) == {
        (0, "300308", "atr_channel"): 300,
        (1, "300394", "dual_ma"): 300,
    }


def test_recovery_filter_defers_only_owned_shares_and_never_sells() -> None:
    owned_buy = _buy(shares=500)
    unrelated_buy = _buy("300502", shares=600)
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

    retained, deferred_orders, deferred_shares = apply_ab5_recovery_buy_ownership(
        pending,
        state_index=0,
        owned_shares={(0, "300308", "atr_channel"): 200},
    )

    assert retained[0][0].target_shares == 300
    assert retained[0][0].symbol == "300308"
    assert retained[1:] == [(unrelated_buy, strategy), (owned_sell, None)]
    assert deferred_orders == 1
    assert deferred_shares == 200


def test_recovery_filter_never_defers_more_than_owned_shares_across_same_book() -> None:
    strategy = SimpleNamespace(name="atr_channel")
    pending = [(_buy(shares=150), strategy), (_buy(shares=200), strategy)]

    retained, deferred_orders, deferred_shares = apply_ab5_recovery_buy_ownership(
        pending,
        state_index=0,
        owned_shares={(0, "300308", "atr_channel"): 200},
    )

    assert [signal.target_shares for signal, _ in retained] == [150]
    assert deferred_orders == 2
    assert deferred_shares == 200


def test_hysteresis_enters_reduced_and_preserves_only_filled_trim_exposure() -> None:
    current = "2026-01-06"
    owned_buy = _buy(shares=500)
    unrelated_buy = _buy("300502", shares=600)
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
        ab5_recovery_owned_shares=[],
    )

    decision = apply_ab5_recovery_hysteresis([state], current, [previous])

    assert decision["state"] == AB5RecoveryState.AB5_REDUCED.value
    assert decision["owned_shares"] == [[0, "300308", "atr_channel", 200]]
    assert decision["deferred_orders"] == 1
    assert decision["deferred_shares"] == 200
    assert state.pending[0][0].target_shares == 300
    assert state.pending[1] == (unrelated_buy, strategy)


def test_recovery_pending_preserves_same_owned_exposure_for_second_safe_close() -> None:
    owned_buy = _buy(shares=500)
    strategy = SimpleNamespace(name="atr_channel")
    state = SimpleNamespace(
        sleeve=SimpleNamespace(trades=[], _c6_orders=None),
        pending=[(owned_buy, strategy)],
    )
    previous = _safe_envelope()
    previous.update(
        date="2026-01-05",
        ab5_recovery_state=AB5RecoveryState.AB5_REDUCED.value,
        ab5_recovery_owned_shares=[[0, "300308", "atr_channel", 200]],
    )

    decision = apply_ab5_recovery_hysteresis([state], "2026-01-06", [previous])

    assert decision["state"] == AB5RecoveryState.AB5_RECOVERY_PENDING.value
    assert decision["owned_shares"] == [[0, "300308", "atr_channel", 200]]
    assert state.pending[0][0].target_shares == 300


def test_second_safe_close_returns_normal_and_releases_full_buy() -> None:
    owned_buy = _buy(shares=500)
    strategy = SimpleNamespace(name="atr_channel")
    state = SimpleNamespace(
        sleeve=SimpleNamespace(trades=[], _c6_orders=None),
        pending=[(owned_buy, strategy)],
    )
    previous = _safe_envelope()
    previous.update(
        date="2026-01-05",
        ab5_recovery_state=AB5RecoveryState.AB5_RECOVERY_PENDING.value,
        ab5_recovery_owned_shares=[[0, "300308", "atr_channel", 200]],
    )

    decision = apply_ab5_recovery_hysteresis([state], "2026-01-06", [previous])

    assert decision["state"] == AB5RecoveryState.NORMAL.value
    assert decision["owned_shares"] == []
    assert decision["deferred_orders"] == 0
    assert state.pending == [(owned_buy, strategy)]


def test_account_budget_adapter_gates_before_canonical_and_annotates_envelope(
    monkeypatch,
) -> None:
    current = "2026-01-06"
    owned_buy = _buy(shares=500)
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
        ab5_recovery_owned_shares=[],
    )
    events = [previous]

    def fake_account_budget(states, date, assets, peak, cfg, score, events, **kwargs):
        assert states[0].pending[0][0].target_shares == 300
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
    assert envelope["ab5_recovery_owned_shares"] == [
        [0, "300308", "atr_channel", 200]
    ]
    assert envelope["ab5_recovery_deferred_buy_orders"] == 1
    assert envelope["ab5_recovery_deferred_buy_shares"] == 200


def test_all_production_replay_paths_use_the_recovery_adapter() -> None:
    import quantfusion.engine.replay_loop as replay_loop
    from quantfusion.engine.universe import BacktestEngine

    assert BacktestEngine._apply_account_risk_budget.__module__ == "quantfusion.engine.universe"
    assert replay_loop.apply_account_risk_budget is apply_account_risk_budget_with_recovery
