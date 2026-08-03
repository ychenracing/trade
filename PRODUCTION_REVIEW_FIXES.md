# Production Review Fixes

This release implements the high-impact findings from the 2026-08 code
and strategy audit while preserving the frozen bull engine.

## Correctness

- Normalizes Eastmoney/Tencent board-lot volume to shares and preserves
  Sina share volume, so the 0.5% ADV rule is provider invariant.
- Rebuilds legacy incremental caches that lack a verified volume-unit
  contract.
- Rejects stale stock observations during positive-momentum leader
  selection, not only stale fixed-index observations.
- Refuses to silently shrink the expected daily universe after a normal
  listed stock has a provider or parsing failure.

## Decision routing

- Keeps historical backtest routing frozen and causal.
- Adds a separate current point-in-time route refreshed from the latest
  common date. Route disagreement suppresses new buys while preserving
  sells, avoiding both look-ahead performance and stale deployment buys.
- Refreshes the two fixed indices through the dedicated index endpoint;
  provider failure preserves last-good files and remains auditable.

## Risk and account handling

- Adds a loose ATR/hard disaster stop plus a long time stop to the weak
  leader strategy before the 30% profit chandelier activates.
- Re-enables `--account` through a standalone point-in-time account signal
  engine. Real holdings never enter the simulated account ledger.

## Reproducibility

- Adds exact full-precision bull regression baselines.
- Adds a resolved hash lock file for dependencies.
- Includes the core engine in Pyright basic checking.
- Adds equal-weight and causal Top-3 buy-and-hold attribution utilities.

The account output remains decision support only. It does not place orders
and cannot guarantee a fixed future drawdown.
