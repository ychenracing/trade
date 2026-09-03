# 18% Absolute Drawdown Trust Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every exact canonical 983-scenario production replay satisfy `abs(max_drawdown) <= 0.18` without removing or special-casing 688205, weakening the threshold, changing alpha, or using the full matrix as a tuning loop.

**Architecture:** Keep formal contract evaluation in `stress_metrics.py`, publication/provenance in `stress_artifacts.py`, and CLI orchestration in `stress.py`. Diagnose the retained results in one batch, then change only the shared portfolio-risk path supported by that evidence; retain future incumbent comparison separately from the one-time initial-contract baseline transition.

**Tech Stack:** Python 3.12, pandas, unittest/pytest, existing Quant Fusion production replay and JSON artifact code.

**Spec:** User-approved `trade_18pct_drawdown_trust_closure_prompt.md` attachment and the live state in PR #20.

## Global Constraints

- The exact canonical 983-scenario plan must satisfy `max(abs(result.max_drawdown)) <= 0.18` with a uniform `1e-12` numerical tolerance.
- Keep 688205, the universe order, all scenarios, seeds, frozen data, fees, slippage, capacity, T+1, limit and matching semantics unchanged.
- Do not restore cross-sleeve pending-order pseudo-netting or change trend/weak alpha, indicators, scoring, or symbol profiles.
- Do not add symbol/date/scenario/seed-specific behavior or use broad/random/Bayesian parameter search.
- Preserve `prefix-05` wealth ratio >= 0.99, other prefix wealth ratios >= 0.95, matched worst total-return deterioration <= 0.02, and worst add-one diagnostic deterioration <= 0.03.
- Diagnostic selection can never publish formal artifacts; old rejected evidence remains byte-identical.
- Run L1 exact checks, L2 failure/boundary/control batch, L3 affected families/shards, then one stable L4 canonical run.
- Every material milestone is verified, committed, pushed through the GitHub API, remote-head verified, and reflected in PR #20.

---

### Task 1: Contract v2 and Initial-Baseline State Machine

**Files:**
- Modify: `quantfusion/application/stress_metrics.py`
- Modify: `quantfusion/application/stress_artifacts.py`
- Modify: `quantfusion/application/stress.py`
- Test: `tests/unit/test_stress_scenarios.py`

**Interfaces:**
- Consumes: exact formal `results`, optional accepted contract-v2 incumbent, optional explicit transition reference.
- Produces: `absolute_hard_gates`, `robustness_diagnostics`, unchanged future `promotion_gates`, `initial_baseline_gates`, and contract-v2 accepted/rejected artifacts.

- [x] **Step 1: Write failing metric-separation tests**

  Add literal fixtures proving `-0.180000000001` fails, `-0.18` passes, obsolete 20/22/22.5% observations cannot override the 18% worst-case gate, and a `-0.2349` add-one wealth change remains diagnostic rather than deciding the drawdown gate.

- [x] **Step 2: Verify the new metric tests fail for the missing v2 API**

  Run: `python -m pytest -q tests/unit/test_stress_scenarios.py -k "absolute_drawdown or robustness_diagnostic"`

  Expected: failures because `_absolute_hard_gates` and `_robustness_diagnostics` do not yet exist.

- [x] **Step 3: Implement the minimum orthogonal metric functions**

  Add `STRESS_CONTRACT_VERSION = 2`; implement `_absolute_hard_gates(results)` with the one 18% drawdown check plus still-applicable count/bucket correctness gates; implement `_robustness_diagnostics(results)` with add-one minimum, p05, p10, median, worst scenario ID, paired drawdown change, TradeRecord/bucket deltas, and terminal-lock transition. Keep `_promotion_gates` for future incumbent-relative checks.

- [x] **Step 4: Run the exact metric tests green**

  Run: `python -m pytest -q tests/unit/test_stress_scenarios.py -k "absolute_drawdown or robustness_diagnostic"`

  Expected: all selected tests pass.

- [x] **Step 5: Write failing artifact/version/bootstrap tests**

  Cover: different contract versions cannot be incumbents; no incumbent without `--establish-initial-baseline` rejects; the flag with an incumbent fails closed; partial/diagnostic/old-contract/hard-gate-failed candidates cannot bootstrap; an explicit current-semantic transition reference must pass the stated wealth/return/add-one protections; old rejected bytes are untouched.

- [x] **Step 6: Verify the bootstrap tests fail for the missing flow**

  Run: `python -m pytest -q tests/unit/test_stress_scenarios.py -k "contract_version or initial_baseline or old_rejected"`

  Expected: failures because the contract version and explicit baseline state machine are absent.

