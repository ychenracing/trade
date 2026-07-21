# Verified Backtest Results

Verification date: 2026-07-21 UTC.

Codex Quant Fusion v17 is a standalone module with built-in AKShare and local CSV
data paths. It uses the same code, parameters, costs, and forward-adjusted data
snapshot across every requested universe. Initial capital is CNY 2,000,000.

| Universe | Cold return | Cold maximum drawdown | Warm return | Warm maximum drawdown |
|---|---:|---:|---:|---:|
| 1 symbol | 662.716567% | -18.489640% | 645.489170% | -18.341367% |
| 3 symbols | 919.967237% | -18.153210% | 1,003.756957% | -17.949178% |
| 5 symbols | 1,058.820940% | -14.659954% | 1,175.236735% | -14.906058% |
| 13 symbols | 1,049.110923% | -18.735018% | 1,218.020342% | -18.324146% |
| 22 symbols | 944.759496% | -17.938673% | 1,033.226909% | -15.916740% |

The 2026-06-30 and 2026-07-20 figures are identical for the 1-, 3-, 5-, and
22-symbol universes. The 13-symbol cold and warm results increased from
1,042.793465% and 1,211.128570% to the values above by 2026-07-20.

The fixed signal-only regime basket confirmed the defensive gate on 2026-06-26 in
all requested universes. Risk-only symbols never entered the order or trade ledgers.

## Cambricon mapping regression

After routing `688256` 寒武纪 through `semiconductor` / `domestic_semiconductor` /
`domestic_design`, the requested nine-symbol universe produced the following
deterministic results:

| Indicator state | End date | Total return | Maximum drawdown |
|---|---:|---:|---:|
| Cold | 2026-06-30 | 1,191.932150% | -14.943993% |
| Cold | 2026-07-20 | 1,191.932150% | -14.943993% |
| Warm | 2026-06-30 | 1,418.077636% | -15.111056% |
| Warm | 2026-07-20 | 1,418.077636% | -15.111056% |

The universe contains `300308`, `688256`, `300502`, `300394`, `603986`,
`688008`, `688347`, `300054`, and `688300`. The sector guard still confirms on
2026-06-26, so extending the end date through 2026-07-20 does not change final
assets in this snapshot. Exact metadata is stored in
`v17_cambricon_universe_backtest_20260720.json` and can be regenerated with
`python backtest_v17_cambricon_universe.py`.

High-cost and weak-regime figures, limitations, and reproducibility commands are
documented in `README.md`. Exact scenario metadata is stored in
`v17_universe_backtest_20260720.json`. The one-through-22 ordered-prefix audit is
stored in `v17_prefix_stress_20260720.json`; its worst adjacent wealth change is
-11.46% when moving from 14 to 15 symbols.
