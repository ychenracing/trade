# Pool ladder 1 / 3 / 5 / 13 / 17 — absorb vs main vs archive

**As of:** 2026-09-18T13:24+0800 (Asia/Shanghai)
**Candidate:** `agent/absorb-archive0805-ordinary-ab5` @ `c1839170dd47` (tree `f5c4cbca539a`)
**Main:** `fa4ef7bafe7f`
**Archive:** `c158435f6603`
**Window:** 2025-04-01 .. 2026-07-20 · capital 2e6 · indicator_state=warm
**No merge.** Fresh runs for pools 13 & 17; reused `_econ*` for 1/3/5 (mechanism unchanged).

## Compact comparison (key metrics)

| Pool | Role | TR | Wealth | maxDD | Sharpe | fills | dss | sell_b | days | turnover | fees | trims | buy_clips | elapsed_s |
|------|------|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| common-1 | main | 3.928538 | 4.9285 | -0.1247 | 3.470 | 36 | 7 | 4 | 7 | 2.826 | 9579.1 | 6 | 188 | 12.0 |
| common-1 | candidate | 3.928538 | 4.9285 | -0.1247 | 3.470 | 36 | 7 | 4 | 7 | 2.826 | 9579.1 | 6 | 188 | 12.2 |
| common-1 | archive | 5.308950 | 6.3089 | -0.1834 | 3.206 | 24 | 5 | 3 | 5 | 2.404 | 10228.0 | 0 | 0 | 22.0 |
| common-3 | main | 6.707950 | 7.7080 | -0.1616 | 3.485 | 342 | 96 | 47 | 63 | 15.618 | 67094.9 | 59 | 24 | 14.1 |
| common-3 | candidate | 6.707950 | 7.7080 | -0.1616 | 3.485 | 342 | 96 | 47 | 63 | 15.618 | 67094.9 | 59 | 24 | 14.6 |
| common-3 | archive | 10.836973 | 11.8370 | -0.1792 | 3.685 | 194 | 71 | 35 | 53 | 13.407 | 76505.3 | 0 | 0 | 23.2 |
| common-5 | main | 7.984487 | 8.9845 | -0.1409 | 3.480 | 437 | 130 | 67 | 80 | 17.808 | 82722.6 | 121 | 14 | 16.5 |
| common-5 | candidate | 9.351673 | 10.3517 | -0.1400 | 3.649 | 375 | 115 | 59 | 73 | 16.448 | 83110.8 | 95 | 11 | 17.3 |
| common-5 | archive | 11.279154 | 12.2792 | -0.1896 | 3.730 | 243 | 99 | 43 | 67 | 13.459 | 70793.8 | 0 | 0 | 27.9 |
| common-13 | main | 7.478371 | 8.4784 | -0.1573 | 3.450 | 657 | 218 | 127 | 111 | 17.790 | 78873.2 | 243 | 37 | 25.9 |
| common-13 | candidate | 7.586823 | 8.5868 | -0.1417 | 3.600 | 755 | 213 | 116 | 103 | 19.762 | 84945.8 | 326 | 52 | 26.9 |
| common-13 | archive | 9.615957 | 10.6160 | -0.2128 | 3.751 | 227 | 113 | 53 | 74 | 10.664 | 54443.1 | 0 | 0 | 53.5 |
| common-17 | main | 8.610543 | 9.6105 | -0.1510 | 3.726 | 645 | 190 | 109 | 92 | 16.008 | 74192.4 | 245 | 63 | 29.1 |
| common-17 | candidate | 8.147943 | 9.1479 | -0.1436 | 3.609 | 585 | 183 | 100 | 93 | 16.678 | 76162.4 | 185 | 61 | 28.9 |
| common-17 | archive | 13.423483 | 14.4235 | -0.2166 | 3.999 | 257 | 116 | 53 | 73 | 10.746 | 66554.2 | 0 | 0 | 67.1 |

## Δ candidate − main & archive gap recovery

| Pool | ΔTR | Δfills | Δtrims | Δbuy_clips | maxDD Δpp | archive_gap_recovery |
|------|----:|-------:|-------:|-----------:|----------:|---------------------:|
| common-1 | +0.000000 | +0 | +0 | +0 | +0.000 | 0.0000 |
| common-3 | +0.000000 | +0 | +0 | +0 | +0.000 | 0.0000 |
| common-5 | +1.367186 | -62 | -26 | -3 | +0.086 | 0.4150 |
| common-13 | +0.108452 | +98 | +83 | +15 | +1.564 | 0.0507 |
| common-17 | -0.462600 | -60 | -60 | -2 | +0.740 | -0.0961 |

## Per-pool detail

### common-1 (`300308`)

