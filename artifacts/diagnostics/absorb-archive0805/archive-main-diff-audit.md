# Archive vs main — fill/trim/clip/equity diff audit

**As of:** 2026-09-18T15:16+0800 (Asia/Shanghai)
**Main:** `/workspace/trade-main` @ `fa4ef7bafe7f`
**Archive:** `/workspace/trade-archive_0805` @ `c158435f6603`
**Window:** 2025-04-01 .. 2026-07-20 · capital 2e6 · warm
**Note:** Soft trade keys ignore `reason` (archive/main reason strings differ).

## Headline

Archive does **far less ordinary fuss** than main AB5:
- **0** ordinary `account_budget_trim` fills and **0** buy-clip intents on every pool measured.
- **Fewer sleeve fills** (~40–60% of main): common-3 194 vs 342; common-5 243 vs 437; Core17 257 vs 645.
- **Higher TR** on all three pools (archive ahead by ~3.3–4.8 TR).
- Paths **diverge on first meaningful trade day 2025-04-24** (also first equity divergence) — i.e. early buy sizing / sleeve allocation, not a late-path accident.
- Main's first ordinary AB5 trims appear mid-window (common-3: 2025-09-03; common-5/Core17: 2025-06-25) with **no same-day archive sell** on that symbol — archive simply never runs this reserve-funding trim.

## Scoreboard

| Pool | main TR | archive TR | Δ(arc−main) | main fills | arc fills | main trims | arc trims | main buy_clips | arc buy_clips |
|------|--------:|-----------:|------------:|-----------:|----------:|-----------:|----------:|---------------:|--------------:|
| common-3 | 6.708 | 10.837 | +4.129 | 342 | 194 | 59 | 0 | 24 | 0 |
| common-5 | 7.984 | 11.279 | +3.295 | 437 | 243 | 121 | 0 | 14 | 0 |
| common-17 | 8.611 | 13.423 | +4.813 | 645 | 257 | 245 | 0 | 63 | 0 |

## First divergences

### common-3

- **First fill-day soft multiset diff:** `2025-04-24`
  - that day fills: main=6 archive=6
  - main-only sample: `['2025-04-24', '300394', 'buy', 9500]` ×1; …(+5 more)
  - archive-only sample: `['2025-04-24', '300394', 'buy', 9000]` ×3; …(+1 more)
- **First buy-day soft multiset diff:** `2025-04-24`
- **First buy-size diff (through first equity):** `{'date': '2025-04-24', 'symbol': '300394', 'main_shares': 27100, 'archive_shares': 27000}`
- **First equity diff:** `{'index': 16, 'main': {'date': '2025-04-24', 'equity': 1985088.2}, 'archive': {'date': '2025-04-24', 'equity': 1985119.52}, 'delta_main_minus_archive': -31.32}`
- **First main-only AB5 trim (no archive sell same day/sym):** `{'date': '2025-09-03', 'symbol': '300394', 'side': 'sell', 'shares': 100, 'price': 141.4984, 'reason': 'account_budget_trim', 'gross': 14149.84}`
- **First sorted soft trade mismatch:** `{'index': 0, 'main': {'date': '2025-04-24', 'symbol': '300394', 'side': 'buy', 'shares': 5400, 'price': 34.6246, 'reason': '[single-strategy probe] Turtle breakout(ADX=29.3)', 'gross': 186972.79}, 'archive': {'date': '2025-04-24', 'symbol': '300394', 'side': 'buy', 'shares': 9000, 'price': 34.6246, 'reason': '[single-strategy probe] Turtle breakout(ADX=29.3)', 'gross': 311621.31}}`

### common-5

- **First fill-day soft multiset diff:** `2025-04-24`
  - that day fills: main=4 archive=6
  - main-only sample: `['2025-04-24', '300394', 'buy', 12200]` ×1; …(+3 more)
  - archive-only sample: `['2025-04-24', '300394', 'buy', 9000]` ×3; …(+1 more)
