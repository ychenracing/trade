"""Non-economic contracts for the frozen C6 diagnostic surface."""
from __future__ import annotations
# ruff: noqa: E501
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from quantfusion.application import c6_diagnostics
from quantfusion.engine.replay import ProductionReplayEngine
from quantfusion.engine.universe import BacktestEngine
from quantfusion.risk.overlay.policy import CrossMarketOverlay


def test_control_without_executed_assertion_cannot_inherit_suite_success(monkeypatch):
    monkeypatch.setattr(c6_diagnostics.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    control = "unmapped/contract"
    p = {"scenario_manifests": {"L1_BASE_SYNTHETIC_CONTROL_IDS": {"ids": [control], "assertions_by_control": {control: [{"id": control + "/contract", "expected": True, "comparator": "equal"}]}}}}
    rows = c6_diagnostics._controls(p, "L1_BASE_SYNTHETIC_CONTROL_IDS")
    assert rows[0]["passed"] is False
    assert rows[0]["assertions"][0]["actual"] is False


def test_control_receipt_rejects_entity_declarations(monkeypatch):
    def process(argv, **kwargs):
        report = Path(next(x.split("=", 1)[1] for x in argv if x.startswith("--junitxml=")))
        report.write_text('<!DOCTYPE testsuites [<!ENTITY x "unsafe">]><testsuites/>')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(c6_diagnostics.subprocess, "run", process)
    control = "book-identity/carried-winner"
    p = {"scenario_manifests": {"L1_BASE_SYNTHETIC_CONTROL_IDS": {"ids": [control], "assertions_by_control": {control: [{"id": control + "/contract", "expected": True, "comparator": "equal"}]}}}}
    with pytest.raises(ValueError, match="DTD or entity"):
        c6_diagnostics._controls(p, "L1_BASE_SYNTHETIC_CONTROL_IDS")

def _manifest(ids: list[str]) -> dict[str, object]:
    encoded = "".join(f"{item}\n" for item in ids).encode()
    return {
        "count": len(ids),
        "unique_count": len(ids),
        "sha256": __import__("hashlib").sha256(encoded).hexdigest(),
        "ids": ids,
    }
def test_manifest_identity_is_sorted_unique_and_fail_closed() -> None:
    manifest = _manifest(["a", "b"])
    assert c6_diagnostics.validate_manifest_identity(["a", "b"], manifest) == ["a", "b"]
    for ids, message in ((["b", "a"], "order"), (["a", "a"], "duplicate")):
        with pytest.raises(ValueError, match=message):
            c6_diagnostics.validate_manifest_identity(ids, manifest)
def test_official_breach_uses_the_official_running_peak() -> None:
    breach = c6_diagnostics.first_official_mdd_breach(
        [
            {"timestamp": "2026-01-01", "equity": 100.0},
            {"timestamp": "2026-01-02", "equity": 120.0},
            {"timestamp": "2026-01-03", "equity": 98.39},
        ]
    )
    assert breach is not None
    assert breach["sample_ordinal"] == 2
    assert breach["peak_owner"] == "official_running_peak"
    assert breach["drawdown"] == pytest.approx(98.39 / 120.0 - 1.0)
def test_cli_matches_the_exact_r_bound_shape() -> None:
    parser = c6_diagnostics.build_parser()
    args = parser.parse_args(
        [
            "--preregistration", "P.json", "--bindings-file", "R.json",
            "--binding-record-id", "c6.base.l1", "--source-revision", "a" * 40,
            "--output", "result.json",
        ]
    )
    assert args.binding_record_id == "c6.base.l1"
    assert args.producer_export is None and args.base_producer_export is None
    with pytest.raises(SystemExit):
        parser.parse_args(["--layer", "L1"])
def test_diagnostic_request_is_closed_and_production_signature_unchanged() -> None:
    production = inspect.signature(ProductionReplayEngine.run)
    assert {"intervention", "diagnostic_request", "trace_sink"}.isdisjoint(
        production.parameters
    )
    request = {
        "schema_version": 1, "intervention_id": "C6_BASE",
        "recording_mode": "DEFAULT", "scenario_id": "prefix-05",
        "diagnostic_noncanonical": True, "allow_publication": False,
    }
    assert ProductionReplayEngine.validate_c6_diagnostic_request(request) == request
    for key, value in (
        ("schema_version", 2), ("intervention_id", "TYPO"),
        ("recording_mode", "TRACE"), ("scenario_id", ""),
        ("diagnostic_noncanonical", False), ("allow_publication", True),
    ):
        with pytest.raises(ValueError, match=key):
            ProductionReplayEngine.validate_c6_diagnostic_request({**request, key: value})
    with pytest.raises(ValueError, match="extra"):
        ProductionReplayEngine.validate_c6_diagnostic_request({**request, "candidate": "x"})
    with pytest.raises(ValueError, match="no-drift"):
        ProductionReplayEngine.validate_c6_diagnostic_request({**request, "recording_mode": "ON", "scenario_id": "prefix-04"})
def test_ablation_map_is_exact_and_production_defaults_full_on() -> None:
    engine = object.__new__(BacktestEngine)
    assert all(engine._c6_feature_enabled(item) for item in ("F0", "F1", "U"))
    expected = {
        "BASELINE": set(), "F0_ONLY": {"F0"}, "F0_F1": {"F0", "F1"},
        "U_ONLY": {"U"}, "C6_BASE": {"F0", "F1", "U"},
        "W3_REAL_INTENTS_FIXED_REFERENCE_U": {"F0", "F1", "U"},
        "W4_FULL_BASE_PRODUCTION_POOL_RELATIVE": {"F0", "F1"},
    }
    for intervention, enabled in expected.items():
        engine._c6_diagnostic_request = {"intervention_id": intervention}
        assert {item for item in ("F0", "F1", "U") if engine._c6_feature_enabled(item)} == enabled
def test_s_counterpart_is_same_scenario_only() -> None:
    assert c6_diagnostics.base_counterpart_id("C6-Base+S::prefix-05") == "C6-Base::prefix-05"
    with pytest.raises(ValueError, match="prefix"):
        c6_diagnostics.base_counterpart_id("C6-Base::prefix-05")


def _declining_frame() -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=5)
    return pd.DataFrame(
        {
            "open": [12.0, 11.5, 11.0, 10.0, 9.8],
            "high": [12.0, 11.5, 11.0, 10.0, 9.8],
            "low": [12.0, 11.5, 11.0, 10.0, 9.8],
            "close": [12.0, 11.5, 11.0, 10.0, 9.8],
            "volume": [1_000_000] * 5,
        },
        index=index,
    )


