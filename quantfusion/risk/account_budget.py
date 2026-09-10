"""AB1 close-known loss envelope; planning stress is not a guaranteed loss bound."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from quantfusion.domain.rules import require_finite, require_int


def account_budget_capacity(
    equity: float, peak: float, cfg: Mapping[str, Any], book_count: int,
) -> dict[str, float]:
    """Use the continuous account HWM, two stress sessions and exit-cost reserve."""
    equity = require_finite('account equity', equity, min_value=0.)
    peak = require_finite('account lifetime peak', peak, min_value=0.01)
    if equity > peak + 1e-8:
        raise ValueError('account lifetime peak must include current equity')
    book_count = require_int('book_count', book_count, min_value=0)
    daily = require_finite('daily_loss_limit', cfg['daily_loss_limit'],
                           min_value=0.000001, max_value=1., inclusive_max=False)
    costs = {key: require_finite(key, cfg[key], min_value=0.)
             for key in ('slippage', 'commission_rate', 'stamp_duty', 'min_commission')}
    maximum = require_finite('max_total_weight', cfg['max_total_weight'],
                              min_value=0., max_value=1.)
    floor = 0.82 * peak
    stress = 1. - (1. - daily)**2
    cost_rate = 2*costs['slippage'] + 2*costs['commission_rate'] + costs['stamp_duty']
    fixed = 2*book_count*costs['min_commission']
    budget = max(0., equity - floor - fixed)
    ordinary_cap = maximum * equity
    return {'equity': equity, 'lifetime_peak': peak, 'floor': floor,
            'stress_fraction': stress, 'cost_rate': cost_rate, 'fixed_cost_reserve': fixed,
            'remaining_loss_budget': budget, 'ordinary_gross_cap': ordinary_cap,
            'gross_cap': min(ordinary_cap, budget/(stress+cost_rate))}
