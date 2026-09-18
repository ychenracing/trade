# Early buy/add sizing divergence — 2025-04-24 deep dive

**STATUS:** `DOCUMENTED_REVERTED` (portable probes net-negative; baseline held)  
**As of:** 2026-09-18T16:10+0800 (Asia/Shanghai)  
**Branch:** `agent/absorb-archive0805-ordinary-ab5`  
**Baseline kept:** budget + immaterial overshoot + near-peak razor **0.9245** (unchanged)  
**Do NOT merge main.** No Cloud Agent. PonyTail.

## 1. Root cause (plain language)

Archive vs main **first fill + first equity split on 2025-04-24** is **not** an AB5 buy-clip / soft-gap event.

### What the day actually looks like

| Pool | Side | 300394 buys | 300502 buys | AB5 buy scale | Pre-trade sleeve reweight |
|------|------|------------:|------------:|--------------:|---------------------------|
| common-5 | main/absorb baseline | **12200** (slow only) | 18000 (6300/3600/8100) | **1.0** (0 shares removed) | 2025-04-08 TRANSITION → fast0.20/base0.35/slow0.45 |
| common-5 | archive | **27000** (9000×3 sleeves) | 18000 (6000×3) | n/a (no AB5 envelopes) | none (equal thirds) |
| Core17 | main/absorb baseline | **0** | 18000 (uneven) | **1.0** | same 2025-04-08 reweight |
| Core17 | archive | **9000** (slow only) | 18000 (6000×3) | n/a | none |

Signal date is **2025-04-23** (fills 2025-04-24). Envelope on fill day: `buy_gross_scale=1.0`, `buy_gap_scale=1.0`, `buy_shares_removed=0`, `ordinary_buy_scale=1.0` (absorb). Soft-gap / ordinary buy-scale knobs cannot close this gap — envelope is already non-binding.

### Two structural drivers (mostly non-portable)

**A. Universe selection (signal eligibility)**  
- **Archive:** `if len(tradable) <= max_positions: return all` — no adaptive TRANSITION cut inside selection; common-5 (5 names) admits every symbol on every sleeve.  
- **Main:** ranks from **five names upward**, and under TRANSITION + external risk shrinks the rank window to `transition_max_positions=4`. Different sleeves (different lookbacks → different scores) drop **different** names.  
- Result on common-5 Apr23: archive fires 300394 on **base+fast+slow**; main fires 300394 on **slow only**.  
- Core17: archive still gets slow 300394; main gets **none**. Sticky/reference-score path differs further — removing adaptive from selection alone did **not** restore Core17’s 300394.

**B. Dynamic sleeve free-cash reweight (sizing when signals fire)**  
- Main runs `free_cash_sleeve_reweight` on **2025-04-08** while still **100% cash / zero positions**, shifting idle capital to slow.  
- Archive has no equivalent — equal ≈666k per sleeve through first buys.  
- Explains uneven 300502 sizes (6300/3600/8100 vs 6000/6000/6000) and larger slow-only 300394 (12200 vs 9000).

**C. Not AB5**  
- No ordinary trim that day; no buy clip; remaining loss budget comfortable.  
- `scaled_late_strategy_join` appears on atr_channel only (main late-join rule absent in archive) — does not drive the turtle fill gap.

### Portability verdict

| Piece | Portable on main AB5? | Notes |
|-------|----------------------|-------|
| AB5 buy scale / soft-gap | Already 1.0 — N/A | Topic B soft-gap already reverted earlier |
| Virgin-cash reweight skip | Yes (tried) | Equalizes sizes; **net-negative** vs absorb prior |
| Selection hard-max pass-through | Yes (tried) | Makes common-5 Apr24 **match archive fills**; **net-negative** vs absorb prior |
| Sticky / reference scoring parity | **No** | Different engine; Core17 300394 remains |

## 2. What we tried / reverted

### Probe 1 — selection pass-through + virgin-cash skip (combined)
- Restored archive-like `len(tradable) <= hard_max_positions` before adaptive rank.  
- Skipped `free_cash_sleeve_reweight` while all sleeves virgin (no positions/pending).  
- **Apr24 common-5 fills matched archive exactly** (27000+18000 equal thirds).  
- **Economics (partial):** common-5 **−1.10 TR vs main** (−2.47 vs prior razor baseline); common-17 +2.45 (path-coupled).  
- **REVERTED.**

