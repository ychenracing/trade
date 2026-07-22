# Quant Fusion v17 Automatic Optimization Result

Status: `candidate_rejected_on_holdout`.

The final holdout was not used to choose parameters. Candidate selection used only expanding training windows, later validation windows, and higher-cost validation stress runs.

Selected candidate: `candidate-94414f3e926e`.
Recommended for execution: `baseline`.

| Final holdout | Total return | Maximum drawdown | Sharpe | Calmar |
|---|---:|---:|---:|---:|
| v17.1 baseline | 152.84% | -11.40% | 3.594 | 44.214 |
| selected | 124.97% | -11.12% | 3.220 | 34.320 |

Return delta: -27.87%; drawdown delta: 0.28%.

Promotion gate: failed; the v17.1 baseline is retained.

A positive holdout result is evidence for this frozen snapshot, not a guarantee that the same parameters will remain optimal in live trading.
