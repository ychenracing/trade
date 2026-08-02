# Quant Fusion Strategy Review

Review date: 2026-07-22 UTC.

Baseline: `main` commit `89a4bdb`, CNY 2,000,000 initial capital, forward-adjusted
daily data, 0.1% one-way slippage, fixed virtual subaccounts, six-symbol portfolio
ceiling, and the frozen data snapshots through 2026-07-20.

## Decision

No proposed trading-rule change was sufficiently stable across time windows and
universe sizes to replace the current defaults. The production trading path is
therefore unchanged. The accepted changes improve measurement and routing safety:

- Calmar ratio is now included in engine results, console output, saved summaries,
  and canonical JSON artifacts.
- Auto-routed symbols without explicit metadata or a recognized name hint are
  listed in `unmapped_symbols` and produce a route warning. They still use the
  deterministic default profile; the fallback is no longer silent.
- This review records rejected experiments as well as favorable results, so later
  work does not rediscover an attractive but composition-sensitive parameter.

## Proposal audit

| Proposal | Verdict | Evidence |
|---|---|---|
| Portfolio volatility targeting | Rejected | Scaling only new orders cut the 22-symbol return from 850.11% to 588.47%, while drawdown changed from 16.90% to 16.94%. The 21-symbol weak-period drawdown worsened to 29.21%. A true active exposure overlay would be a different strategy and needs independent validation. |
| Cross-sectional top-K selection | Already implemented | Large universes already use causal multi-horizon risk-adjusted momentum, a fixed reference percentile, and a global top-six admission rule. |
| Adaptive position count | Rejected | The portfolio ceiling of six is an intentional concentration constraint. Earlier size tests and the complete one-through-22 prefix audit do not support `N // 3 + 1` or another universal formula. |
| Heterogeneous mean-reversion and volatility sleeves | Research only | The current sleeves diversify allocation and candidate horizons but retain one trend contract. Adding new strategy families is not a defect fix and has no supplied out-of-sample evidence. |
| Kelly sizing | Rejected | The cited strategy win rates contain too few independent trades. Online Kelly estimates would be unstable and could increase exposure after a lucky run. |
| Gradual sector exposure | Research only | The current guard deliberately liquidates after repeated shocks and requires repeated recovery. Partial portfolio reductions need explicit lot attribution, T+1 execution, recovery, and audit semantics; a scalar sketch is insufficient. |
| Replace the regime basket with current holdings or high-ADV symbols | Rejected | The existing basket is fixed and signal-only. Its observations do not depend on trades or on which tradable symbols were supplied. Selecting references from each run would make the risk regime composition-dependent. |
| Continuous signal-strength fusion | Rejected | The proposal refers to signal metadata that does not exist, cannot use near-miss signals because only triggered buys enter fusion, and reduces a full confirmation from the current 1.1 scale to at most 1.0. |
| Volume-weighted momentum rank | Rejected | The 22-symbol return fell to 787.81%; the weak-period return fell to 6.75% and drawdown worsened to 28.98%. Volume expansion was not a reliable trend-quality proxy. |
| Time-decaying trailing stops | Rejected | The 5-symbol return fell to 902.96% and drawdown rose to 17.63%; the 22-symbol return fell to 765.48% and drawdown rose to 20.61%. The rule removed profitable trend tails. |
| Partial profit taking | Rejected | The example marks `close <= entry_price` as profitable and is internally inconsistent. More importantly, systematic partial exits can truncate the fat-tail gains that fund a trend strategy. |
| Automatic profile classification from full-history volatility | Rejected | Full-history classification introduces look-ahead bias, and volatility is not an industry label. Deterministic fallback plus explicit route auditing is safer. |
| Dynamic warm-up and global cold fallback | Rejected | Indicators already use each symbol's available pre-start history. One newly listed stock should not force every other symbol into cold state. Missing period data is rejected explicitly. |
| Walk-forward validation | Accepted as a process requirement | The review used the target period, a 2024 weak period, 2025 half-year splits, a 2026 recent window, and one-through-22 prefixes. These reduce false confidence but are not claimed as fully independent out-of-sample evidence. |

## Controlled experiment comparison

All values below use the same data, costs, execution path, and capital. Returns and
drawdowns are shown as percentages. `Recent 6` is 2026-01-05 through 2026-07-20;
`Weak 21` is 2024-01-02 through 2025-03-31 and excludes not-yet-listed `920045`.

