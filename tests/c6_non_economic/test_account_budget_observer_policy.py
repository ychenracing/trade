"""Production AB5 observation-only policy contracts."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pandas as pd

from quantfusion.account.models import AccountPosition, AccountSnapshot
from quantfusion.application import account_scan
from quantfusion.config.engine import default_engine_config
from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.domain.models import Position, Signal
from quantfusion.engine.ensemble import EnsembleSleeveBacktestEngine
from quantfusion.engine.universe import BacktestEngine
from quantfusion.risk.account_budget_observer import account_budget_observer_status


def _state() -> tuple[BacktestEngine, SimpleNamespace, pd.Timestamp]:
    cfg = default_engine_config()
    sleeve = EnsembleSleeveBacktestEngine(
        100_000.0,
        cfg=cfg,
        policy=PortfolioPolicy(),
        allocation_lookbacks=(3, 5, 10),
        sleeve_name="fast",
    )
    sleeve._reset_run_state({"300308": "test"})
    sleeve.positions = {
        "300308": {
            "turtle_breakout": Position(
                symbol="300308",
                strategy_name="turtle_breakout",
                shares=8_000,
                entry_price=10.0,
                entry_date="2026-01-01",
                stop_loss=7.0,
                highest_since_entry=10.0,
                highest_close_since_entry=10.0,
                last_buy_date="2026-01-01",
            )
        }
    }
    sleeve.cash = 10_000.0
    day = pd.Timestamp("2026-01-05")
    frame = pd.DataFrame(
        {
            "open": [10.0, 10.0, 10.0],
            "close": [10.0, 10.0, 10.0],
            "high": [10.0, 10.0, 10.0],
            "low": [10.0, 10.0, 10.0],
            "volume": [1e8, 1e8, 1e8],
        },
        index=pd.to_datetime(["2026-01-03", "2026-01-04", "2026-01-05"]),
    )
    buy = Signal(
        "300308",
        "turtle_breakout",
        "buy",
        target_shares=1_000,
        price=10.0,
        signal_date="2026-01-05",
        reason="existing strategy buy",
    )
    sell = Signal(
        "300308",
        "turtle_breakout",
        "sell",
        target_shares=500,
        price=10.0,
        signal_date="2026-01-05",
        reason="existing strategy sell",
    )
    state = SimpleNamespace(
        sleeve=sleeve,
        data_map={"300308": frame},
        pending=[(buy, SimpleNamespace(name="turtle_breakout")), (sell, None)],
        all_dates=list(frame.index),
        date_to_pos={value: index for index, value in enumerate(frame.index)},
    )
    return BacktestEngine(cfg={"account_risk_budget_enabled": True}), state, day


def test_observation_only_budget_never_changes_trade_queue_or_account_state():
    engine, state, day = _state()
    before_pending = deepcopy(state.pending)
    before_account = deepcopy(
        (state.sleeve.positions, state.sleeve.cash, vars(state.sleeve.risk))
    )
    events: list[dict[str, object]] = []

    engine._apply_account_risk_budget(
        [state], day, 90_000.0, 100_000.0, events
    )

    assert state.pending == before_pending
    assert (
        state.sleeve.positions,
        state.sleeve.cash,
        vars(state.sleeve.risk),
    ) == before_account
    receipt = events[-1]
    assert receipt["event"] == "account_budget_envelope"
    assert receipt["mechanism"] == "AB5"
    assert receipt["policy_mode"] == "OBSERVE_ONLY"
    assert receipt["health_status"] == "EVALUATED"
    assert receipt["buy_shares_removed"] == 0
    assert receipt["new_reduction_orders"] == 0


def test_account_budget_status_separates_policy_from_evaluation_health():
    events = [
        {
            "event": "account_budget_envelope",
            "mechanism": "AB5",
            "policy_mode": "OBSERVE_ONLY",
            "health_status": "EVALUATED",
        }
    ]

    status = account_budget_observer_status(events, True)

    assert status["enabled"] is True
    assert status["mechanism"] == "AB5"
    assert status["policy_mode"] == "OBSERVE_ONLY"
    assert status["health_status"] == "EVALUATED"
    assert status["status"] == "OBSERVED"


def test_account_budget_status_fails_closed_for_incomplete_observation():
    status = account_budget_observer_status(
        [{"event": "account_budget_envelope", "mechanism": "AB5"}], True
    )

    assert status["policy_mode"] is None
    assert status["health_status"] is None
    assert status["status"] == "INVALID_OBSERVATION"


def test_real_account_observer_does_not_rewrite_manual_advice():
    dates = pd.date_range("2025-10-01", periods=100, freq="D")
    frame = pd.DataFrame(
        {
            "open": [10.0] * len(dates),
            "close": [10.0] * len(dates),
            "high": [10.0] * len(dates),
            "low": [10.0] * len(dates),
            "volume": [1e8] * len(dates),
        },
        index=dates,
    )
    as_of = dates[-1].strftime("%Y-%m-%d")
    snapshot = AccountSnapshot(
        schema_version=3,
        account_id="main",
        snapshot_date=as_of,
        cash=10_000.0,
        peak_equity=100_000.0,
        positions=(
            AccountPosition(
                symbol="300308",
                shares=8_000,
                sellable_shares=8_000,
                avg_cost=10.0,
                entry_date=dates[0].strftime("%Y-%m-%d"),
                highest_close=10.0,
            ),
        ),
    )
    actions = [
        {
            "symbol": "300308",
            "action": "HOLD",
            "shares": 8_000,
            "sellable_shares": 8_000,
            "recommended_shares": 0,
            "blocked_shares": 0,
            "execution_status": "NO_ACTION",
            "close": 10.0,
            "reason": "original advice",
        }
    ]
    prepared = {
        "300308": (frame, as_of, default_engine_config(), {}),
    }
    before = deepcopy(actions)

    receipt = account_scan.AccountSignalEngine._apply_account_budget(
        snapshot,
        prepared,
        actions,
        equity=90_000.0,
        as_of=as_of,
    )

    assert actions == before
    assert receipt["policy_mode"] == "OBSERVE_ONLY"
    assert receipt["health_status"] == "EVALUATED"
    assert receipt["status"] == "OBSERVED"
    assert receipt["trade_intervention_allowed"] is False
    assert receipt["buy_shares_removed"] == 0
    assert receipt["new_reduction_orders"] == 0
