# Verified Backtest Results

Verification date: 2026-08-01 UTC.

Quant Fusion is a standalone module with built-in AKShare and local CSV
data paths. It uses the same code, parameters, costs, and forward-adjusted data
snapshot across every requested universe. Initial capital is CNY 2,000,000.

| Universe | Cold return | Cold maximum drawdown | Warm return | Warm maximum drawdown | Warm Sharpe | Warm Calmar | Warm trades |
|---|---|---|---:|---:|---:|---:|---:|
| 1 symbol | 536.66% | -18.49% | 530.89% | -18.34% | 3.21 | 18.23 | 24 |
| 3 symbols | 1059.72% | -18.32% | 1083.70% | -17.92% | 3.69 | 34.47 | 194 |
| 5 symbols | 1078.67% | -16.80% | 1115.99% | -15.86% | 3.70 | 39.93 | 222 |
| 13 symbols | 894.16% | -16.93% | 1038.74% | -18.41% | 3.61 | 32.37 | 324 |
| 22 symbols | 843.49% | -17.13% | 983.57% | -16.22% | 3.76 | 35.07 | 244 |

The 2026-06-30 and 2026-07-20 figures are identical for the 1-, 3-, and 5-symbol
universes. The 13-symbol results increase slightly through July; the 22-symbol
warm result rises from 977.83% to 983.57%.

The fixed signal-only regime basket confirmed the defensive gate on 2026-06-26 in
all requested universes. Risk-only symbols never entered the order or trade ledgers.

## Cambricon mapping regression

After routing `688256` 寒武纪 through `semiconductor` / `domestic_semiconductor` /
`domestic_design`, the requested nine-symbol universe produced the following
deterministic results:

| Indicator state | End date | Total return | Maximum drawdown | Sharpe | Calmar | Trades |
|---|---|---|---:|---:|---:|---:|---:|
| Cold | 2026-06-30 | 1187.05% | -15.89% | 3.90 | 46.77 | 225 |
| Cold | 2026-07-20 | 1187.05% | -15.89% | 3.81 | 41.99 | 225 |
| Warm | 2026-06-30 | 1147.30% | -15.43% | 3.88 | 46.76 | 277 |
| Warm | 2026-07-20 | 1147.30% | -15.43% | 3.79 | 42.02 | 277 |

The universe contains `300308`, `688256`, `300502`, `300394`, `603986`,
`688008`, `688347`, `300054`, and `688300`. The sector guard still confirms on
2026-06-26, so extending the end date through 2026-07-20 does not change final
assets in this snapshot. Exact metadata is stored in
`cambricon_universe_backtest.json` and can be regenerated with
`python backtest_cambricon_universe.py`.

High-cost and weak-regime figures, limitations, and reproducibility commands are
documented in `README.md`. Exact scenario metadata is stored in
`universe_backtest.json`. The one-through-22 ordered-prefix audit is
stored in `prefix_stress.json`; its worst adjacent wealth change is
-13.23% when moving from 9 to 10 symbols.

The 2026-07-22 proposal audit and controlled before/after experiments are recorded
in `STRATEGY_REVIEW.md`. No tested trading-rule candidate was stable
enough across time windows and universe sizes to replace the current defaults.

## Risk Management Features

### Risk State Identity

The `risk_state.json` file now includes enhanced identity fields:
- `schema_version`: schema version (currently 1) for forward compatibility.
  Unknown versions are rejected on load (fail-closed) to prevent
  misinterpreting fields with changed semantics.
- `symbols_hash`: SHA-256 fingerprint of sorted symbol codes + count + start date
  + indicator_state + capital + warmup days. Capital is included because
  different capital means different position sizing and risk exposure.
- `total_symbols`: number of symbols in the universe
- `run_id`: unique run identifier (UUID4-based) for traceability

Old risk state files without `symbols_hash` are rejected (fail-closed) to
prevent cross-contamination between different universes or configurations.

### Risk State Reliability

- **Atomic writes**: risk state and JSON artifact are written to temp files,
  flushed with `os.fsync`, then atomically renamed via `os.replace()`. This
  prevents partial writes from corrupting files on disk full, process kill, or
  power loss.
- **Corruption fail-closed**: if `risk_state.json` is corrupted, unreadable,
  or fails schema validation (wrong field types, NaN/Inf, negative values,
  unknown schema version), the scan exits with code 1 instead of silently
  discarding the previous terminal lock state.
