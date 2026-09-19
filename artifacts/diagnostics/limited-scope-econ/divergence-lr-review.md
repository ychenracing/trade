# Limited-scope econ — divergence / LR review (PR #126)

as_of: 2026-09-19T20:47+0800

## Taxonomy (binding)

| Class | Items |
| --- | --- |
| Implementation / evidence-export error | E1 common-5 scored with Core17 §4.2 routes; E2 fees/turnover written as 0.0; E3 `M_delta_pp` stores fraction not pp |
| True economic failure (summary-only) | Primary & alt fail Core17 §4.2 on retained summaries; wealth down; B not cut (primary B/fills up); alt also fails MDD fraction gate |
| Cannot fully reproduce | Candidate sources/patches; B0 & candidate raw replays/fills/risk events/run logs; LR residual-risk cover; accidental hard-path edits; equity divergence windows |

## LR vs required overshoot

**CANNOT_FULLY_REPRODUCE.** Live patches not in reflog/stash/worktree/PR/unreachable objects. Incumbent ordinary preserve still uses per-book `math.ceil` on main. Phase0 shows large amp on immaterial days, but that does not verify candidate LR met notional/risk-reduction cover.

## Other call paths

**CANNOT_FULLY_REPRODUCE.** Branch production tree matches `origin/main` after revert. No patch left to audit alert/shock/direct-loss/buy paths.

## Wealth loss

Summaries only: primary Core17 `W_ratio≈0.782` with `B_ratio≈1.132` and `fills_delta=+50` — path divergence, not a labeling quirk. Equity-curve windows **MISSING**.

## common-5

Under §4.3 protection only, both common-5 summaries would pass W≥0.97 and MDD fraction Δ≤0.005. **Not** a promotion path; Core17 remains decisive failure.

## Fixes

Only evidence/documentation errata on this PR branch. No economic retune. No main merge.