- [x] **Step 7: Implement contract-v2 provenance and publication**

  Include `stress_contract_version` in provenance and run signatures; add `--establish-initial-baseline` and explicit `--initial-baseline-reference`; require both only when no current-contract incumbent exists; validate reference scenario/provenance compatibility without treating the old-contract reference as an incumbent; publish `baseline_kind=initial_current_contract` only when absolute, initial-reference, completeness, permutation, and provenance gates all pass.

- [x] **Step 8: Run the complete stress contract module**

  Run: `python -m pytest -q tests/unit/test_stress_scenarios.py`

  Expected: 0 failures; existing tests updated to the v2 names and semantics.

- [x] **Step 9: Offline-reevaluate retained results without replay**

  Run a short Python command that loads `artifacts/validation/candidates/stress-f5625e5b5813a5b58c52d076ad3c38e33d8b3292-rejected.json`, applies `_absolute_hard_gates` and `_robustness_diagnostics`, and records the exact JSON in the PR.

  Expected: rejected because worst max drawdown is `-0.2122314802406625`; add-one `-0.23490347753273277` remains diagnostic.

- [x] **Step 10: Checkpoint A**

  Run compile/lint on changed files plus the complete stress test module; commit one coherent contract-v2 state, push via GitHub Git Data API, verify remote head, and replace PR #20's current state.

### Task 2: Batch Diagnostic Selector and Evidence

**Files:**
- Modify only if needed: `quantfusion/application/stress_scenarios.py`
- Modify only if needed: `quantfusion/application/stress.py`
- Modify only if needed: `quantfusion/application/stress_metrics.py`
- Test: `tests/unit/test_stress_scenarios.py`
- Create: `artifacts/diagnostics/18pct-drawdown-root-cause.json`

**Interfaces:**
- Consumes: retained candidate results and an ordered newline-delimited scenario-ID file.
- Produces: one non-canonical diagnostic artifact with compact peak/trough, risk-trigger, execution-overshoot, exposure/cash, concentration, trade/bucket, lock and rearm evidence.

- [x] **Step 1: Freeze the diagnostic cohort**

  Derive all 152 `abs(max_drawdown) > 0.18` failures, all 123 `0.17 <= abs(max_drawdown) <= 0.18` boundaries, `prefix-05`, `add-one-05-688205`, and one deterministic passing control per family from the retained candidate. Preserve canonical order and de-duplicate IDs.

- [x] **Step 2: Write failing multi-ID selector tests**

  Prove file-order normalization to canonical order, duplicate rejection, unknown-ID rejection, empty-file rejection, selector/canonical separation, and inability to publish diagnostic results.

- [x] **Step 3: Verify selector tests fail**

  Run: `python -m pytest -q tests/unit/test_stress_scenarios.py -k "scenario_ids_file or diagnostic_selection"`

  Expected: failures because `--scenario-ids-file` is missing.

- [x] **Step 4: Implement only the required file selector**

  Extend `select_scenarios` and the CLI with `--scenario-ids-file`; do not change scenario construction, IDs, order, signature, or formal completeness.

- [x] **Step 5: Add compact diagnostic telemetry**

  From the real replay result, serialize only summary evidence: peak/trough/recovery dates, alert/confirmed/emergency/terminal dates and drawdowns, first following executable reduction, overshoot, peak/trigger/trough exposure and cash, held/group concentration, warning-period buys/adds, risk/sector/re-entry counts, locks/rearm, and trade/bucket deltas. Do not add daily paths to canonical artifacts.

- [x] **Step 6: Run selector and telemetry tests**

  Run: `python -m pytest -q tests/unit/test_stress_scenarios.py -k "scenario_ids_file or diagnostic"`

  Expected: 0 failures.

- [x] **Step 7: Run the cohort once as one diagnostic batch**

  Set `CHECKPOINT_A_REMOTE_SHA` to the exact remote PR head returned and read back by the GitHub connector, then run: `python -m quantfusion.application.stress --source-revision "$CHECKPOINT_A_REMOTE_SHA" --scenario-ids-file artifacts/diagnostics/18pct-drawdown-cohort.txt --diagnostic-checkpoint artifacts/checkpoints/18pct-diagnostic.json --diagnostic-output artifacts/diagnostics/18pct-drawdown-root-cause.json`

  Expected: every frozen cohort ID completes once; output is diagnostic/non-canonical.

- [x] **Step 8: Derive one common-cause report**

  Compare all failures, boundaries and controls; quantify warning-to-trigger, trigger-to-fill overshoot, post-warning risk additions, exposure, concentration, locks and rearm. Record supported causes and rejected hypotheses in the diagnostic artifact and PR body.

