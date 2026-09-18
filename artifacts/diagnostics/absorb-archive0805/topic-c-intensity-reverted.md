# Topic C — trim intensity decay — REVERTED

**Design:** Split near-peak razor into bands:
- eq/peak ≤ 0.9245 → intensity 0 (defer)
- 0.9245 < eq/peak ≤ 0.925 → intensity 1 (Pool J band)
- 0.925 < eq/peak ≤ 0.930 + immaterial + comfortable → intensity 0.5
- else → intensity 1

**Measured** vs razor-0.9245 prior:

| Pool | ΔTR vs prior | notes |
|------|-------------:|-------|
| common-5 | ~0 | preserved |
| common-17 | ~0 | no Core17 lift |
| pool_j | **−0.764** | fail (worse than main) |
| pool_e | ~0 | |
| pool_h | +0.156 | |
| pool_i | ~0 | |
| pool_f | −0.038 | |

**Verdict:** REVERT. Half-intensity in the 0.925–0.930 band path-depends into
Pool J −0.76 TR despite explicit 0.9245–0.925 full-trim guard. Core17
unchanged (no benefit). Benefits do not outweigh harms.