def test_base_observes_complete_s_evidence_without_s_action() -> None:
    frame = _declining_frame()
    positions = {
        symbol: {
            "trend": SimpleNamespace(
                shares=9_000,
                entry_price=11.0,
                highest_close_since_entry=12.0,
                entry_date="2025-12-01",
            )
        }
        for symbol in ("601869", "002384")
    }
    sleeve = SimpleNamespace(
        sleeve_name="fast",
        positions=positions,
        cash=20_000.0,
        _remaining_adv_capacity=lambda *args: 100_000,
        _opening_limit_state=lambda *args: None,
    )
    risk_symbols = (
        "300308", "300502", "688008", "688072", "002409", "688256",
        "601869", "002384",
    )
    state = SimpleNamespace(
        sleeve=sleeve,
        data_map={symbol: frame for symbol in ("601869", "002384")},
        all_dates=list(frame.index),
        pending=[],
    )
    overlay = CrossMarketOverlay(
        risk_frames={symbol: frame for symbol in risk_symbols}
    )
    overlay._risk_level = 1
    overlay._assets_history = [220_000.0, 215_000.0, 210_000.0, 200_000.0]

    evidence = overlay.observe_c6_s_evidence(
        [state],
        pd.Timestamp("2026-01-04"),
        3,
        {"601869": 10.0, "002384": 10.0},
        200_000.0,
        0.05,
        lambda symbol: 0.0 if symbol == "601869" else 1.0,
    )

    definitions = json.loads(
        Path("artifacts/diagnostics/c6-preregistration.json").read_text()
    )["schema_catalog"]["definitions"]
    assert set(evidence) == set(definitions["s_evidence"]["exact_keys"])
    assert set(evidence["coverage"]) == set(
        definitions["qualification_coverage"]["exact_keys"]
    )
    assert set(evidence["leave_held_components_out"]) == set(
        definitions["qualification_leave_held"]["exact_keys"]
    )
    assert set(evidence["fillability"]) == set(
        definitions["qualification_fillability"]["exact_keys"]
    )
    assert set(evidence["shortfall"]) == set(
        definitions["qualification_shortfall"]["exact_keys"]
    )
    assert state.pending == []
    assert evidence["worst_cluster"] == "optical"
    assert evidence["stressed_cluster_set"] == sorted(
        evidence["stressed_cluster_set"]
    )
    assert evidence["coverage"]["coverage_passed"] is True
    assert evidence["leave_held_components_out"]["passed"] is True
    assert evidence["legacy_gate_open"] is False
    assert evidence["early_sell_required"] is True
    assert evidence["planned_shares"] == 2_000
    assert evidence["executable_lot_shares"] == 2_000
    assert evidence["scheduled_execution_batch"]["execution_open"] == "2026-01-05"

    overlay._c6_diagnostic_evidence_enabled = True
    overlay._risk_level_day = 3
    overlay._assets_history = [220_000.0, 215_000.0, 210_000.0]
    actions = overlay.evaluate(
        [state],
        pd.Timestamp("2026-01-04"),
        3,
        200_000.0,
        210_526.31578947368,
        lambda symbol: 0.0 if symbol == "601869" else 1.0,
    )
    assert actions == ()
    assert overlay.c6_s_evidence["early_sell_required"] is True
    assert state.pending == []


