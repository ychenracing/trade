"""Production defaults must apply the accepted risk envelope, not merely expose it."""

import pytest

from tests.c6_non_economic.test_c6_recovery_boundaries import synthetic_market as synthetic_market

from quantfusion.account.models import AccountPosition, AccountSnapshot
from quantfusion.application import stress
from quantfusion.config.engine import default_engine_config
from quantfusion.engine.replay import c6_diagnostic_engine_config
from quantfusion.engine.universe import BacktestEngine
from tests.unit.test_account_truth_boundary import (
    AS_OF, _decision, _frame, _run_with_market,
)


def test_public_engine_and_formal_defaults_enable_ab5():
    assert default_engine_config().get("account_risk_budget_enabled") is True
    assert BacktestEngine().cfg["account_risk_budget_enabled"] is True
    args = stress.build_argument_parser().parse_args(["--source-revision", "a" * 40])
    assert args.candidate_id == "C6-Base+AB5"


def test_historical_diagnostic_identity_explicitly_disables_budget():
    cfg = c6_diagnostic_engine_config({}, {"intervention_id": "C6_BASE"})
    assert cfg.get("account_risk_budget_enabled") is False
    assert c6_diagnostic_engine_config({}, {"intervention_id": "C6_BASE_AB5"})[
        "account_risk_budget_enabled"
    ] is True


@pytest.mark.parametrize("sellable,expected", [(8000, 1400), (500, 500), (0, 0)])
def test_account_default_budget_trims_actual_holdings_without_fictitious_fills(sellable, expected):
    snapshot = AccountSnapshot(3, "main", AS_OF, 10000., 100000., (
        AccountPosition("300308", 8000, sellable, 10., "2025-10-01"),
    ))
    result, _ = _run_with_market(snapshot=snapshot, symbols={"300308": "held"},
        decision=_decision("frozen_trend_engine"), frames={"300308": _frame(close=10.)})
    action = result["actions"][0]
    assert action["action"] == "REDUCE_REVIEW"
    assert action["recommended_shares"] == expected
    assert action["blocked_shares"] == 1400 - expected
    assert "account_budget_trim" in action["reason"]
    budget = result["account_risk_budget"]
    assert budget["enabled"] is True and budget["status"] == "APPLIED"
    assert budget["lifetime_peak"] == 100000.
    assert budget["planned_not_filled"] is True
    assert snapshot.cash == 10000. and snapshot.positions[0].shares == 8000


def test_account_budget_no_loss_room_blocks_buy_candidates():
    snapshot = AccountSnapshot(3, "main", AS_OF, 70000., 100000., ())
    result, _ = _run_with_market(snapshot=snapshot, symbols={"300308": "candidate"},
        decision=_decision("frozen_trend_engine"), frames={"300308": _frame()})
    assert result["buys_suppressed"] is True
    assert not any(a["action"] == "BUY_CANDIDATE" for a in result["actions"])
    assert "ACCOUNT_RISK_BUDGET" in result["buy_suppression_reasons"]


def test_account_new_high_is_observed_not_a_fabricated_history_reset():
    snapshot = AccountSnapshot(3, "main", AS_OF, 100000., 90000., ())
    result, _ = _run_with_market(snapshot=snapshot, symbols={"300308": "candidate"},
        decision=_decision("frozen_trend_engine"), frames={"300308": _frame()})
    budget = result["account_risk_budget"]
    assert budget["lifetime_peak"] == 100000.
    assert budget["input_peak_equity"] == 90000.
    assert snapshot.peak_equity == 90000.


def test_account_missing_valuation_never_silently_disables_budget():
    snapshot = AccountSnapshot(3, "main", AS_OF, 10000., 100000., (
        AccountPosition("300308", 8000, 8000, 10., "2025-10-01"),
    ))
    result, _ = _run_with_market(snapshot=snapshot, symbols={"300308": "held"},
        decision=_decision("frozen_trend_engine"), frames={"300308": ValueError("missing")})
    assert result["account_risk_budget"] == {
        "enabled": True, "mechanism": "AB5", "status": "NOT_READY",
        "reason": "incomplete account valuation or market evidence",
    }
    assert result["buys_suppressed"] is True


