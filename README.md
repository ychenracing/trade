# Quant Fusion

Quant Fusion is a standalone deterministic daily-bar backtester for
concentrated A-share technology portfolios. The complete data-loading, signal,
T+1 execution, transaction-cost, exposure, causal-liquidity, allocation, and risk
implementation is contained in `quant_fusion.py`; it does not import or
require another strategy module at runtime.

## Design

- Tradable symbols and regime-observation symbols are separate. The default
  signal-only regime basket is `300308`, `300502`, `300394`, `688008`, and
  `603986`; these symbols are never traded unless the caller also includes them
  in the tradable universe.
- The same fixed basket is the reference distribution for 10-, 20-, 40-, and
  80-day risk-adjusted momentum. Existing scores therefore do not change merely
  because a tradable symbol is added or removed.
- When the universe has no more than six symbols, all symbols remain eligible
  and their independent entry signals decide whether to trade. Above six, a
  symbol must reach the 50th reference percentile before it can consume one of
  the six candidate slots.
- One- and two-symbol universes have little useful cross-sectional information.
  The engine therefore switches every sleeve to a slower time-series trend contract
  without changing the 60% symbol exposure ceiling.
- Forced industry-group slots are disabled. The synchronized portfolio
  coordinator admits at most six distinct symbols across all sleeves, not six
  per sleeve, and can ignore weaker additions to a large universe.
- Allocation horizons are `(3, 5, 10)`, `(5, 10, 20)`, and `(5, 20, 60)` days.
  Candidate horizons are `(10, 20, 40)`, `(10, 20, 40)`, and `(10, 40, 80)`;
  the long sleeve reduces dependence on one ranking horizon.
- The confirmed cycle-drawdown threshold is `23% - 2% / N`, where `N` is the
  number of tradable symbols. A confirmed cycle lock waits ten trading days in
  cash and may then rearm.
- The emergency cycle threshold is `27% - 2% / N`. A separate 28% lifetime
  drawdown boundary is terminal for the affected sleeve.
- The 18% shadow alert remains audit-only. Orders still execute no earlier than a
  later tradable open, so gaps can exceed a decision threshold.
- The three sleeves retain fixed virtual subaccounts, so unused cash is not
  borrowed by another sleeve. They advance on one shared trading calendar: all
  sells execute before globally ranked new symbols are admitted, then buys run.
- The portfolio ADV budget is 0.5% of the prior 20 daily volumes. Each sleeve
  receives one-third and all same-date, same-symbol, same-side orders consume a
  shared balance; no strategy order receives a fresh capacity allowance.
- Same-symbol strategy confirmations share cash, exposure, and ADV capacity
  proportionally before execution. Board-lot rounding can create at most a
  one-lot attribution difference. Batch validation applies the strictest
  strategy exposure cap and rejects mixed-symbol or mixed-price batches.
- Buy rejections identify the concrete failed execution check. If a
  portfolio-level liquidation supersedes an existing strategy sell, the prior
  reason and requested size remain in the order audit trail.
- The breadth guard requires four of five regime symbols. Missing observations
  are audited and pause recovery without erasing prior shock confirmations or
  releasing an active guard.
- Open positions are marked to market at the requested end date. The engine no
  longer offers a synthetic same-close period-end liquidation.
- Calmar is reported beside return, drawdown, and Sharpe in in-memory results,
  console summaries, saved CSV summaries, and canonical JSON artifacts.
- A symbol without explicit routing metadata or a recognized name hint now
  raises `RuntimeError` by default (`strict_unmapped: True`) instead of silently
  falling back to the default trend profile. All 26 universe symbols are
  explicitly mapped in `_SYMBOL_GROUP`, `_SYMBOL_PROFILE`, and
  `_KNOWN_CLASSIFICATION`. Disable with `strict_unmapped: False` for research.

