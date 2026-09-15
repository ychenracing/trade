"""Native promotion must preserve original and incumbent economics together."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from quantfusion.application import production_pool, stress, stress_artifacts as a, stress_metrics as m, stress_scenarios as s
from tests.unit import test_stress_scenarios as helpers

ROOT = Path(__file__).resolve().parents[2]
ORIGINAL = ROOT / 'artifacts/validation/candidates/stress-86fd22448b9aad9d5e6194c0c065c40d56d7bddd-rejected.json'


def fixture():
    plan = s._multi_seed_scenarios(random_samples=50, permutation_samples=50, seeds=s.DEFAULT_SEEDS)
    original = json.loads(ORIGINAL.read_text())
    incumbent = json.loads((ROOT / 'artifacts/diagnostics/no_waiver/production-primary/incumbent-reference.json').read_text())
    rows = [dict(helpers.StressScenarioTests._complete_result(item), total_return=14., max_drawdown=-.05) for item in plan]
    provenance = dict(helpers.StressScenarioTests._provenance('a' * 40, len(plan)), candidate_id='native-default',
                      data_fingerprint=original['data_fingerprint'],
                      scenario_signature=s._scenario_signature(plan))
    return plan, original, incumbent, rows, provenance


def artifacts(rows, provenance, original, incumbent):
    common = dict(provenance, seeds=list(s.DEFAULT_SEEDS), trade_count_semantics=m.TRADE_COUNT_SEMANTICS)
    universe = dict(common, results=rows,
                    absolute_hard_gates=m._absolute_hard_gates(rows),
                    retained_robustness_hard_gates=m._retained_robustness_hard_gates(rows),
                    robustness_diagnostics=m._robustness_diagnostics(rows),
                    initial_baseline_gates=m._initial_baseline_gates(rows, original),
                    promotion_gates=m._promotion_gates(rows, incumbent))
    universe.update(production_pool.assess(rows, original, incumbent))
    prefix = dict(common, results=[row for row in rows if row['scenario_type'] == 'prefix'])
    return prefix, universe


def publish(tmp_path, setup, **kwargs):
    plan, original, incumbent, rows, provenance = setup
    prefix, universe = artifacts(rows, provenance, original, incumbent)
    with patch.object(a, 'VALIDATION_ARTIFACT_DIR', tmp_path):
        accepted = a._publish_formal_artifacts(prefix, universe, scenarios=plan,
            provenance=provenance, incumbent=incumbent, formal_plan_complete=True,
            initial_baseline_reference=original, **kwargs)
    return accepted, universe


def test_native_success_requires_both_references_and_records_their_distinct_identities(tmp_path):
    setup = fixture()
    accepted, _ = publish(tmp_path, setup)
    assert accepted
    saved = json.loads((tmp_path / 'universe_stress.json').read_text())
    assert saved['initial_baseline_gates']['passed']
    assert saved['promotion_gates']['passed']
    assert 'release_acceptance' not in saved
    receipt = saved['native_joint_acceptance']
    assert receipt['waivers_used'] is False
    assert receipt['original_reference_payload_sha256'] != receipt['incumbent_payload_sha256']


@pytest.mark.parametrize('failure', ['drawdown', 'paired_wealth', 'production_wealth'])
def test_any_native_failure_is_rejected_even_when_legacy_assessor_would_accept(tmp_path, failure):
    setup = fixture()
    _, original, incumbent, rows, _ = setup
    by_id = {r['scenario_id']: r for r in rows}
    if failure == 'drawdown':
        by_id['prefix-01']['max_drawdown'] = -.18000001
    else:
        sid, ratio = ('prefix-01', .649) if failure == 'paired_wealth' else ('prefix-17', .989)
        base = next(r for r in incumbent['results'] if r['scenario_id'] == sid)
        by_id[sid]['total_return'] = (1 + base['total_return']) * ratio - 1
    with patch('quantfusion.application.c6_release_acceptance.release_formal_assessment', return_value={'passed': True}) as legacy:
        accepted, _ = publish(tmp_path, setup)
    assert accepted is False
    legacy.assert_not_called()
    assert not (tmp_path / 'universe_stress.json').exists()
    rejected = json.loads(next((tmp_path / 'candidates').glob('*rejected.json')).read_text())
    assert rejected['canonical'] is False and rejected['rejection_reasons']


def test_native_never_accepts_the_legacy_release_flag(tmp_path):
    with pytest.raises(ValueError, match='[Nn]ative.*[Ww]aiver|[Nn]ative.*release'):
        publish(tmp_path, fixture(), ab5_release_acceptance=True)


def test_native_cannot_substitute_the_shrunken_incumbent_as_original(tmp_path):
    setup = list(fixture())
    setup[1] = deepcopy(setup[2])
    with pytest.raises(ValueError, match='[Oo]riginal.*reference|[Nn]ative.*reference'):
        publish(tmp_path, setup)


def test_native_refuses_legacy_receipt_attached_to_new_candidate(tmp_path):
    plan, original, incumbent, rows, provenance = fixture()
    prefix, universe = artifacts(rows, provenance, original, incumbent)
    universe['release_acceptance'] = {'assessment': {'passed': True}}
    with patch.object(a, 'VALIDATION_ARTIFACT_DIR', tmp_path), pytest.raises(ValueError, match='[Nn]ative.*release|[Nn]ative.*waiver'):
        a._publish_formal_artifacts(prefix, universe, scenarios=plan, provenance=provenance,
            incumbent=incumbent, formal_plan_complete=True, initial_baseline_reference=original)


def test_default_formal_identity_is_native_not_a_historical_waiver_candidate():
    args = stress.build_argument_parser().parse_args(['--source-revision', 'a' * 40])
    assert args.candidate_id == 'native-default'


@pytest.mark.parametrize('identity', ['C6-Base', 'C6-Base+S', 'C6-Base+AB5', 'C6-Base+AB5+S'])
def test_historical_labels_cannot_restore_a_supported_formal_publication_route(monkeypatch, identity):
    monkeypatch.setattr('sys.argv', ['stress', '--source-revision', 'a' * 40,
                                    '--candidate-id', identity])
    def forbid_replay_preparation(*args, **kwargs):
        pytest.fail('historical full run reached replay preparation')
    monkeypatch.setattr(a, '_build_provenance', forbid_replay_preparation)
    with pytest.raises(ValueError, match='Historical.*diagnostic'):
        stress.main()


def test_publication_rejects_removed_gate_diagnostic_tampering(tmp_path):
    plan, original, incumbent, rows, provenance = fixture()
    prefix, universe = artifacts(rows, provenance, original, incumbent)
    universe['original_contract_diagnostics']['prefix_wealth'].pop()
    with patch.object(a, 'VALIDATION_ARTIFACT_DIR', tmp_path), pytest.raises(ValueError, match='original_contract_diagnostics'):
        a._publish_formal_artifacts(prefix, universe, scenarios=plan, provenance=provenance,
            incumbent=incumbent, formal_plan_complete=True, initial_baseline_reference=original)


def test_reference_loader_survives_canonical_replacement(tmp_path):
    from quantfusion.application import native_joint
    _, original, incumbent, _, provenance = fixture()
    (tmp_path / 'universe_stress.json').write_text('{}')
    with patch.object(a, 'VALIDATION_ARTIFACT_DIR', tmp_path):
        assert native_joint.load_incumbent_reference() == incumbent
    incumbent['source_revision'] = 'f' * 40
    with pytest.raises(ValueError, match='frozen incumbent'):
        native_joint.validate_references(provenance, original, incumbent)


@pytest.mark.parametrize('damage', ['missing_receipt', 'returns', 'source', 'diagnostics'])
def test_native_reload_recomputes_acceptance_instead_of_trusting_saved_pass(tmp_path, damage):
    accepted, _ = publish(tmp_path, fixture())
    assert accepted
    path = tmp_path / 'universe_stress.json'
    assert a._load_incumbent(path) is not None
    saved = json.loads(path.read_text())
    if damage == 'missing_receipt':
        del saved['native_joint_acceptance']
    elif damage == 'returns':
        saved['results'][0]['total_return'] = -.99
    elif damage == 'source':
        saved['source_fingerprint'] = 'f' * 64
    else:
        saved['original_contract_diagnostics']['adjacent_wealth'].pop()
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError):
        a._load_incumbent(path)


def test_frozen_loader_rejects_native_replacement_without_recursive_loading(tmp_path, monkeypatch):
    from quantfusion.application import native_joint
    accepted, _ = publish(tmp_path, fixture())
    assert accepted
    monkeypatch.setattr(native_joint, 'INCUMBENT_REFERENCE_PATH', tmp_path / 'universe_stress.json')
    with pytest.raises(ValueError, match='frozen incumbent'):
        native_joint.load_incumbent_reference()
