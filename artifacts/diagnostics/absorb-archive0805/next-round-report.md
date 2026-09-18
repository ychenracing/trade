# Absorb archive0805 — next-round report

**STATUS:** `DIRECTIONAL_PROGRESS` (shippable net-positive vs main & vs ac0aa38)  
**As of:** 2026-09-18T15:00+0800 (Asia/Shanghai)  
**Branch:** `agent/absorb-archive0805-ordinary-ab5`  
**HEAD:** (see git; mechanism commit `5c678a3` + this docs commit)  
**Base mechanism:** ac0aa38 budget + immaterial overshoot + near-peak razor  
**Do NOT merge main.** No Cloud Agent.

## What landed vs reverted

| Step | Outcome | Notes |
|------|---------|-------|
| **1. Pool J shortfall** | **LANDED** `5c678a3` | First divergence 2025-09-25 deferral at eq/peak≈**0.9248**. Razor `0.925→0.9245`. Pool J ΔTR vs main **0** (was −0.557). common-5/E/F/H/I wins preserved; Core17 flat. |
| **2. Buy-clipping reduction** | **REVERTED** | Conditional gap-skip (under-cap + comfortable + eq/peak≤0.90) passed units but **never fired** on live pools (gap binds when budget is not comfortable). Broader skip broke shock-budget units. Not shipped. |
| **3. Core17 less-trimming** | **REVERTED** | Near-peak scan: only safe-looking immaterial multi-group day is **2025-11-12** (eq/peak 0.9252, 3 groups). ≥3-group exception ≤0.928 deferred that day → Core17 **−1.73 TR** (fail >1). common-13 −0.81, pool_f −0.21. No safe near-peak Core17 deferral on this window. |

## Mechanism now (production)

Defer ordinary held trims only when **all** hold:

1. Immaterial overshoot: `gross - gross_cap ≤ daily_loss_limit × equity`
2. Budget comfortable: `remaining_loss_budget ≥ 0.85 × equity × stress_fraction`
3. Near-peak razor: `equity / peak ≤ 0.9245`

Buy envelope unchanged: `min(gross_scale, gap_scale)`. Hard paths untouched.

## Full scoreboard vs main (fresh candidate @ HEAD)

| Pool | ΔTR vs main | Notes |
|------|------------:|-------|
| common-1 | +0.000 | flat |
| common-3 | +0.000 | flat |
| common-5 | **+1.367** | keep |
| common-13 | +0.000 | flat |
| common-17 / Core17 | +0.000 | flat (= main; no >1 drop) |
| pool_a–d | +0.000 | flat |
| pool_e | **+0.883** | keep |
| pool_f | **+0.234** | keep |
| pool_g | SKIP | missing CSV |
| pool_h | **+1.001** | keep |
| pool_i | **+0.630** | keep |
| pool_j | **+0.000** | **fixed** (was −0.557) |

**Improved 5 / Worsened 0 / Flat 9** (runnable pools).  
**Benefits > harms:** **YES** vs main and vs ac0aa38 (Pool J defect closed; no new regressions).

## vs archive (remaining gaps)

Archive still ahead on most validation TR (e.g. Core17 13.42 vs 8.61; common-5 11.28 vs 9.35). Archive has 0 ordinary AB5 trims/clips (different risk model). Absorb is **partial patience on main AB5**, not archive parity.

## Remaining gaps / next levers (not done)

1. **Buy clips:** gap debit is the dominant binder; skipping it conflicts with unit debit invariants or does not fire when gated safely. Needs a different design (not “drop gap”).
2. **Core17 archive gap (~4.8 TR):** every near-peak ordinary deferral on this window that was tested harmed Core17 (06-24 −3; 11-12 −1.73). Further less-trimming needs a non-near-peak lever (e.g. materiality tightening only off-peak) or accept flat Core17.
3. **pool_g CSV** still missing.
4. Path-dependent buy/trim interaction after deferrals remains the main absorb↔main coupling risk.

## Artifacts

- `all-main-pools-report.md` / `all-main-pools-metrics.json` (refreshed)
- `pool-j-razor-fix.md`, `pool_j_first_divergence.json`, `deferral_peak_scan.json`, `razor9245-probe.json`
- `buyclip-binding-scan.json`, `buygap-skip-probe.json` (reverted probe)
- `core17-nearpeak-scan.json`, `core17-div3-probe.json` (reverted probe)
- `budget-gate-report.md` (ac0aa38 era; razor stamp now 0.9245 in code)

## Acceptance check

| Criterion | Result |
|-----------|--------|
| Net positive vs ac0aa38 | YES (Pool J fixed; others unchanged) |
| Net positive vs main | YES (5 up, 0 down) |
| No catastrophic Core17 | YES (ΔTR=0) |
| Pool J not worse | YES (equal main) |
