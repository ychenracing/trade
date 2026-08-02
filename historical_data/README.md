# Historical validation snapshot

This directory is the reproducible evidence set for regime routing and the
2022–2024 validation. It is separate from `market_data`: the latter remains
byte-for-byte frozen for the published 2025–2026 regression baseline.

## Contents

- 16 A-share AI hardware / semiconductor stocks, forward-adjusted (`qfq`).
- `000300` (CSI 300) and `000682` (technology risk index), unadjusted and used
  only as non-trading regime evidence.
- `SHA256SUMS`, which freezes every CSV byte.
- `MANIFEST.json`, which records dates, row counts, splice counts, overlap
  comparisons and volume-unit normalization.

The historical prefix came from the audited Eastmoney/Tencent validation
snapshot. The existing repository's 2024+ `market_data` rows were retained as
the authoritative tail wherever the two snapshots overlapped. Common-date OHLC
was compared before the splice; the largest absolute difference is recorded per
symbol in `MANIFEST.json`. Volume units were normalized from lots to shares when
needed, including the source-specific `688256` scale.

## Reproduce integrity checks

```bash
cd historical_data
sha256sum -c SHA256SUMS
```

`run_regime_validation.py` independently records SHA-256, first/last date and
row count in `regime_validation_results.json`.

## Methodological limits

- The snapshot has no 2021 stock history, so a 240-session selector cannot
  form valid evidence at the start of 2022. The router deliberately holds cash
  rather than shorten the lookback after observing the year.
- IPOs retain their true shorter histories. A symbol with no usable history is
  omitted without invalidating the rest of a requested pool.
- Public qfq histories can be restated after corporate actions. Refreshing this
  directory creates a new research snapshot and requires a new manifest and
  regression review; it must not silently overwrite these results.
