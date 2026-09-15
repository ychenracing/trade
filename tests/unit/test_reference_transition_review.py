"""A comparator review cannot activate a weaker or unqualified acceptance route."""
from copy import deepcopy
import importlib
import hashlib
import json
from pathlib import Path

import pytest

from quantfusion.application import native_joint

ROOT = Path(__file__).resolve().parents[2]


def tool():
    assert (ROOT / 'scripts/review_reference_transition.py').is_file(), 'reference impact reviewer is missing'
    return importlib.import_module('scripts.review_reference_transition')


def evidence():
    original = native_joint.load_original_reference()
    results = deepcopy([row for row in original['results'] if row['scenario_type'] == 'prefix'])
    return {'identity': {'source_revision': 'a' * 40, 'source_fingerprint': 'b' * 64, 'data_fingerprint': original['data_fingerprint'], 'cfg_overrides': {}, 'diagnostic_noncanonical': True, 'allow_publication': False, 'scenario_ids': [row['scenario_id'] for row in results]}, 'results': results}


def test_unchanged_comparison_is_only_a_diagnostic_not_qualification():
    e = evidence()
    before = deepcopy(e)
    result = tool().review(e)
    assert result['status'] == 'REFERENCE_QUALIFICATION_REQUIRED'
    assert result['activation_allowed'] is False and result['canonical'] is False
    assert result['modest_change_screen']['within_budget'] is True
    assert result['rows'][4]['old_reference_floor'] == pytest.approx(12.580154771921677)
    assert result['rows'][6]['old_joint_infimum'] == pytest.approx(8.6171130786335)
    assert result['rows'][9]['strict_lower_bound'] is True
    assert result['rows'][-1]['proposed_joint_infimum'] == pytest.approx(8.847967050773612)
    assert e == before


def test_wealth_not_profit_and_no_compounding_of_prior_adjustments():
    e = evidence()
    e['results'][2]['total_return'] = (1 + e['results'][2]['total_return']) * 0.96 - 1
    first = tool().review(e)
    assert first['rows'][2]['reference_floor_change'] == pytest.approx(-0.04)
    assert first['modest_change_screen']['within_budget'] is True
    e['results'][2]['total_return'] = (1 + e['results'][2]['total_return']) * 0.96 - 1
    second = tool().review(e)
    assert second['rows'][2]['reference_floor_change'] == pytest.approx(-0.0784)
    assert second['modest_change_screen']['within_budget'] is False
    assert second['status'] == 'MATERIAL_TARGET_CHANGE'


def test_incumbent_cannot_hide_material_loss_of_original_protection():
    e = evidence()
    e['results'][6]['total_return'] = (1 + e['results'][6]['total_return']) * 0.5 - 1
    result = tool().review(e)
    assert result['rows'][6]['joint_infimum_change'] == pytest.approx(0.0)
    assert result['modest_change_screen']['within_budget'] is False


def test_production17_protection_cannot_be_lowered():
    e = evidence()
    e['results'][-1]['total_return'] = 0.01
    row = tool().review(e)['rows'][-1]
    assert row['proposed_reference_floor'] >= row['old_reference_floor']
    assert row['proposed_own_floor'] >= row['old_own_floor']
    assert row['production_floor_retained'] is True


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), -1.0, -2.0, True])
def test_invalid_wealth_is_rejected(bad):
    e = evidence()
    e['results'][2]['total_return'] = bad
    with pytest.raises(ValueError, match='finite|wealth|number'):
        tool().review(e)


@pytest.mark.parametrize('field,bad', [('source_revision', 'abc'), ('source_fingerprint', 'abc'), ('data_fingerprint', 'c' * 64), ('cfg_overrides', {'max_drawdown': 0.25}), ('allow_publication', True)])
def test_incompatible_or_unbound_reference_is_rejected(field, bad):
    e = evidence()
    e['identity'][field] = bad
    with pytest.raises(ValueError):
        tool().review(e)


def test_missing_prefix_is_not_imputed_from_previous_candidate():
    e = evidence()
    e['results'].pop(3)
    with pytest.raises(ValueError, match='complete.*prefix|missing.*prefix'):
        tool().review(e)


