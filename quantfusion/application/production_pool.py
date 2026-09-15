"""Production-first acceptance; original prefix and adjacency results stay visible."""
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
    """Apply the owner-approved contract; retain every unchanged predicate."""
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
    del absolute['checks']['all_date_symbol_side_buckets_at_most_200']
    absolute['checks'][f'all_date_symbol_side_buckets_at_most_{native_joint.ALL_BUCKET_MAX}'] = (
        absolute['observed']['all_worst_date_symbol_side_buckets'] <= native_joint.ALL_BUCKET_MAX
    )
    # Preserve removed predicates in the independent original assessment.
    current['retained_robustness_hard_gates']['checks'] = {}
    initial = current['initial_baseline_gates']
    for key in ('prefix_05_wealth_at_least_99pct', 'other_prefix_wealth_at_least_95pct'):
        del initial['checks'][key]
    promotion = current['promotion_gates']
    del promotion['checks']['fixed_prefix_wealth_at_least_99pct']
    main_ratio = metrics._wealth_change(by_id['prefix-17'], reference['prefix-17']) + 1.
    promotion['checks']['production17_wealth_at_least_99pct'] = main_ratio >= .99 - 1e-12
    observed: dict[str, Any] = {}
    for family, values in ratios.items():
        summary = {'minimum': min(values), 'p10': metrics._quantile(values, .10),
                   'median': metrics._quantile(values, .50)}
        observed[family] = summary
        for label, floor in (('minimum', .70), ('p10', .90), ('median', 1.)):
            promotion['checks'][f'{family}_paired_wealth_{label}'] = summary[label] >= floor - 1e-12
    promotion['observed'].update(production17_wealth_ratio=main_ratio,
                                  paired_family_wealth=observed)
    promotion['tolerances'].pop('prefix_wealth_ratio')
    promotion['tolerances'].update(production17_wealth_ratio=.99,
                                   family_wealth_min=.70, family_wealth_p10=.90,
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
    }
    current['economic_contract'] = CONTRACT_ID
    return current
