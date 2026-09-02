# Current-Only Breaking Cleanup + Stress Diagnostic Decomposition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove obsolete compatibility and dead replay injection paths, add safe partial stress diagnostics, and reduce stress/config complexity without changing current production economics.

**Architecture:** Canonical callers import `quantfusion.*` directly; scripts run only as modules; stress construction, metrics/gates, and artifacts are small functional modules behind a thin orchestration CLI. Diagnostic selection is deterministic and structurally barred from formal publication.

**Tech Stack:** Python 3.11/3.12, argparse, pathlib, dataclasses, pandas, pytest/unittest, Ruff, Pyright, Bandit, pip-audit.

**Spec:** `docs/superpowers/specs/2026-09-02-current-only-stress-diagnostics-design.md`

## Global Constraints

- Preserve all current production economic behavior and exact frozen economic regression.
- Keep `worst_add_one_wealth_at_least_minus_18pct=-18%` and all other scenario, seed, metric, hard-gate, and promotion values unchanged.
- Keep AccountSnapshot v3, risk-state schema, provider normalization, and RiskAction adapter contracts.
- Add no dependency, migration layer, generic framework, or replacement compatibility wrapper.
- Use progressive tests; do not use the full stress matrix as a debugging loop.

---

### Task 1: Reverse the architecture compatibility contract

**Files:**
- Modify: `tests/contract/test_architecture.py`
- Modify: `tests/contract/test_repository_hygiene.py`
- Modify: canonical-import consumers under `tests/`
- Delete: the 15 root Python facade/CLI files inventoried by the contract
- Modify: `README.md`, `docs/ARCHITECTURE.md`

**Interfaces:**
- Consumes: public exports from `quantfusion.config`, `quantfusion.data`, `quantfusion.domain`, `quantfusion.engine`, `quantfusion.risk`, and `quantfusion.application`.
- Produces: one canonical import surface; missing root modules are intentional.

- [ ] Add failing architecture tests that require every listed legacy root file to be absent and canonical code to remain independent of root modules.
- [ ] Run the focused architecture tests and confirm failure names the existing files.
- [ ] Move test imports and subprocess calls to canonical packages or `python -m` script/application entry points.
- [ ] Delete root facades and duplicate root CLIs without adding wrappers.
- [ ] Update current usage docs and run architecture, hygiene, import, and CLI tests.

### Task 2: Remove the historical account-state injection chain

**Files:**
- Modify: `quantfusion/domain/models.py`, `quantfusion/domain/__init__.py`
- Modify: `quantfusion/engine/universe.py`, `ensemble.py`, `ensemble_orchestration.py`, `market_regime.py`, `execution_flow.py`, `replay.py`, `replay_loop.py`, `universe_selection.py`, `results.py`, `configuration.py`
- Modify: directly affected unit/regression tests

**Interfaces:**
- Consumes: simulation inputs and optional current risk state.
- Produces: engine and replay APIs with no `account_state` parameter or request field; AccountSnapshot v3 remains under `quantfusion.account`.

- [ ] Add failing signature/request tests proving current public engine and replay paths expose no `account_state` injection.
- [ ] Run them and confirm failure on the old parameters/fields.
- [ ] Remove `AccountState`, request propagation, injection flags, `_apply_account_state`, and unreachable orchestration blocks.
- [ ] Replace historical injection tests with current AccountSnapshot v3 and simulation-boundary tests.
- [ ] Run focused engine, replay, account, daily-scan, compile, Ruff, and Pyright checks.

### Task 3: Remove path, alias, and overlay compatibility

**Files:**
- Modify: `quantfusion/config/paths.py` and its callers
- Modify: trade metrics/result exports containing `account_order_count`
- Modify: `quantfusion/risk/overlay/policy.py` and overlay tests
- Delete when caller-free: `quantfusion/portfolio/policy.py` and other pure re-export facades
- Modify: focused contracts and docs

**Interfaces:**
- Consumes: explicit `Path` values or canonical path constants; `CrossMarketOverlay.evaluate()`; `date_symbol_side_count`.
- Produces: no old directory mapping, misleading order-count alias, or `on_day()` wrapper.

- [ ] Add failing behavior/signature tests for literal old paths, absent aliases, and the evaluate-only overlay API.
- [ ] Run them and confirm the intended legacy behavior causes failure.
- [ ] Replace resolver callers with `Path(...).expanduser()` or canonical constants and delete the resolver if it has no distinct current responsibility.
- [ ] Remove order-count aliases and migrate consumers to accurate metrics.
- [ ] Migrate overlay tests to `evaluate()` plus the real adapter boundary and delete `on_day()`.
- [ ] Remove caller-free re-export modules and run path, overlay, result, architecture, compile, Ruff, and Pyright checks.
- [ ] Commit, remotely publish, verify Checkpoint A, create/update the PR, and continue.

### Task 4: Make stress semantics current-only

**Files:**
- Modify: stress implementation and `tests/unit/test_stress_scenarios.py`
- Delete: historical `artifacts/validation/prefix_stress.json` and `universe_stress.json` when no current contract consumes them
- Modify: repository-hygiene and validation documentation

**Interfaces:**
- Consumes: artifacts whose `trade_count_semantics` is exactly `trade_records`.
- Produces: accepted current-semantic incumbent comparison, explicit no-incumbent state, or rejected non-canonical candidate.