- [ ] **Step 9: Checkpoint B**

  Run exact selector/diagnostic tests; commit code plus compact evidence, push, verify remote head, and refresh PR #20.

### Task 3: Preregister and Evaluate at Most Three General Risk Candidates

**Files:**
- Modify after evidence: the minimum subset of `quantfusion/config/portfolio.py`, `quantfusion/engine/universe.py`, `quantfusion/engine/ensemble_allocation.py`, `quantfusion/engine/ensemble_orchestration.py`, `quantfusion/risk/managers.py`
- Test: `tests/regression/test_quant_fusion.py`
- Test: directly affected unit/integration tests

**Interfaces:**
- Consumes: Checkpoint-B common-cause evidence.
- Produces: one selected general portfolio-risk mechanism with no symbol/date/scenario branch.

- [ ] **Step 1: Preregister no more than three causal candidates in PR #20**

  For each candidate, freeze exact thresholds/mechanism, causal chain, expected return/turnover/cash impact, and rejection conditions before running candidate results. Candidate 1 changes one mechanism; Candidate 2 may add one mechanism to Candidate 1; Candidate 3 may adjust trigger timing only if measured overshoot supports it.

- [ ] **Step 2: Write failing synthetic risk tests for Candidate 1**

  The tests must prove warning/guard state prevents net-risk increases, ordinary buys cannot override risk reductions, state persists across days/routes, rearm uses valid trading-day cooldown, and arbitrary symbols—including but not branching on 688205—receive identical treatment.

- [ ] **Step 3: Verify Candidate-1 tests fail for the missing behavior**

  Run only the exact new test methods; confirm each fails on the intended observable behavior.

- [ ] **Step 4: Implement Candidate 1 minimally**

  Reuse existing manager state, pending-signal priority, and liquidation/action adapters. Do not add a parallel risk engine or dependency.

- [ ] **Step 5: Run Candidate-1 L1 tests**

  Run exact new tests plus directly affected regression methods; require 0 failures.

- [ ] **Step 6: Run Candidate-1 L2 cohort**

  Run the full failure/boundary cohort, `prefix-05`, `add-one-05-688205`, and family controls in one diagnostic batch. Reject immediately if any cohort/control drawdown exceeds 18%, prefix protections fail, worst return deteriorates by more than 0.02, add-one worsens by more than 0.03, or safety invariants fail.

- [ ] **Step 7: Evaluate Candidate 2 only if Candidate 1 is dominated or fails**

  Repeat the exact TDD and L2 pattern with the preregistered second mechanism; do not modify unrelated parameters.

- [ ] **Step 8: Evaluate Candidate 3 only if Candidate 2 is dominated or fails**

  Repeat once with the preregistered third mechanism. If all three fail, perform one common-cause review and one mechanism-level redesign only, as authorized by the spec.

- [ ] **Step 9: Select the non-dominated candidate**

  Compare drawdown, wealth retention, worst/median return, Sharpe, Calmar, TradeRecords, buckets, risk actions, cash and exposure. Select the feasible candidate with highest wealth retention and lowest complexity/turnover.

- [ ] **Step 10: Checkpoint C**

  Run all affected L1/L2 checks; commit only the selected implementation/tests and the Pareto evidence, push, verify remote head, and refresh PR #20.

### Task 4: Affected Family/Shard L3

**Files:**
- Modify only for common-cause defects found by L3: the Task-3 affected production/test files
- Update: PR #20 verification and candidate evidence

**Interfaces:**
- Consumes: Checkpoint-C selected candidate.
- Produces: scenario-ID-aligned L3 comparison against the retained candidate.

- [ ] **Step 1: Run fixed families and deterministic affected random shards**

  Run all prefixes, leave-one-out, add-one, permutation controls, and deterministic shards covering every original failure/boundary random subset.

- [ ] **Step 2: Collect every L3 failure before editing**

  Produce one list of all drawdown, return, wealth-retention, trade/bucket, cash/exposure, risk-action and invariant failures.

- [ ] **Step 3: Apply at most one coherent common-cause fix**

  Write the exact failing test first, verify red, implement the shared fix, and rerun exact failures plus the affected shard/module before expanding.

- [ ] **Step 4: Lock the stable Final Candidate**

  Require all affected L3 comparisons and safety invariants to pass. No further economic parameter changes after this point without returning to targeted and L3 validation.

- [ ] **Step 5: Checkpoint D**

  Commit the stable final-candidate source/tests/evidence, push, verify remote head, and refresh PR #20.

### Task 5: Engineering Gates, One Canonical L4, Artifact and Docs

