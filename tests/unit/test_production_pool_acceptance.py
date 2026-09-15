"""Explicit owner amendments govern; original failures cannot disappear."""
from copy import deepcopy

import pytest

from quantfusion.application import production_pool
from tests.unit.test_native_joint_acceptance import fixture


def test_main_wealth_cannot_be_substituted_by_strong_subpools():
    _, original, incumbent, rows, _ = fixture()
    row = next(r for r in rows if r['scenario_id'] == 'prefix-17')
    baseline = next(r for r in incumbent['results'] if r['scenario_id'] == 'prefix-17')
    row['total_return'] = (1 + baseline['total_return']) * .989 - 1
    result = production_pool.assess(rows, original, incumbent)
    assert not result['promotion_gates']['checks']['production17_wealth_at_least_99pct']


def test_original_failure_is_reported_without_vetoing_authorized_tradeoff():
    _, original, incumbent, rows, _ = fixture()
    by_id = {r['scenario_id']: r for r in rows}
    base = next(r for r in incumbent['results'] if r['scenario_id'] == 'prefix-03')
    by_id['prefix-03']['total_return'] = (1 + base['total_return']) * .8 - 1
    before = deepcopy(rows)
    result = production_pool.assess(rows, original, incumbent)
    assert all(result[k]['passed'] for k in ('absolute_hard_gates', 'retained_robustness_hard_gates', 'initial_baseline_gates', 'promotion_gates'))
    assert not result['original_contract_assessment']['initial_baseline_gates']['passed']
    assert not result['original_contract_assessment']['retained_robustness_hard_gates']['passed']
    assert rows == before


@pytest.mark.parametrize('drawdown,passed', [(-.18, True), (-.180000001, False)])
def test_original_risk_boundary_is_retained(drawdown, passed):
    _, original, incumbent, rows, _ = fixture()
    next(row for row in rows if row['scenario_id'] == 'prefix-17')['max_drawdown'] = drawdown
    assert production_pool.assess(rows, original, incumbent)['absolute_hard_gates']['passed'] is passed


@pytest.mark.parametrize('family', ['prefix', 'leave_one_out', 'add_one', 'permutation'])
@pytest.mark.parametrize('level,ratio', [('minimum', .649), ('p10', .849), ('median', .999)])
def test_every_family_has_its_own_paired_wealth_bounds(family, level, ratio):
    _, original, incumbent, rows, _ = fixture()
    bases = {r['scenario_id']: r for r in incumbent['results']}
    selected = [r for r in rows if r['scenario_type'] == family]
    for row in (selected[:1] if level == 'minimum' else selected):
        row['total_return'] = (1 + bases[row['scenario_id']]['total_return']) * ratio - 1
    result = production_pool.assess(rows, original, incumbent)
    assert not result['promotion_gates']['checks'][f'{family}_paired_wealth_{level}']


@pytest.mark.parametrize('bad', ['missing', 'duplicate', 'symbols', 'nan', 'insolvent'])
def test_incomplete_or_invalid_pairing_fails_closed(bad):
    _, original, incumbent, rows, _ = fixture()
    if bad == 'missing':
        rows.pop()
    elif bad == 'duplicate':
        rows.append(rows[0])
    elif bad == 'symbols':
        rows[0]['symbols'] = ['invalid']
    elif bad == 'nan':
        rows[0]['total_return'] = float('nan')
    else:
        rows[0]['total_return'] = -1.
    with pytest.raises(ValueError):
        production_pool.assess(rows, original, incumbent)


@pytest.mark.parametrize('field', ['source_revision', 'total_return'])
def test_frozen_incumbent_cannot_be_replaced(field):
    _, original, incumbent, rows, _ = fixture()
    if field == 'source_revision':
        incumbent[field] = 'f' * 40
    else:
        incumbent['results'][0][field] += .01
    with pytest.raises(ValueError, match='frozen incumbent'):
        production_pool.assess(rows, original, incumbent)


def test_all_removed_protections_have_individual_diagnostics():
    _, original, incumbent, rows, _ = fixture()
    by_id = {row['scenario_id']: row for row in rows}
    by_id['prefix-10']['total_return'] = 6.
    result = production_pool.assess(rows, original, incumbent)
    report = result['original_contract_diagnostics']
    assert len(report['prefix_wealth']) == 17
    assert len(report['adjacent_wealth']) == 16
    bases = {row['scenario_id']: row for row in original['results']}
    for row in report['prefix_wealth']:
        sid = row['scenario_id']
        assert row['wealth_ratio'] == pytest.approx((1 + by_id[sid]['total_return']) / (1 + bases[sid]['total_return']))
        assert row['passed'] == (row['wealth_ratio'] >= row['minimum'] - 1e-12)
    cliff = next(row for row in report['adjacent_wealth'] if row['from'] == 'prefix-09')
    assert cliff['to'] == 'prefix-10'
    assert cliff['wealth_ratio'] == pytest.approx(7 / 15)
    assert cliff['checks'] == {'at_least_minus_30pct': False, 'above_minus_10pct': False}