def test_shared_risk_plan_exists_for_account_and_replay_adapters():
    from quantfusion.risk import account_budget
    assert callable(getattr(account_budget, "plan_account_risk_budget", None))



@pytest.mark.parametrize("mode", ["replay", "auto", "trend", "weak", "single"])
def test_every_production_replay_entry_evaluates_budget_by_default(synthetic_market, mode):
    import contextlib
    import io
    from quantfusion.engine.replay import ProductionReplayEngine, RegimeAdaptiveBacktestEngine
    common = dict(data_dir=str(synthetic_market), indicator_state="warm")
    symbols = {"300308": "synthetic"}
    with contextlib.redirect_stdout(io.StringIO()):
        if mode in {"auto", "trend", "weak"}:
            result = RegimeAdaptiveBacktestEngine().run(symbols, "2026-01-05", "2026-01-09",
                **common, regime_data_dir=str(synthetic_market), deployment_mode=mode)
        elif mode == "replay":
            result = ProductionReplayEngine().run(symbols, "2026-01-05", "2026-01-09",
                **common, regime_data_dir=str(synthetic_market))
        elif mode == "single":
            result = BacktestEngine().run(symbols, "2026-01-05", "2026-01-09",
                **common, allocation_mode="single")
    status = result["account_risk_budget"]
    assert status["enabled"] is True and status["status"] == "APPLIED"
    assert status["latest"]["date"] == "2026-01-09"
    events = [r for r in result["risk_events"] if r.get("event") == "account_budget_envelope"]
    assert len(events) == 5, "one account budget per close, not once per funded sleeve"


def test_daily_cli_rejects_a_replay_that_did_not_evaluate_default_budget(tmp_path):
    from tests.integration.test_daily_schema_contracts import ArtifactStrictJSONTests
    helper = ArtifactStrictJSONTests()
    result = helper._make_mock_result()
    result.pop("account_risk_budget")
    assert helper._run_main_with_mock(str(tmp_path), result) == 1


def test_standalone_retained_risk_sell_cannot_be_bought_back_in_same_batch(monkeypatch):
    from quantfusion.engine.replay_loop import CoreReplayLoopMixin
    from tests.c6_non_economic.test_c6_retained_winner import _Sleeve, _signal, _Strategy
    import pandas as pd
    import quantfusion.engine.replay_loop as loop
    sleeve = _Sleeve('single', carry_overlay_sell=True)
    sleeve.cfg = {'account_risk_budget_enabled': True}
    sleeve.equity_curve = [{'assets': 100000.}]
    sleeve.risk_events = []
    sleeve._total_assets = lambda *_: 100000.
    sleeve._evaluate_trading_day = lambda *args: args[-1]
    # Isolate the open-batch contract; the real shared close plan is covered above.
    monkeypatch.setattr(loop, 'apply_account_risk_budget', lambda *_: None)
    execute = sleeve._execute_pending_signals

    def execute_all(pending, data, day, positions, directions=None):
        if directions is not None:
            return execute(pending, data, day, positions, directions)
        pending = execute(pending, data, day, positions, frozenset({'sell'}))
        return execute(pending, data, day, positions, frozenset({'buy'}))

    sleeve._execute_pending_signals = execute_all
    day = pd.Timestamp('2026-01-06')
    pending = [(_signal('sell'), None), (_signal('buy'), _Strategy('fast'))]
    remaining = CoreReplayLoopMixin._process_trading_day(
        sleeve, {}, {}, {}, [day], {day: 0}, day, pending)
    assert sleeve.executed_buys == []
    assert len(remaining) == 1 and remaining[0][0].direction == 'sell'
