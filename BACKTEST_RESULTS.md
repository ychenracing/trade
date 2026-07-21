# Verified Backtest Results

Verification date: 2026-07-21 UTC.

Codex Quant Fusion v17 is a standalone module with built-in AKShare and local CSV
data paths. It uses the same code, parameters, costs, and forward-adjusted data
snapshot across every requested universe. Initial capital is CNY 2,000,000.

| Universe | Cold return | Cold maximum drawdown | Warm return | Warm maximum drawdown |
|---|---:|---:|---:|---:|
| 1 symbol | 662.716567% | -18.489640% | 645.489170% | -18.341367% |
| 3 symbols | 992.137940% | -18.321672% | 1,012.807942% | -17.918988% |
| 5 symbols | 1,071.991794% | -15.975509% | 1,145.480399% | -14.938319% |
| 13 symbols | 836.495572% | -16.931701% | 945.348654% | -18.407181% |
| 22 symbols | 622.255461% | -16.525002% | 850.106024% | -16.900028% |

The 2026-06-30 and 2026-07-20 figures are identical for the 1-, 3-, and 5-symbol
universes. The 13-symbol results increase slightly through July; the 22-symbol
warm result rises from 838.619737% to 850.106024%.

The fixed signal-only regime basket confirmed the defensive gate on 2026-06-26 in
all requested universes. Risk-only symbols never entered the order or trade ledgers.

## Cambricon mapping regression

After routing `688256` 寒武纪 through `semiconductor` / `domestic_semiconductor` /
`domestic_design`, the requested nine-symbol universe produced the following
deterministic results:

| Indicator state | End date | Total return | Maximum drawdown |
|---|---:|---:|---:|
| Cold | 2026-06-30 | 991.995458% | -16.143191% |
| Cold | 2026-07-20 | 991.995458% | -16.143191% |
| Warm | 2026-06-30 | 1,167.935349% | -15.111072% |
| Warm | 2026-07-20 | 1,167.935349% | -15.111072% |

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
-13.23% when moving from 9 to 10 symbols.