def test_frozen_contract_cannot_drift_after_results(tmp_path, monkeypatch):
    from quantfusion.application import native_joint
    _, original, incumbent, rows, _ = fixture()
    path = tmp_path / 'contract.json'
    path.write_text('{}')
    monkeypatch.setattr(native_joint, 'PRIMARY_CONTRACT_PATH', path)
    with pytest.raises(ValueError, match='contract content changed'):
        production_pool.assess(rows, original, incumbent)


@pytest.mark.parametrize('count,passed', [(238, True), (239, True), (283, True), (400, True)])
def test_owner_turnover_revision_keeps_old_result_visible(count, passed):
    _, original, incumbent, rows, _ = fixture()
    row = next(r for r in rows if r['scenario_type'] == 'leave_one_out')
    row['date_symbol_side_count'] = count
    result = production_pool.assess(rows, original, incumbent)
    assert result['absolute_hard_gates']['passed'] is passed
    if passed:
        assert result['promotion_gates']['passed'] is True
    assert result['original_contract_assessment']['absolute_hard_gates']['passed'] is False


def test_only_approved_table_can_veto_economics():
    _, original, incumbent, rows, _ = fixture()
    bases = {r['scenario_id']: r for r in incumbent['results']}
    for row in rows:
        row['total_return'] = (1 + bases[row['scenario_id']]['total_return']) * 1.05 - 1
        row['date_symbol_side_count'] = 400
        row['max_drawdown'] = -.05
        row['reason_attribution']['risk_reduction'] = 1000
    main = next(row for row in rows if row['scenario_id'] == 'prefix-17')
    main['total_return'] = (1 + bases['prefix-17']['total_return']) * .99 - 1
    result = production_pool.assess(rows, original, incumbent)
    assert all(result[k]['passed'] for k in ('absolute_hard_gates', 'retained_robustness_hard_gates', 'initial_baseline_gates', 'promotion_gates'))
    assert not result['original_contract_assessment']['absolute_hard_gates']['passed']


@pytest.mark.parametrize('family', ['prefix', 'leave_one_out', 'add_one', 'permutation'])
@pytest.mark.parametrize('level,ratio', [('minimum', .65), ('p10', .85), ('median', 1.)])
def test_authorized_family_boundary_is_inclusive(family, level, ratio):
    _, original, incumbent, rows, _ = fixture()
    bases = {row['scenario_id']: row for row in incumbent['results']}
    selected = [row for row in rows if row['scenario_type'] == family]
    for row in (selected[:1] if level == 'minimum' else selected):
        row['total_return'] = (1 + bases[row['scenario_id']]['total_return']) * ratio - 1
    result = production_pool.assess(rows, original, incumbent)
    assert result['promotion_gates']['checks'][f'{family}_paired_wealth_{level}']


def test_random_outlier_is_disclosed_without_claiming_universal_acceptance():
    _, original, incumbent, rows, _ = fixture()
    row = next(r for r in rows if r['scenario_type'] == 'random_subset')
    base = next(r for r in incumbent['results'] if r['scenario_id'] == row['scenario_id'])
    row['total_return'] = (1 + base['total_return']) * .36 - 1
    row['max_drawdown'] = -.19
    before = deepcopy(rows)
    result = production_pool.assess(rows, original, incumbent)
    assert all(result[k]['passed'] for k in ('absolute_hard_gates', 'retained_robustness_hard_gates', 'initial_baseline_gates', 'promotion_gates'))
    diagnostic = result['original_contract_diagnostics']['outside_deployment_failures']
    assert diagnostic['wealth_below_65pct'][0]['scenario_id'] == row['scenario_id']
    assert diagnostic['drawdown_above_18pct'][0]['scenario_id'] == row['scenario_id']
    assert not result['original_contract_assessment']['absolute_hard_gates']['passed']
    assert rows == before


@pytest.mark.parametrize('dimension', ['maximum', 'p90', 'count_above_18pct'])
def test_random_risk_distribution_cannot_deteriorate(dimension):
    _, original, incumbent, rows, _ = fixture()
    selected = [r for r in rows if r['scenario_type'] == 'random_subset']
    if dimension == 'maximum':
        selected[0]['max_drawdown'] = -.22
    elif dimension == 'p90':
        for row in selected:
            row['max_drawdown'] = -.20
    else:
        for row in selected:
            row['max_drawdown'] = -.1801
    result = production_pool.assess(rows, original, incumbent)
    assert not result['promotion_gates']['checks'][f'random_drawdown_{dimension}_not_worse']


def test_main_requires_measured_improvement_not_only_tolerance():
    _, original, incumbent, rows, _ = fixture()
    main = next(r for r in rows if r['scenario_id'] == 'prefix-17')
    base = next(r for r in incumbent['results'] if r['scenario_id'] == 'prefix-17')
    main.update(total_return=base['total_return'], max_drawdown=base['max_drawdown'])
    result = production_pool.assess(rows, original, incumbent)
    assert not result['promotion_gates']['checks']['production17_has_economic_improvement']


def test_main_risk_cannot_worsen_below_absolute_limit():
    _, original, incumbent, rows, _ = fixture()
    main = next(r for r in rows if r['scenario_id'] == 'prefix-17')
    main['max_drawdown'] = -.179
    result = production_pool.assess(rows, original, incumbent)
    assert result['absolute_hard_gates']['passed']
    assert not result['promotion_gates']['checks']['production17_drawdown_not_worse']
