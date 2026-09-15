"""Owner-approved economic tradeoffs must not bypass retained protections."""
from copy import deepcopy

import pytest

from quantfusion.application import production_pool
from tests.unit.test_native_joint_acceptance import fixture


def assess(rows, original, incumbent):
    return production_pool.assess(rows, original, incumbent)


def test_main_wealth_cannot_be_substituted_by_strong_subpools():
    _, original, incumbent, rows, _ = fixture()
    row = next(r for r in rows if r['scenario_id'] == 'prefix-17')
    baseline = next(r for r in incumbent['results'] if r['scenario_id'] == 'prefix-17')
    row['total_return'] = (1 + baseline['total_return']) * 1.099 - 1
    result = assess(rows, original, incumbent)
    assert not result['promotion_gates']['checks']['production17_wealth_at_least_110pct']


def test_uniform_subpool_boundary_and_original_audit_are_separate():
    _, original, incumbent, rows, _ = fixture()
    by_id = {r['scenario_id']: r for r in rows}
    base = next(r for r in original['results'] if r['scenario_id'] == 'prefix-03')
    by_id['prefix-03']['total_return'] = (1 + base['total_return']) * .60 - 1
    before = deepcopy(rows)
    result = assess(rows, original, incumbent)
    assert result['initial_baseline_gates']['checks']['other_prefix_wealth_at_least_60pct']
    assert not result['original_contract_assessment']['initial_baseline_gates']['passed']
    assert rows == before
    by_id['prefix-03']['total_return'] -= .00001
    assert not assess(rows, original, incumbent)['initial_baseline_gates']['passed']


@pytest.mark.parametrize('drawdown,passed', [(-.20, True), (-.200000001, False)])
def test_new_risk_boundary_does_not_change_old_risk_result(drawdown, passed):
    _, original, incumbent, rows, _ = fixture()
    rows[0]['max_drawdown'] = drawdown
    result = assess(rows, original, incumbent)
    assert result['absolute_hard_gates']['passed'] is passed
    assert not result['original_contract_assessment']['absolute_hard_gates']['passed']


def test_retained_bucket_and_worst_return_protections_still_fail():
    _, original, incumbent, rows, _ = fixture()
    for row in rows:
        if row['scenario_type'] == 'random_subset':
            row['date_symbol_side_count'] = 161
            row['total_return'] = -.9
    result = assess(rows, original, incumbent)
    assert not result['absolute_hard_gates']['passed']
    assert not result['promotion_gates']['checks']['random_worst_return_not_worse']


def test_missing_production_pool_is_not_a_partial_pass():
    _, original, incumbent, rows, _ = fixture()
    rows = [r for r in rows if r['scenario_id'] != 'prefix-17']
    with pytest.raises(ValueError):
        assess(rows, original, incumbent)
