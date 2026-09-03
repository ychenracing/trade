# HANDOFF_PROMPT — 18% Drawdown Trust Closure

Continue GitHub repository `ychenracing/trade` from Draft PR #20 on branch
`codex/18pct-drawdown-trust-closure`. Re-read the live PR, branch head,
applicable `AGENTS.md`, project brief, and this handoff before changing files.
The last verified remote head before the handoff checkpoint was
`06f488d0ae36b00c06dd83e90eed73177bc7bb4d`; use the live PR head as authority.

## Fixed product decisions

- Keep `688205`; do not special-case it or any symbol, date, scenario, or seed.
- The exact canonical 983-scenario hard gate remains
  `abs(max_drawdown) <= 0.18`; do not relax it.
- Do not change alpha, indicators, breakout/exit signals, scoring, symbol
  profiles, universe/order, formal scenarios, frozen data, fees, slippage,
  capacity, T+1, limit, or matching semantics.
- `add-one` relative terminal wealth is a robustness diagnostic, not account
  drawdown and not an absolute `-18%` gate.
- Do not run canonical 983 as a tuning loop or publish/merge a rejected result.

## Completed and remotely recoverable

- Bootstrap: `92c98887c825ce6185cf3a050adef8cefdd11302`.
- Checkpoint A, contract v2: `609fd1c175c04a9f11adcbd4ac819b1892980d71`.
- Diagnostic runner: `f01b305df5f818fb10bc9033167a8207ffd10c54`.
- Checkpoint B evidence: `06f488d0ae36b00c06dd83e90eed73177bc7bb4d`.
- Contract v2 separates `absolute_hard_gates`,
  `robustness_diagnostics`, future-incumbent `promotion_gates`, and explicit
  one-time `initial_baseline_gates`/publication.
- `--scenario-ids-file` is diagnostic-only and cannot publish formal artifacts.
- The 281-scenario cohort contains all 152 retained failures, all 123 boundary
  cases, and six de-duplicated controls. Its ID-file SHA-256 is
  `ae7f45ef63eea103f1fc2c7ebcb19541d30d64c590518a2ceb2b0a4c4234f196`.
- Root-cause diagnostic SHA-256 is
  `eabde710c19e4d33d76d44ccc1ba8e242085119046f36cb8d7653958436f2830`.

## Root cause

The merged account commonly carries 90%-100% close exposure in only two or
three symbols. A synchronized loss can cross 18% before the first close-based
risk event can act: 109 of 152 retained failures were already beyond 18% at
their first recorded trigger. The failure median trigger exposure was 93.01%,
133 of 152 failures held only two or three symbols, and 28 failures still had
later warning-period buys. Threshold-only retuning cannot repair a drawdown
already recorded at the confirming close.

## Bounded candidate result

Three preregistered, general mechanisms were evaluated only on decisive L1
scenarios. All failed, so no candidate was selected and every production
prototype/test hook was removed:

1. C1 first-alert 25% account reduction: 4/7 tested paths still exceeded 18%;
   `prefix-05` wealth retention was about 65.69% versus the required 99%.
2. C2 85% gross cap while holding at most three symbols: 7/8 tested paths
   exceeded 18%; `prefix-05` wealth retention was about 23.78%.
3. C3 pre-armed 17.5% standing account stop: the first stop contained an
   individual discontinuity, but next-session re-entry caused 6-41 stop cycles;
   6/8 paths exceeded 18% and `prefix-05` wealth retention was about 29.63%.

Exact available results and rejection reasons are in
`artifacts/diagnostics/18pct-drawdown-pareto.json`. C1 console values are marked
with their retained output precision; C2/C3 values are full precision.

## Current truth and immutable evidence

- No production tail-risk mechanism is selected.
- No affected-family L3 or new canonical 983 L4 was run.
- No contract-v2 accepted baseline exists; no canonical artifact was published.
- PR #20 must remain Draft and must not merge.
- The old retained rejected artifact remains
  `artifacts/validation/candidates/stress-f5625e5b5813a5b58c52d076ad3c38e33d8b3292-rejected.json`
  with SHA-256
  `adda276bea8a11b76fa6881e4e7a9770bf8cfb79bb93a397aa5aa405327358c2`.
- `688205` remains present with no special branch.
- Core alpha is unchanged.

## Verified engineering state

- `python -m pytest -q`: 510 passed, 223 subtests passed.
- Related stress/risk/regression selection: 146 passed, 183 subtests passed.
- `python -m compileall -q .`: passed.
- `python -m ruff check --select=E,F,W --ignore=E501,E402,E731,E741 .`: passed.
- `python -m pyright quantfusion`: 0 errors, 0 warnings.
- `python -m bandit -r quantfusion scripts -ll`: no medium/high issues.
- `python -m pip_audit --strict -r requirements-lock.txt`: no known vulnerabilities.

## Required owner decision before more implementation

The existing product constraints have no feasible candidate in the authorized
bounded search. Ask the owner to choose whether to authorize a materially
longer post-stop cooldown/lower-exposure regime despite expected wealth loss
beyond the current guards, or to revise another return/behavior constraint.
Do not infer permission to relax the 18% hard gate, add a fourth candidate,
change execution semantics, or resume L3/L4. After a decision, preregister one
coherent mechanism and rejection conditions before running new economic tests.