Universe-size robustness does not mean composition invariance. A universe that
removes the strongest underlying assets cannot be guaranteed the return of a
universe that contains them. The fixed default regime basket is appropriate for
the bundled technology-hardware and semiconductor domain; another investment domain
should supply its own stable benchmark basket before live use.

## Causal regime-adaptive deployment

`regime_adaptive.py` is an orchestration layer around the frozen production
engine. It makes one auditable decision using only observations before the
requested start date:

- Both fixed, non-trading indices (`000300`, `000682`) must have MA20 above
  MA60 to route into the original `BacktestEngine` unchanged.
- Otherwise it ranks the requested pool by positive 240-session momentum,
  selects at most three leaders, and enters once at the next tradable open.
- Each leader is capped at 59%. A 3-ATR chandelier becomes active only after a
  30% peak gain; after exit the symbol is not re-entered in that selection
  period.
- Missing, stale or insufficient evidence fails closed. If no positive leader
  is observable, the policy holds cash.
- Signals still obey the existing T+1, limit, board-lot, fee, slippage, ADV and
  single-account execution contracts.

This layer is for daily manual decision support, not broker automation. Its
decision and evidence are included in backtest results and daily signal JSON.

## Repository layout

- `quant_fusion.py`: complete standalone production engine and CLI.
- `quant_fusion_optimizer.py`: independent automatic parameter-search,
  walk-forward validation, Pareto selection, and final-holdout reporting layer.
- `test_quant_fusion.py`: unit, isolation, routing, risk, and regression
  tests.
- `test_quant_fusion_optimizer.py`: leakage, constraint, parameter-support,
  dynamic-listing, and deterministic-search tests.
- `regime_adaptive.py`: causal regime router and low-turnover weak-market
  strategy; it does not modify the frozen trend engine.
- `run_regime_validation.py`: deterministic development, comparison, blind
  holdout, prior-year and bull-golden validation protocol.
- `test_regime_adaptive.py`: causal-boundary, stale-evidence, late-IPO,
  real-execution and golden-regression tests.
- `test_daily_signal_scan.py`: account loading, risk state persistence,
  signal classification, position reconstruction, schema validation
  (type, finite, range, version), pre-save NaN/Inf rejection, strict JSON
  compliance (artifact and risk state), error-only artifact on invalid
  results, buy suppression with blocked-signal separation, CLI integration
  via subprocess (exit codes, `--account` rejection, `--reset-risk-state`
  safety, corrupted state fail-closed), symbol mapping tests, risk state
  date validation (forward contamination prevention), strict result type
  checking (rejects strings, bool, None, non-dict), artifact-first
  transaction ordering, run_id consistency, and risk state not injected
  into engine (time-direction error prevention).
- `backtest_universes.py`, `stress_test_prefixes.py`, and
  `backtest_cambricon_universe.py`: reproducible portfolio checks.
- `daily_signal_scan.py`: daily signal scan for the 26-stock AI sector universe
  with stale-data fail-closed and risk state persistence. Real-account
  integration (`--account`) is currently disabled pending reconstruction as a
  separate account signal engine.
- `market_data`:
  canonical forward-adjusted snapshots required by the regression suite.
- `historical_data`: frozen 2022–2026 qfq research snapshot for 16 overlapping
  AI symbols plus two non-trading indices, with manifest and SHA-256 checks.
- `regime_validation_results.json`: per-pool machine-readable adaptive
  validation evidence.
- `universe_backtest.json`, `prefix_stress.json`, and
  `cambricon_universe_backtest.json`: canonical result artifacts.
- `STRATEGY_REVIEW.md`: proposal audit, controlled experiments, and
  release decisions.

## Data

The reproducible 23-symbol Eastmoney forward-adjusted snapshot is stored in
`market_data` (22 symbols with full history from 2024-01-01 plus `920045`
which begins on 2025-12-31). Provider volume is converted from board lots to
shares before the ADV participation rule is applied. `SHA256SUMS` freezes the
exact CSV bytes, and the regression suite fails if a file is truncated or
modified.

