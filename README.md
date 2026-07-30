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
- When the universe has no more than ten symbols, all symbols remain eligible
  and their independent entry signals decide whether to trade. Above ten, a
  symbol must reach the 50th reference percentile before it can consume one of
  the ten candidate slots.
- One- and two-symbol universes have little useful cross-sectional information.
  The engine therefore switches every sleeve to a slower time-series trend contract
  without changing the 60% symbol exposure ceiling.
- Forced industry-group slots are disabled. The synchronized portfolio
  coordinator admits at most ten distinct symbols across all sleeves, not ten
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
- A symbol without explicit routing metadata or a recognized name hint still
  receives the deterministic default profile, but now produces a route warning
  and appears in `unmapped_symbols` instead of falling back silently.

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
- `backtest_universes.py`, `stress_test_prefixes.py`, and
  `backtest_cambricon_universe.py`: reproducible portfolio checks.
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

| Tradable universe | Cold return | Cold max drawdown | Warm return | Warm max drawdown | Warm Sharpe | Warm Calmar |
|---|---:|---:|---:|---:|---:|---:|
| 1 symbol | 662.7166% | -18.4896% | 645.4892% | -18.3414% | 3.2042 | 21.6069 |
| 3 symbols | 992.1379% | -18.3217% | 1,012.8079% | -17.9190% | 3.5976 | 32.5411 |
| 5 symbols | 1,071.9918% | -15.9755% | 1,145.4804% | -14.9383% | 3.7086 | 43.3317 |
| 13 symbols | 836.4956% | -16.9317% | 945.3487% | -18.4072% | 3.3981 | 29.8727 |
| 22 symbols | 622.2555% | -16.5250% | 850.1060% | -16.9000% | 3.3343 | 29.7161 |

The requested multi-symbol warm wealth-factor ratio between the worst and best
universe is 76.28%. The exhaustive ordered-prefix audit also tests every count
from one through 22. Counts two through 22 all return above 800%; the worst
single-symbol addition changes wealth by -13.23% (9 to 10 symbols). The
one-symbol result remains structurally lower because the 60% symbol cap leaves at
least 40% cash at entry and provides no rotation opportunity. The two-symbol
prefix reaches -21.76% drawdown, so the sub-20% claim applies to the five requested
1-, 3-, 5-, 13-, and 22-symbol warm scenarios, not every possible composition.

At 0.5% one-way slippage, warm returns were 642.0017%, 1,048.9399%, 1,032.5983%,
and 873.9249% for the 1-, 5-, 13-, and 22-symbol universes respectively.

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
python -m unittest -v test_quant_fusion.py test_quant_fusion_optimizer.py
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
portfolio-wide ten-symbol ceiling, all five requested universe sizes, sub-20%
drawdown for the requested warm universes, Cambricon's complete route, the
2026-06-26 regime-gate event, the complete one-through-22 prefix artifact, and
the mapped nine-symbol Cambricon artifact. It also checks deterministic detection
of unmapped auto routes and the presence of positive Calmar values in successful
target-period runs.

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
