"""Deployment-scoped acceptance with complete stress and historical diagnostics."""
from __future__ import annotations

from copy import deepcopy
import math
from typing import Any

from quantfusion.application import native_joint, stress_metrics as metrics

CONTRACT_ID = "production-primary"
FAMILIES = ('prefix', 'leave_one_out', 'add_one', 'random_subset', 'permutation')


def assess(
    results: list[dict[str, Any]], original: dict[str, Any],
    incumbent: dict[str, Any],
) -> dict[str, Any]:
    """Assess the deployed pool and stability without hiding arbitrary-pool failures."""
    native_joint.validate_references(original, original, incumbent)
    reference = metrics._current_incumbent_by_id(incumbent)
    by_id = {row['scenario_id']: row for row in results}
    if len(by_id) != len(results) or set(by_id) != set(reference):
        raise ValueError('Production-primary requires every exact incumbent scenario once')
    ratios: dict[str, list[float]] = {family: [] for family in FAMILIES}
    for sid, row in by_id.items():
        base = reference[sid]
        family = row['scenario_type']
        if (family not in ratios or family != base['scenario_type']
                or row.get('symbols') != base.get('symbols')):
            raise ValueError('Production-primary scenario family or ordered symbols changed')
        wealth, base_wealth = 1. + float(row['total_return']), 1. + float(base['total_return'])
        if not all(math.isfinite(v) and v > 0. for v in (wealth, base_wealth)):
            raise ValueError('Production-primary requires finite positive paired wealth')
        ratios[family].append(wealth / base_wealth)
    if any(not values for values in ratios.values()) or 'prefix-17' not in by_id:
        raise ValueError('Production-primary requires all families and the production pool')
    old = {
        'absolute_hard_gates': metrics._absolute_hard_gates(results),
        'retained_robustness_hard_gates': metrics._retained_robustness_hard_gates(results),
        'initial_baseline_gates': metrics._initial_baseline_gates(results, original),
        'promotion_gates': metrics._promotion_gates(results, incumbent),
    }
    current: dict[str, Any] = deepcopy(old)
    absolute = current['absolute_hard_gates']
    scoped = [row for row in results if row['scenario_type'] != 'random_subset']
    scoped_worst = max(abs(float(row['max_drawdown'])) for row in scoped)
    absolute['checks'] = {
        'deployment_and_structured_sensitivity_drawdown_at_most_18pct':
            scoped_worst <= .18 + metrics.DRAWDOWN_COMPARISON_TOLERANCE,
    }
    absolute['observed'].update(scoped_worst_drawdown=scoped_worst,
                                excluded_family='random_subset')
    # The sole approved table replaces old economics; raw predicates stay in old.
    current['retained_robustness_hard_gates']['checks'] = {}
    current['initial_baseline_gates']['checks'] = {}
    promotion = current['promotion_gates']
    promotion['checks'] = {'permutation_invariant': promotion['checks']['permutation_invariant']}
    main_ratio = metrics._wealth_change(by_id['prefix-17'], reference['prefix-17']) + 1.
    main_mdd = abs(float(by_id['prefix-17']['max_drawdown']))
    base_mdd = abs(float(reference['prefix-17']['max_drawdown']))
    promotion['checks']['production17_wealth_at_least_99pct'] = main_ratio >= .99 - 1e-12
    promotion['checks']['production17_drawdown_not_worse'] = main_mdd <= base_mdd + 1e-15
    promotion['checks']['production17_has_economic_improvement'] = (
        main_ratio > 1. + 1e-12 or main_mdd < base_mdd - 1e-15
    )
    observed: dict[str, Any] = {}
    previous_checks: dict[str, bool] = {}
    for family, values in ratios.items():
        summary = {'minimum': min(values), 'p10': metrics._quantile(values, .10),
                   'median': metrics._quantile(values, .50)}
        observed[family] = summary
        for label, floor in (('minimum', .65), ('p10', .85), ('median', .95)):
            previous_checks[f'{family}_paired_wealth_{label}'] = summary[label] >= floor - 1e-12
        floors = [('p10', .85), ('median', 1.)]
        if family != 'random_subset':
            floors.append(('minimum', .65))
        for label, floor in floors:
            promotion['checks'][f'{family}_paired_wealth_{label}'] = summary[label] >= floor - 1e-12
    risk_comparison = {}
    for name, source in (('candidate', by_id), ('incumbent', reference)):
        severities = [abs(float(row['max_drawdown'])) for row in source.values()
                      if row['scenario_type'] == 'random_subset']
        risk_comparison[name] = {
            'maximum': max(severities), 'p90': metrics._quantile(severities, .9),
            'count_above_18pct': sum(value > .18 + 1e-15 for value in severities),
        }
    for label in ('maximum', 'p90', 'count_above_18pct'):
        promotion['checks'][f'random_drawdown_{label}_not_worse'] = (
            risk_comparison['candidate'][label] <= risk_comparison['incumbent'][label] + 1e-15
        )
    promotion['observed'].update(production17_wealth_ratio=main_ratio,
                                  paired_family_wealth=observed,
                                  random_drawdown_comparison=risk_comparison,
                                  deployment_symbols=reference['prefix-17']['symbols'])
    promotion['tolerances'].pop('prefix_wealth_ratio')
    promotion['tolerances'].update(production17_wealth_ratio=.99,
                                   structured_family_wealth_min=.65, family_wealth_p10=.85,
                                   family_wealth_median=1.)
    for gate in current.values():
        gate['passed'] = all(gate['checks'].values())
    current['original_contract_assessment'] = old
    originals = metrics._transition_reference_by_id(original)
    prefixes = [by_id[f'prefix-{number:02}'] for number in range(1, 18)]
    prefix_report = []
    for row in prefixes:
        sid = row['scenario_id']
        ratio = metrics._wealth_change(row, originals[sid]) + 1.
        floor = .99 if sid == 'prefix-05' else .95
        prefix_report.append({'scenario_id': sid, 'wealth_ratio': ratio,
                              'minimum': floor, 'passed': ratio >= floor - 1e-12})
    adjacent_report = []
    for left, right in zip(prefixes, prefixes[1:]):
        change = metrics._wealth_change(right, left)
        checks = {'at_least_minus_30pct': change >= -.30 - 1e-12}
        if left['scenario_id'] == 'prefix-09':
            checks['above_minus_10pct'] = change > -.10
        adjacent_report.append({'from': left['scenario_id'], 'to': right['scenario_id'],
                                'wealth_ratio': change + 1., 'wealth_change': change,
                                'checks': checks, 'passed': all(checks.values())})
    current['original_contract_diagnostics'] = {
        'prefix_wealth': prefix_report, 'adjacent_wealth': adjacent_report,
        'previous_production_contract_checks': {
            **previous_checks,
            'all_scenarios_max_drawdown_at_most_18pct':
                old['absolute_hard_gates']['checks']['all_scenarios_max_drawdown_at_most_18pct'],
        },
        'outside_deployment_failures': {
            'wealth_below_65pct': [
                {'scenario_id': sid, 'wealth_ratio': metrics._wealth_change(row, reference[sid]) + 1.}
                for sid, row in by_id.items() if row['scenario_type'] == 'random_subset'
                and metrics._wealth_change(row, reference[sid]) + 1. < .65 - 1e-12
            ],
            'drawdown_above_18pct': [
                {'scenario_id': sid, 'max_drawdown': row['max_drawdown']}
                for sid, row in by_id.items() if row['scenario_type'] == 'random_subset'
                and abs(float(row['max_drawdown'])) > .18 + 1e-15
            ],
        },
    }
    current['economic_contract'] = CONTRACT_ID
    return current