def test_s_evidence_is_finalized_against_the_official_breach_sample() -> None:
    evidence = c6_diagnostics._empty_s_evidence()
    evidence.update(
        {
            "first_causal_stressed_cluster_close": "2026-01-04",
            "first_early_sell_required_close": "2026-01-04",
            "scheduled_execution_batch": {
                "decision_close": "2026-01-04",
                "execution_open": "2026-01-05",
                "calendar_ordinal": 4,
            },
            "pre_trade_open_drawdown": -0.10,
        }
    )
    finalized = c6_diagnostics.finalize_s_evidence(
        evidence,
        [
            {"timestamp": "2026-01-01", "equity": 100.0},
            {"timestamp": "2026-01-04", "equity": 95.0},
            {"timestamp": "2026-01-05", "equity": 79.0},
        ],
        ["2026-01-01", "2026-01-04", "2026-01-05"],
    )
    assert finalized["lead_batch_count"] == 1
    assert finalized["official_sample_relation"] == "OPEN_MARK_GAP_NOT_OFFICIAL_SAMPLE"
    assert finalized["identical_valuation_instant_proven"] is False


def _warm_fixture():
    from quantfusion.config.portfolio import PortfolioPolicy
    from quantfusion.engine.universe import SleeveBacktestEngine
    from quantfusion.risk.managers import ConfirmedDrawdownRiskManager, RecoverableDrawdownRiskManager
    policy = PortfolioPolicy()
    dates = list(pd.to_datetime(['2026-01-05', '2026-01-06']))
    states = []
    for index, name in enumerate(('fast', 'base', 'slow')):
        cash = 2000000 / 3 if index < 2 else 2000000 - 2 * (2000000 / 3)
        sleeve = SleeveBacktestEngine(cash, cfg={}, policy=policy, allocation_lookbacks=(10,), sleeve_name=name)
        sleeve._reset_run_state({'a': 'A'})
        sleeve.risk = ConfirmedDrawdownRiskManager(sleeve.cfg, policy)
        frame = pd.DataFrame({'close': [1., 2., 999.]}, index=pd.to_datetime(['2026-01-01', '2026-01-02', '2026-01-05']))
        states.append(SimpleNamespace(sleeve=sleeve, pending=[], all_dates=dates, data_map={'a': frame}, indicator_map={'a': {'value': frame.close.copy()}}))
    return states, RecoverableDrawdownRiskManager({}, policy)


