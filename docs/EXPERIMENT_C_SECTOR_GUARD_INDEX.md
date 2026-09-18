# Experiment C — Index-based sector guard

## Intent

Prior stock-membership swaps and trade-pool breadth guards lost money vs
`origin/main`. This experiment drives sector-guard **shock / recovery** from
fixed product indices already in the tree:

| Role | Code | Config key |
|------|------|------------|
| Broad | `000300` (CSI300) | `REGIME_INDEX_FILES["broad"]` |
| Technology | `000682` | `REGIME_INDEX_FILES["technology"]` |

Market-regime state machine behavior is unchanged: it still reads
`policy.regime_symbols`. Those stocks are **not** used for the sector-guard
observation set in this experiment (optional unused diagnostic only).

AB5 / recovery stock knobs elsewhere are untouched. No auto-selected
referees from the trade pool.

## Probes (one branch, labeled separately)

### `tech_only` (`sector_guard_index_mode=tech_only`)

- Observation set: `{000682}` only.
- `equal_return` := 000682 daily close-to-close return.
- `shock_breadth` / `recovery_breadth` := **binary MA membership** of that
  single index (`1.0` above MA, `0.0` below).
- Multi-name breadth is **N/A**; existing thresholds
  (`shock_breadth <= 0.2`, `recovery_breadth >= 0.8`) therefore collapse to
  below/above the configured MA.
- `sector_guard_min_symbols` default = 1.

### `dual_confirm` (`sector_guard_index_mode=dual_confirm`)

- Observation set: `{000300, 000682}`.
- Shock day requires **each** index to independently meet
  `(daily return <= sector_shock_return AND close <= shock MA)`.
- Recovery day requires **each** index to independently meet
  `(daily return > 0 AND close > recovery MA AND its own short normalized
  path is above its recovery MA)`.
- Stricter than equal-weight averaging of the two series; fewer false clears.
- `sector_guard_min_symbols` default = 2.

## Fail-closed

Index frames load from `data/regime/{code}.csv` (override with
`sector_guard_index_data_dir`). Missing directory or CSV raises
`RuntimeError` at first guard update — no invented series.

## How to reproduce

```bash
/workspace/trade/.venv/bin/python scripts/run_sector_guard_index_compare.py
```

Artifact: `artifacts/diagnostics/sector-guard-index-compare.json`.

## Non-goals

- No merge to main, no force-push, no Cloud Agent.
- Formal 958 acceptance on main does **not** cover this candidate.

## Validation result (warm 2025-04-01→2026-07-20, capital 2e6)

Both probes **underperform** `origin/main` on pools 1/3/5/13/17:

- **tech_only**: first `guard_on` moves earlier to 2026-06-01; return deltas negative on every pool.
- **dual_confirm**: no `guard_on` at all (AND too strict); still loses return vs main, implying main's single 2026-06-26 guard was net-helpful.

**Recommendation: discard** (do not merge). Optional iterate ideas only — soften dual_confirm recovery/shock, or hybrid with the stock basket. Formal 958 does not cover this candidate.

See `artifacts/diagnostics/sector-guard-index-compare.json`.
