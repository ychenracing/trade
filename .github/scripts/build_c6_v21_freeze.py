from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path.cwd().resolve()
MAIN = "3b4b9c2554b2ac54d065173d3889040a30bc6e89"
PR_HEAD = "9bea501127306060af0af90b20186780c9913ee2"
P19 = "3611ac948287ee147ed40e77eda8ea01bab27a35"
IB19 = "168cd3856cc1b60c928fe54c4a826b6045df01e9"
IS19 = "00485e8ceb67f4105d76fa62368f583ce82a81c5"
R19 = "4db5cadc90d450f513dfdda10e0436cf330c62f3"
WORKFLOW21 = "f9da08afebf22b3dc03a1fb3a0ec351a79adf42c"
TRANSITION_SOURCE = "86fd22448b9aad9d5e6194c0c065c40d56d7bddd"
TRANSITION_PATH = (
    "artifacts/validation/candidates/"
    "stress-86fd22448b9aad9d5e6194c0c065c40d56d7bddd-rejected.json"
)
P_PATH = "artifacts/diagnostics/c6-preregistration.json"
R_PATH = "artifacts/diagnostics/c6-run-bindings.json"
GOVERNANCE_PATHS = {
    P_PATH,
    "artifacts/diagnostics/c6-recovery-engineering-checkpoint.json",
    "artifacts/diagnostics/c6-v10-contract-review.json",
    "artifacts/diagnostics/c6-v11-recovery-assessment.json",
    "docs/C6_RECOVERY_CONTRACT.md",
}
S_PATHS = {
    "quantfusion/application/c6_s_qualification.py",
    "quantfusion/risk/overlay/actions.py",
    "quantfusion/risk/overlay/policy.py",
    "quantfusion/risk/overlay/policy_base.py",
    "tests/c6_non_economic/test_c6_early_concentration.py",
    "tests/c6_non_economic/test_c6_s_qualification.py",
}
S_SHARED_BASE_FROM_IB19 = {"quantfusion/risk/overlay/policy_base.py"}
SKIP_PR_PATHS = {".github/workflows/ci.yml"}
REFS = {
    "P": "refs/heads/codex/c6-preregistration-v21",
    "I_B": "refs/heads/codex/c6-base-v21",
    "I_S": "refs/heads/codex/c6-s-v21",
    "R": "refs/heads/codex/c6-evidence-v21",
}


def run(*args: str, cwd: Path = ROOT, text: bool = True, check: bool = True):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=check,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=text,
    )


def out(*args: str, cwd: Path = ROOT) -> str:
    return run(*args, cwd=cwd).stdout.strip()


def raw_at(revision: str, path: str) -> bytes:
    return run("show", f"{revision}:{path}", text=False).stdout


def write_at(revision: str, path: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw_at(revision, path))


def tree(revision: str) -> str:
    return out("rev-parse", f"{revision}^{{tree}}")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_file_identity(revision: str, path: str) -> dict[str, str]:
    record = out("ls-tree", revision, "--", path).split()
    if len(record) != 4 or record[0] != "100644" or record[1] != "blob":
        raise RuntimeError(f"not one regular Git blob: {revision}:{path}")
    return {
        "mode": "100644",
        "git_blob": record[2],
        "sha256": sha256_bytes(raw_at(revision, path)),
    }


def identity4(revision: str, path: str) -> dict[str, str]:
    item = git_file_identity(revision, path)
    return {
        "commit": revision,
        "tree": tree(revision),
        "blob": item["git_blob"],
        "sha256": item["sha256"],
    }


def changed_paths(start: str, end: str) -> list[str]:
    data = out("diff", "--name-only", "--no-renames", start, end)
    return [] if not data else data.splitlines()