def test_duplicate_or_wrong_membership_is_not_treated_as_a_complete_prefix():
    e = evidence()
    e['results'][3] = deepcopy(e['results'][2])
    with pytest.raises(ValueError):
        tool().review(e)
    e = evidence()
    e['results'][3]['symbols'].reverse()
    with pytest.raises(ValueError):
        tool().review(e)


def test_bigger_or_nonfinite_budget_is_rejected():
    for budget in (0.051, float('nan'), float('inf'), -0.01, True):
        with pytest.raises(ValueError):
            tool().review(evidence(), change_budget=budget)


def test_candidate_actual_predecessors_and_strict_nine_ten_are_used():
    reference, candidate = (evidence(), evidence())
    for row in candidate['results']:
        row['total_return'] = 19.0
        row['max_drawdown'] = -0.1
    candidate['results'][8]['total_return'] = 39.0
    # Below the strict boundary; no tolerance may rescue this observation.
    candidate['results'][9]['total_return'] = 35.0 - 1e-12
    result = tool().review(reference, candidate=candidate)
    assert result['candidate_diagnostic_checks']['retained_robustness']['passed'] is False
    assert result['candidate_diagnostic_checks']['formal_random_p90'] is None
    assert result['activation_allowed'] is False


def test_loader_checks_raw_hash_and_per_row_identity(tmp_path):
    d = tool()
    e = evidence()
    (tmp_path / 'identity.json').write_text(json.dumps(e['identity']))
    (tmp_path / 'results.json').write_text(json.dumps(e))
    for row in e['results']:
        raw = b'synthetic raw bytes for integrity tests, not executable pickle'
        (tmp_path / (row['scenario_id'] + '.pkl')).write_bytes(raw)
        (tmp_path / (row['scenario_id'] + '.json')).write_text(json.dumps({'result': row, 'source_fingerprint': 'b' * 64, 'raw_sha256': hashlib.sha256(raw).hexdigest()}))
    assert d.load_run(tmp_path)['identity'] == e['identity']
    p = tmp_path / 'prefix-03.json'
    payload = json.loads(p.read_text())
    payload['source_fingerprint'] = 'c' * 64
    p.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match='identity'):
        d.load_run(tmp_path)
    payload['source_fingerprint'] = 'b' * 64
    p.write_text(json.dumps(payload))
    (tmp_path / 'prefix-03.pkl').write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='hash'):
        d.load_run(tmp_path)


def test_review_result_is_rejected_by_native_reference_validator():
    original = native_joint.load_original_reference()
    incumbent = json.loads((ROOT / 'artifacts/validation/universe_stress.json').read_text())
    with pytest.raises(ValueError, match='exact original'):
        native_joint.validate_references(original, tool().review(evidence()), incumbent)


def test_symbol_count_and_signed_drawdown_must_match_metric_contract():
    for field, value in [('symbol_count', 17), ('max_drawdown', 0.1), ('max_drawdown', -1.01)]:
        e = evidence()
        e['results'][2][field] = value
        with pytest.raises(ValueError):
            tool().review(e)


@pytest.mark.parametrize('index,ratio', [(2, .95), (4, .99)])
def test_candidate_floor_tolerance_matches_native_ratio_not_absolute_wealth(index, ratio):
    reference, candidate = evidence(), evidence()
    wealth = 1 + reference['results'][index]['total_return']
    candidate['results'][index]['total_return'] = wealth * (ratio - .5e-12) - 1
    sid = candidate['results'][index]['scenario_id']
    checked = tool().review(reference, candidate=candidate)['candidate_diagnostic_checks']
    assert sid not in checked['wealth_floor_violations']['old_own_floors']
    assert sid not in checked['wealth_floor_violations']['proposed_own_floors']
    candidate['results'][index]['total_return'] = wealth * (ratio - 2e-12) - 1
    checked = tool().review(reference, candidate=candidate)['candidate_diagnostic_checks']
    assert sid in checked['wealth_floor_violations']['old_own_floors']
    assert sid in checked['wealth_floor_violations']['proposed_own_floors']