def test_warm_boundary_captures_raw_state_before_replay_without_aliases():
    from quantfusion.engine.ensemble_orchestration import capture_c6_warm_state
    states, risk = _warm_fixture()
    captured = capture_c6_warm_state(states, risk, None)
    states[0].sleeve.cash = 123
    states[0].sleeve.positions['contaminated'] = {}
    states[0].data_map['a'].iloc[0, 0] = -999
    risk.peak_assets = 5000000
    warm = c6_diagnostics._warm_snapshot({'_c6_warm_state': captured}, 2000000)
    assert warm['phase'] == 'before_first_valuation'
    assert warm['regime_and_transitions']['asof_timestamp'] is None
    assert warm['account_peaks']['cycle_peak_assets'] == 0
    assert warm['sleeve_peaks'][0]['lifetime_peak_assets'] is None
    assert warm['sleeve_cash'][0]['cash'] == 2000000 / 3
    assert warm['indicator_history'][0]['source_row_count'] == 2
    assert warm['indicator_history'][0]['history_end'] == '2026-01-02'
    assert warm['unauthorized_economic_state_empty'] is True


@pytest.mark.parametrize('contamination', ['cash', 'positions', 'pending', 'trades', 'lock', 'peak', 'sticky', 'safe_mode', 'external_risk'])
def test_warm_boundary_rejects_pre_window_economic_contamination(contamination):
    from quantfusion.engine.ensemble_orchestration import capture_c6_warm_state
    states, risk = _warm_fixture()
    sleeve = states[0].sleeve
    if contamination == 'cash':
        sleeve.cash = 123
    elif contamination == 'positions':
        sleeve.positions['a'] = {'strategy': object()}
    elif contamination == 'pending':
        states[0].pending.append(object())
    elif contamination == 'trades':
        sleeve.trades.append(object())
    elif contamination == 'lock':
        risk.persistent_lock = True
    elif contamination == 'peak':
        risk.peak_assets = 1
    elif contamination == 'sticky':
        sleeve._sticky_beat_days['a'] = 1
    elif contamination == 'safe_mode':
        sleeve._safe_mode_active = True
    elif contamination == 'external_risk':
        sleeve._external_risk_level = 2
    with pytest.raises(ValueError, match='warm boundary'):
        capture_c6_warm_state(states, risk, None)


def test_warm_boundary_missing_capture_cannot_reconstruct_end_state():
    states, _ = _warm_fixture()
    with pytest.raises(ValueError, match='warm boundary'):
        c6_diagnostics._warm_snapshot({'_c6_states': states}, 2000000)


def _synthetic_daily_signals(directory, *, ready, opinion):
    from tests.integration._daily_scan_support import FakeSignal
    from tests.integration.test_daily_artifact_transactions import LastGoodArtifactProtectionTests
    fixture = LastGoodArtifactProtectionTests()
    result = fixture._make_mock_result(
        deployment_decision={'name': 'cash_preservation'},
        warmup_health={'warmup_status': ready, 'reasons': []},
        risk_opinion=opinion,
        pending_signals=[FakeSignal('buy', 'turtle', '300308', 100, 150., 'breakout', '2026-07-30'),
                         FakeSignal('sell', 'turtle', '300502', 100, 150., 'stop_loss', '2026-07-30')],
    )
    assert fixture._run_main_with_mock(str(directory), result) == 0
    return json.loads((directory / 'signals_2026-07-30.json').read_text())


def test_readiness_not_ready_suppresses_buys_but_preserves_sells(tmp_path):
    ready = _synthetic_daily_signals(tmp_path / 'ready', ready='READY', opinion=None)
    blocked = _synthetic_daily_signals(tmp_path / 'blocked', ready='NOT_READY', opinion=None)
    assert ready['summary']['buy'] == 1
    assert blocked['summary']['buy'] == 0
    assert blocked['summary']['warmup_not_ready'] is True
    assert not ready['summary']['current_route_mismatch']
    assert [x for x in blocked['pending_signals'] if x['direction'] == 'sell'] == [x for x in ready['pending_signals'] if x['direction'] == 'sell']
    assert blocked['summary']['sell'] == 1


def test_governance_opinion_cannot_change_executable_signals(tmp_path):
    baseline = _synthetic_daily_signals(tmp_path / 'baseline', ready='READY', opinion=None)
    severe = _synthetic_daily_signals(tmp_path / 'severe', ready='READY', opinion={
        'risk_level': 3, 'block_new_entries': True, 'block_pyramids': True,
        'recommended_gross_cap': 0., 'bull_silent': False,
    })
    for key in ('pending_signals', 'blocked_signals', 'signals', 'summary'):
        assert severe[key] == baseline[key]
    assert baseline['summary']['buy'] == baseline['summary']['sell'] == 1