def numstat(start: str, end: str, paths: list[str] | None = None) -> tuple[int, int]:
    args = ["diff", "--numstat", "--no-renames", start, end]
    if paths:
        args += ["--", *paths]
    total_add = total_del = 0
    for line in out(*args).splitlines():
        if not line:
            continue
        plus, minus, _ = line.split("\t", 2)
        if not plus.isdigit() or not minus.isdigit():
            raise RuntimeError("binary diff is forbidden")
        total_add += int(plus)
        total_del += int(minus)
    return total_add, total_del


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def deep_replace(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        for old, new in replacements.items():
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [deep_replace(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: deep_replace(item, replacements) for key, item in value.items()}
    return value


def ensure_absent_remote_refs() -> None:
    remote = out("remote", "get-url", "origin")
    for label, ref in REFS.items():
        result = run("ls-remote", "--refs", remote, ref, check=False)
        if result.returncode not in {0, 2}:
            raise RuntimeError(f"cannot inspect remote {label} ref")
        if result.stdout.strip():
            raise RuntimeError(f"v21 ref already exists: {label} {result.stdout.strip()}")


def verify_fixed_refs() -> None:
    expected = {
        "refs/heads/main": MAIN,
        "refs/heads/codex/c6-causal-risk-closure-v11": PR_HEAD,
        "refs/heads/codex/c6-v21-workflow-anchor": WORKFLOW21,
        "refs/heads/codex/c6-preregistration-v19": P19,
        "refs/heads/codex/c6-base-v19": IB19,
        "refs/heads/codex/c6-s-v19": IS19,
        "refs/heads/codex/c6-evidence-v19": R19,
    }
    remote = out("remote", "get-url", "origin")
    for ref, sha in expected.items():
        actual = run("ls-remote", "--refs", remote, ref).stdout.strip()
        if actual != f"{sha}\t{ref}":
            raise RuntimeError(f"frozen/live ref drift: {ref}: {actual}")
    ensure_absent_remote_refs()


def worktree(path: Path, revision: str) -> None:
    if path.exists():
        shutil.rmtree(path)
    run("worktree", "add", "--detach", str(path), revision)
    out("config", "user.name", "github-actions[bot]", cwd=path)
    out("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com", cwd=path)


def copy_paths(destination: Path, revision: str, paths: set[str] | list[str]) -> None:
    for path in sorted(paths):
        write_at(revision, path, destination / path)


def diff_shape(start: str, end: str) -> tuple[list[str], int, int]:
    paths = changed_paths(start, end)
    added, deleted = numstat(start, end)
    return paths, added, deleted


def implementation_identity(template: dict[str, Any], start: str, end: str) -> dict[str, Any]:
    result = copy.deepcopy(template)
    paths, added, deleted = diff_shape(start, end)
    result.update(
        commit=end,
        tree=tree(end),
        comparison_base_commit=start,
        comparison_base_tree=tree(start),
        first_parent_ancestor=True,
        merge_commit_count=0,
        changed_paths=paths,
        added_lines=added,
        deleted_lines=deleted,
        required_blobs={path: git_file_identity(end, path) for path in paths},
    )
    return result


def main() -> None:
    verify_fixed_refs()
    sys.path.insert(0, str(ROOT))
    from quantfusion.application.c6_contract import (
        binding_identity,
        canonical_payload_hash,
        economic_tree_manifest,
        load_preregistration,
        load_run_bindings,
        validate_implementation_git_proofs,
    )

    pr_changes = set(changed_paths(MAIN, PR_HEAD))
    missing_governance = GOVERNANCE_PATHS - pr_changes
    if missing_governance:
        raise RuntimeError(f"expected governance additions missing from PR: {sorted(missing_governance)}")
    if not S_PATHS <= pr_changes:
        raise RuntimeError("expected six S-only paths are not all present in PR diff")
    base_from_pr = pr_changes - GOVERNANCE_PATHS - S_PATHS - SKIP_PR_PATHS
    base_paths = set(base_from_pr) | S_SHARED_BASE_FROM_IB19
    if not base_paths or base_paths & S_PATHS != S_SHARED_BASE_FROM_IB19:
        raise RuntimeError("Base/S path partition is malformed")

    # The three production S paths must still be the exact old S code; performance work
    # is outside them. This proves the old Base blobs are the correct subtraction source.
    for path in (
        "quantfusion/risk/overlay/actions.py",
        "quantfusion/risk/overlay/policy.py",
        "quantfusion/risk/overlay/policy_base.py",
        "quantfusion/application/c6_s_qualification.py",
    ):
        if raw_at(PR_HEAD, path) != raw_at(IS19, path):
            raise RuntimeError(f"S production path drifted since frozen v19: {path}")

    p_root = Path("/tmp/c6-v21-p")
    worktree(p_root, MAIN)
    copy_paths(p_root, PR_HEAD, GOVERNANCE_PATHS - {P_PATH})
    p = json.loads(raw_at(P19, P_PATH))
    p["experiment_id"] = "c6-causal-risk-closure-17x958-v21"
    p["frozen_at"] = "2026-09-07T10:35:00Z"
    p["authority"]["base_revision"] = MAIN
    p["authority"]["base_tree"] = tree(MAIN)
    recovery = p["authority"]["recovery_continuation"]
    recovery["v21_correctness_and_parallel_rebind"] = {
        "classification": "B_flow_neutral_cash_hwm_correctness_plus_A_execution_efficiency",
        "invalid_execution_revision": "v19",
        "invalid_bound_run_id": 34070745634,
        "invalid_economic_evidence_policy": "retain forensic bytes; import zero records; never resume/relabel/use for residual/S/D/L2/L4",
        "correctness_main": MAIN,
        "corrected_transition_reference": TRANSITION_PATH,
        "workflow_anchor": WORKFLOW21,
        "parallel_l1": {
            "shard_count": 12,
            "partition": "whole frozen checkpoint_every=10 worker chunks round-robin by chunk ordinal",
            "workers_per_shard": 4,
            "aggregate": "same-workflow exact union/hash/schema/formula revalidation and frozen-order reconstruction",
            "checkpoint_import": "none",
        },
        "economic_hypotheses_changed": False,
        "frozen_economic_counts": {"L1_unique_scenarios": 765, "Base_evaluations": 3831, "Base_execution_items": 3875, "official_scenarios": 958},
        "hard_drawdown_gate": 0.18,
    }
    p["purpose"] = (
        "Correctness-required v21 rebind after invalidating v19 flow-neutral sleeve cash/HWM accounting, "
        "plus A-class execution-overhead reduction. Preserve fixed Base/S economics, 765/3831/3875 manifests, "
        "official 17/958 gates, data, seeds, qualification and mechanical selection. Import no v19 economic record."
    )

    replacements = {
        "c6-v19-": "c6-v21-",
        "codex/c6-base-v19": "codex/c6-base-v21",
        "codex/c6-s-v19": "codex/c6-s-v21",
        "codex/c6-evidence-v19": "codex/c6-evidence-v21",
        "codex/c6-v17-workflow-anchor": "codex/c6-v21-workflow-anchor",
    }
    for key in ("checkpoint_and_lease_protocol", "workflow_trigger_matrix", "run_templates"):
        p[key] = deep_replace(p[key], replacements)

    transition_bytes = raw_at(MAIN, TRANSITION_PATH)
    transition_sha = sha256_bytes(transition_bytes)
    transition_payload = json.loads(transition_bytes)
    transition = p["transition_reference"]
    transition["path"] = TRANSITION_PATH
    sha_keys = [key for key in transition if key in {"sha256", "file_sha256", "artifact_sha256", "artifact_full_byte_sha256"}]
    if len(sha_keys) != 1:
        raise RuntimeError(f"unexpected transition SHA fields: {sha_keys} / {sorted(transition)}")
    transition[sha_keys[0]] = transition_sha
    for key in ("source_revision", "source_fingerprint", "data_fingerprint", "calendar_hash"):
        if key in transition and key in transition_payload:
            transition[key] = transition_payload[key]
    if "source_revision" in transition:
        transition["source_revision"] = TRANSITION_SOURCE

    # Rebind all base file identities and the economic-tree identity to corrected main.
    for path in list(p["frozen_file_identities"]):
        p["frozen_file_identities"][path] = git_file_identity(MAIN, path)
    dependency = p["run_templates"]["dependency_lock"]
    dep_identity = git_file_identity(MAIN, dependency["path"])
    dependency.update(dep_identity)
    economic = p["economic_tree_and_A"]
    economic_manifest = economic_tree_manifest(MAIN, economic["A_ALLOWLIST"], repository=ROOT)
    economic["base_manifest_entry_count"] = len(economic_manifest)
    economic["base_economic_tree_sha256"] = canonical_payload_hash(economic_manifest)

    freeze = p["implementation_freeze"]
    freeze["P_allowed_paths"] = sorted(GOVERNANCE_PATHS)
    freeze["I_B_allowed_paths"] = sorted(base_paths)
    freeze["I_S_allowed_paths"] = sorted(S_PATHS)

    # Exact engineering budgets: current PR for Base paths except policy_base, which
    # deliberately comes from the old clean Base source; S is the exact six-path overlay.
    base_pr_paths = sorted(base_from_pr)
    base_add, base_del = numstat(MAIN, PR_HEAD, base_pr_paths) if base_pr_paths else (0, 0)
    pb_add, pb_del = numstat(MAIN, IB19, sorted(S_SHARED_BASE_FROM_IB19))
    base_add += pb_add
    base_del += pb_del
    s_add = s_del = 0
    for path in sorted(S_PATHS):
        start = IB19 if path == "quantfusion/risk/overlay/policy_base.py" else MAIN
        plus, minus = numstat(start, PR_HEAD, [path])
        s_add += plus
        s_del += minus
    freeze["I_B_diff_budget"] = {
        "maximum_changed_paths": len(base_paths),
        "maximum_added_lines": base_add,
        "maximum_deleted_lines": base_del,
    }
    freeze["I_S_diff_budget"] = {
        "maximum_changed_paths": len(S_PATHS),
        "maximum_added_lines": s_add,
        "maximum_deleted_lines": s_del,
    }

    # L1 now consumes precomputed shards through literal, preregistered argv. No new
    # runtime late slot is introduced.
    for spec in p["run_templates"]["binding_specs"]:
        if spec["record_id"] in {"c6.base.l1", "c6.base_plus_s.l1"}:
            argv = spec["argv_template"]
            if "--parallel-evaluations" in argv or "--parallel-shard-count" in argv:
                raise RuntimeError("parallel argv already present unexpectedly")
            index = argv.index("--output")
            spec["argv_template"] = argv[:index] + [
                "--parallel-evaluations", "../parallel-evaluations",
                "--parallel-shard-count", "12",
            ] + argv[index:]

    write_json(p_root / P_PATH, p)
    # Current validator runs from the PR implementation while validating the candidate P.
    load_preregistration(p_root / P_PATH, repository=p_root)
    run("add", *sorted(GOVERNANCE_PATHS), cwd=p_root)
    p_changes = out("diff", "--cached", "--name-status", "--no-renames", cwd=p_root).splitlines()
    if sorted(line.split("\t", 1)[1] for line in p_changes) != sorted(GOVERNANCE_PATHS) or any(not line.startswith("A\t") for line in p_changes):
        raise RuntimeError(f"P is not exact evidence/prereg additions: {p_changes}")
    run("commit", "-m", "C6 v21 preregistration: flow-corrected parallel rebind", cwd=p_root)
    p_sha = out("rev-parse", "HEAD", cwd=p_root)

    # I_B: add exact Base implementation onto P. S-only files are absent; policy_base
    # is restored from old I_B because its five extra S lines are the only shared-file delta.
    copy_paths(p_root, PR_HEAD, base_from_pr)
    copy_paths(p_root, IB19, S_SHARED_BASE_FROM_IB19)
    run("add", *sorted(base_paths), cwd=p_root)
    staged = out("diff", "--cached", "--name-only", "--no-renames", cwd=p_root).splitlines()
    if staged != sorted(base_paths):
        raise RuntimeError(f"I_B path set mismatch: {staged}")
    run("commit", "-m", "Freeze C6-Base v21 on corrected main with parallel runner", cwd=p_root)
    ib_sha = out("rev-parse", "HEAD", cwd=p_root)
    ib_paths, ib_add, ib_del = diff_shape(p_sha, ib_sha)
    if ib_paths != sorted(base_paths) or (ib_add, ib_del) != (base_add, base_del):
        raise RuntimeError("I_B exact diff proof differs from preregistered budget")

    # I_S: exactly six pre-existing S-only paths, no new strategy behavior.
    copy_paths(p_root, PR_HEAD, S_PATHS)
    run("add", *sorted(S_PATHS), cwd=p_root)
    staged = out("diff", "--cached", "--name-only", "--no-renames", cwd=p_root).splitlines()
    if staged != sorted(S_PATHS):
        raise RuntimeError(f"I_S path set mismatch: {staged}")
    run("commit", "-m", "Freeze C6-Base+S v21 with exact six-path S overlay", cwd=p_root)
    is_sha = out("rev-parse", "HEAD", cwd=p_root)
    is_paths, is_add, is_del = diff_shape(ib_sha, is_sha)
    if is_paths != sorted(S_PATHS) or (is_add, is_del) != (s_add, s_del):
        raise RuntimeError("I_S exact diff proof differs from preregistered budget")

    # R is an evidence-only direct child of P, binding actual P/I_B/I_S/workflow Git objects.
    run("checkout", "--detach", p_sha, cwd=p_root)
    old_r = json.loads(raw_at(R19, R_PATH))
    r = deep_replace(old_r, {
        P19: p_sha,
        IB19: ib_sha,
        IS19: is_sha,
        "d8bc65f3edaf1869e0c6c26ba9f26d7e7931ced4": WORKFLOW21,
        "codex/c6-v17-workflow-anchor": "codex/c6-v21-workflow-anchor",
        "c6-v19-": "c6-v21-",
        "codex/c6-base-v19": "codex/c6-base-v21",
        "codex/c6-s-v19": "codex/c6-s-v21",
        "codex/c6-evidence-v19": "codex/c6-evidence-v21",
    })
    p_identity = identity4(p_sha, P_PATH)
    r["P"] = p_identity
    workflow = copy.deepcopy(r["workflow"])
    workflow.update(
        revision=WORKFLOW21,
        dispatch_ref="codex/c6-v21-workflow-anchor",
        git_blob=git_file_identity(WORKFLOW21, ".github/workflows/c6-bound-economic.yml")["git_blob"],
        sha256=git_file_identity(WORKFLOW21, ".github/workflows/c6-bound-economic.yml")["sha256"],
    )
    r["workflow"] = workflow
    r["implementations"]["I_B"] = implementation_identity(old_r["implementations"]["I_B"], p_sha, ib_sha)
    r["implementations"]["I_S"] = implementation_identity(old_r["implementations"]["I_S"], ib_sha, is_sha)

    specs = {item["record_id"]: item for item in p["run_templates"]["binding_specs"]}
    for record in r["binding_records"]:
        spec = specs[record["record_id"]]
        implementation = r["implementations"][record["source_alias"]]
        record["P"] = p_identity
        record["workflow"] = workflow
        record["source_revision"] = implementation["commit"]
        record["source_tree"] = implementation["tree"]
        record["source_blob_identities"] = implementation["required_blobs"]
        record["logical_run_id"] = spec["logical_run_id"]
        record["runtime_late_slots"] = spec["runtime_late_slots"]
        argv = [item.replace("{SOURCE_REVISION}", implementation["commit"]) for item in spec["argv_template"]]
        if any("{SOURCE_REVISION}" in item for item in argv):
            raise RuntimeError("unresolved P compile-time source slot")
        record["argv"] = argv
        resolved = record["resolved_inputs"]
        if "transition_reference_path" in resolved:
            resolved["transition_reference_path"] = TRANSITION_PATH
        if "transition_reference_sha256" in resolved:
            resolved["transition_reference_sha256"] = transition_sha
        record["record_signature"] = binding_identity(record)

    # Root cross-record data is descriptive but must not retain stale active identities.
    r["cross_record_invariants"] = deep_replace(r["cross_record_invariants"], {
        P19: p_sha, IB19: ib_sha, IS19: is_sha,
        "d8bc65f3edaf1869e0c6c26ba9f26d7e7931ced4": WORKFLOW21,
        "codex/c6-v17-workflow-anchor": "codex/c6-v21-workflow-anchor",
        "c6-v19-": "c6-v21-",
    })
    r["selection_validator"] = deep_replace(r["selection_validator"], {
        P19: p_sha, IB19: ib_sha, IS19: is_sha,
        "d8bc65f3edaf1869e0c6c26ba9f26d7e7931ced4": WORKFLOW21,
        "c6-v19-": "c6-v21-",
    })
    write_json(p_root / R_PATH, r)
    load_run_bindings(p_root / R_PATH)
    run("add", R_PATH, cwd=p_root)
    status = out("diff", "--cached", "--name-status", "--no-renames", cwd=p_root)
    if status != f"A\t{R_PATH}":
        raise RuntimeError(f"R is not evidence-only: {status}")
    run("commit", "-m", "Bind all seven C6 v21 stages to corrected parallel identities", cwd=p_root)
    r_sha = out("rev-parse", "HEAD", cwd=p_root)
    if out("rev-parse", f"{r_sha}^", cwd=p_root) != p_sha:
        raise RuntimeError("R is not a direct child of P")
    validate_implementation_git_proofs(p, r, repository=p_root, bindings_revision=r_sha)

    # Targeted source verification; no economic replay occurs here.
    ib_root = Path("/tmp/c6-v21-ib")
    is_root = Path("/tmp/c6-v21-is")
    worktree(ib_root, ib_sha)
    worktree(is_root, is_sha)
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q",
         "tests/c6_non_economic/test_c6_contract.py",
         "tests/c6_non_economic/test_c6_parallel_l1.py",
         "tests/c6_non_economic/test_c6_bound_run.py",
         "tests/contract/test_architecture.py::DependencyDirectionTests::test_canonical_import_graph_is_acyclic",
         "--tb=short"], cwd=ib_root, check=True,
    )
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q",
         "tests/c6_non_economic/test_c6_s_qualification.py",
         "tests/c6_non_economic/test_c6_early_concentration.py",
         "--tb=short"], cwd=is_root, check=True,
    )
    subprocess.run([sys.executable, "-m", "quantfusion.application.c6_parallel_l1_cli", "--help"], cwd=ib_root, check=True, stdout=subprocess.DEVNULL)

    ensure_absent_remote_refs()
    run(
        "push", "--atomic", "origin",
        f"{p_sha}:{REFS['P']}",
        f"{ib_sha}:{REFS['I_B']}",
        f"{is_sha}:{REFS['I_S']}",
        f"{r_sha}:{REFS['R']}",
    )

    receipt = {
        "schema_version": 1,
        "kind": "c6_v21_freeze_receipt",
        "main": MAIN,
        "pr_head": PR_HEAD,
        "workflow": WORKFLOW21,
        "P": p_sha,
        "I_B": ib_sha,
        "I_S": is_sha,
        "R": r_sha,
        "transition_reference_path": TRANSITION_PATH,
        "transition_reference_sha256": transition_sha,
        "P_paths": sorted(GOVERNANCE_PATHS),
        "I_B_paths": ib_paths,
        "I_B_added_lines": ib_add,
        "I_B_deleted_lines": ib_del,
        "I_S_paths": is_paths,
        "I_S_added_lines": is_add,
        "I_S_deleted_lines": is_del,
        "L1_parallel_shards": 12,
        "checkpoint_imported_records": 0,
        "economic_dispatches": 0,
    }
    output = Path(os.environ.get("GITHUB_WORKSPACE", ROOT)) / "c6-v21-freeze-receipt.json"
    write_json(output, receipt)
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