`688256` 寒武纪 is explicitly classified as `semiconductor`, assigned to the
`domestic_semiconductor` risk group, and routed through the `domestic_design`
parameter profile. The CLI also recognizes its preset stock name.

The extended `historical_data` snapshot preserves the canonical `market_data`
tail on every common date. It is used for 2022–2024 research and fixed-index
evidence only; see `historical_data/README.md` for provenance and limitations.

The engine supports two explicit data paths:

- Omit `--data-dir` to fetch forward-adjusted daily data through AKShare. The
  online loader tries Eastmoney, Sina, and Tencent in deterministic failover
  order and validates every returned frame.
- Pass `--data-dir PATH` to read `PATH/<symbol>.csv`. Local files must contain
  forward-adjusted daily OHLCV data and pass the same validation contract.

## Quick start

```bash
python -m pip install -r requirements.txt
python quant_fusion.py \
  --start 2025-04-01 \
  --end 2026-07-20 \
  --capital 2000000 \
  --data-dir market_data \
  --indicator-state warm \
  --no-plot
```

For online data, omit the local directory option:

```bash
python quant_fusion.py \
  --start 2025-04-01 \
  --end 2026-07-20 \
  --indicator-state warm \
  --no-plot
```

Run the full regime validation and rebuild the machine-readable report:

```bash
python run_regime_validation.py --workers 4
```

The final blind seed and pool generator are committed in the script. The blind
set was first opened after the strategy constants and tests were frozen; later
runs reproduce that fixed protocol and must not be treated as new holdouts.

The CLI defaults to warm indicator state because a live strategy normally has
pre-start history. Cold state remains available for initialization sensitivity
tests. Both states start the portfolio flat on the requested first date.

### Risk state continuity

The `BacktestEngine.run()` method accepts an optional `risk_state` parameter
that restores risk management state from a previous run. However, the daily
scan does **NOT** use this parameter — see below.

```python
# The engine *can* accept risk_state, but the daily scan does NOT pass it.
result = engine.run(
    symbols_dict, start_date, end_date,
    # risk_state is intentionally omitted by the daily scan
)
```

**Risk state is NOT injected into the engine.** The daily scan replays the
full history from `--start-date` to `--end-date` every time. Injecting the
previous run's end-state (e.g. `terminal_risk_lock=True` from July 30) into
a fresh replay starting from July 1 would create a time-direction error:
the future末端 state would change the past historical path. Instead, the
engine independently rebuilds all risk states from the actual historical
data. The saved `risk_state.json` is loaded for **display and continuity
checking only** — it shows the user what the previous run detected, but
does NOT influence the current backtest.

**Risk state date validation:** the saved `scan_date` must be <= the
requested `end_date`. This prevents forward contamination — e.g. loading an
August 1 risk state into a July 20 replay would be direct look-ahead bias.
Violations are rejected (fail-closed, exit code 1).

Risk state includes `schema_version` for forward compatibility. Unknown
schema versions are rejected (fail-closed) to prevent misinterpreting fields
with changed semantics. All state fields are type-validated on load:
`schema_version` (int), `scan_date` (str), `terminal_risk_lock` (bool),
`sector_guard_active` (bool), `cycle_lock_count` (int, non-negative),
`max_drawdown` (finite number), `total_return` (finite number),
`final_assets` (finite number, non-negative). Numeric values are also
validated before saving — NaN/Inf and negative `cycle_lock_count` are
rejected at write time.

When the risk state identity hash does not match (different symbol set,
count, or configuration), buy signals are suppressed (fail-closed). In the
JSON artifact, blocked buy signals are placed in a separate
`blocked_signals` list — `pending_signals` only contains executable signals,
so downstream consumers that check `direction` on `pending_signals` will
never see blocked buys. The old risk state is preserved (not overwritten)
until the user runs `--reset-risk-state`.

