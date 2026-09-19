# Trade Core17 — September 18, 2026 closing signals

## Result

The unchanged production daily scan completed with exit code 0. Its final output is WAIT for all 17 stocks: 0 Buy, 0 Sell, 0 Hold, and 17 Wait. No pending buy or sell signals were emitted.

This is a restricted simulation result, not 17 independent bearish forecasts and not an instruction to sell existing real holdings.

## Run identity and data

- Production version: 1.0.3; source revision: `5f797f7524cd5e012f1abdc96f26b0e724e6c039`.
- Closing-data date: September 18, 2026 (Asia/Shanghai); next trading session: September 21, 2026.
- Simulation: July 1 to September 18, 2026; initial capital CNY 2,000,000; automatic routing; default AB5 enabled and evaluated.
- All 17 stock inputs, the additional stock reference, and both fixed indices cover September 18. Stock data came from the existing Sina fallback; both indices came from the existing Tencent fallback.
- The downloaded archive matched GitHub's byte length and SHA-256. All 74 manifest-listed evidence files passed length/hash verification. Zhongji Innolight and Piotech closing prices matched independent quotations.
- No strategy code or risk thresholds were changed; the run used an isolated branch and did not place broker orders.

## Per-stock output

Closing prices are CNY per share from the exact forward-adjusted daily-bar snapshot consumed by Trade. The final-day close is shown below. Daily changes are calculated from the same snapshot.

| Ticker | Stock | Close (CNY) | Daily change | Final signal | Momentum shortlist |
|---|---|---:|---:|---|---|
| 300308 | Zhongji Innolight | 926.43 | +3.40% | WAIT | Not selected |
| 300502 | Eoptolink | 445.00 | +4.87% | WAIT | Not selected |
| 300394 | TFC Optical Communication | 284.66 | +2.52% | WAIT | Not selected |
| 688256 | Cambricon | 1,113.14 | +0.65% | WAIT | Not selected |
| 603986 | GigaDevice | 388.18 | +4.95% | WAIT | Not selected |
| 688072 | Piotech | 720.06 | +3.76% | WAIT | Selected |
| 688300 | Novoray | 176.80 | +1.21% | WAIT | Not selected |
| 300054 | Dinglong | 72.58 | +2.01% | WAIT | Not selected |
| 688361 | Skyverse Technology | 386.36 | +7.71% | WAIT | Selected |
| 002409 | Yoke Technology | 138.83 | +3.07% | WAIT | Not selected |
| 688498 | Yuanjie Semiconductor | 1,835.00 | +2.82% | WAIT | Selected |
| 688120 | Hwatsing | 263.43 | -0.59% | WAIT | Not selected |
| 002384 | Dongshan Precision | 196.15 | +1.35% | WAIT | Not selected |
| 688082 | ACM Research (Shanghai) | 292.77 | +3.65% | WAIT | Not selected |
| 300604 | Changchuan Technology | 279.73 | +4.71% | WAIT | Not selected |
| 601869 | YOFC | 454.99 | -2.58% | WAIT | Not selected |
| 300408 | Three-Circle Group | 130.96 | +2.30% | WAIT | Not selected |

WAIT means the simulation holds no shares in that stock and has no retained buy/sell plan. It does not mean HOLD, SELL, or SHORT. A shortlist selection is not a buy signal.

## Restrictions and interpretation

**Buy suppression is active.** The scan records `current_route_mismatch=true`. Both route objects are named `positive_momentum_hold`, but the top-level replay `selected_symbols` contains all 17 stocks while the current leader selection contains only Yuanjie Semiconductor (688498), Skyverse Technology (688361), and Piotech (688072). The daily-scan consistency comparison therefore flags a mismatch. This is an internal selection-set inconsistency, not evidence that all 17 stocks are bearish. No individual blocked-buy records were emitted, so this must not be described as 17 blocked buy signals.

**Reference-basket coverage is degraded.** The broader risk-reference basket has 14 of 23 members available (60.87%), producing `warmup_status=DEGRADED` and `reference_basket_incomplete`. This is separate from stock-date freshness: all 17 requested stocks have September 18 data, the indicator-ready ratio is 100%, and the stale-symbol count is zero.

**Risk and portfolio state.** The index-based regime is `choppy`. The simulation reports `sector_guard_active=true`, `terminal_risk_lock=false`, and zero shares in every stock. Its actual-account applicability is unverified because no current real-account snapshot was supplied.

The momentum shortlist, in model order, is Yuanjie Semiconductor, Skyverse Technology, then Piotech. They remain observation candidates only; the final production output for each is WAIT.

## Reproducibility

```bash
python -m quantfusion.application.daily_scan --start-date 2026-07-01 --end-date 2026-09-18 --capital 2000000 --cache-dir runtime/cache --regime-data-dir runtime/regime --output-dir runtime/simulation
```

Run ID: `35444126483`; workflow revision: `5128eb2b172a943b4d201b4e6a29b2146de993c7`; artifact ID: `10584312674`.
Raw archive: `trade_core17_close_20260918.zip` (342,533 bytes).
Archive SHA-256: `672920229580db9ac6b91755a6ccb6eb5484c346d12370592a2e37a3e9f62ff9`.
Signal source: `simulation/signals_2026-09-18.json`; exact input snapshot: `simulation/snapshots/2026-09-18/`.
The GitHub Actions artifact expires on October 19, 2026; retain the supplied ZIP for the complete original data and logs.
