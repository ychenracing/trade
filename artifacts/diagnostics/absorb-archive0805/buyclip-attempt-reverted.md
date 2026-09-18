# Buy-clip reduction — attempted, reverted

**STATUS:** `REVERTED`  
Conditional `ordinary_buy_scale = gross_scale` when under-cap + comfortable rlb + eq/peak≤0.90.

- Units: pass at ≤0.90; fail at ≤0.9245 (shock-budget tests require gap).
- Live pools: **0 gap-skip days** — when gap binds, budget is typically not comfortable.
- Conclusion: not a worthwhile change; do not drop gap globally.