### Probe 2 — virgin-cash skip only
- Apr24: 300394 still slow-only (9000); 300502 equalized to 6000×3.  
- Full ladder (validation + research A–J except G):

| Pool | TR | Δ vs main | Δ vs prior |
|------|---:|----------:|-----------:|
| common-5 | 7.626 | **−0.359** | **−1.726** |
| common-17 | 11.059 | **+2.448** | +2.448 |
| common-3 / pool_b | 6.025 | **−0.683** | −0.683 |
| common-13 | 7.387 | −0.092 | −0.092 |
| common-1 / pool_a | 3.929 | 0 | 0 |
| pool_j | 4.130 | +0.121 | +0.121 |
| pool_e | 7.253 | +0.203 | **−0.681** |
| pool_h | 1.723 | +0.655 | **−0.347** |
| pool_i | 3.161 | +0.717 | +0.088 |
| pool_f | 3.522 | **−4.818** | **−5.052** |
| pool_c | 8.977 | +0.443 | +0.443 |
| pool_d | 4.629 | +1.214 | +1.214 |

**Summary:** improved 7 / worsened 5 / flat 2 vs main; **7 worse vs prior**. pool_f catastrophe. Gate fails.  
- **REVERTED.**

### Not repeated
- Soft-gap buy blend (Topic B) — already known net-negative; envelope already 1.0 on this day.

## 3. Scoreboard vs main (baseline held)

Mechanism unchanged after revert. Prior confirmed scoreboard still applies (`all-main-pools-report.md` / topic-switch):

| Pool | Absorb TR (baseline) | ΔTR vs main | notes |
|------|---------------------:|------------:|-------|
| common-5 | 9.352 | **+1.367** | keep |
| common-17 | 8.611 | 0 | flat |
| pool_j | 4.009 | 0 | equal main (razor held) |
| pool_h | 2.069 | **+1.001** | keep |

Runnable pools vs main at baseline: **Improved 5 / Worsened 0 / Flat 9**.

## 4. Does this close a meaningful archive gap?

**Diagnostic yes, economic no (on portable slices).**

- We now know the 2025-04-24 split is **universe eligibility + virgin sleeve cash shape**, not AB5 fuss.  
- Closing it on main in an archive-like way **destroys** the absorb AB5-patience gains on common-5 / pool_f / prior path.  
- Archive gap on Core17 (~13.42 vs 8.61) and common-5 (~11.28 vs 9.35) remains; early-path parity is **coupled** to later AB5 economics and is not a free lunch.

## 5. Honest next if blocked

1. **Do not** chase more AB5 trim/clip knobs for this day — envelope already idle.  
2. If revisiting early path: need a **risk-gated** design that preserves absorb’s post-entry patience (e.g. virgin-cash skip **only** when AB5 disabled — useless here; or selection pass-through only for pools ≤N **with** a Core17/pool_f regression harness before land). Current naive ports fail the gate.  
3. Remaining archive edge is still largely **non-portable risk model** (0 ordinary trims/clips) + **sticky/score universe engine**.  
4. Optional audit-only: instrument per-sleeve candidate sets on 2025-04-23 for Core17 to name which rank/sticky rule drops 300394 (no mechanism change).

## Artifacts
- `early-buy-divergence-apr24-raw.json` — main/absorb/archive day dump  
- `early-buy-signals-apr24.json` — fusions / weights / orders  
- `early-buy-virgin-cash-probe.json` — full ladder, REVERTED  
- `early-buy-combined-probe-reverted.json` — partial combined probe  
- This report

## Acceptance

| Criterion | Result |
|-----------|--------|
| Deep-dive 2025-04-24 root cause | YES |
| Portable mechanism landed | NO — both probes reverted |
| Baseline mechanism preserved | YES (0.9245) |
| Benefits > harms on most pools | NO for probes; YES for held baseline |
| Do not merge main | YES |
