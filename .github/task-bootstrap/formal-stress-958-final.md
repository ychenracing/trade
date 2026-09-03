# Exact 958-scenario formal stress acceptance

## Objective
Run the exact current 17-symbol formal stress plan once, persist the truthful accepted/rejected result, synchronize executable behavior, tests, comments and documentation, then complete PR/CI/merge closure.

## Acceptance criteria
- Base is the latest verified `main` containing the ordered 17-symbol trade pool.
- Exact plan is 958 unique scenarios: 17 prefix, 17 leave-one-out, 24 add-one, 750 random subset and 150 permutation.
- Complete `ProductionReplayEngine` replay with current frozen data, seeds, fees, slippage, T+1 and execution semantics.
- No threshold, strategy, data, scenario or seed change to manufacture acceptance.
- Accepted result becomes the first current-plan canonical artifact only through the explicit fail-closed baseline path; rejected result stays non-canonical and is still preserved.
- Historical 22-symbol/983-scenario rejected artifact remains byte-identical.
- README, validation, architecture, code comments and tests describe the same current 17/958 contract and exact result.
- Complete engineering checks, PR checks and post-merge verification pass.

## Scope
Formal stress orchestration, artifact publication, exact current-contract tests, stale current-plan comments/documentation, and final evidence only.

## Out of scope
Alpha, portfolio-risk policy, thresholds, universe membership/order, frozen market data, scenario definitions/seeds, execution semantics, fees/slippage/capacity, and historical artifact mutation.

## Baseline
This branch was created from the then-current `main`. The workflow must independently verify that `main` contains the ordered 17-symbol universe and exact 958-plan definition before any long replay.

## Risks / UNKNOWN
- `main` may still be awaiting the preceding PR #22 merge; the workflow must fail closed rather than run the old 22/983 plan.
- The formal gates may reject the new 17-symbol plan. Rejection is a valid product result and must not be hidden.
- Runtime is long; exactly one stable full run is permitted unless an operational failure produces no complete artifact.

## Next step
Push and verify this checkpoint, then launch one self-contained GitHub Actions workflow that validates the base, runs engineering checks, executes the exact 958 plan once, persists the result, removes temporary infrastructure, waits for final PR CI, and merges only when repository protections permit.
