# Topic D — ordinary 0.85 trim intensity (immaterial forced trims) — REVERTED

**Design:** Keep full funding for MATERIAL ordinary overshoot. For immaterial
trims forced by near-peak razor or tight RLB only, fund 85% of overshoot.
Defer band (≤0.9245 + comfortable) unchanged. Not peak-razor deletion.

**Measured** vs razor-0.9245 prior:

| Pool | ΔTR vs prior | notes |
|------|-------------:|-------|
| common-5 | ~0 | preserved |
| common-17 | **−0.357** | Core17 regression |
| pool_j | **−0.653** | fail vs main |
| pool_e | ~0 | |
| pool_h | ~0 | |
| pool_i | −0.022 | |
| pool_f | +0.019 | |

**Verdict:** REVERT. Softening forced near-peak / tight-RLB trims reopens the
Pool J and Core17 wound the 0.9245 razor closed. Benefits do not outweigh harms.