**Artifact-first transaction ordering:** the JSON artifact is written to
disk BEFORE risk state is saved. This is a true two-phase commit: if the
artifact write fails, no risk state has been committed — preventing
state/artifact inconsistency. If the risk state save subsequently fails,
the artifact already exists on disk with `risk_state_saved: false` and an
error message, and the scan exits with code 1.

Both risk state and JSON artifact are serialized with `allow_nan=False` to
guarantee strict JSON (ECMA-404) output. The backtest result is validated
IMMEDIATELY after `engine.run()` returns — before any printing or
formatting — for type, finiteness, and presence of `final_assets`,
`total_return`, `max_drawdown`, `sharpe`, and `total_trades`. Strict type
checking rejects strings (even if float-convertible like `"1.23"`), `bool`
(which is a subclass of `int` in Python), `None`, and non-dict results.
When any field is invalid, an error artifact is written to a SEPARATE file
(`signals_<date>.error.json`) and the scan exits with code 1. The last
successful artifact (`signals_<date>.json`) is never overwritten by an
error artifact. A `latest_success.json` pointer file is updated only on
successful artifact write, providing a stable reference for downstream
consumers to find the last good signals. The artifact, `risk_state.json`,
and `latest_success.json` all share the same `run_id` for traceability.

## Automatic parameter optimization

The optimizer preserves `quant_fusion.py` as the only execution engine. It
changes no signal, fill, T+1, cost, liquidity, or accounting code. It searches
portfolio controls and route-preserving multipliers around each stock's existing
industry profile, so an optical-module stock and semiconductor-equipment stock do
not silently receive one identical absolute parameter set.

The default protocol enforces these hard limits before a candidate can run:

- maximum symbol weight: 60%;
- maximum total weight: 100%;
- maximum concurrent symbols: six;
- maximum validation and higher-cost validation drawdown: 20%.

Selection uses expanding training windows followed by non-overlapping validation
windows. It penalizes performance instability and training-to-validation
degradation, rejects isolated parameter spikes without a one-axis neighbor, and
chooses from the validation return/drawdown Pareto frontier. The final holdout is
run only after parameter selection. It cannot choose a different parameter
candidate, but it is a mandatory one-time promotion gate: a candidate is not
recommended if ordinary or stressed holdout drawdown breaches 20%, or if its
return falls below the already-feasible production baseline.

Example for the five-symbol reference universe:

```bash
python quant_fusion_optimizer.py \
  --symbol 300308,300502,300394,688008,603986 \
  --data-dir market_data \
  --start 2024-01-02 \
  --test-start 2026-01-05 \
  --end 2026-07-20 \
  --train-months 12 \
  --validation-months 6 \
  --step-months 6 \
  --candidates 40 \
  --seed 17 \
  --output-dir optimizer_output
```

The output directory contains:

- `optimization_report.json`: every candidate, fold, rejection, Pareto result,
  cost stress, and untouched final-holdout comparison;
- `recommended_config.json`: the selected compact modifiers plus materialized
  per-symbol configuration that can be passed back to the engine;
- `optimization_summary.md`: concise baseline-versus-selected holdout results.

Local data is mandatory for optimization so hundreds of candidate runs share one
frozen snapshot. A stock with no observations in an early fold is excluded from
that fold and becomes eligible only after it has enough historical rows; the
optimizer never backfills a pre-listing period. A custom finite search space can
be supplied with `--search-space`. Historical best parameters remain research
results, not a promise of future optimality.

### Frozen five-symbol validation on 2026 data

The canonical 40-candidate run used 2024-01-02 through 2025-12-31 only for
parameter selection and first opened 2026-01-05 through 2026-07-20 after the
validation winner was frozen. The research winner combined a three-position
ceiling, the moderate portfolio-risk bundle, and a 0.8 per-route risk multiplier.
It passed the pre-test 20% constraint but failed the one-time promotion gate:

