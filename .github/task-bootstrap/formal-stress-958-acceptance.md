# Formal Stress 958 Acceptance Task

- Objective: run the exact current 17-symbol, 958-scenario formal stress acceptance; retain the truthful accepted/rejected outcome; update directly related tests and documentation so counts, semantics, provenance, and publication state agree.
- Acceptance criteria: exact 958/958 completion; 958 unique scenario IDs; finite metrics; `ProductionReplayEngine` / `production_daily_replay`; valid source/data/scenario/run fingerprints; all current gate families evaluated; historical artifacts byte-identical; current comments/docs/tests synchronized; required CI green; merge through PR only.
- Included scope: current formal stress runner and artifact publication, directly related tests, README, `docs/VALIDATION.md`, `docs/ARCHITECTURE.md`, and generated 17-symbol stress evidence.
- Excluded scope: changing alpha, risk economics, the 18% threshold, universe membership/order, seeds, fees, slippage, capacity, T+1, matching semantics, or forcing rejected evidence canonical.
- Verified baseline SHA: `88690b2cb1db8803c8f6cc48f774eebe9db40b9e` (`main`, PR #22 merged).
- Feature branch: `codex/formal-stress-958-acceptance`.
- Related PR: #24.
- Current verified state: PR #22 merged the authoritative 17-symbol universe and all five final CI jobs passed. Earlier PR #24 attempts failed before stress execution because they were based on pre-merge `main` and one generated Python heredoc was malformed. This checkpoint merges the verified current `main`, removes superseded runners, and retains one validated runner.
- Immutable constraints: no direct push to `main`; no reset, clean, rebase, force push, or history rewrite; the historical 22-symbol/983-scenario artifact remains byte-identical; exact GitHub state is authoritative.
- Risks: the complete formal run can truthfully reject; source-fingerprint changes after execution invalidate provenance; runtime exceeds the interactive 10-minute wait limit.
- UNKNOWN: current 958 gate outcome and exact complete runtime.
- Next step: run the retained preflight, commit the stable current-plan documentation/test checkpoint, execute the exact complete 958 plan once, validate and persist the result, remove all temporary task infrastructure, then complete PR/CI/merge closure.
