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
  + indicator_state (cash/capital is excluded because it changes daily)
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
  or fails schema validation (wrong field types), the scan exits with code 1
  instead of silently discarding the previous terminal lock state.
- **Schema validation**: all required fields are validated for correct types:
  `schema_version` (int, must be known version), `scan_date` (str),
  `terminal_risk_lock` (bool), `sector_guard_active` (bool),
  `cycle_lock_count` (int), `max_drawdown` (number), `total_return` (number),
  `final_assets` (number). Strings like `"false"` (which are truthy in
  Python) are rejected. Unknown `schema_version` values are rejected to
  enforce forward compatibility.
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
- **State-before-artifact ordering**: risk state is saved before the JSON
  artifact. If the state save fails (e.g. disk full), the artifact includes
  `risk_state_saved: false` and an error message so the user knows the state
  was not persisted.
- **Reset-account safety**: `--account` is checked before `--reset-risk-state`
  in the CLI, so combining both flags does not silently delete the old risk
  state before the account error is reported.

### Core API Account-State Guard

All public `run()` methods in `BacktestEngine` and `_EnsembleBacktestEngine`
raise `NotImplementedError` when `account_state` is passed. This ensures the
broken account injection logic cannot be triggered by any caller — not just
the daily scan CLI. The old injection code is retained as dead code with
deprecation comments pending the separate account signal engine.

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
