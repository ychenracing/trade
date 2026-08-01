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

## Repository layout

- `quant_fusion.py`: complete standalone production engine and CLI.
- `quant_fusion_optimizer.py`: independent automatic parameter-search,
  walk-forward validation, Pareto selection, and final-holdout reporting layer.
- `test_quant_fusion.py`: unit, isolation, routing, risk, and regression
  tests.
- `test_quant_fusion_optimizer.py`: leakage, constraint, parameter-support,
  dynamic-listing, and deterministic-search tests.
- `test_daily_signal_scan.py`: account loading, risk state persistence,
  signal classification, position reconstruction, and symbol mapping tests.
- `backtest_universes.py`, `stress_test_prefixes.py`, and
  `backtest_cambricon_universe.py`: reproducible portfolio checks.
- `daily_signal_scan.py`: daily signal scan for the 26-stock AI sector universe
  with stale-data fail-closed and risk state persistence. Real-account
  integration (`--account`) is currently disabled pending reconstruction as a
  separate account signal engine.
- `market_data`:
  canonical forward-adjusted snapshots required by the regression suite.
- `universe_backtest.json`, `prefix_stress.json`, and
  `cambricon_universe_backtest.json`: canonical result artifacts.
- `STRATEGY_REVIEW.md`: proposal audit, controlled experiments, and
  release decisions.

## Data

The reproducible 22-symbol Eastmoney forward-adjusted snapshot is stored in
`market_data`. Provider volume is converted from board lots to
shares before the ADV participation rule is applied. `920045` begins on
2025-12-31 and is unavailable before that date. `SHA256SUMS` freezes the exact
CSV bytes, and the regression suite fails if a file is truncated or modified.

`688256` 寒武纪 is explicitly classified as `semiconductor`, assigned to the
`domestic_semiconductor` risk group, and routed through the `domestic_design`
parameter profile. The CLI also recognizes its preset stock name.

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

The CLI defaults to warm indicator state because a live strategy normally has
pre-start history. Cold state remains available for initialization sensitivity
tests. Both states start the portfolio flat on the requested first date.

### Risk state continuity

The `BacktestEngine.run()` method accepts an optional `risk_state` parameter
that restores risk management state from a previous run:

```python
result = engine.run(
    symbols_dict, start_date, end_date,
    risk_state={
        "terminal_risk_lock": True,
        "sector_guard_active": False,
        "cycle_lock_count": 1,
    },
)
```

When `terminal_risk_lock` is `True`, the portfolio-level risk manager starts
locked and no new trades are entered. When `sector_guard_active` is `True`,
the sector breadth guard is active from the first trading day. This enables
daily signal scans to preserve risk continuity across consecutive runs.

The `daily_signal_scan.py` script automatically loads the previous run's risk
state from `risk_state.json` and passes it to the engine, ensuring that a
terminal lock or active sector guard persists across daily scans.

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

## Weak-regime limitation

For 2024-01-02 through 2025-03-31, cold results remained positive, but concentrated
gap risk exceeded the target-period drawdowns:

| Available universe | Return | Maximum drawdown |
|---|---:|---:|
| 1 symbol | 1.2020% | -22.1571% |
| 3 symbols | 1.9566% | -32.2830% |
| 5 symbols | 12.4500% | -28.2286% |
| 12 symbols, excluding not-yet-listed `920045` | 13.6775% | -27.1750% |
| 21 symbols, excluding not-yet-listed `920045` | 26.4999% | -28.1595% |

The terminal rule is evaluated at a close and liquidation occurs at a later open.
It is therefore a decision boundary, not a guaranteed realized-drawdown ceiling.
These stress results are an explicit limitation, not an accepted live-risk claim.

## Verification

Run the standard-library regression suite and the complete cross-universe runner:

```bash
python -m py_compile quant_fusion.py quant_fusion_optimizer.py
python -m unittest -v test_quant_fusion.py test_quant_fusion_optimizer.py test_daily_signal_scan.py
python backtest_universes.py
python stress_test_prefixes.py
python backtest_cambricon_universe.py
```

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
risk state persistence round-trips, signal classification, position
reconstruction from trade ledgers, and that all 26 universe symbols are
explicitly mapped.

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

### Risk state persistence

After each run, risk state (`terminal_risk_lock`, `sector_guard_active`,
`max_drawdown`, `cycle_lock_count`) is saved to `risk_state.json` in the
output directory with enhanced identity fields (`symbols_hash`, `run_id`).
Identity uses stable fields only (symbol set + count + start date);
cash/capital is excluded because it changes daily. On the next run, the
previous state is loaded and displayed for continuity checking — if the
previous run had an active terminal lock or sector guard, a warning is shown
even if the current backtest doesn't detect it (because the backtest starts
fresh each time). Old risk state files without `symbols_hash` are rejected
(fail-closed) to prevent cross-contamination between different universes or
configurations.

### Usage

```bash
# Simulation mode (default)
python daily_signal_scan.py [--end-date YYYY-MM-DD] [--cache-dir DIR] [--capital N]

# Override capital and start date
python daily_signal_scan.py --capital 1500000 --start-date 2026-06-01
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
