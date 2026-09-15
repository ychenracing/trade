"""Explicit production-pool priority, with the original contract audited intact."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from quantfusion.application import stress_metrics as metrics

CONTRACT_ID = "production-pool-priority"


def assess(
    results: list[dict[str, Any]], original: dict[str, Any],
    incumbent: dict[str, Any],
) -> dict[str, Any]:
    """Change only the four owner-approved predicates; retain other gates."""
    old = {
        "absolute_hard_gates": metrics._absolute_hard_gates(results),
        "retained_robustness_hard_gates": metrics._retained_robustness_hard_gates(results),
        "initial_baseline_gates": metrics._initial_baseline_gates(results, original),
        "promotion_gates": metrics._promotion_gates(results, incumbent),
    }
    current = deepcopy(old)
    prefixes = {r['scenario_id']: r for r in results if r['scenario_type'] == 'prefix'}
    if set(prefixes) != {f'prefix-{i:02d}' for i in range(1, 18)}:
        raise ValueError('Production-pool comparison requires all17 exact prefixes')
    absolute = current['absolute_hard_gates']
    del absolute['checks']['all_scenarios_max_drawdown_at_most_18pct']
    absolute['checks']['all_scenarios_max_drawdown_at_most_20pct'] = (
        abs(absolute['observed']['all_worst_drawdown']) <= .20 + 1e-15
    )
    retained = current['retained_robustness_hard_gates']
    retained['checks'] = {'worst_adjacent_wealth_at_least_minus_40pct': (
        retained['observed']['worst_adjacent_wealth_change'] >= -.40 - 1e-12
    )}
    for family, reference, replaced in (
        ('initial_baseline_gates', original,
         ('prefix_05_wealth_at_least_99pct', 'other_prefix_wealth_at_least_95pct')),
        ('promotion_gates', incumbent, ('fixed_prefix_wealth_at_least_99pct',)),
    ):
        gate = current[family]
        by_id = {r['scenario_id']: r for r in reference['results']}
        ratios = {sid: metrics._wealth_change(row, by_id[sid]) + 1.
                  for sid, row in prefixes.items()}
        for key in replaced:
            del gate['checks'][key]
        main_ratio = ratios['prefix-17']
        other_ratio = min(r for sid, r in ratios.items() if sid != 'prefix-17')
        gate['checks'].update(
            production17_wealth_at_least_110pct=main_ratio >= 1.10 - 1e-12,
            other_prefix_wealth_at_least_60pct=other_ratio >= .60 - 1e-12,
        )
        gate['observed'].update(production17_wealth_ratio=main_ratio,
                                nonproduction_prefix_wealth_ratio_min=other_ratio)
        if family == 'promotion_gates':
            gate['tolerances'].pop('prefix_wealth_ratio')
            gate['tolerances'].update(production17_wealth_ratio=1.10,
                                      nonproduction_prefix_wealth_ratio=.60)
    for gate in current.values():
        gate['passed'] = all(gate['checks'].values())
    current['original_contract_assessment'] = old
    current['economic_contract'] = CONTRACT_ID
    return current
