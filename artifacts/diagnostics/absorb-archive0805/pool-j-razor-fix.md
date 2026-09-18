# Pool J shortfall — first divergence + razor tighten

**STATUS:** `LANDED`  
**As of:** 2026-09-18T15:10+0800 (Asia/Shanghai)  
**Base mechanism:** ac0aa38 budget+overshoot+peak razor  
**Change:** near-peak defer ceiling `0.925` → `0.9245`

## First divergence (pool_j vs main)

| Item | Value |
|------|-------|
| First envelope diff that matters | **2025-09-25** candidate-only deferral |
| equity/peak that day | **0.924806** (≤ old 0.925, > new 0.9245) |
| Main that day | 13 `new_reduction_orders`; cand deferred (0) |
| First wealth split | 2025-09-26 (cand −10.4k assets) |
| First trade split | main sells 601869 trim 2025-09-26; cand larger trim 2025-09-29 |
| Terminal ΔTR (old) | **−0.557279** |

Second cand deferral 2026-05-29 (eq/peak 0.913) is path-downstream; after blocking 09-25, pool_j has **0 deferrals** and matches main.

## Why not a Pool-J-only special case

Threshold was already a structural buffer under Core17’s ~0.928–0.931 first-bind band. Pool J hit **0.9248**, inside the old razor’s “allow” side by <1e-3. Tightening by 5e-4 is the minimal continuous fix; no group-count / pool-size gate.

## Probe after 0.9245 (cand vs main)

| Pool | ΔTR vs main | vs prior 0.925 | deferrals |
|------|------------:|---------------:|----------:|
| common-5 | +1.367186 | 0 | 3 |
| common-17 | ~0 | 0 | 0 |
| pool_e | +0.883354 | 0 | 1 |
| pool_f | +0.234450 | 0 | 2 |
| pool_h | +1.001205 | 0 | 5 |
| pool_i | +0.629525 | 0 | 2 |
| pool_j | **~0** (fixed) | **+0.557** | **0** |

Unit: `71 passed` (`test_proportional_reserve_research` + shock + c6 budget).

## Artifacts

- `pool_j_first_divergence.json`
- `deferral_peak_scan.json`
- `razor9245-probe.json`
