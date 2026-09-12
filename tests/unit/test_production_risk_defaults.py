"""Production entry points must actually apply the selected account risk budget."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from inspect import signature

import pandas as pd
import pytest

from quantfusion.account.models import AccountPosition, AccountSnapshot, PointInTimeSignal
from quantfusion.application import account_scan, stress, stress_artifacts
from quantfusion.config.engine import default_engine_config
from quantfusion.config.profiles import config_for_symbol
from quantfusion.engine.replay import c6_diagnostic_engine_config
from quantfusion.engine.universe import BacktestEngine
from quantfusion.regime.models import DeploymentDecision, RegimeEvidence


def test_public_defaults_and_profiles_enable_budget():
    assert default_engine_config().get('account_risk_budget_enabled') is True
    assert BacktestEngine().cfg['account_risk_budget_enabled'] is True
    assert config_for_symbol('300308')['account_risk_budget_enabled'] is True


def test_explicit_historical_diagnostic_retains_disabled_identity():
    request = {'intervention_id': 'C6_BASE'}
    assert c6_diagnostic_engine_config({}, request)['account_risk_budget_enabled'] is False
    with pytest.raises(ValueError, match='own evidence identity'):
        c6_diagnostic_engine_config({'account_risk_budget_enabled': True}, request)


def test_stress_defaults_name_the_enabled_candidate():
    for fn in (stress._run_scenario, stress_artifacts._build_provenance,
               stress_artifacts._run_signature):
        assert signature(fn).parameters['candidate_id'].default == 'C6-Base+AB5'


def account_fixture(monkeypatch, *, sellable=8000, cash=10000., peak=100000.):
    as_of = '2026-01-30'
    dates = pd.bdate_range(end=as_of, periods=180)
    frame = pd.DataFrame({'open': 10., 'close': 10., 'high': 10., 'low': 10.,
                          'volume': 10000000.}, index=dates)
    snapshot = AccountSnapshot(3, 'main', as_of, cash, peak, (
        AccountPosition('300308', 8000, sellable, 10., str(dates[0].date())),))
    decision = DeploymentDecision('frozen_trend_engine', as_of, 'synthetic',
                                  RegimeEvidence(as_of, 'trend', ()), None)
    monkeypatch.setattr(account_scan.data_contracts, 'refresh_regime_indices', lambda *a, **k: {})
    monkeypatch.setattr(account_scan.RegimeAdaptiveBacktestEngine, 'decide_current', lambda *a, **k: decision)
    engine = account_scan.AccountSignalEngine(cache_dir='unused', regime_data_dir='unused')
    monkeypatch.setattr(engine, '_frame', lambda *a, **k: frame.copy())
    return engine, snapshot, {'300308': 'held', '300502': 'candidate'}


@pytest.mark.parametrize('sellable', [0, 40, 8000])
def test_account_budget_actually_reduces_holdings_and_respects_t1(monkeypatch, sellable):
    engine, snapshot, symbols = account_fixture(monkeypatch, sellable=sellable)
    before = deepcopy(snapshot)
    result = engine.run(snapshot, symbols, as_of=snapshot.snapshot_date)
    action = next(a for a in result['actions'] if a['symbol'] == '300308')
    assert action['action'] == 'REDUCE_REVIEW'
    assert 'account_budget_trim' in action['reason']
    assert action['risk_budget_required_shares'] > 0
    assert action['recommended_shares'] <= sellable
    assert action['risk_budget_required_shares'] == action['recommended_shares'] + action['blocked_shares']
    assert result['account_risk_budget']['enabled'] is True
    assert result['account_risk_budget']['status'] == 'APPLIED'
    assert result['account_risk_budget']['hwm_source'] == 'account_snapshot.peak_equity'
    assert snapshot == before


def test_account_budget_blocks_new_buy_without_crediting_planned_sells(monkeypatch):
    engine, snapshot, symbols = account_fixture(monkeypatch)
    candidate = PointInTimeSignal('300502', 'turtle_breakout', 'buy', 1., .60, 0, None, ('turtle_breakout',))
    monkeypatch.setattr(engine, '_evaluate_trend_candidates', lambda *a, **k: [candidate])
    result = engine.run(snapshot, symbols, as_of=snapshot.snapshot_date)
    buy = next(a for a in result['actions'] if a['symbol'] == '300502')
    assert buy['action'] == 'BLOCKED'
    assert buy['indicative_target_shares'] == 0
    assert 'account_budget' in buy['reason']
    assert result['buys_suppressed'] is True


def test_account_missing_valuation_cannot_claim_budget_was_applied(monkeypatch):
    engine, snapshot, symbols = account_fixture(monkeypatch)
    monkeypatch.setattr(engine, '_frame', lambda *a, **k: (_ for _ in ()).throw(ValueError('no quote')))
    result = engine.run(snapshot, symbols, as_of=snapshot.snapshot_date)
    assert result['account_risk_budget']['enabled'] is True
    assert result['account_risk_budget']['status'] == 'BLOCKED_DATA'
    assert result['buys_suppressed'] is True


def test_account_invalid_peak_is_not_silently_replaced(monkeypatch):
    engine, snapshot, symbols = account_fixture(monkeypatch)
    with pytest.raises(ValueError, match='peak'):
        engine.run(replace(snapshot, peak_equity=float('nan')), symbols,
                   as_of=snapshot.snapshot_date)


def test_standalone_close_uses_actual_account_budget(monkeypatch):
    from tests.c6_non_economic.test_account_risk_budget import fixture
    from quantfusion.engine.replay_loop import CoreReplayLoopMixin

    _, state, dates = fixture()
    sleeve = state.sleeve
    sleeve.equity_curve = [{'assets': 100000., 'cash': 10000., 'position_value': 90000., 'date': '2026-01-02'}]
    # The fixture owns real positions/cash; isolate only the upstream alpha close.
    monkeypatch.setattr(CoreReplayLoopMixin, '_process_trading_day', lambda *args: [])
    pending = sleeve._process_trading_day({'300308': 'held'}, state.data_map, {},
                                         state.all_dates, state.date_to_pos, dates[0], [])
    assert pending and all(s.direction == 'sell' for s, _ in pending)
    assert any(e['event'] == 'account_budget_envelope' for e in sleeve.risk_events)
    assert sleeve.cash == 10000.


def test_daily_rejects_flag_without_actual_budget_evaluations():
    from quantfusion.application import daily_support
    from quantfusion.risk.account_budget import account_budget_status

    valid_event = {'event': 'account_budget_envelope', 'date': '2026-01-30'}
    result = {'risk_events': [valid_event], 'account_risk_budget': account_budget_status(
        True, [valid_event], hwm_source='merged_account.lifetime_peak_assets')}
    assert daily_support.validate_production_risk_result(result) == []
    result['risk_events'] = []
    assert daily_support.validate_production_risk_result(result)


def test_shared_budget_preserves_native_scalar_arithmetic():
    """Input normalization must preserve the original producer's float sum."""
    import numpy as np
    from quantfusion.risk.account_budget import account_budget_plan

    cfg = default_engine_config()
    native_buys = [('300308', 1, price) for price in (.1, .2, .3)]
    wrapped_buys = [(code, shares, np.float64(price)) for code, shares, price in native_buys]
    native, _ = account_budget_plan(100., 100., cfg, 3, [], native_buys, sell_order=[])
    wrapped, _ = account_budget_plan(100., 100., cfg, 3, [], wrapped_buys, sell_order=[])
    assert type(wrapped['requested_buy_gap_debit']) is float
    assert wrapped == native
