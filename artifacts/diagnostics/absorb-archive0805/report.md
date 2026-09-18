# Absorb archive_0805 into ordinary AB5 (new effort)

**STATUS:** `DIRECTIONAL_PROGRESS`  
**As of:** 2026-09-18T13:15+0800 (Asia/Shanghai)  
**Branch:** `agent/absorb-archive0805-ordinary-ab5` (from main `fa4ef7bafe7f`)  
**Archive reference:** `c158435f6603`  
**NOT continuing rejected PR #120.** Main is **not** merged.

## Goal

On main-branch code, absorb archive_0805 advantages (higher return, fewer ops)
while keeping current data, account continuity, fail-closed, and the risk-identification
framework. Focus: reduce AB5 **ordinary-state** held trims and buy clipping without
deleting hard risk capabilities.

## Mechanism (plain language)

**Ordinary multi-group immaterial overshoot deferral.**

When AB5 is in ordinary preserve state (no account alert, no shock episode):

1. If the live book spans **≥2 sub-industry groups** and gross exceeds the close-known
   cap by **at most one `daily_loss_limit` × equity** (same materiality already used for
   alert invested-concentration), **do not** emit pro-rata held trims.
2. If the book is **single-group concentrated**, or the overshoot is **material**, keep
   main’s shared pro-rata reserve funding.
3. Buy scaling still uses incumbent `min(gross_scale, gap_scale)` (PR #120 taught that
   tightening scarce/gap buy blocking loses return on common-5).
4. Alert / shock / direct-loss / weak-book / concentration / locks / fail-closed are unchanged.

Diagnostics fields added on the envelope receipt: `ordinary_path_active`,
`ordinary_held_overshoot`, `ordinary_held_trim_deferred`, `ordinary_buy_scale`.

### What we tried and rejected locally

| Attempt | Outcome |
|---------|---------|
| Full ordinary trim removal (always defer when preserve + gross>cap) | common-3 TR fell ~0.73–0.86 vs main |
| Drop gap from ordinary buy scale (`gross_scale` only) | Broke high-gap / ordinary-budget unit contracts |
| Immaterial deferral without multi-group gate | common-5 improved, but common-3 still below main |
| **Final: immaterial + multi-group gate** | common-1/3 identical to main; common-5 clear win |

## Measurement window

- Dates: `2025-04-01` .. `2026-07-20`
- Capital: `2e6`, `indicator_state=warm`
- Engines: main/candidate `BacktestEngine`; archive `quant_fusion.BacktestEngine` @ archive_0805

## Metrics vs main vs archive

### common-1 (`300308`)

| | total_return | wealth | maxDD | fills | dss | sell_b | days | turnover | fees | trims | buy_clip_intents | removed_shares |
|--|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| main | 3.928538 | 4.9285 | -0.1247 | 36 | 7 | 4 | 7 | 2.826 | 9579.1 | 6 | 188 | 806200 |
| candidate | 3.928538 | 4.9285 | -0.1247 | 36 | 7 | 4 | 7 | 2.826 | 9579.1 | 6 | 188 | 806200 |
| archive | 5.308950 | 6.3089 | -0.1834 | 24 | 5 | 3 | 5 | 2.404 | 10228.0 | 0 | 0 | 0 |

Delta cand−main TR: **+0.000000** (identical). Archive gap recovery: **0.0000**.

### common-3 (`300308,300502,300394`)

| | total_return | wealth | maxDD | fills | dss | sell_b | days | turnover | fees | trims | buy_clip_intents | removed_shares |
|--|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| main | 6.707950 | 7.7080 | -0.1616 | 342 | 96 | 47 | 63 | 15.618 | 67094.9 | 59 | 24 | 544400 |
| candidate | 6.707950 | 7.7080 | -0.1616 | 342 | 96 | 47 | 63 | 15.618 | 67094.9 | 59 | 24 | 544400 |
| archive | 10.836973 | 11.8370 | -0.1792 | 194 | 71 | 35 | 53 | 13.407 | 76505.3 | 0 | 0 | 0 |

Delta cand−main TR: **+0.000000** (identical; single-group optical keeps main trims). Archive gap recovery: **0.0000**.

### common-5 (`300308,300502,300394,688256,603986`)

| | total_return | wealth | maxDD | fills | dss | sell_b | days | turnover | fees | trims | buy_clip_intents | removed_shares |
|--|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| main | 7.984487 | 8.9845 | -0.1409 | 437 | 130 | 67 | 80 | 17.808 | 82722.6 | 121 | 14 | 316800 |
| candidate | 9.351673 | 10.3517 | -0.1400 | 375 | 115 | 59 | 73 | 16.448 | 83110.8 | 95 | 11 | 253500 |
| archive | 11.279154 | 12.2792 | -0.1896 | 243 | 99 | 43 | 67 | 13.459 | 70793.8 | 0 | 0 | 0 |

Delta cand−main: TR **+1.367186**, fills **-62**, trims **-26**, buy_clip_intents **-3**, maxDD Δpp **+0.086**.  
Archive gap recovery ratio: **0.4150** (~41.5% of archive−main TR gap).

## What improved / worsened

| Pool | vs main | Notes |
|------|---------|-------|
| common-1 | flat (identical) | Single-name / shock-path trims unchanged |
| common-3 | flat (identical) | All-optical single-group → main reserve behavior preserved |
| common-5 | **higher return, fewer fills/trims/buy-clips, maxDD slightly better** | Multi-group immaterial deferral reduces ordinary chops |

Still far below archive absolute TR on all pools (archive has no AB5 and different sizing). Buy clipping on common-1 remains high (188); gap envelope intentionally left intact.

## Risks

- Deferred ordinary trims leave gross above cap for longer on diversified books until material overshoot, alert, or shock. Floor / remaining_loss_budget / hard paths still apply.
- Mechanism is group-count gated; a mis-labeled `SYMBOL_SUB_INDUSTRY` map could change deferral eligibility.
- Not a full archive parity path; further buy-clip reduction needs a design that does not violate high-gap stress unit contracts.
- Diagnostic artifacts under this path are **non-canonical** until a later promotion decision.

## Code changes

- `quantfusion/risk/account_budget.py` — ordinary preserve materiality + multi-group deferral; receipt diagnostics
- `tests/unit/test_proportional_reserve_research.py` — contract tests for deferral / single-group / material / alert

## Unit evidence

`pytest tests/unit/test_proportional_reserve_research.py tests/unit/test_observed_shock_budget.py tests/c6_non_economic/test_account_risk_budget.py` → **68 passed**.

## Git / merge policy

- Branch pushed for review: `agent/absorb-archive0805-ordinary-ab5`
- **Do NOT merge to main** from this effort without an explicit user decision
- PR #120 remains rejected/closed; this is a new branch

## Artifacts

- `artifacts/diagnostics/absorb-archive0805/report.md` (this file)
- `artifacts/diagnostics/absorb-archive0805/metrics.json`
- Per-role raw: `_econ{1,3,5}_{candidate,incumbent,archive}.json`