- **Schema validation**: all required fields are validated for correct types:
  `schema_version` (int, must be known version), `scan_date` (str),
  `terminal_risk_lock` (bool), `sector_guard_active` (bool),
  `cycle_lock_count` (int, non-negative), `max_drawdown` (finite number),
  `total_return` (finite number), `final_assets` (finite number,
  non-negative). Strings like `"false"` (which are truthy in Python) are
  rejected. Unknown `schema_version` values are rejected to enforce forward
  compatibility. NaN and Inf are rejected because they break comparisons and
  formatting.
- **Pre-save validation**: numeric values are validated before writing —
  NaN/Inf in `max_drawdown`, `total_return`, or `final_assets` raise
  `ValueError`, and negative `cycle_lock_count` raises `ValueError`. This
  prevents creating an invalid state file that would be rejected on the next
  load.
- **Same-day rerun preservation**: re-running the scan on the same day no
  longer discards the previous risk state. Terminal lock and sector guard
  continuity is preserved across same-day reruns.
- **Identity mismatch buy suppression**: when the identity hash does not
  match (different symbol set, count, or configuration), buy signals are
  suppressed (fail-closed) to prevent entering new positions without verified
  risk-state continuity. In the display, pure buy signals become "观望 (风险
  状态不匹配)" and mixed buy/sell signals show only the sell part with
  "[买入已抑制]". In the JSON artifact, blocked buy signals are placed in a
  separate `blocked_signals` list — `pending_signals` only contains
  executable signals, so downstream consumers that check `direction` on
  `pending_signals` will never see blocked buys.
- **State preservation on mismatch**: when the identity does not match, the
  old risk state is NOT overwritten — the previous terminal lock and sector
  guard are preserved. Use `--reset-risk-state` to intentionally establish a
  new identity after a configuration change.
- **Artifact-first transaction ordering**: the JSON artifact is written to
  disk BEFORE risk state is saved. This is a true two-phase commit: if the
  artifact write fails, no risk state has been committed — preventing
  state/artifact inconsistency. If the risk state save subsequently fails
  (e.g. disk full, permission error, invalid values), the artifact already
  exists on disk with `risk_state_saved: false` and an error message. The
  scan exits with code 1 so scripts, cron jobs, and external schedulers
  can detect the failure and alert.
- **Last-good artifact protection**: when a run fails (NaN/Inf in result,
  wrong-type fields, missing fields, or nested NaN during serialization),
  the error artifact is written to a SEPARATE file
  (`signals_<date>.error.json`) — the last successful
  (`signals_<date>.json`) is never overwritten. A
  `latest_success.json` pointer file is updated only on successful
  artifact write, providing a stable reference for downstream consumers
  to find the last good signals. Each artifact includes a unique `run_id`
  for traceability.
- **Result validation timing**: the backtest result is validated
  IMMEDIATELY after `engine.run()` returns — before any printing or
  formatting — for type, finiteness, and presence of `final_assets`,
  `total_return`, `max_drawdown`, `sharpe`, and `total_trades`.
  Strict type checking rejects strings (even if float-convertible like
  `"1.23"`), `bool` (which is a subclass of `int` in Python), `None`,
  and non-dict results. This prevents `TypeError`/`KeyError` from
  None, string, or missing fields during f-string formatting.
- **Strict JSON compliance**: both risk state and JSON artifact are
  serialized with `allow_nan=False` to guarantee strict JSON (ECMA-404)
  output. Non-standard tokens (`NaN`, `Infinity`, `-Infinity`) are rejected
  at serialization time, not silently written as non-standard literals.
  This ensures downstream consumers using strict JSON parsers (e.g.
  JavaScript `JSON.parse`, Go `encoding/json`) can always parse the files.
- **Result validation before formatting**: the backtest result is validated
  IMMEDIATELY after `engine.run()` returns — before any printing or
  formatting — for type, finiteness, and presence of `final_assets`,
  `total_return`, `max_drawdown`, `sharpe`, and `total_trades`. Strict
  type checking rejects strings (even if float-convertible like `"1.23"`),
  `bool` (which is a subclass of `int` in Python), `None`, and non-dict
  results. If any field is invalid, an error artifact is written to a
  SEPARATE file (`signals_<date>.error.json`) and the scan exits with
  code 1. The last successful artifact (`signals_<date>.json`) is never
  overwritten. This catches upstream computation bugs before they reach
  signal consumers.