**Files:**
- Modify before L4: all fingerprinted Python comments/constants needed for the final behavior
- Modify after accepted L4 only if outside fingerprints: `README.md`, `docs/VALIDATION.md`, `docs/ARCHITECTURE.md`
- Create: `artifacts/validation/prefix_stress.json`
- Create: `artifacts/validation/universe_stress.json`

**Interfaces:**
- Consumes: stable Checkpoint-D source and its verified remote SHA.
- Produces: accepted contract-v2 canonical artifacts and synchronized documentation.

- [ ] **Step 1: Finish all source-fingerprinted code/comments**

  Confirm no Python source change remains before running canonical L4.

- [ ] **Step 2: Run complete engineering verification once**

  Run: `python -m compileall -q .`

  Run: `~/.local/bin/ruff check --select=E,F,W --ignore=E501,E402,E731,E741 .`

  Run: `python -m pytest -q`

  Run: `~/.local/bin/pyright quantfusion`

  Run: `~/.local/bin/bandit -r quantfusion scripts -ll`

  Run: `~/.local/bin/pip-audit --strict -r requirements-lock.txt`

  Run the exact economic-regression command from `.github/workflows/ci.yml`.

- [ ] **Step 3: Push the fingerprint-stable source and verify remote SHA**

  Commit/push if Step 2 required any non-economic correction; obtain the exact remote 40-character source revision.

- [ ] **Step 4: Run one exact canonical 983 L4**

  Set `FINAL_SOURCE_SHA` to the exact remote PR head returned and read back after the fingerprint-stable source push, then run: `python -m quantfusion.application.stress --source-revision "$FINAL_SOURCE_SHA" --establish-initial-baseline --initial-baseline-reference artifacts/validation/candidates/stress-f5625e5b5813a5b58c52d076ad3c38e33d8b3292-rejected.json`

  Expected: exact 983/983, unique IDs, finite production metrics, invariant permutations, absolute worst drawdown <= 0.18, return protections pass, and canonical publication succeeds.

- [ ] **Step 5: Verify immutable and new artifacts**

  Confirm the old rejected artifact SHA-256 remains `adda276bea8a11b76fa6881e4e7a9770bf8cfb79bb93a397aa5aa405327358c2`; record new canonical paths, SHA-256, source/data/scenario/run fingerprints and exact metrics.

- [ ] **Step 6: Update docs outside the source fingerprint**

  Accurately explain the 18% frozen-replay target, next-open overshoot limitation, add-one diagnostic meaning, gate separation, explicit initial baseline, and exact accepted evidence. Remove stale 20/22/22.5% acceptance claims and any wording that calls -23.49% account loss/drawdown.

- [ ] **Step 7: Verify docs and artifact contracts without rerunning 983**

  Run repository hygiene, stress artifact tests, JSON parsing, compile, and lint only. Confirm documentation files are not in the source/data/scenario/run fingerprint inputs.

- [ ] **Step 8: Checkpoint E**

  Commit canonical artifacts/docs, push, verify remote head, and fully refresh PR #20 with exact L1-L4 evidence and merge readiness.

### Task 6: Review, CI, Squash Merge and Post-Merge Verification

**Files:**
- Modify only if an actionable review/required-check failure proves a scoped defect.
- Update: PR #20 body.

**Interfaces:**
- Consumes: Checkpoint-E PR head.
- Produces: verified squash merge on `main`.

- [ ] **Step 1: Request focused code review**

  Review contract separation, baseline state machine, diagnostic isolation, risk economics, causal execution, artifact immutability, and all acceptance thresholds against the PR diff.

- [ ] **Step 2: Resolve Critical/Important findings**

  For valid findings, write a failing test first, make the smallest root-cause fix, rerun affected checks, and repeat L4 only if the fix changes economic results or fingerprinted source.

- [ ] **Step 3: Mark PR ready and wait for required checks**

  Verify PR head/base, commits, changed files, reviews, threads, conflicts, and required check results. Do not treat non-required long workflows as blockers.

- [ ] **Step 4: Squash merge using the repository-supported method**

  Merge only when all acceptance criteria, required checks/reviews, and conflict checks pass. Never push directly to `main`.

- [ ] **Step 5: Verify post-merge state**

  Verify PR merged state, squash SHA, latest `main` SHA/tree, canonical artifacts/docs, unchanged old rejected SHA-256, and post-merge required CI.

- [ ] **Step 6: Checkpoint F and final report**

  Report only freshly verified source/artifact metrics, exact tests, CI, PR/commit/SHA state, core-alpha unchanged status, 688205 retained/no-special-case status, and remaining real-world execution limitations.
