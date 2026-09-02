# Current-Only Breaking Cleanup + Stress Diagnostic Decomposition

## Goal

Make `quantfusion/` the only Python implementation and API, remove obsolete
compatibility and unreachable account injection code, and make stress failures
diagnosable without a full formal run. Preserve current production economics
and every current correctness boundary.

## Constraints

- Do not change strategy, risk, allocation, regime, execution, fee, T+1,
  volume, signal timing, capital, universe, seed, scenario, metric, hard-gate,
  or promotion-tolerance semantics.
- Keep `worst_add_one_wealth_at_least_minus_18pct` at `-18%`.
- Keep strict AccountSnapshot v3, risk-state validation, provider-specific
  normalization, and RiskAction-to-execution adapter boundaries.
- Delete compatibility instead of adding adapters, migrations, aliases,
  frameworks, dependencies, or speculative abstractions.
- Formal publication is permitted only for the exact complete scenario plan.
  Any filtered or sharded run is diagnostic and cannot update canonical
  artifacts or incumbents.
- A current-semantic rejected candidate remains rejected and non-canonical.

## Design

### Canonical imports and commands

All production code, tests, docs, and CI import `quantfusion.*` directly. Root
Python facades and duplicate CLIs are deleted. Scripts have one supported
entry form: `python -m scripts.<module>`; direct-file bootstrapping and
`sys.path` mutation are deleted.

### Current engine boundary

Historical `AccountState` injection is removed from request objects, public
run signatures, orchestration, replay propagation, and execution helpers.
Current account advice continues through strict `quantfusion.account`
`AccountSnapshot` v3 inputs and never seeds historical replay state.

### Current repository paths

Bundled defaults use `MARKET_DATA_DIR` and `REGIME_DATA_DIR`. User-supplied
paths remain ordinary `Path` values. The old `market_data` and
`historical_data` names are never rewritten.

### Stress responsibilities

Keep a small functional design:

1. `stress_scenarios.py`: deterministic construction and selection of the
   existing scenario plan, preserving IDs and order.
2. `stress_metrics.py`: metrics, summaries, hard gates, permutation checks,
   and current-only promotion evaluation.
3. `stress_artifacts.py`: provenance, checkpoints, validation, rejection, and
   formal publication policy.
4. `stress.py`: argument parsing, orchestration, exit status, and diagnostic
   output.

Selection supports an exact scenario ID, scenario type, and deterministic
index-modulo shard. Selection preserves formal order. Any selector makes the
run diagnostic. The orchestration passes publication eligibility explicitly;
artifact code rejects partial publication independently.

### Stress incumbent semantics

Live code accepts only `trade_count_semantics="trade_records"`. Historical
incumbent conversion and incomparable-economic-contract states are deleted.
An absent accepted current-semantic incumbent uses the existing no-incumbent
path, made explicit and fail-closed if necessary. Historical pre-PR13
canonical artifacts are removed from live inputs. Current rejected evidence
may remain only under the candidate path.

### Configuration and engine cleanup

Delete compatibility aliases after callers move to canonical config facts.
Move pure profile construction from the engine into one existing
`quantfusion.config` boundary only when exact dictionary equality can be
proved. Do not mechanically split other engine modules without a simpler
dependency direction and focused tests.

## Verification

- TDD red/green for selection, partial-publication rejection, removed public
  parameters/aliases, and path behavior.
- Focused architecture, account, engine, overlay, script, and stress tests at
  each checkpoint.
- Exact frozen-universe metrics and economic-sequence fingerprint after any
  engine-call-chain refactor.
- One final engineering suite on the stable candidate.
- One final formal stress only if needed and runnable; an existing product
  hard-gate failure is recorded, not tuned away or repeatedly rerun.
