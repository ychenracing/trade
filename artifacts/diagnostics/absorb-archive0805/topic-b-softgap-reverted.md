# Topic B — soft-gap buy blend — REVERTED

**Design:** ordinary-path soft gap (≥0.75, gross not binding) blend
`ordinary_buy_scale = 0.5*(gross+gap)` — not wholesale gap delete.

**Measured** (vs razor-0.9245 prior):

| Pool | ΔTR vs prior | blends fired |
|------|-------------:|-------------:|
| common-5 | **−0.798** | 2 |
| common-17 | ~0 | 1 |
| pool_e | **−1.270** (also −0.387 vs main) | 1 |
| pool_f | +0.003 | 3 |
| pool_h | ~0 | 1 |
| pool_i | −0.029 | 1 |
| pool_j | ~0 | 1 |

**Verdict:** REVERT. Soft gap relief fires but path-dependent buy/trim
coupling destroys common-5 absorb gains and turns pool_e net-negative vs
main. Benefits do not outweigh harms.
