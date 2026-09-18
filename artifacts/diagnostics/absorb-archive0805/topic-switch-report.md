# Absorb archive0805 — topic switch report (beyond peak-defer razor)

**STATUS:** `BASELINE_HELD` (Topics B/C/D all REVERTED; mechanism unchanged)  
**As of:** 2026-09-18T15:30+0800 (Asia/Shanghai)  
**Branch:** `agent/absorb-archive0805-ordinary-ab5`  
**HEAD:** branch tip (docs commits after mechanism `5c678a3`; run `git rev-parse HEAD`)
**Baseline kept:** mechanism from `7b22259` (budget+overshoot) + `5c678a3` (razor 0.9245)  
**Do NOT merge main.** No Cloud Agent. PonyTail.

## A. Diff audit (LANDED — artifacts kept)

Window 2025-04-01..2026-07-20 · capital 2e6 · warm.  
Main `/workspace/trade-main` @ `fa4ef7ba` vs archive `/workspace/trade-archive_0805` @ `c158435`.

**Headline:** Archive does **far less ordinary fuss** than main AB5:
- **0** ordinary `account_budget_trim` fills and **0** buy-clip intents on common-3/5/17.
- **Fewer sleeve fills** (~40–60% of main): c3 194 vs 342; c5 243 vs 437; Core17 257 vs 645.
- **Higher TR** (archive ahead ~3.3–4.8 TR) with **worse maxDD** on c5/Core17.
- Paths **diverge on first meaningful trade day 2025-04-24** (also first equity divergence) — early buy sizing / sleeve allocation, not a late-path accident.
- Main's first ordinary AB5 trims appear mid-window with **no same-day archive sell** on that symbol.

| Pool | main TR | archive TR | main fills | arc fills | main trims | arc trims | main clips | arc clips |
|------|--------:|-----------:|-----------:|----------:|-----------:|----------:|-----------:|----------:|
| common-3 | 6.708 | 10.837 | 342 | 194 | 59 | 0 | 24 | 0 |
| common-5 | 7.984 | 11.279 | 437 | 243 | 121 | 0 | 14 | 0 |
| common-17 | 8.611 | 13.423 | 645 | 257 | 245 | 0 | 63 | 0 |

Artifacts: `archive-main-diff-audit.md` / `.json` / `-full.json`.

## B. Buy-side redesign — REVERTED

**Tried:** ordinary soft-gap blend (≥0.75 gap, gross not binding) →
`ordinary_buy_scale = 0.5*(gross+gap)`. Not wholesale gap delete.  
(RLB-comfort-gated variants never fire: gap binds iff RLB is tight.)

**Why revert:** blends fired (1–3 days) but path-coupled losses:
- common-5 **−0.80** TR vs prior
- pool_e **−1.27** TR vs prior (also −0.39 vs main)

Artifacts: `topic-b-softgap-probe.json`, `topic-b-softgap-reverted.md`.

## C. Trim intensity decay — REVERTED

**Tried:** split near-peak bands — defer ≤0.9245; full trim (0.9245,0.925];  
half intensity (0.925,0.930] for immaterial+comfortable.

**Why revert:** pool_j **−0.76** TR vs main despite Pool J full-trim guard;
Core17 unchanged (no benefit). Path dependence from 0.925–0.930 days
(e.g. pool_j 2025-09-15 @ 0.929) cascades.

Artifacts: `topic-c-intensity-probe.json`, `topic-c-intensity-reverted.md`.

## D. Closer-to-archive ordinary behavior — REVERTED

**Tried:** coherent mild fuss cut — material overshoot still fully funded;
immaterial trims forced by near-peak razor / tight RLB fund **85%** only.
Defer band unchanged (no razor deletion; no coupon state).

**Why revert:**
- pool_j **−0.65** TR vs main
- Core17 **−0.36** TR vs main
- Softening forced near-peak / tight-RLB trims reopens the wound 0.9245 closed.

Also scanned overshoot days (`topic-d-overshoot-scan.json`): pool_j razor day
2025-09-25 @ 0.9248 must stay full-trim; Core17 2025-11-12 @ 0.9252 and
2025-06-24 @ 0.929 sit in the dangerous half-intensity band.

Artifacts: `topic-d-intensity85-probe.json`, `topic-d-intensity85-reverted.md`,
`topic-d-overshoot-scan.json`.

## Final scoreboard vs main (baseline reconfirmed)

Mechanism: budget + immaterial overshoot + near-peak razor **0.9245**.  
Fresh confirm @ post-revert tree:

| Pool | TR | ΔTR vs main | notes |
|------|---:|------------:|-------|
| common-5 | 9.352 | **+1.367** | keep |
| common-17 | 8.611 | 0 | flat; no >1 drop |
| pool_j | 4.009 | 0 | equal main (razor fix held) |
| pool_h | 2.069 | **+1.001** | keep |

Full prior ladder (unchanged mechanism) from `all-main-pools-report.md`:
**Improved 5 / Worsened 0 / Flat 9** runnable pools vs main.

## Remaining archive gap

Archive still ahead on most validation TR (Core17 ~13.42 vs 8.61; common-5
~11.28 vs 9.35). Archive has 0 ordinary AB5 trims/clips (different risk model).
Absorb remains **partial patience on main AB5**, not archive parity.

Structural gap starts on **2025-04-24 buy sizing**, not only near-peak deferrals.
Buy-envelope and near-peak intensity levers tested here did not land net-positive.

## Acceptance check

| Criterion | Result |
|-----------|--------|
| Topic A audit landed | YES |
| B/C/D benefits > harms | NO — all reverted |
| Baseline mechanism preserved | YES (5c678a3) |
| No catastrophic Core17 | YES (ΔTR=0 at baseline) |
| Pool J not worse than main | YES at baseline |
| Do not merge main | YES |