- [ ] Add failing tests rejecting legacy trade semantics and excluding historical incumbent conversion states.
- [ ] Run them and confirm current compatibility branches are exercised.
- [ ] Delete legacy constants, comparisons, conversion/status branches, fixtures, and historical live incumbents.
- [ ] Preserve current rejection/candidate retention and unchanged gate literals.
- [ ] Run focused stress and artifact contract tests.

### Task 5: Add deterministic diagnostic selection and publication isolation

**Files:**
- Create: `quantfusion/application/stress_scenarios.py`
- Modify: `quantfusion/application/stress.py`
- Modify: `tests/unit/test_stress_scenarios.py`

**Interfaces:**
- Produces: `select_scenarios(scenarios, *, scenario_id, scenario_type, shard_index, shard_count) -> tuple[list[dict], bool]`, where the boolean is formal-plan completeness.
- Shard rule: keep ordered scenario at zero-based index `i` iff `i % shard_count == shard_index`.

- [ ] Add failing tests for exact ID, family, stable shard membership/order, invalid shard arguments, empty selection, and all-scenarios completeness.
- [ ] Run them and confirm the selector is missing.
- [ ] Implement the smallest pure selector and CLI arguments.
- [ ] Add a failing orchestration/artifact test showing every partial selector cannot call formal publication or update canonical paths.
- [ ] Implement explicit diagnostic output/checkpoint behavior and independent artifact-level publication rejection.
- [ ] Run single-case dry/fake execution, family selection, shard selection, and focused stress tests.

### Task 6: Unify scripts on module execution

**Files:**
- Delete: `scripts/_bootstrap.py`
- Modify: the six affected `scripts/*.py` modules
- Modify: README, CI/tests, and subprocess contracts

**Interfaces:**
- Produces: `python -m scripts.<module>` as the only supported script invocation, including on Windows.

- [ ] Add failing subprocess tests that invoke representative modules with `python -m` and no path mutation.
- [ ] Remove `if __package__`, alternate imports, and `sys.path` mutation.
- [ ] Update every repository-owned invocation and run script/CLI tests.
- [ ] Commit, remotely publish, verify Checkpoint B, refresh the PR, and continue.

### Task 7: Split stress responsibilities without semantic drift

**Files:**
- Create: `quantfusion/application/stress_metrics.py`
- Create: `quantfusion/application/stress_artifacts.py`
- Modify: `quantfusion/application/stress.py`, `stress_scenarios.py`
- Modify: focused stress tests

**Interfaces:**
- Scenario module owns plan construction/signature/selection only.
- Metrics module owns metrics, summary, hard gates, permutation, and promotion only.
- Artifact module owns provenance, checkpoint validation, current candidate validation, rejection, and formal publication only.
- `stress.py` owns parser, orchestration, stdout, and exit code.

- [ ] Capture literal characterization tests for scenario IDs/order/signature, gate dictionaries, promotion output, run signature, and publication paths.
- [ ] Move pure functions in responsibility-sized batches; after each move run the characterization tests.
- [ ] Keep thresholds, formulas, field names, IDs, order, and publication policy byte-for-byte equivalent except explicitly removed legacy semantics and added diagnostic metadata.
- [ ] Run focused stress suite, compile, Ruff, and Pyright.

### Task 8: Remove bounded config/engine compatibility debt

**Files:**
- Modify: `quantfusion/engine/configuration.py`, `causal.py`, `signals.py`, `core.py`
- Create only if justified: one `quantfusion/config/profiles.py`
- Modify: exact config/profile regression tests

**Interfaces:**
- Consumes: `default_engine_config()` and `PER_SYMBOL_OVERRIDE_KEYS` directly.
- Produces: exactly equal runtime dictionaries with no second schema or engine compatibility wrappers.

- [ ] Add exact pre-refactor profile dictionary fixtures/assertions and verify current output.
- [ ] Replace `_PER_SYMBOL_OVERRIDE_KEYS` with the canonical constant and remove obsolete `_default_config` call paths when all consumers can use the public builder directly.
- [ ] Move pure profile builders only if it reduces engine responsibility without wrappers; otherwise document the evidence-based decision and leave them.
- [ ] Run config, engine, regression, compile, Ruff, and Pyright checks.
- [ ] Commit, remotely publish, verify Checkpoint C, refresh the PR, and continue.

### Task 9: Final documentation, evidence, and merge-ready PR

**Files:**
- Modify: `README.md`, `docs/ARCHITECTURE.md`, `docs/VALIDATION.md`, `.github/CHATGPT_PROJECT_BRIEF.md` only for stable current facts
- Remove: this temporary design/plan pair before final merge if it adds no lasting user value

**Interfaces:**
- Produces: current commands and contracts only; accurate PR state and evidence.

- [ ] Re-scan for forbidden compatibility symbols, deleted root files, old commands, path aliases, and historical stress states.
- [ ] Record before/after Python file count, root-module count, line count, and compatibility-symbol count.
- [ ] Run final `compileall`, Ruff, full pytest, Pyright, Bandit, dependency audit, and exact economic regression once on the stable tree.
- [ ] Run the formal stress once only if the stable candidate and available data make it necessary; preserve any existing product hard-gate rejection honestly.
- [ ] Request focused code review, fix Critical/Important findings, and rerun affected checks.
- [ ] Push the final remote checkpoint, verify local/remote/default SHAs and clean status, make the PR ready, and wait for required CI.
- [ ] Verify checks/reviews/mergeability and stop without merging `main`.
