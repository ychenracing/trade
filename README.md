# Codex Quant Fusion v17

Codex Quant Fusion v17 is a standalone deterministic daily-bar backtester for
concentrated A-share technology portfolios. The complete data-loading, signal,
T+1 execution, transaction-cost, exposure, causal-liquidity, allocation, and risk
implementation is contained in `codex_quant_fusion_v17.py`; it does not import or
require another strategy module at runtime.

## Version 17 design

- Tradable symbols and regime-observation symbols are separate. The default
  signal-only regime basket is `300308`, `300502`, `300394`, `688008`, and
  `603986`; these symbols are never traded unless the caller also includes them
  in the tradable universe.
- The same fixed basket is the reference distribution for 10-, 20-, 40-, and
  80-day risk-adjusted momentum. Existing scores therefore do not change merely
  because a tradable symbol is added or removed.
- When the universe has no more than six symbols, all symbols remain eligible and
  their independent entry signals decide whether to trade. Above six, a symbol
  must reach the 50th reference percentile before it can consume one of the six
  candidate slots.
- One- and two-symbol universes have little useful cross-sectional information.
  V17 therefore switches every sleeve to a slower time-series trend contract
  without changing the 60% symbol exposure ceiling.
- Forced industry-group slots are disabled. The causal momentum allocator may use
  up to six positions and can ignore weaker additions to a large universe.
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
- The portfolio ADV budget remains 0.5% of the prior 20 daily volumes and is
  divided among independent sleeves without adding leverage.

Universe-size robustness does not mean composition invariance. A universe that
removes the strongest underlying assets cannot be guaranteed the return of a
universe that contains them. The fixed default regime basket is appropriate for
the bundled AI-infrastructure and semiconductor domain; another investment domain
should supply its own stable benchmark basket before live use.

## Repository layout

- `codex_quant_fusion_v17.py`: complete standalone production engine and CLI.
- `test_codex_quant_fusion_v17.py`: unit, isolation, routing, risk, and regression
  tests.
- `backtest_v17_universes.py`, `stress_test_v17_prefixes.py`, and
  `backtest_v17_cambricon_universe.py`: reproducible portfolio checks.
- `market_data_qfq_22_20260720` and `market_data_qfq_9_cambricon_20260720`:
  canonical forward-adjusted snapshots required by the regression suite.
- `v17_universe_backtest_20260720.json`, `v17_prefix_stress_20260720.json`, and
  `v17_cambricon_universe_backtest_20260720.json`: canonical result artifacts.

Historical implementations remain available on their original Git branches and
are intentionally not duplicated on `main`.

## Data

The reproducible 22-symbol Eastmoney forward-adjusted snapshot is stored in
`market_data_qfq_22_20260720`. Provider volume is converted from board lots to
shares before the ADV participation rule is applied. `920045` begins on
2025-12-31 and is unavailable before that date.

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
python codex_quant_fusion_v17.py \
  --start 2025-04-01 \
  --end 2026-07-20 \
  --capital 2000000 \
  --data-dir market_data_qfq_22_20260720 \
  --indicator-state warm \
  --no-plot
```

For online data, omit the local directory option:

```bash
python codex_quant_fusion_v17.py \
  --start 2025-04-01 \
  --end 2026-07-20 \
  --indicator-state warm \
  --no-plot
```

The CLI defaults to warm indicator state because a live strategy normally has
pre-start history. Cold state remains available for initialization sensitivity
tests. Both states start the portfolio flat on the requested first date.

## Cross-universe target results

Initial capital is CNY 2,000,000. All scenarios use identical v17 parameters,
default costs, 0.1% one-way slippage, and data through 2026-07-20.

| Tradable universe | Cold return | Cold max drawdown | Warm return | Warm max drawdown |
|---|---:|---:|---:|---:|
| 1 symbol | 662.7166% | -18.4896% | 645.4892% | -18.3414% |
| 3 symbols | 919.9672% | -18.1532% | 1,003.7570% | -17.9492% |
| 5 symbols | 1,058.8209% | -14.6600% | 1,175.2367% | -14.9061% |
| 13 symbols | 1,049.1109% | -18.7350% | 1,218.0203% | -18.3241% |
| 22 symbols | 944.7595% | -17.9387% | 1,033.2269% | -15.9167% |

The requested multi-symbol warm wealth-factor ratio between the worst and best
universe is above 83%. The exhaustive ordered-prefix audit also tests every count
from one through 22. Counts two through 22 all return above 1,000%, and the worst
single-symbol addition changes wealth by -11.46% (14 to 15 symbols), compared
with the much larger discontinuities observed before the universe-invariant
policy was introduced. The one-symbol result remains structurally lower because
the 60% symbol cap leaves at least 40% cash at entry and provides no rotation
opportunity.

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
python -m py_compile codex_quant_fusion_v17.py
python -m unittest -v test_codex_quant_fusion_v17.py
python backtest_v17_universes.py
python stress_test_v17_prefixes.py
python backtest_v17_cambricon_universe.py
```

The tests cover standalone isolated startup, absence of versioned imports,
AKShare provider failover, strict local CSV selection, policy validation,
concentration scaling, temporary rearming, terminal locks, signal-only basket
isolation, all five requested universe sizes, sub-20% drawdown for the requested
universes, bounded multi-symbol wealth dispersion, Cambricon's complete route,
the 2026-06-26 regime-gate event, the complete one-through-22 prefix artifact,
and the mapped nine-symbol Cambricon artifact.

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