| Final holdout | Total return | Maximum drawdown | Sharpe | Calmar |
|---|---:|---:|---:|---:|
| production baseline | 152.8439% | -11.3952% | 3.5943 | 44.2142 |
| validation winner | 124.9701% | -11.1156% | 3.2199 | 34.3196 |

The 0.28 percentage-point drawdown improvement did not justify 27.87 percentage
points less return, so `recommended_config.json` retains the production baseline. The
complete evidence is stored in
`optimizer_validation/optimization_report.json`; the concise
comparison is in the adjacent `optimization_summary.md`. No further parameter
tuning used the opened 2026 holdout.

## Cross-universe target results

Initial capital is CNY 2,000,000. All scenarios use identical strategy parameters,
default costs, 0.1% one-way slippage, and data through 2026-07-20.

| Tradable universe | Cold return | Cold max drawdown | Warm return | Warm max drawdown | Warm Sharpe | Warm Calmar | Warm trades |
|---|---|---|---:|---:|---:|---:|---:|
| 1 symbol | 536.66% | -18.49% | 530.89% | -18.34% | 3.21 | 18.23 | 24 |
| 3 symbols | 1059.72% | -18.32% | 1083.70% | -17.92% | 3.69 | 34.47 | 194 |
| 5 symbols | 1078.67% | -16.80% | 1115.99% | -15.86% | 3.70 | 39.93 | 222 |
| 13 symbols | 894.16% | -16.93% | 1038.74% | -18.41% | 3.61 | 32.37 | 324 |
| 22 symbols | 843.49% | -17.13% | 983.57% | -16.22% | 3.76 | 35.07 | 244 |

The requested multi-symbol warm wealth-factor ratio between the worst and best
universe is 88.13% (22-symbol vs 5-symbol). The exhaustive ordered-prefix audit
also tests every count from one through 22. The one-symbol result remains
structurally lower because the 60% symbol cap leaves at least 40% cash at entry
and provides no rotation opportunity. The sub-20% drawdown claim applies to the
five requested 1-, 3-, 5-, 13-, and 22-symbol warm scenarios, not every possible
composition. Exact scenario metadata is stored in `BACKTEST_RESULTS.md` and
`universe_backtest.json`; the prefix audit is stored in `prefix_stress.json`.

## Regime-adaptive validation

The frozen 2024 protocol improves the 16 deterministic pools from 43.75%
profitable and -2.63% median return to 87.50% and +22.35%. The final 26-pool
seed `20260805`, opened after rule freeze, is 96.15% profitable, 100% non-loss
and +24.84% median return. Its worst drawdown is 31.68%, so the release does not
claim a hard 20% drawdown ceiling or future mathematical optimality. Complete
per-pool evidence and limitations are in `REGIME_ADAPTIVE_REFACTOR_REPORT.md`
and `regime_validation_results.json`.

## Verification

Run the standard-library regression suite and the complete cross-universe runner:

```bash
python -m py_compile quant_fusion.py quant_fusion_optimizer.py daily_signal_scan.py \
  regime_adaptive.py run_regime_validation.py
python -m pytest -v --tb=short
python run_regime_validation.py --workers 4
python backtest_universes.py
python stress_test_prefixes.py
python backtest_cambricon_universe.py
```

A GitHub Actions CI workflow (`.github/workflows/ci.yml`) runs the full test
suite (auto-discovered via pytest, including optimizer tests) on Python 3.11
and 3.12, plus ruff linting, bandit security scanning, pip-audit dependency
vulnerability scanning, pyright type checking, and a dedicated backtest
regression job that verifies multi-universe warm metrics (1, 3, 5, 13, and
22-symbol — return, drawdown, trade count) against frozen baselines. All
checks are gating — a lint, security, type, test, or regression failure
blocks the pipeline.

