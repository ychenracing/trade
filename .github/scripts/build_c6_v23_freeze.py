from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path.cwd().resolve()
SOURCE_BASE = "3b4b9c2554b2ac54d065173d3889040a30bc6e89"
LIVE_MAIN = "de01f669a901da92d1819f22a8e76da8f551e2fd"
PR_HEAD = "5cede8cd8916f33fbf701d454a3a939a8352c0d3"
P22 = "c50216f70cba21d830b6feb2079ed68039821ecf"
IB22 = "addb5c8ebf98ac43e08676cbc6ffe81a2627d9d7"
IS22 = "884851bdba4a5372c5272b28bd4df3e30b5824b9"
R22 = "ea6dfa36b9796df7e4078d6f8d88b539a54072e3"
W21 = "f9da08afebf22b3dc03a1fb3a0ec351a79adf42c"
W23 = "8db7981012416d6a8fe4c4394e68051d8c293336"
BASE22_RUN = 34128460919
BASE22_JOB = 101789958390
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
S_SHARED_BASE_FROM_IB22 = {"quantfusion/risk/overlay/policy_base.py"}
SKIP_PR_PATHS = {".github/workflows/ci.yml"}
REFS = {
    "P": "refs/heads/codex/c6-preregistration-v23",
    "I_B": "refs/heads/codex/c6-base-v23",
    "I_S": "refs/heads/codex/c6-s-v23",
    "R": "refs/heads/codex/c6-evidence-v23",
}
BASE22_ARTIFACTS = {
    0: (10024330850, "sha256:6418073aefd56eeb6dc92a7e1a3adafbf84c4b1fd8559628d878d244e9dbe8db"),
    1: (10023438649, "sha256:6cc87e127e50e80d136d7e82e9a25d4ebca4640338a34f96301cfe8235a08d31"),
    2: (10023713395, "sha256:33a58d98e02f2fb7f9da68b483b681a19edf4d18fb382f0c54c9afe3511ece77"),
    3: (10023684086, "sha256:cbf20254308aa273f904ead8e5c2168355a3e43a31147e87efbf69aa1e536f5d"),
    4: (10023697635, "sha256:bdc2a88db800adcfade4b07bbeece8ed1864e4816855c2b214f546828bb59710"),
    5: (10024300356, "sha256:232c334a3607392073bb5945a223287791fa9ef227fe0ec0f7b3ed9c159ca77a"),
    6: (10023650878, "sha256:d7c4928c767d34cba6e0c7e8ac3316a42a9d12dba09645904d148e2c66377324"),
    7: (10022784784, "sha256:f8cff24b50c13341b64bce5397df1f09825a54b6344e47f3e398763f2629f572"),
    8: (10023716462, "sha256:1a2f640104a81044ace3f9dcc21991d4c61c68dfa6ff6d9a4861db38832b7cc4"),
    9: (10023686351, "sha256:1e4ea7babc79c833b200152f1b9ad0bd0ddbde813163fb1909a31fa10d45837a"),
    10: (10024256138, "sha256:eadb1218d868f7ccda9e2f1dbf2bec85f8c68ff2f052b00dfca49e6bac9caf3a"),
    11: (10024209166, "sha256:d80514f7782a021f6c4801807ed6d9b1278054ba30c33fb9c0d61fce02231598"),
}


def run(*args: str, cwd: Path = ROOT, text: bool = True, check: bool = True):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=check,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text,
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


def ensure_absent_remote_refs() -> None:
    remote = out("remote", "get-url", "origin")
    for label, ref in REFS.items():
        result = run("ls-remote", "--refs", remote, ref, check=False)
        if result.returncode not in {0, 2}:
            raise RuntimeError(f"cannot inspect remote {label} ref")
        if result.stdout.strip():
            raise RuntimeError(f"v23 ref already exists: {label} {result.stdout.strip()}")


