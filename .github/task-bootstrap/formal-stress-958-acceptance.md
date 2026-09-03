# Formal Stress 958 Acceptance Task

- Objective: run the exact current 17-symbol, 958-scenario formal stress acceptance; retain truthful accepted/rejected outcome; update all directly related code comments and documentation so counts, semantics, provenance, and publication state agree.
- Acceptance criteria: exact 958/958 completion; unique scenario IDs; finite metrics; production replay semantics; valid source/data/scenario/run fingerprints; all current contract gates evaluated; immutable prior artifacts preserved; comments/docs/tests synchronized; required CI green; merge only when repository and product gates permit.
- Included scope: current formal stress plan/runner/artifact publication, directly related tests, README, docs/VALIDATION.md, docs/ARCHITECTURE.md, CLI help/comments, generated current 17-symbol stress evidence.
- Excluded scope: changing alpha, risk economics, 18% threshold, universe membership/order, seeds, fees/slippage/capacity/T+1/matching semantics, or forcing rejected evidence canonical.
- Verified baseline SHA: aaa136fb6c9cf65d6abafde5b1707df4d7297cf4
- Feature branch: codex/formal-stress-958-acceptance
- Immutable constraints: no direct push to main; no reset/clean/rebase/force-push/history rewrite; old 22-symbol/983 artifacts remain byte-identical; exact current GitHub state is authoritative.
- Risks: full formal run can truthfully reject; source-fingerprint changes after the run invalidate provenance; stale 983 wording or hard-coded counts can create contract drift.
- UNKNOWN: current 958 gate outcome, incumbent/baseline eligibility, exact runtime until the complete run finishes.
- Next step: verify this remote checkpoint SHA, inspect current main/PR/workflow/contract, finish all fingerprinted source/comment updates, run focused verification, then execute the exact complete 958 plan once.