def test_healthy_bull_replay_and_s_noop_preserve_all_five_paths(tmp_path, record_property, monkeypatch):
    """Exercise real replay exclusively on generated, monotonically rising bars."""
    from dataclasses import asdict
    from quantfusion.config.portfolio import PortfolioPolicy
    from quantfusion.config.overlay import RISK_BASKET
    from quantfusion.config.regime import REGIME_INDEX_FILES
    dates = pd.bdate_range('2024-01-01', '2026-01-09')
    close = pd.Series([10. + i * .02 for i in range(len(dates))], index=dates)
    frame = pd.DataFrame({'open': close, 'high': close, 'low': close * .999,
                          'close': close, 'volume': 10000000.})
    frame.index.name = 'date'
    symbols = ['300308', '300502', '300394', '688256', '603986']
    for symbol in set(symbols) | set(PortfolioPolicy().regime_symbols) | set(RISK_BASKET) | set(REGIME_INDEX_FILES.values()):
        frame.to_csv(tmp_path / f'{symbol}.csv')
    paths = []
    effective = None
    opening_states = []
    replay_states = []
    original_open = BacktestEngine._execute_ensemble_open
    def capture_open(engine, states, date, *args, **kwargs):
        replay_states[:] = states
        opening_states.append({
            'date': str(date),
            'orders': [[asdict(signal) for signal, _ in state.pending] for state in states],
            'positions': [{symbol: {name: asdict(position) for name, position in books.items()}
                           for symbol, books in state.sleeve.positions.items()} for state in states],
        })
        return original_open(engine, states, date, *args, **kwargs)
    monkeypatch.setattr(BacktestEngine, '_execute_ensemble_open', capture_open)
    for intervention in ('BASELINE', 'C6_BASE', 'C6_BASE_PLUS_S', 'PRODUCTION'):
        opening_states = []
        engine = ProductionReplayEngine(2000000.)
        kwargs = {'data_dir': str(tmp_path), 'regime_data_dir': str(tmp_path)}
        runner = engine.run
        if intervention != 'PRODUCTION':
            runner = engine.run_c6_diagnostic
            kwargs['diagnostic_request'] = {'schema_version': 1, 'intervention_id': intervention,
                                           'recording_mode': 'DEFAULT', 'scenario_id': 'synthetic-healthy-bull',
                                           'diagnostic_noncanonical': True, 'allow_publication': False}
        result = runner({symbol: symbol for symbol in symbols}, '2026-01-05', '2026-01-09', **kwargs)
        states = replay_states

        # Direct engine ledgers, without the diagnostic serializer's reconstructions.
        path = {
            'orders': {'open_batches': [x['orders'] for x in opening_states], 'events': result['order_events'], 'pending': [[asdict(signal) for signal, _ in state.pending] for state in states]},
            'fills': [asdict(trade) for trade in result['trades']],
            'cash': [[(row['date'], row['cash']) for row in state.sleeve.equity_curve] for state in states],
            'positions': {'open_batches': [x['positions'] for x in opening_states], 'final': [{symbol: {name: asdict(position) for name, position in books.items()}
                           for symbol, books in s.sleeve.positions.items()} for s in states]},
            'equity': result['equity_curve'].to_json(date_format='iso') if hasattr(result['equity_curve'], 'to_json') else result['equity_curve'],
        }
        assert path['fills'], 'fixture must exercise actual execution'
        paths.append({key: __import__('hashlib').sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest() for key, value in path.items()})
        if intervention == 'C6_BASE_PLUS_S':
            effective = sum(event.get('event') == 'concentration_trim' for event in result['risk_events'])
        if intervention != 'PRODUCTION':
            assert c6_diagnostics._warm_snapshot(result, 2000000.)['phase'] == 'before_first_valuation'
    assert paths[0] == paths[1] == paths[2] == paths[3]
    assert effective == 0
    record_property('c6.assertion.s/no-op-control/fixture-identity', json.dumps('s/no-op-control/v1'))
    record_property('c6.assertion.s/no-op-control/s-effective-count', json.dumps(effective))
    record_property('c6.assertion.s/no-op-control/all-five-path-hashes-equal', json.dumps(paths[1] == paths[2]))