| Candidate | 5-symbol return / DD | 22-symbol return / DD | Weak 21 return / DD | Recent 6 return / DD |
|---|---:|---:|---:|---:|
| Baseline | 1,145.48 / -14.94 | 850.11 / -16.90 | 8.05 / -28.39 | 250.45 / -10.30 |
| Strict short-MA pyramid guard | 1,168.23 / -14.94 | 869.41 / -16.90 | 7.65 / -28.38 | 250.45 / -10.30 |
| 0.90 pyramid risk decay | 1,143.94 / -14.94 | 868.80 / -16.90 | 8.13 / -28.34 | 250.45 / -10.30 |
| Time-decaying trailing stop | 902.96 / -17.63 | 765.48 / -20.61 | 8.46 / -28.12 | 252.56 / -10.30 |
| Volume-weighted ranking | 1,145.48 / -14.94 | 787.81 / -17.10 | 6.75 / -28.98 | 250.45 / -10.30 |
| New-order volatility scalar | 802.68 / -17.01 | 588.47 / -16.94 | 6.26 / -29.21 | 184.65 / -11.44 |
| Price-below-falling-MA guard | 1,145.48 / -14.94 | 864.02 / -16.90 | 8.05 / -28.39 | 250.45 / -10.30 |

The strict moving-average guard looked favorable in the headline 5-, 13-, and
22-symbol warm scenarios, but the complete prefix audit showed a 56.24 percentage
point return loss for two symbols and a drop below the existing 800% floor at 16
symbols. The weaker falling-MA rule still reduced returns materially at two, 17,
and 18 symbols. Neither was promoted to a default.

## Practical implication

The best supported route to higher return with controlled drawdown is not another
in-sample scalar. It is to preserve the six-position causal execution model, select
a compact and economically coherent tradable universe, keep the fixed independent
regime basket, and require future trading-rule changes to pass the same multi-window
and complete-prefix comparison before release.

## Code quality and reliability improvements

The following infrastructure and reliability improvements were made after an
external code audit. None of these changes alter the trading path, signal logic,
or backtest results:

- **Strict JSON compliance**: both risk state and JSON artifact are serialized
  with `allow_nan=False` to guarantee strict JSON (ECMA-404) output.
  Non-standard tokens (`NaN`, `Infinity`) are rejected at serialization time.
- **Result validation before artifact**: the backtest result is validated for
  finite values before constructing the artifact. Invalid results produce an
  error-only artifact and exit code 1.
- **Pre-save validation**: NaN/Inf in `max_drawdown`, `total_return`, or
  `final_assets`, and negative `cycle_lock_count`, are rejected at write time.
- **Schema validation**: all required fields are validated for correct types,
  finiteness, non-negativity, date format (`YYYY-MM-DD`), and string format
  (`symbols_hash` must be 16-char hex). Unknown schema versions are rejected.
- **CI pipeline**: all checks are gating — ruff, bandit, pip-audit, pyright,
  pytest auto-discovery, and a dedicated backtest regression job that verifies
  multi-universe warm metrics (1, 3, 5, 13, and 22-symbol — return,
  drawdown, trade count) against frozen baselines.
- **CLI integration tests**: exit codes verified via real subprocess calls for
  `--account` rejection, `--reset-risk-state` safety, corrupted state
  fail-closed, schema-invalid state fail-closed, and unknown schema version.
- **Last-good artifact protection**: failed runs (NaN/Inf, wrong-type fields,
  missing fields, nested NaN) write error artifacts to `.error.json` — the
  last successful `signals_<date>.json` is never overwritten. A
  `latest_success.json` pointer file is updated only on success.
- **State/artifact transaction safety**: the JSON artifact is written to
  disk BEFORE risk state is saved (artifact-first transaction ordering).
  If the artifact write fails, no risk state has been committed —
  preventing state/artifact inconsistency. If the risk state save
  subsequently fails, the artifact already exists with
  `risk_state_saved: false`.
- **Risk state not injected into engine**: the daily scan replays the
  full history from `--start-date` to `--end-date` each time. The
  previous run's end-state (e.g. `terminal_risk_lock=True` from July 30)
  is NOT injected into the engine — this prevents the time-direction
  error where a future末端 state would change the past historical path.
  The saved `risk_state.json` is loaded for display and continuity
  checking only.
- **Risk state date validation**: the saved `scan_date` must be <= the
  requested `end_date`. Loading an August 1 risk state into a July 20
  replay is rejected (fail-closed, exit code 1) to prevent forward
  contamination.
- **Run ID consistency**: the artifact, `risk_state.json`, and
  `latest_success.json` pointer all share the same `run_id` for
  traceability.
- **Result validation timing**: the backtest result is validated immediately
  after `engine.run()` returns, before any printing or formatting, for
  type, finiteness, and presence of `final_assets`, `total_return`,
  `max_drawdown`, `sharpe`, and `total_trades`. Strict type checking
  rejects strings (even if float-convertible like `"1.23"`), `bool`
  (which is a subclass of `int`), `None`, and non-dict results.