def verify_fixed_refs() -> None:
    expected = {
        "refs/heads/main": LIVE_MAIN,
        "refs/heads/codex/c6-causal-risk-closure-v11": PR_HEAD,
        "refs/heads/codex/c6-preregistration-v22": P22,
        "refs/heads/codex/c6-base-v22": IB22,
        "refs/heads/codex/c6-s-v22": IS22,
        "refs/heads/codex/c6-evidence-v22": R22,
        "refs/heads/codex/c6-v21-workflow-anchor": W21,
        "refs/heads/codex/c6-v23-workflow-anchor": W23,
    }
    remote = out("remote", "get-url", "origin")
    for ref, sha in expected.items():
        actual = run("ls-remote", "--refs", remote, ref).stdout.strip()
        if actual != f"{sha}\t{ref}":
            raise RuntimeError(f"frozen/live ref drift: {ref}: {actual}")
    allowed_live_main = {
        ".github/scripts/c6_auto_resume.py",
        ".github/scripts/c6_stage_advance.py",
        ".github/scripts/test_c6_stage_advance.py",
        ".github/workflows/c6-auto-resume.yml",
        ".github/workflows/c6-stage-advance.yml",
    }
    if set(changed_paths(SOURCE_BASE, LIVE_MAIN)) != allowed_live_main:
        raise RuntimeError("live main drift is not relay-only")
    if out("rev-parse", f"{W23}^") != W21:
        raise RuntimeError("W23 is not direct child of W21")
    if changed_paths(W21, W23) != [".github/workflows/c6-bound-economic.yml"]:
        raise RuntimeError("W23 changed more than the bound workflow")
    ensure_absent_remote_refs()