def _s_comparison_fixture():
    from copy import deepcopy
    def record(variant, scenario):
        return {'evaluation_id': f'{variant}::{scenario}', 'variant_id': variant, 'scenario_id': scenario,
                'orders': [{'execution_timestamp': '2026-01-06', 'shares': 100}],
                'fills': [{'timestamp': '2026-01-06', 'shares': 100}],
                'cash_series': [{'timestamp': '2026-01-05', 'cash': 1000}],
                'position_series': [{'timestamp': '2026-01-05', 'shares': 0}],
                'equity_series': [{'timestamp': '2026-01-05', 'equity': 1000}],
                'causal_matrix': {'s_evidence': {'first_early_sell_required_close': None}}}
    base = [record('C6-Base', 'a'), record('C6-Base', 'b')]
    selected = [record('C6-Base+S', 'a'), record('C6-Base+S', 'b')]
    return deepcopy(base), deepcopy(selected)


def test_s_common_prefix_uses_one_strict_boundary_and_full_noop_paths():
    base, selected = _s_comparison_fixture()
    selected[0]['causal_matrix']['s_evidence']['first_early_sell_required_close'] = '2026-01-06'
    selected[0]['orders'][0]['shares'] = 50
    selected[0]['fills'][0]['shares'] = 50
    common, no_effect = c6_diagnostics.compare_s_paths(base, selected, ['a', 'b'])
    assert len(common) == 2 and all(row['equal'] for row in common)
    assert common[0]['first_s_effective_timestamp'] == '2026-01-06'
    assert len(no_effect) == 1 and no_effect[0]['item_id'] == 'C6-Base+S::b'
    assert no_effect[0]['equal']
    selected[0]['cash_series'][0]['cash'] = 999
    selected[1]['orders'][0]['shares'] = 1
    common, no_effect = c6_diagnostics.compare_s_paths(base, selected, ['a', 'b'])
    assert not common[0]['equal'] and not common[1]['equal']
    assert not no_effect[0]['equal']


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'extra'])
def test_s_comparison_coverage_rejects_missing_duplicate_extra(mutation):
    base, selected = _s_comparison_fixture()
    for target in (base, selected):
        changed = list(target)
        if mutation == 'missing':
            changed.pop()
        elif mutation == 'duplicate':
            changed[-1] = changed[0]
        else:
            changed.append({**changed[0], 'scenario_id': 'unknown', 'evaluation_id': changed[0]['variant_id'] + '::unknown'})
        with pytest.raises(ValueError, match='exact ordered'):
            c6_diagnostics.compare_s_paths(changed if target is base else base, changed if target is selected else selected, ['a', 'b'])


@pytest.mark.parametrize('observed', [None, False, 1])
def test_s_noop_receipt_requires_observed_typed_values(monkeypatch, observed):
    import xml.etree.ElementTree as ET
    control = 's/no-op-control'
    assertion = control + '/s-effective-count'
    monkeypatch.setattr(c6_diagnostics, '_control_nodes', lambda: {control: ['tests/fake.py::test_control']})
    def process(argv, **kwargs):
        report = Path(next(x.split('=', 1)[1] for x in argv if x.startswith('--junitxml=')))
        root = ET.Element('testsuite')
        case = ET.SubElement(root, 'testcase', classname='tests.fake', name='test_control')
        if observed is not None:
            props = ET.SubElement(case, 'properties')
            ET.SubElement(props, 'property', name='c6.assertion.' + assertion, value=json.dumps(observed))
        report.write_bytes(ET.tostring(root))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(c6_diagnostics.subprocess, 'run', process)
    p = {'scenario_manifests': {'S': {'ids': [control], 'assertions_by_control': {control: [{'id': assertion, 'comparator': 'equal', 'expected': 0}]}}}}
    rows = c6_diagnostics._controls(p, 'S')
    assert not rows[0]['passed']
    assert rows[0]['assertions'][0]['actual'] is observed