| | total_return | wealth | maxDD | sharpe | fills | dss | sell_b | days | turnover | fees | trims | buy_clip_intents | removed_shares |
|--|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| main | 3.928538 | 4.9285 | -0.1247 | 3.470 | 36 | 7 | 4 | 7 | 2.826 | 9579.1 | 6 | 188 | 806200 |
| candidate | 3.928538 | 4.9285 | -0.1247 | 3.470 | 36 | 7 | 4 | 7 | 2.826 | 9579.1 | 6 | 188 | 806200 |
| archive | 5.308950 | 6.3089 | -0.1834 | 3.206 | 24 | 5 | 3 | 5 | 2.404 | 10228.0 | 0 | 0 | 0 |

Delta cand−main TR: **+0.000000**. Archive gap recovery: **0.0000**.

### common-3 (`300308,300502,300394`)

| | total_return | wealth | maxDD | sharpe | fills | dss | sell_b | days | turnover | fees | trims | buy_clip_intents | removed_shares |
|--|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| main | 6.707950 | 7.7080 | -0.1616 | 3.485 | 342 | 96 | 47 | 63 | 15.618 | 67094.9 | 59 | 24 | 544400 |
| candidate | 6.707950 | 7.7080 | -0.1616 | 3.485 | 342 | 96 | 47 | 63 | 15.618 | 67094.9 | 59 | 24 | 544400 |
| archive | 10.836973 | 11.8370 | -0.1792 | 3.685 | 194 | 71 | 35 | 53 | 13.407 | 76505.3 | 0 | 0 | 0 |

Delta cand−main TR: **+0.000000**. Archive gap recovery: **0.0000**.

### common-5 (`300308,300502,300394,688256,603986`)

| | total_return | wealth | maxDD | sharpe | fills | dss | sell_b | days | turnover | fees | trims | buy_clip_intents | removed_shares |
|--|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| main | 7.984487 | 8.9845 | -0.1409 | 3.480 | 437 | 130 | 67 | 80 | 17.808 | 82722.6 | 121 | 14 | 316800 |
| candidate | 9.351673 | 10.3517 | -0.1400 | 3.649 | 375 | 115 | 59 | 73 | 16.448 | 83110.8 | 95 | 11 | 253500 |
| archive | 11.279154 | 12.2792 | -0.1896 | 3.730 | 243 | 99 | 43 | 67 | 13.459 | 70793.8 | 0 | 0 | 0 |

Delta cand−main TR: **+1.367186**. Archive gap recovery: **0.4150**.

### common-13 (`300308,300502,300394,688256,603986,688072,688300,300054,688361,002409,688498,688120,002384`)

| | total_return | wealth | maxDD | sharpe | fills | dss | sell_b | days | turnover | fees | trims | buy_clip_intents | removed_shares |
|--|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| main | 7.478371 | 8.4784 | -0.1573 | 3.450 | 657 | 218 | 127 | 111 | 17.790 | 78873.2 | 243 | 37 | 1131300 |
| candidate | 7.586823 | 8.5868 | -0.1417 | 3.600 | 755 | 213 | 116 | 103 | 19.762 | 84945.8 | 326 | 52 | 1696200 |
| archive | 9.615957 | 10.6160 | -0.2128 | 3.751 | 227 | 113 | 53 | 74 | 10.664 | 54443.1 | 0 | 0 | 0 |

Delta cand−main TR: **+0.108452**. Archive gap recovery: **0.0507**.

### common-17 (`300308,300502,300394,688256,603986,688072,688300,300054,688361,002409,688498,688120,002384,688082,300604,601869,300408`)

| | total_return | wealth | maxDD | sharpe | fills | dss | sell_b | days | turnover | fees | trims | buy_clip_intents | removed_shares |
|--|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| main | 8.610543 | 9.6105 | -0.1510 | 3.726 | 645 | 190 | 109 | 92 | 16.008 | 74192.4 | 245 | 63 | 2215800 |
| candidate | 8.147943 | 9.1479 | -0.1436 | 3.609 | 585 | 183 | 100 | 93 | 16.678 | 76162.4 | 185 | 61 | 1912300 |
| archive | 13.423483 | 14.4235 | -0.2166 | 3.999 | 257 | 116 | 53 | 73 | 10.746 | 66554.2 | 0 | 0 | 0 |

Delta cand−main TR: **-0.462600**. Archive gap recovery: **-0.0961**.

## Notes / caveats

- Pools 1/3/5 reused existing `_econ*` artifacts (mechanism code identical since a64b217; HEAD c183917 is docs stamp only).
- Pools 13/17 freshly measured at current HEAD.
- Archive CSVs for 688498/002384/601869/300408 were copied locally from absorb market data (untracked in archive).
- Archive ClassVar maps for 002384/300408 injected at runtime only (not committed).
- `archive_gap_recovery = (cand−main)/(archive−main)` when archive>main.
- Sum of per-role elapsed_sec across all loaded econ JSONs: **391.1s** (includes reused 1/3/5).

