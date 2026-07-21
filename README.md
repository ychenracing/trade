# Codex Quant Fusion v14

Codex Quant Fusion v14 is a deterministic daily-bar backtester for a concentrated A-share technology portfolio. It combines Turtle breakouts, dual moving averages, and ATR channels, then applies shared cash limits, symbol and sector caps, T+1 execution, board-limit checks, transaction costs, momentum slot selection, and a confirmed sector-breadth risk guard.

The bundled forward-adjusted snapshots cover:

- `300308` 中际旭创
- `300502` 新易盛
- `300394` 天孚通信
- `688008` 澜起科技
- `603986` 兆易创新

## Important assumptions

- Signals use information available at the current close and execute no earlier than the next tradable open.
- Input files must contain forward-adjusted daily OHLCV data.
- The simulator includes commission, minimum commission, stamp duty, slippage, A-share lot sizing, and approximate board-limit handling.
- It does not model queue priority, intraday market impact, partial fills, suspensions without bars, or guaranteed exits during continuous limit-down sequences.
- The five-symbol breadth guard requires all five configured symbols by default. It intentionally remains inactive for a single-symbol run.
- The supplied period is a small, highly concentrated sample. The results are a reproducibility check, not evidence of future performance or investment advice.

## Quick start

```bash
python -m pip install -r requirements.txt
python codex_quant_fusion_v14.py backtest \
  --start 2025-04-01 \
  --end 2026-07-20 \
  --capital 2000000 \
  --data-dir market_data_qfq \
  --no-plot
```

When `--data-dir` is omitted, the loader tries Eastmoney, Sina, and Tencent in a deterministic fallback order through AKShare and a fixed Tencent HTTPS endpoint.

## Verification

Install development tools and run all checks:

```bash
python -m pip install -r requirements-dev.txt
ruff format --check codex_quant_fusion_v14.py test_codex_quant_fusion_v14.py
ruff check codex_quant_fusion_v14.py test_codex_quant_fusion_v14.py
python -m py_compile codex_quant_fusion_v14.py test_codex_quant_fusion_v14.py
pytest -q test_codex_quant_fusion_v14.py
bandit -q codex_quant_fusion_v14.py
pip-audit -r requirements.txt
```

The regression suite checks repeated-run state isolation, caller symbol-order invariance, T+1 sector-guard liquidation, insufficient-universe guard behavior, localized AKShare headers, future-data causality, and the exact requested-period snapshot.

## Reproduced results

Initial capital is CNY 2,000,000. Both requested tests use the five unique symbols listed above.

| Period | Total return | Maximum drawdown | Final assets |
|---|---:|---:|---:|
| 2025-04-01 through 2026-06-30 | 1,074.4713% | -15.1777% | CNY 23,489,425.60 |
| 2025-04-01 through 2026-07-20 | 1,074.4713% | -15.1777% | CNY 23,489,425.60 |

The sector guard confirms on 2026-06-26. Its liquidation instructions execute on 2026-06-29 under the T+1 rule, so the later end date does not change final assets in this snapshot.

## Main risk controls

- Portfolio allocation cap: 100%.
- Default per-symbol cap: 60%; routed symbol profiles may override it, subject to the portfolio and group caps.
- Maximum concurrent symbols: six, so the requested five-symbol universe can hold all five.
- Industry group caps: 100% for overseas compute and 80% for domestic semiconductor exposure.
- Drawdown circuit breaker: 16.5% with liquidation enabled.
- Daily loss limit: 6%.
- Sector shock: equal-weight daily return at or below -5% and breadth at or below 20%, confirmed twice within four trading days.
- Sector recovery: positive return, at least 80% recovery breadth, sector trend repair, and two consecutive confirmations.

See `BacktestEngine._default_config()` and the industry profile builders in the source for the complete parameter set.
