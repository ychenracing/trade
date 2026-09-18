# Budget + overshoot gate (Direction 1)

**STATUS:** `DIRECTIONAL_PROGRESS`  
**As of:** 2026-09-18T13:53+0800 (Asia/Shanghai)  
**Branch:** `agent/absorb-archive0805-ordinary-ab5` @ `6204a9efeb0d` (tree `ffc72b6807eb`)  
**Main:** `fa4ef7ba` · **Archive:** `c158435`  
**Do NOT merge main.**

## Exact predicate (plain language)

Defer ordinary held trims **only when all three** hold:

1. **Immaterial overshoot:** `gross - gross_cap ≤ daily_loss_limit × equity`
2. **Budget comfortable:** `remaining_loss_budget ≥ 0.85 × equity × stress_fraction`
   (at least 85% of the ordinary two-session stress envelope on equity)
3. **Not in near-peak razor band:** `equity / peak ≤ 0.925`
   (exclude the thin band where `gross_cap` first binds under a still-high cushion;
   a single deferral there was path-catastrophic on Core17)

Otherwise keep main’s shared pro-rata reserve trim. **No** sub-industry group-count gate.
Buy envelope unchanged (`min(gross_scale, gap_scale)`). Hard paths untouched.

## Compact comparison

| Pool | Role | TR | Wealth | maxDD | fills | trims | buy_clips |
|------|------|---:|---:|---:|---:|---:|---:|
| common-1 | main | 3.928538 | 4.9285 | -0.1247 | 36 | 6 | 188 |
| common-1 | candidate | 3.928538 | 4.9285 | -0.1247 | 36 | 6 | 188 |
| common-1 | archive | 5.308950 | 6.3089 | -0.1834 | 24 | 0 | 0 |
| common-3 | main | 6.707950 | 7.7080 | -0.1616 | 342 | 59 | 24 |
| common-3 | candidate | 6.707950 | 7.7080 | -0.1616 | 342 | 59 | 24 |
| common-3 | archive | 10.836973 | 11.8370 | -0.1792 | 194 | 0 | 0 |
| common-5 | main | 7.984487 | 8.9845 | -0.1409 | 437 | 121 | 14 |
| common-5 | candidate | 9.351673 | 10.3517 | -0.1400 | 375 | 95 | 11 |
| common-5 | archive | 11.279154 | 12.2792 | -0.1896 | 243 | 0 | 0 |
| common-13 | main | 7.478371 | 8.4784 | -0.1573 | 657 | 243 | 37 |
| common-13 | candidate | 7.478371 | 8.4784 | -0.1573 | 657 | 243 | 37 |
| common-13 | archive | 9.615957 | 10.6160 | -0.2128 | 227 | 0 | 0 |
| common-17 / Core17 | main | 8.610543 | 9.6105 | -0.1510 | 645 | 245 | 63 |
| common-17 / Core17 | candidate | 8.610543 | 9.6105 | -0.1510 | 645 | 245 | 63 |
| common-17 / Core17 | archive | 13.423483 | 14.4235 | -0.2166 | 257 | 0 | 0 |

## Δ candidate − main

| Pool | ΔTR | Δfills | Δtrims | Δbuy_clips | maxDD Δpp | 17≥main? |
|------|----:|-------:|-------:|-----------:|----------:|:--------:|
| common-1 | +0.000000 | +0 | +0 | +0 | +0.000 | n/a |
| common-3 | +0.000000 | +0 | +0 | +0 | +0.000 | n/a |
| common-5 | +1.367186 | -62 | -26 | -3 | +0.086 | n/a |
| common-13 | +0.000000 | +0 | +0 | +0 | +0.000 | n/a |
| common-17 | +0.000000 | +0 | +0 | +0 | +0.000 | YES |

## Notes

- Core17 TR **equals main** (not below).
- common-5 keeps the full **+1.367** TR / fewer ops win previously seen with multi-group.
- common-1/3/13 identical to main on this window.
- Near-peak razor exclusion is required: deferring the 2025-06-24 Core17 overshoot (equity/peak≈0.929, single-group book) alone collapsed TR by ~3.1.
- Replaced multi-group (≥2 sub-industry) switch entirely.

## Risks

- `equity/peak ≤ 0.925` is a structural buffer below the ~0.928–0.931 band where budget-binding overshoot first appears under a still-high cushion; still a threshold.
- Deferred trims leave gross above cap until material overshoot, alert, shock, or budget tightens.
- Not archive parity; buy clipping intentionally unchanged.

## Unit evidence

`pytest tests/unit/test_proportional_reserve_research.py tests/unit/test_observed_shock_budget.py tests/c6_non_economic/test_account_risk_budget.py` → **70 passed**.