- **First buy-day soft multiset diff:** `2025-04-24`
- **First buy-size diff (through first equity):** `{'date': '2025-04-24', 'symbol': '300394', 'main_shares': 12200, 'archive_shares': 27000}`
- **First equity diff:** `{'index': 16, 'main': {'date': '2025-04-24', 'equity': 1989755.57}, 'archive': {'date': '2025-04-24', 'equity': 1985119.52}, 'delta_main_minus_archive': 4636.04}`
- **First main-only AB5 trim (no archive sell same day/sym):** `{'date': '2025-06-25', 'symbol': '300308', 'side': 'sell', 'shares': 100, 'price': 125.4744, 'reason': 'account_budget_trim', 'gross': 12547.44}`
- **First sorted soft trade mismatch:** `{'index': 0, 'main': {'date': '2025-04-24', 'symbol': '300394', 'side': 'buy', 'shares': 12200, 'price': 34.6246, 'reason': '[single-strategy probe] Turtle breakout(ADX=29.3)', 'gross': 422420.0}, 'archive': {'date': '2025-04-24', 'symbol': '300394', 'side': 'buy', 'shares': 9000, 'price': 34.6246, 'reason': '[single-strategy probe] Turtle breakout(ADX=29.3)', 'gross': 311621.31}}`

### common-17

- **First fill-day soft multiset diff:** `2025-04-24`
  - that day fills: main=3 archive=4
  - main-only sample: `['2025-04-24', '300502', 'buy', 6300]` ×1; …(+2 more)
  - archive-only sample: `['2025-04-24', '300394', 'buy', 9000]` ×1; …(+1 more)
- **First buy-day soft multiset diff:** `2025-04-24`
- **First buy-size diff (through first equity):** `{'date': '2025-04-24', 'symbol': '300394', 'main_shares': 0, 'archive_shares': 9000}`
- **First equity diff:** `{'index': 16, 'main': {'date': '2025-04-24', 'equity': 1993577.17}, 'archive': {'date': '2025-04-24', 'equity': 1990757.96}, 'delta_main_minus_archive': 2819.22}`
- **First main-only AB5 trim (no archive sell same day/sym):** `{'date': '2025-06-25', 'symbol': '002384', 'side': 'sell', 'shares': 100, 'price': 36.6733, 'reason': 'account_budget_trim', 'gross': 3667.33}`
- **First sorted soft trade mismatch:** `{'index': 0, 'main': {'date': '2025-04-24', 'symbol': '300502', 'side': 'buy', 'shares': 3600, 'price': 45.4954, 'reason': '[single-strategy probe] Turtle breakout(ADX=24.0)', 'gross': 163783.62}, 'archive': {'date': '2025-04-24', 'symbol': '300394', 'side': 'buy', 'shares': 9000, 'price': 34.6246, 'reason': '[single-strategy probe] Turtle breakout(ADX=29.3)', 'gross': 311621.31}}`

## Plain language — what archive does less / more

### Archive does LESS
1. **Ordinary held trims:** main AB5 pro-rata `account_budget_trim` when gross > gross_cap (reserve funding). Archive risk model does not emit these; trim count = 0.
2. **Buy clips:** main scales buys by `min(gross_scale, gap_scale)` and removes shares when the envelope binds. Archive shows 0 clip intents in envelope events.
3. **Trade churn / sleeve fills:** ~40–60% of main fill count; lower turnover, fewer sell buckets.

### Archive does MORE (economically)
1. **Lets positions ride** through ordinary overshoot that main would trim or clip.
2. **Higher terminal TR** on common-3/5/17 in this window (with **worse maxDD** on common-5/17).
3. Early path: different buy clip/sizing on **2025-04-24** already separates equity — absorb work that only tweaks late near-peak deferrals cannot close the full archive gap.

## Implications for topics B/C/D

| Topic | Audit implication |
|-------|-------------------|
| **B buy-side** | Gap/gross clipping is a real main−archive gap (14–63 intents on these pools). Prefer soft scaling toward gross when gap is soft / protection complete — not wholesale gap delete (prior skip never fired or broke units). |
| **C trim intensity** | Main funds 100% of ordinary overshoot via pro-rata trims; archive funds 0%. A fractional fund (e.g. 50%) is the natural middle — but must avoid Core17 −1.7/−3 collapse from near-peak full deferrals. |
| **D archive-like ordinary** | Combine intensity decay + hard defer budget/coupon rather than razor deletion; diversification/budget structure over symbol exceptions. Early buy-path gap remains structural. |

## Artifacts
- `archive-main-diff-audit.json` (compact)
- `archive-main-diff-audit-full.json` (per-pool detail)