- **Risk state not injected into engine**: the daily scan replays the
  full history from `--start-date` to `--end-date` each time without
  passing the previous run's end-state to the engine. This prevents the
  time-direction error where a future末端 state (e.g.
  `terminal_risk_lock=True` from July 30) would change the past
  historical path when replaying from July 1. The saved
  `risk_state.json` is loaded for display and continuity checking only.
- **Risk state date validation**: the saved `scan_date` must be <= the
  requested `end_date`. Loading a risk state from a future date (e.g.
  August 1 state into a July 20 replay) is rejected (fail-closed, exit
  code 1) to prevent forward contamination / look-ahead bias.
- **Run ID consistency**: the artifact, `risk_state.json`, and
  `latest_success.json` pointer all share the same `run_id` for
  traceability — a single `run_id` is generated per run and passed to
  all three artifacts.
- **Reset-account safety**: `--account` is checked before `--reset-risk-state`
  in the CLI, so combining both flags does not silently delete the old risk
  state before the account error is reported. This is verified by real CLI
  integration tests using subprocess.
- **CLI integration tests**: the daily scan is tested through real subprocess
  calls that verify exit codes for `--account` rejection, `--reset-risk-state`
  safety, corrupted state fail-closed, schema-invalid state fail-closed, and
  unknown schema version rejection.
- **Last-good artifact protection tests**: failed runs (NaN/Inf, wrong-type
  fields, missing fields, nested NaN) are verified to write error artifacts
  to `.error.json` without overwriting the last successful
  `signals_<date>.json`. The `latest_success.json` pointer file is verified
  to update only on success and remain unchanged on failure.
- **State/artifact transaction tests**: nested NaN in `pending_signals` is
  verified to prevent risk state from being saved (state/artifact
  consistency). Artifact write failures are verified to exit with code 1
  and NOT save risk state (artifact-first transaction). Risk state save
  failures are verified to leave the artifact on disk with
  `risk_state_saved: false`.
- **Risk state date validation tests**: loading a risk state with
  `scan_date > end_date` is verified to be rejected (fail-closed) to
  prevent forward contamination. Same-day and past-date states are
  verified to be accepted.
- **Strict result validation tests**: strings (even float-convertible like
  `"1.23"`), `bool`, `None`, and non-dict results are verified to be
  rejected by the result validator. `total_trades` is verified to be
  checked for type (int, not bool/float/string) and non-negativity.
- **Run ID consistency tests**: the artifact, `risk_state.json`, and
  `latest_success.json` pointer are verified to all share the same
  `run_id`.
- **Risk state not injected tests**: `engine.run()` is verified to NOT
  receive a `risk_state` parameter, preventing the time-direction error.
- **Strict JSON artifact tests**: the artifact and risk state files are
  verified to never contain NaN/Infinity tokens as JSON values (using a
  strict constant-rejecting JSON parser). Error-only artifact production is
  tested when results contain NaN or Inf in `max_drawdown`, `total_return`,
  or `sharpe`. Valid results are verified to produce a normal `ok` artifact.

### Core API Account-State Guard

The public `run()` method in `BacktestEngine` raises `NotImplementedError`
when `account_state` is passed. This ensures the broken account injection
logic cannot be triggered by any caller — not just the daily scan CLI.
`_EnsembleBacktestEngine` inherits `run()` from `_CausalBacktestEngine`,
which does not accept an `account_state` parameter at all, so the guard is
redundant but harmless. The old injection code is retained as dead code
with deprecation comments pending the separate account signal engine.

### Real Account Mode (disabled)

The `--account` flag in `daily_signal_scan.py` is currently **disabled**. The
real-account integration has multiple architecture defects that prevent safe
use:

- Single-sleeve account snapshot is cleared by `_reset_run_state()` before
  the historical loop begins, so no real positions are actually injected.
- Three-sleeve ensemble mode mixes real and simulated ledgers — only the
  first sleeve receives real positions while the other two retain full
  simulated accounts.
- External-account liquidation signals carry `strategy=None`, which would
  crash the execution path (now defensively handled, but the broader
  architecture is still unsound).
- Peak equity is seeded before positions are injected, potentially
  triggering a false terminal lock on the first historical day.
- Full-investment accounts with zero cash cannot initialize the engine.
- Account-mode performance metrics (return, drawdown, Sharpe) have no
  economic meaning because the equity curve is a hybrid of simulated
  history and a late-injected real snapshot.

The correct fix is to build a separate account signal engine that does not
patch real-account state into the historical backtest state machine. Until
then, only simulation mode (default) should be used.