def api_get(path: str) -> Any:
    token = os.environ["GITHUB_TOKEN"]
    request = urllib.request.Request(
        f"https://api.github.com/repos/ychenracing/trade/{path}",
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def verify_base22_recovery_artifacts() -> None:
    run_payload = api_get(f"actions/runs/{BASE22_RUN}")
    expected_run = {
        "workflow_id": 349948458,
        "head_branch": "codex/c6-v21-workflow-anchor",
        "head_sha": W21,
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "failure",
        "run_attempt": 1,
        "display_title": "c6-bound-c6.base.l1-c6-v22-base-l1-a0",
    }
    if any(run_payload.get(key) != value for key, value in expected_run.items()):
        raise RuntimeError("Base22 source run identity drifted")
    jobs = api_get(f"actions/runs/{BASE22_RUN}/jobs?per_page=100")["jobs"]
    shards: dict[int, tuple[str | None, str | None]] = {}
    for job in jobs:
        name = str(job.get("name", ""))
        prefix = "Parallel L1 core shard "
        if name.startswith(prefix) and name[len(prefix):].isdigit():
            shards[int(name[len(prefix):])] = (job.get("status"), job.get("conclusion"))
    if shards != {index: ("completed", "success") for index in range(12)}:
        raise RuntimeError("Base22 shard jobs are not exactly twelve successes")
    central = [job for job in jobs if job.get("id") == BASE22_JOB]
    if len(central) != 1 or central[0].get("conclusion") != "failure":
        raise RuntimeError("Base22 central failure identity drifted")
    artifacts = api_get(f"actions/runs/{BASE22_RUN}/artifacts?per_page=100")["artifacts"]
    observed: dict[int, tuple[int | None, str | None]] = {}
    for artifact in artifacts:
        name = str(artifact.get("name", ""))
        prefix = f"c6-l1-shard-{BASE22_RUN}-"
        if name.startswith(prefix) and name[len(prefix):].isdigit():
            index = int(name[len(prefix):])
            observed[index] = (artifact.get("id"), artifact.get("digest"))
            if artifact.get("expired") is not False:
                raise RuntimeError("Base22 recovery artifact expired")
            workflow_run = artifact.get("workflow_run") or {}
            if workflow_run.get("id") != BASE22_RUN or workflow_run.get("head_sha") != W21:
                raise RuntimeError("Base22 artifact workflow identity drifted")
    if observed != BASE22_ARTIFACTS or len(artifacts) != 12:
        raise RuntimeError("Base22 artifact set/digests drifted")


def main() -> None:
    verify_fixed_refs()
    verify_base22_recovery_artifacts()
    sys.path.insert(0, str(ROOT))
    from quantfusion.application.c6_contract import (
        binding_identity,
        canonical_payload_hash,
        economic_tree_manifest,
        load_preregistration,
        load_run_bindings,
        validate_implementation_git_proofs,
    )

    pr_changes = set(changed_paths(SOURCE_BASE, PR_HEAD))
    missing_governance = GOVERNANCE_PATHS - pr_changes
    if missing_governance:
        raise RuntimeError(f"expected governance additions missing from PR: {sorted(missing_governance)}")
    if not S_PATHS <= pr_changes:
        raise RuntimeError("expected six S-only paths are not all present in PR diff")
    base_from_pr = pr_changes - GOVERNANCE_PATHS - S_PATHS - SKIP_PR_PATHS
    base_paths = set(base_from_pr) | S_SHARED_BASE_FROM_IB22
    if not base_paths or base_paths & S_PATHS != S_SHARED_BASE_FROM_IB22:
        raise RuntimeError("Base/S path partition is malformed")

    for path in (
        "quantfusion/risk/overlay/actions.py",
        "quantfusion/risk/overlay/policy.py",
        "quantfusion/risk/overlay/policy_base.py",
        "quantfusion/application/c6_s_qualification.py",
    ):
        if raw_at(PR_HEAD, path) != raw_at(IS22, path):
            raise RuntimeError(f"S production path drifted since frozen v22: {path}")

    p_root = Path("/tmp/c6-v23-p")
    worktree(p_root, SOURCE_BASE)
    copy_paths(p_root, PR_HEAD, GOVERNANCE_PATHS - {P_PATH})
    p = json.loads(raw_at(P22, P_PATH))
    p["experiment_id"] = "c6-causal-risk-closure-17x958-v23"
    p["frozen_at"] = "2026-09-07T17:32:30Z"
    p["authority"]["base_revision"] = SOURCE_BASE
    p["authority"]["base_tree"] = tree(SOURCE_BASE)
    recovery = p["authority"]["recovery_continuation"]
    recovery["v23_recovered_shard_attestation"] = {
        "classification": "A_runner_interruption_plus_parallel_semantic_attestation",
        "invalid_execution_revision": "v22",
        "invalid_bound_run_id": BASE22_RUN,
        "invalid_central_job_id": BASE22_JOB,
        "invalid_economic_result_policy": "no sealed Base22 result/checkpoint exists; import zero result/checkpoint records; never native-rerun or relabel Base22",
        "prior_frozen_identities": {
            "P": P22,
            "I_B": IB22,
            "I_S": IS22,
            "R": R22,
            "workflow": W21,
        },
        "workflow_anchor": W23,
        "recovered_intermediate_policy": {
            "source_run_id": BASE22_RUN,
            "source_binding": "c6.base.l1",
            "source_revision": IB22,
            "source_workflow_revision": W21,
            "successful_shard_jobs": 12,
            "reused_core_evaluations": 3825,
            "base_evaluations_total": 3831,
            "base_result_or_checkpoint_imported_records": 0,
            "v19_imported_records": 0,
            "v21_imported_records": 0,
            "artifacts": [
                {"shard_index": index, "artifact_id": artifact_id, "archive_digest": digest}
                for index, (artifact_id, digest) in sorted(BASE22_ARTIFACTS.items())
            ],
        },
        "validation": {
            "producer_semantic_validation": "full validate_checkpoint_item before Base22 shard upload",
            "consumer_semantic_validation": "full validate_checkpoint_item independently per recovered shard under I_B23/P23, producing byte-bound validation.json",
            "central_validation": "single-pass hash/schema/partition/union/frozen-order plus exact compressed-shard attestation verification",
            "shard_bytes_mutable": False,
        },
        "profiling_evidence": {
            "run_id": 34144700261,
            "shard_index": 7,
            "record_count": 320,
            "load_seconds": 31.064624,
            "identity_second_pass_seconds": 15.048350,
            "hash_schema_third_pass_seconds": 40.435521,
            "semantic_fourth_pass_seconds": 377.589220,
            "measured_total_seconds": 464.137715,
        },
        "economic_hypotheses_changed": False,
        "frozen_economic_counts": {
            "L1_unique_scenarios": 765,
            "Base_evaluations": 3831,
            "Base_execution_items": 3875,
            "official_scenarios": 958,
        },
        "hard_drawdown_gate": 0.18,
    }
    p["purpose"] = (
        "Engineering-required v23 rebind after the Base22 GitHub runner interruption. "
        "Reuse only the twelve explicitly authenticated successful Base22 core shard intermediates, "
        "perform a second full byte-bound semantic validation in parallel, and preserve fixed Base/S economics, "
        "765/3831/3875 manifests, official 17/958 gates, data, seeds, qualification and mechanical selection."
    )

    replacements = {
        "c6-v22-": "c6-v23-",
        "codex/c6-base-v22": "codex/c6-base-v23",
        "codex/c6-s-v22": "codex/c6-s-v23",
        "codex/c6-evidence-v22": "codex/c6-evidence-v23",
        "codex/c6-v21-workflow-anchor": "codex/c6-v23-workflow-anchor",
        W21: W23,
    }
    for key in ("checkpoint_and_lease_protocol", "workflow_trigger_matrix", "run_templates"):
        p[key] = deep_replace(p[key], replacements)

    transition_bytes = raw_at(SOURCE_BASE, TRANSITION_PATH)
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

    for path in list(p["frozen_file_identities"]):
        p["frozen_file_identities"][path] = git_file_identity(SOURCE_BASE, path)
    dependency = p["run_templates"]["dependency_lock"]
    dependency.update(git_file_identity(SOURCE_BASE, dependency["path"]))
    economic = p["economic_tree_and_A"]
    economic_manifest = economic_tree_manifest(SOURCE_BASE, economic["A_ALLOWLIST"], repository=ROOT)
    economic["base_manifest_entry_count"] = len(economic_manifest)
    economic["base_economic_tree_sha256"] = canonical_payload_hash(economic_manifest)

    freeze = p["implementation_freeze"]
    freeze["P_allowed_paths"] = sorted(GOVERNANCE_PATHS)
    freeze["I_B_allowed_paths"] = sorted(base_paths)
    freeze["I_S_allowed_paths"] = sorted(S_PATHS)
    base_pr_paths = sorted(base_from_pr)
    base_add, base_del = numstat(SOURCE_BASE, PR_HEAD, base_pr_paths) if base_pr_paths else (0, 0)
    pb_add, pb_del = numstat(SOURCE_BASE, IB22, sorted(S_SHARED_BASE_FROM_IB22))
    base_add += pb_add
    base_del += pb_del
    s_add = s_del = 0
    for path in sorted(S_PATHS):
        start = IB22 if path == "quantfusion/risk/overlay/policy_base.py" else SOURCE_BASE
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

    for spec in p["run_templates"]["binding_specs"]:
        if spec["record_id"] not in {"c6.base.l1", "c6.base_plus_s.l1"}:
            continue
        argv = list(spec["argv_template"])
        if "--parallel-shard-source-revision" in argv or "--parallel-validation-attestations-required" in argv:
            raise RuntimeError("recovery argv already present unexpectedly")
        output_index = argv.index("--output")
        shard_source = IB22 if spec["record_id"] == "c6.base.l1" else "{SOURCE_REVISION}"
        spec["argv_template"] = argv[:output_index] + [
            "--parallel-shard-source-revision", shard_source,
            "--parallel-validation-attestations-required",
        ] + argv[output_index:]

    write_json(p_root / P_PATH, p)
    load_preregistration(p_root / P_PATH, repository=p_root)
    run("add", *sorted(GOVERNANCE_PATHS), cwd=p_root)
    p_changes = out("diff", "--cached", "--name-status", "--no-renames", cwd=p_root).splitlines()
    if sorted(line.split("\t", 1)[1] for line in p_changes) != sorted(GOVERNANCE_PATHS) or any(not line.startswith("A\t") for line in p_changes):
        raise RuntimeError(f"P is not exact evidence/prereg additions: {p_changes}")
    run("commit", "-m", "C6 v23 preregistration: recovered shard attestation", cwd=p_root)
    p_sha = out("rev-parse", "HEAD", cwd=p_root)

    copy_paths(p_root, PR_HEAD, base_from_pr)
    copy_paths(p_root, IB22, S_SHARED_BASE_FROM_IB22)
    run("add", *sorted(base_paths), cwd=p_root)
    staged = out("diff", "--cached", "--name-only", "--no-renames", cwd=p_root).splitlines()
    if staged != sorted(base_paths):
        raise RuntimeError(f"I_B path set mismatch: {staged}")
    run("commit", "-m", "Freeze C6-Base v23 with recovered shard attestation", cwd=p_root)
    ib_sha = out("rev-parse", "HEAD", cwd=p_root)
    ib_paths, ib_add, ib_del = diff_shape(p_sha, ib_sha)
    if ib_paths != sorted(base_paths) or (ib_add, ib_del) != (base_add, base_del):
        raise RuntimeError("I_B exact diff proof differs from preregistered budget")

    copy_paths(p_root, PR_HEAD, S_PATHS)
    run("add", *sorted(S_PATHS), cwd=p_root)
    staged = out("diff", "--cached", "--name-only", "--no-renames", cwd=p_root).splitlines()
    if staged != sorted(S_PATHS):
        raise RuntimeError(f"I_S path set mismatch: {staged}")
    run("commit", "-m", "Freeze C6-Base+S v23 with exact six-path S overlay", cwd=p_root)
    is_sha = out("rev-parse", "HEAD", cwd=p_root)
    is_paths, is_add, is_del = diff_shape(ib_sha, is_sha)
    if is_paths != sorted(S_PATHS) or (is_add, is_del) != (s_add, s_del):
        raise RuntimeError("I_S exact diff proof differs from preregistered budget")

    run("checkout", "--detach", p_sha, cwd=p_root)
    old_r = json.loads(raw_at(R22, R_PATH))
    r = deep_replace(old_r, {
        P22: p_sha,
        IB22: ib_sha,
        IS22: is_sha,
        W21: W23,
        "codex/c6-v21-workflow-anchor": "codex/c6-v23-workflow-anchor",
        "c6-v22-": "c6-v23-",
        "codex/c6-base-v22": "codex/c6-base-v23",
        "codex/c6-s-v22": "codex/c6-s-v23",
        "codex/c6-evidence-v22": "codex/c6-evidence-v23",
    })
    p_identity = identity4(p_sha, P_PATH)
    r["P"] = p_identity
    workflow = copy.deepcopy(r["workflow"])
    workflow.update(
        revision=W23,
        dispatch_ref="codex/c6-v23-workflow-anchor",
        git_blob=git_file_identity(W23, ".github/workflows/c6-bound-economic.yml")["git_blob"],
        sha256=git_file_identity(W23, ".github/workflows/c6-bound-economic.yml")["sha256"],
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

    replacements_r = {
        P22: p_sha,
        IB22: ib_sha,
        IS22: is_sha,
        W21: W23,
        "codex/c6-v21-workflow-anchor": "codex/c6-v23-workflow-anchor",
        "c6-v22-": "c6-v23-",
    }
    r["cross_record_invariants"] = deep_replace(r["cross_record_invariants"], replacements_r)
    r["selection_validator"] = deep_replace(r["selection_validator"], replacements_r)
    write_json(p_root / R_PATH, r)
    load_run_bindings(p_root / R_PATH)
    run("add", R_PATH, cwd=p_root)
    status = out("diff", "--cached", "--name-status", "--no-renames", cwd=p_root)
    if status != f"A\t{R_PATH}":
        raise RuntimeError(f"R is not evidence-only: {status}")
    run("commit", "-m", "Bind all seven C6 v23 stages to W23 recovered-shard identities", cwd=p_root)
    r_sha = out("rev-parse", "HEAD", cwd=p_root)
    if out("rev-parse", f"{r_sha}^", cwd=p_root) != p_sha:
        raise RuntimeError("R is not a direct child of P")
    validate_implementation_git_proofs(p, r, repository=p_root, bindings_revision=r_sha)

    ib_root = Path("/tmp/c6-v23-ib")
    is_root = Path("/tmp/c6-v23-is")
    worktree(ib_root, ib_sha)
    worktree(is_root, is_sha)
    subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q",
            "tests/c6_non_economic/test_c6_contract.py",
            "tests/c6_non_economic/test_c6_parallel_l1.py",
            "tests/c6_non_economic/test_c6_parallel_l1_single_pass.py",
            "tests/c6_non_economic/test_c6_parallel_l1_attestation.py",
            "tests/c6_non_economic/test_c6_parallel_l1_recovery_cli.py",
            "tests/c6_non_economic/test_c6_bound_run.py",
            "tests/c6_non_economic/test_c6_diagnostics.py",
            "tests/contract/test_architecture.py::DependencyDirectionTests::test_canonical_import_graph_is_acyclic",
            "--tb=short",
        ],
        cwd=ib_root,
        check=True,
    )
    subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q",
            "tests/c6_non_economic/test_c6_s_qualification.py",
            "tests/c6_non_economic/test_c6_early_concentration.py",
            "--tb=short",
        ],
        cwd=is_root,
        check=True,
    )
    for module in (
        "quantfusion.application.c6_parallel_l1_cli",
        "quantfusion.application.c6_parallel_l1_validate_cli",
    ):
        subprocess.run(
            [sys.executable, "-m", module, "--help"],
            cwd=ib_root,
            check=True,
            stdout=subprocess.DEVNULL,
        )

    base_record = next(item for item in r["binding_records"] if item["record_id"] == "c6.base.l1")
    s_record = next(item for item in r["binding_records"] if item["record_id"] == "c6.base_plus_s.l1")
    if ["--parallel-shard-source-revision", IB22] != base_record["argv"][base_record["argv"].index("--parallel-shard-source-revision"):base_record["argv"].index("--parallel-shard-source-revision") + 2]:
        raise RuntimeError("Base23 R does not bind exact Base22 shard source")
    if "--parallel-validation-attestations-required" not in base_record["argv"]:
        raise RuntimeError("Base23 R does not require attestations")
    s_index = s_record["argv"].index("--parallel-shard-source-revision")
    if s_record["argv"][s_index + 1] != is_sha or "--parallel-validation-attestations-required" not in s_record["argv"]:
        raise RuntimeError("S23 R does not bind current S shard source/attestation")

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
        "kind": "c6_v23_freeze_receipt",
        "source_base": SOURCE_BASE,
        "live_main": LIVE_MAIN,
        "pr_head": PR_HEAD,
        "workflow": W23,
        "workflow_parent": W21,
        "P": p_sha,
        "I_B": ib_sha,
        "I_S": is_sha,
        "R": r_sha,
        "base22_source_run_id": BASE22_RUN,
        "base22_source_revision": IB22,
        "base22_reused_core_evaluations": 3825,
        "base22_result_checkpoint_imported_records": 0,
        "base22_artifacts": [
            {"shard_index": index, "artifact_id": artifact_id, "archive_digest": digest}
            for index, (artifact_id, digest) in sorted(BASE22_ARTIFACTS.items())
        ],
        "P_paths": sorted(GOVERNANCE_PATHS),
        "I_B_paths": ib_paths,
        "I_B_added_lines": ib_add,
        "I_B_deleted_lines": ib_del,
        "I_S_paths": is_paths,
        "I_S_added_lines": is_add,
        "I_S_deleted_lines": is_del,
        "economic_dispatches": 0,
    }
    output = Path(os.environ.get("GITHUB_WORKSPACE", ROOT)) / "c6-v23-freeze-receipt.json"
    write_json(output, receipt)
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
