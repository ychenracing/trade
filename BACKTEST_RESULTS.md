# Verified Backtest Results

Verification date: 2026-08-01 UTC.

Quant Fusion is a standalone module with built-in AKShare and local CSV
data paths. It uses the same code, parameters, costs, and forward-adjusted data
snapshot across every requested universe. Initial capital is CNY 2,000,000.

| Universe | Cold return | Cold maximum drawdown | Warm return | Warm maximum drawdown | Warm Sharpe | Warm Calmar |
|---|---|---|---:|---:|---:|---:|---:|
| 1 symbol | 536.66% | -18.49% | 530.89% | -18.34% | 3.21 | 18.23 |
| 3 symbols | 1059.72% | -18.32% | 1083.70% | -17.92% | 3.69 | 34.47 |
| 5 symbols | 1078.67% | -16.80% | 1115.99% | -15.86% | 3.70 | 39.93 |
| 13 symbols | 894.16% | -16.93% | 1038.74% | -18.41% | 3.61 | 32.37 |
| 22 symbols | 843.49% | -17.13% | 983.57% | -16.22% | 3.76 | 35.07 |

The 2026-06-30 and 2026-07-20 figures are identical for the 1-, 3-, and 5-symbol
universes. The 13-symbol results increase slightly through July; the 22-symbol
warm result rises from 977.83% to 983.57%.

The fixed signal-only regime basket confirmed the defensive gate on 2026-06-26 in
all requested universes. Risk-only symbols never entered the order or trade ledgers.

## Cambricon mapping regression

After routing `688256` 寒武纪 through `semiconductor` / `domestic_semiconductor` /
`domestic_design`, the requested nine-symbol universe produced the following
deterministic results:

| Indicator state | End date | Total return | Maximum drawdown | Sharpe | Calmar |
|---|---|---|---:|---:|---:|---:|
| Cold | 2026-06-30 | 1187.05% | -15.89% | 3.90 | 46.77 |
| Cold | 2026-07-20 | 1187.05% | -15.89% | 3.81 | 41.99 |
| Warm | 2026-06-30 | 1147.30% | -15.43% | 3.88 | 46.76 |
| Warm | 2026-07-20 | 1147.30% | -15.43% | 3.79 | 42.02 |

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

### SAFE_PARAMS (Conservative Fallback)

When the market regime transitions to CHOPPY, the system automatically activates
conservative parameters (tighter trailing stops, lower position sizing, faster
reversal exits) by applying them directly to each strategy instance's runtime
configuration. This mechanism is enabled by default and requires no manual
intervention. When the market recovers to TREND, the original parameters are
restored automatically.

### Risk State Identity

The `risk_state.json` file now includes enhanced identity fields:
- `symbols_hash`: SHA-256 fingerprint of sorted symbol codes + count + config
- `total_symbols`: number of symbols in the universe
- `run_id`: unique run identifier for traceability

Old risk state files without `symbols_hash` are rejected (fail-closed) to
prevent cross-contamination between different universes or configurations.