def test_account_timeline_cannot_take_an_earlier_sleeve_alert():
    events = [
        {'date': '2026-01-01', 'sleeve': 'fast', 'event': 'portfolio_drawdown_alert_on', 'drawdown': .1},
        {'date': '2026-01-02', 'sleeve': 'portfolio', 'event': 'portfolio_drawdown_alert_on', 'drawdown': .15,
         'peak_owner': 'manager_cycle_peak', 'peak_assets': 2000000., 'current_assets': 1700000., 'threshold': .15},
    ]
    actual = c6_diagnostics._manager_event(events, 'portfolio_drawdown_alert_on')
    assert actual['timestamp'] == '2026-01-02'
    assert actual['current_assets'] == 1700000.
    assert c6_diagnostics._manager_event(events[:1], 'portfolio_drawdown_alert_on')['timestamp'] is None


def test_real_emergency_and_terminal_event_names_and_peak_owners_are_retained():
    from quantfusion.config.portfolio import PortfolioPolicy
    from quantfusion.risk.managers import RecoverableDrawdownRiskManager
    policy = PortfolioPolicy()
    for assets, name, owner in [
        (2000000. * (1 - policy.emergency_drawdown - .001), 'emergency_cycle_drawdown_lock', 'manager_cycle_peak'),
        (2000000. * (1 - policy.terminal_drawdown - .001), 'terminal_portfolio_drawdown_lock', 'manager_lifetime_peak'),
    ]:
        manager = RecoverableDrawdownRiskManager({}, policy)
        manager.check_portfolio_risk(2000000., '2026-01-01')
        manager.check_portfolio_risk(assets, '2026-01-02')
        events = [{**event, 'sleeve': 'portfolio'} for event in manager.drain_audit_events()]
        actual = c6_diagnostics._manager_event(events, name)
        assert actual['timestamp'] == '2026-01-02'
        assert actual['peak_owner'] == owner
        assert actual['status_source'] == name


def test_official_breach_retains_the_first_equal_peak_timestamp():
    breach = c6_diagnostics.first_official_mdd_breach([
        {'timestamp': '2026-01-01', 'equity': 100.},
        {'timestamp': '2026-01-02', 'equity': 100.},
        {'timestamp': '2026-01-03', 'equity': 80.},
    ])
    assert breach['peak_timestamp'] == '2026-01-01'


def test_account_event_capture_preserves_actual_cycle_and_lifetime_peak_owners():
    from quantfusion.config.portfolio import PortfolioPolicy
    from quantfusion.risk.managers import RecoverableDrawdownRiskManager
    from quantfusion.engine.ensemble_orchestration import capture_account_risk_events
    manager = RecoverableDrawdownRiskManager({}, PortfolioPolicy())
    peaks = {}
    manager.check_portfolio_risk(2000000., '2026-01-01')
    assert capture_account_risk_events(manager, 2000000., '2026-01-01', peaks) == []
    manager.check_portfolio_risk(2000000., '2026-01-02')
    capture_account_risk_events(manager, 2000000., '2026-01-02', peaks)
    assets = 2000000. * (1 - manager.policy.terminal_drawdown - .001)
    manager.check_portfolio_risk(assets, '2026-01-03')
    events = capture_account_risk_events(manager, assets, '2026-01-03', peaks)
    terminal = next(x for x in events if x['event'] == 'terminal_portfolio_drawdown_lock')
    assert terminal['peak_owner'] == 'manager_lifetime_peak'
    assert terminal['peak_assets'] == 2000000.
    assert terminal['peak_timestamp'] == '2026-01-01'
    assert terminal['current_assets'] == assets
    assert terminal['threshold'] == manager.policy.terminal_drawdown


def test_cluster_weight_uses_marked_value_over_account_assets():
    positions = [
        {'timestamp': '2026-01-01', 'symbol': 'a', 'market_value': 900.},
        {'timestamp': '2026-01-01', 'symbol': 'b', 'market_value': 100.},
        {'timestamp': '2026-01-02', 'symbol': 'a', 'market_value': 100.},
        {'timestamp': '2026-01-02', 'symbol': 'b', 'market_value': 300.},
    ]
    assets = {'2026-01-01': 2000., '2026-01-02': 1000.}
    assert c6_diagnostics.maximum_cluster_weight(positions, assets, {'a': 'optical', 'b': 'equipment'}) == .45
    assert c6_diagnostics.maximum_cluster_weight([], assets, {}) == 0.
