# Formal Stress 958 Consistency Closure

- Objective: remove any temporary acceptance infrastructure left on main and enforce agreement among the current 17-symbol/958 code contract, comments/docstrings, tests, README, architecture, validation documentation, and recorded result.
- Acceptance: no temporary task files; current plan remains 958 with exact family counts; stale current-plan 983 language is rejected; historical 22-symbol/983 evidence remains explicitly historical; full engineering checks and PR CI pass.
- Scope: comments/docstrings/documentation consistency, one focused automated contract, temporary file cleanup.
- Excluded: alpha, risk economics, thresholds, universe, seeds, data, formal result, artifacts, matching/execution behavior.
- Verified baseline SHA: 88690b2cb1db8803c8f6cc48f774eebe9db40b9e
- Branch: codex/formal-stress-958-consistency-closure
- Risks: overbroad text replacement could corrupt legitimate historical 983 evidence; scanner false positives.
- UNKNOWN: whether temporary task files survived the prior merge; exact set is verified below.
- Next: add a semantic consistency test, run focused/full checks, push, trigger PR CI, merge if green.