The tests cover standalone isolated startup, absence of external imports,
AKShare provider failover, strict local CSV selection, policy validation,
concentration scaling, temporary rearming, terminal locks, signal immutability,
fair same-symbol allocation, cumulative ADV accounting, missing-data guard
quorum, strict batch exposure, low-price minimum-fee affordability, detailed
rejection auditing, data snapshot checksums, signal-only basket isolation, the
portfolio-wide six-symbol ceiling, all five requested universe sizes, sub-20%
drawdown for the requested warm universes, Cambricon's complete route, the
2026-06-26 regime-gate event, the complete one-through-22 prefix artifact, and
the mapped nine-symbol Cambricon artifact. It also checks deterministic detection
of unmapped auto routes and the presence of positive Calmar values in successful
target-period runs. The daily-signal-scan tests verify account JSON loading,
risk state persistence round-trips, corrupted state fail-closed behavior,
same-day rerun preservation, signal classification, position
reconstruction from trade ledgers, schema validation (type, finite, range,
and version checks), pre-save NaN/Inf rejection, buy suppression with
blocked-signal separation, strict JSON compliance (artifact and risk state
must never contain NaN/Infinity tokens as JSON values), error-only
artifact production when results contain NaN/Inf, CLI integration via
subprocess (exit codes, `--account` rejection, `--reset-risk-state`
safety, corrupted state fail-closed), and that all 26 universe symbols
are explicitly mapped. They also verify last-good artifact protection
(failed runs write to `.error.json` and never overwrite the last
successful `signals_<date>.json`), nested NaN detection (state is not
saved when artifact serialization fails), `latest_success.json` pointer
file updates, wrong-type/missing field error artifacts, and artifact
write failure exit codes. Additional tests verify: risk state is NOT
injected into `engine.run()` (prevents time-direction error), risk state
date validation (rejects `scan_date > end_date` to prevent forward
contamination), strict result type checking (rejects strings, bool, None,
non-dict results, and validates `total_trades`), artifact-first transaction
ordering (artifact written before risk state, no state saved on artifact
write failure), and run_id consistency (artifact, risk state, and
latest_success pointer share the same run_id). The
quant-fusion tests include end-to-end coverage of external-account sell
execution with `strategy=None` and API contract enforcement (`account_state`
raises `NotImplementedError`).

## Daily signal scan

`daily_signal_scan.py` fetches the latest forward-adjusted data via AKShare
(with incremental cache), runs the Quant Fusion backtest, and extracts the
latest pending signal (buy / sell / hold) for each of the 26 AI-sector symbols.

### Modes

- **Simulation mode** (default): runs a fresh backtest from `--start-date`.
  Signals reflect what the strategy *would have* done, not your real portfolio.
- **Account mode** (`--account account.json`): **currently disabled**. The
  real-account integration has multiple architecture defects (single-sleeve
  snapshot cleared by reset, three-sleeve mixed ledger, external liquidation
  crash, peak-equity timing errors, zero-cash initialization failure, and
  meaningless performance metrics). It will be re-enabled as a separate
  account signal engine that does not patch real-account state into the
  historical backtest state machine.

### Stale data fail-closed

If any symbol's cached data is stale (network fetch failed) or data end dates
are inconsistent across symbols, the scan refuses to produce signals and exits
with code 1. Override with `--allow-stale` only when you understand the risk;
stale-data signals must not be used for live trading decisions.

### Causal deployment route

The daily scan defaults to `--deployment-mode auto`. Fixed-index regime
evidence comes from `historical_data`; leader momentum comes from the same
incremental qfq cache already refreshed during the tradability pre-screen. The
JSON artifact records the route, boundary, fixed-index observations, requested
pool, selected leaders and unavailable symbols under `deployment`.

If index evidence is stale or incomplete, the outer router fails closed into
the weak policy and may hold cash. `--deployment-mode trend` and
`--deployment-mode weak` are diagnostic overrides, not tuning switches for
choosing whichever historical result is better.

### Risk state persistence

After each run, risk state (`terminal_risk_lock`, `sector_guard_active`,
`max_drawdown`, `cycle_lock_count`) is saved to `risk_state.json` in the
output directory with enhanced identity fields (`schema_version`,
`symbols_hash`, `run_id`). Identity uses stable fields only (symbol set +
count + start date + indicator state + capital + warmup days). Capital is
included because different capital means different position sizing and risk
exposure. On the next run, the previous state is loaded and displayed
for continuity checking — if the previous run had an active terminal lock or
sector guard, a warning is shown even if the current backtest doesn't detect
it (because the backtest starts fresh each time). Old risk state files
without `symbols_hash` are rejected (fail-closed) to prevent
cross-contamination between different universes or configurations.

Risk state writes are atomic (temp file + `os.replace`) to prevent corruption
from disk full, process kill, or power loss. The JSON artifact is also
written atomically and is committed to disk BEFORE risk state (artifact-first
transaction). Both risk state and artifact are serialized with
`allow_nan=False` to guarantee strict JSON (ECMA-404) output. Corrupted
risk state files, or files that fail schema validation (wrong field types,
NaN/Inf, negative values, unknown schema version), cause the scan to exit
with code 1 rather than silently discarding terminal lock state. Risk state
date validation ensures `scan_date <= end_date` — loading a future-dated
state is rejected (fail-closed) to prevent forward contamination. Same-day reruns preserve the previous state so terminal
lock and sector guard continuity is maintained. Numeric values are validated
before saving — NaN/Inf and negative `cycle_lock_count` are rejected at write
time to prevent creating an invalid state file. If the risk state save fails
(disk full, permission error, invalid values), the scan exits with code 1 so
scripts and schedulers can detect the failure. The artifact, `risk_state.json`,
and `latest_success.json` pointer all share the same `run_id` for traceability.

When the identity hash does not match (different symbol set, count, or
configuration), buy signals are suppressed (fail-closed) to prevent entering
new positions without verified risk-state continuity. In the display, pure
buy signals become "观望 (风险状态不匹配)" and mixed buy/sell signals show
only the sell part with "[买入已抑制]". In the JSON artifact, blocked buy
signals are marked with `blocked=true` and `executable=false`. The old risk
state is NOT overwritten — use `--reset-risk-state` to intentionally
establish a new identity after a configuration change.

### Core API account-state guard

The public `run()` method in `BacktestEngine` raises `NotImplementedError`
when `account_state` is passed, ensuring the broken account injection logic
cannot be triggered by any caller — not just the daily scan CLI.
`_EnsembleBacktestEngine` inherits `run()` from `_CausalBacktestEngine`,
which does not accept an `account_state` parameter at all.

### Usage

```bash
# Simulation mode (default)
python daily_signal_scan.py [--end-date YYYY-MM-DD] [--cache-dir DIR] [--capital N]

# Override capital and start date
python daily_signal_scan.py --capital 1500000 --start-date 2026-06-01

# Diagnostic route override and alternate fixed-index snapshot
python daily_signal_scan.py --deployment-mode weak \
  --regime-data-dir historical_data

# Reset risk state after intentionally changing configuration
python daily_signal_scan.py --reset-risk-state
```

### Account JSON format (reference only — `--account` is disabled)

The `account_example.json` file documents the intended schema for future
real-account integration. The `--account` flag currently exits with an
error message explaining the architecture defects that must be resolved
before this mode can be safely used.

## Important assumptions

- Signals use information available at the current close and execute no earlier
  than a later tradable open.
- The simulator includes commission, minimum commission, stamp duty, slippage,
  A-share lot sizing, approximate board-limit handling, and prior-volume ADV caps.
- It does not reproduce opening-auction depth, queue priority, intraday impact, or
  guaranteed exits during continuous limit-down sequences.
- The target period and the default regime basket were used during development.
  Cross-universe regression reduces one form of overfitting but is not independent
  out-of-sample evidence.
- Results are deterministic historical simulations, not future-return guarantees
  or investment advice.
