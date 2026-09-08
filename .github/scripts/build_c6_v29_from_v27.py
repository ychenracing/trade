from __future__ import annotations

import subprocess
from pathlib import Path

PR26_HEAD = "80b4fc401ae1689ccc648c85c032bd32e0861e35"
PR29_HEAD = "7aa0b5e606bc13f757539a1f78ec16e708b0fb97"
MAIN26 = "594298fca61dd50d811d6c4f94843c9b47b5a05c"
MAIN29 = "d86ad59e9eca76630dbe955941793e4d255a64a0"
W23 = "8db7981012416d6a8fe4c4394e68051d8c293336"
W26 = "f77d7d4f91e3816d5cde705cbb2f50255aa9200e"
W27 = "3a0eb31775d633b0a8dea431207c141ae1f290cc"
W28 = "037b609b99e84081d2e0137604074fc279797a7c"
W29_REF = "codex/c6-v29-workflow-anchor"
P26 = "a56a97b56cf1479b46dcc258416255f6ac19b571"
IB26 = "2d7b6e2a153dcb3bc359f0ca75908e6a0c7e9631"
IS26 = "8c83a242481f510f3d5cba977de4628903011c62"
R26 = "85e7b3158f130e2f475de1bd67081ac3629739f1"
P27 = "e02089ec12c25a8912f2adf0cf9c53f3b844069b"
IB27 = "24682791cbe96acc69d36cd0dcfc42e69d61bcda"
IS27 = "d6b06596f3280d0e8ce3573d481cae99443ae866"
R27 = "1744434eac1c80bb1d74fa0d92fb13193aab9062"
EXACT_CI_26 = 34164530161
EXACT_CI_29 = 34192282657
V26_DISPATCH_BRANCH = "codex/c6-dispatch/c6-v26-base-l1/c6.base.l1/a0"
V26_DISPATCH_COMMIT = "b40d1a14f757c4703dae327a4c2e2ec0547ef261"
V26_DISPATCH_RUN = 34170560344
V26_BOUND_RUN = 34170566443
V26_CENTRAL_JOB = 101891187197
V26_BOUND_TITLE = "c6-bound-c6.base.l1-c6-v26-base-l1-a0"
V27_BASE_DISPATCH_BRANCH = "codex/c6-dispatch/c6-v27-base-l1/c6.base.l1/a0"
V27_BASE_DISPATCH_COMMIT = "b9b8dbe8d712d6392994265a2d056c0dd99a391b"
V27_BASE_DISPATCH_RUN = 34180184742
V27_BASE_BOUND_RUN = 34180737092
V27_BASE_CENTRAL_JOB = 101925714778
V27_BASE_SEAL_ARTIFACT = 10043064982
V27_BASE_SEAL_DIGEST = "sha256:b3cc5266c9802f68d8ca81c18e13c1cdd2f5d77a6bbf00ee636d9d1887f0628c"
V27_BASE_STAGE_RUN = 34262502342
V27_BASE_STAGE_STATUS_ARTIFACT = 10074734892
V27_BASE_STAGE_STATUS_DIGEST = "sha256:d2c9c89f3d97f1aa817f949b87173abe0eb9b1b12b6db1e9d21e6ff0b89d4b18"
V27_QUAL_DISPATCH_BRANCH = "codex/c6-dispatch/c6-v27-s-qualification/c6.s.qualification/a0"
V27_QUAL_DISPATCH_COMMIT = "b5496d8d60f367182c186862ff92a6d2936b8529"
V27_QUAL_BOUND_RUN = 34273464686
V27_QUAL_RUN_JOB = 102220655253


def run(*args: str, cwd: Path | None = None, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        input=input_text,
        text=True,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def out(*args: str, cwd: Path | None = None) -> str:
    return run(*args, cwd=cwd).stdout.strip()


def replace_once(text: str, old: str, new: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one token, got {count}: {old!r}")
    return text.replace(old, new)


def create_w29() -> str:
    remote = out("git", "remote", "get-url", "origin")
    existing = run("git", "ls-remote", "--refs", remote, f"refs/heads/{W29_REF}").stdout.strip()
    if not existing:
        raise SystemExit("W29 workflow anchor is missing")
    sha, ref = existing.split("\t")
    if ref != f"refs/heads/{W29_REF}":
        raise SystemExit("unexpected W29 ref response")
    if out("git", "rev-parse", f"{sha}^") != W28:
        raise SystemExit("W29 is not a direct child of W28")
    if out("git", "diff", "--name-only", W28, sha).splitlines() != [".github/workflows/c6-dispatch.yml"]:
        raise SystemExit("W29 changed more than the dispatch workflow")
    if out("git", "rev-parse", f"{W28}^") != W27:
        raise SystemExit("W28 is not a direct child of W27")
    if out("git", "diff", "--name-only", W27, W28).splitlines() != [".github/workflows/c6-bound-economic.yml"]:
        raise SystemExit("W28 changed more than the bound workflow")
    condition = "if: ${{ !cancelled() && inputs.binding_id != 'c6.synthetic.resume' && needs.base_recovery_guard.result == 'success' && (needs.parallel_l1.result == 'success' || needs.parallel_l1.result == 'skipped') }}"
    if out("git", "show", f"{W28}:.github/workflows/c6-bound-economic.yml").count(condition) != 1:
        raise SystemExit("W28 does not contain the exact dependency-status fix")
    dispatch = out("git", "show", f"{sha}:.github/workflows/c6-dispatch.yml")
    if dispatch.count('expected_workflow_ref = "codex/c6-v29-workflow-anchor"') != 1:
        raise SystemExit("W29 dispatch does not bind its exact trusted anchor")
    return sha


def main() -> None:
    # Reuse the already-tested v26 adapter, then advance only immutable
    # identities and the verified v27 orchestration-failure evidence.
    run("python", ".github/scripts/build_c6_v26_from_v23.py")
    source = Path("/tmp/build_c6_v26_freeze.py")
    text = source.read_text(encoding="utf-8")
    w29 = create_w29()

    text = replace_once(text, f'PR_HEAD = "{PR26_HEAD}"', f'PR_HEAD = "{PR29_HEAD}"')
    text = replace_once(text, f'LIVE_MAIN = "{MAIN26}"', f'LIVE_MAIN = "{MAIN29}"')
    text = replace_once(text, f'EXACT_CI_RUN = {EXACT_CI_26}', f'EXACT_CI_RUN = {EXACT_CI_29}')
    text = replace_once(
        text,
        'p["frozen_at"] = "2026-09-07T22:25:00Z"',
        'p["frozen_at"] = "2026-09-08T20:45:00Z"',
    )

    text = text.replace("v26", "v29")
    text = text.replace("W26", "W29")
    text = replace_once(
        text,
        f'W29 = "{W26}"',
        f'W26_OLD = "{W26}"\nW27_OLD = "{W27}"\nW28_OLD = "{W28}"\nW29 = "{w29}"',
    )

    constants_anchor = f'R25 = "71748de130d31d132e69999645e3285f81a651f9"\n'
    constants = constants_anchor + (
        f'P26 = "{P26}"\nIB26 = "{IB26}"\nIS26 = "{IS26}"\nR26 = "{R26}"\n'
        f'V26_DISPATCH_BRANCH = "{V26_DISPATCH_BRANCH}"\n'
        f'V26_DISPATCH_COMMIT = "{V26_DISPATCH_COMMIT}"\n'
        f'V26_DISPATCH_RUN = {V26_DISPATCH_RUN}\n'
        f'V26_BOUND_RUN = {V26_BOUND_RUN}\n'
        f'V26_CENTRAL_JOB = {V26_CENTRAL_JOB}\n'
        f'V26_BOUND_TITLE = "{V26_BOUND_TITLE}"\n'
        f'P27 = "{P27}"\nIB27 = "{IB27}"\nIS27 = "{IS27}"\nR27 = "{R27}"\n'
        f'V27_BASE_DISPATCH_BRANCH = "{V27_BASE_DISPATCH_BRANCH}"\n'
        f'V27_BASE_DISPATCH_COMMIT = "{V27_BASE_DISPATCH_COMMIT}"\n'
        f'V27_BASE_DISPATCH_RUN = {V27_BASE_DISPATCH_RUN}\n'
        f'V27_BASE_BOUND_RUN = {V27_BASE_BOUND_RUN}\n'
        f'V27_BASE_CENTRAL_JOB = {V27_BASE_CENTRAL_JOB}\n'
        f'V27_BASE_SEAL_ARTIFACT = {V27_BASE_SEAL_ARTIFACT}\n'
        f'V27_BASE_SEAL_DIGEST = "{V27_BASE_SEAL_DIGEST}"\n'
        f'V27_BASE_STAGE_RUN = {V27_BASE_STAGE_RUN}\n'
        f'V27_BASE_STAGE_STATUS_ARTIFACT = {V27_BASE_STAGE_STATUS_ARTIFACT}\n'
        f'V27_BASE_STAGE_STATUS_DIGEST = "{V27_BASE_STAGE_STATUS_DIGEST}"\n'
        f'V27_QUAL_DISPATCH_BRANCH = "{V27_QUAL_DISPATCH_BRANCH}"\n'
        f'V27_QUAL_DISPATCH_COMMIT = "{V27_QUAL_DISPATCH_COMMIT}"\n'
        f'V27_QUAL_BOUND_RUN = {V27_QUAL_BOUND_RUN}\n'
        f'V27_QUAL_RUN_JOB = {V27_QUAL_RUN_JOB}\n'
    )
    text = replace_once(text, constants_anchor, constants)

    expected_anchor = '        "refs/heads/codex/c6-evidence-v25": R25,\n'
    expected = expected_anchor + (
        '        "refs/heads/codex/c6-preregistration-v26": P26,\n'
        '        "refs/heads/codex/c6-base-v26": IB26,\n'
        '        "refs/heads/codex/c6-s-v26": IS26,\n'
        '        "refs/heads/codex/c6-evidence-v26": R26,\n'
        '        "refs/heads/codex/c6-v26-workflow-anchor": W26_OLD,\n'
        '        "refs/heads/codex/c6-preregistration-v27": P27,\n'
        '        "refs/heads/codex/c6-base-v27": IB27,\n'
        '        "refs/heads/codex/c6-s-v27": IS27,\n'
        '        "refs/heads/codex/c6-evidence-v27": R27,\n'
        '        "refs/heads/codex/c6-v27-workflow-anchor": W27_OLD,\n'
        '        "refs/heads/codex/c6-v28-workflow-anchor": W28_OLD,\n'
    )
    text = replace_once(text, expected_anchor, expected)

    old_anchor_checks = '''    if out("rev-parse", f"{W23_OLD}^") != W21:\n        raise RuntimeError("W23 is not direct child of W21")\n    if changed_paths(W21, W23_OLD) != [".github/workflows/c6-bound-economic.yml"]:\n        raise RuntimeError("W23 changed more than the bound workflow")\n    if out("rev-parse", f"{W29}^") != W23_OLD:\n        raise RuntimeError("W29 is not direct child of W23")\n    if changed_paths(W23_OLD, W29) != [".github/workflows/c6-dispatch.yml"]:\n        raise RuntimeError("W29 changed more than the dispatch workflow")\n'''
    new_anchor_checks = '''    if out("rev-parse", f"{W23_OLD}^") != W21:\n        raise RuntimeError("W23 is not direct child of W21")\n    if changed_paths(W21, W23_OLD) != [".github/workflows/c6-bound-economic.yml"]:\n        raise RuntimeError("W23 changed more than the bound workflow")\n    if out("rev-parse", f"{W26_OLD}^") != W23_OLD:\n        raise RuntimeError("W26 is not direct child of W23")\n    if changed_paths(W23_OLD, W26_OLD) != [".github/workflows/c6-dispatch.yml"]:\n        raise RuntimeError("W26 changed more than the dispatch workflow")\n    if out("rev-parse", f"{W27_OLD}^") != W26_OLD:\n        raise RuntimeError("W27 is not direct child of W26")\n    if changed_paths(W26_OLD, W27_OLD) != [".github/workflows/c6-dispatch.yml"]:\n        raise RuntimeError("W27 changed more than the dispatch workflow")\n    if out("rev-parse", f"{W28_OLD}^") != W27_OLD:\n        raise RuntimeError("W28 is not direct child of W27")\n    if changed_paths(W27_OLD, W28_OLD) != [".github/workflows/c6-bound-economic.yml"]:\n        raise RuntimeError("W28 changed more than the bound workflow")\n    if out("rev-parse", f"{W29}^") != W28_OLD:\n        raise RuntimeError("W29 is not direct child of W28")\n    if changed_paths(W28_OLD, W29) != [".github/workflows/c6-dispatch.yml"]:\n        raise RuntimeError("W29 changed more than the dispatch workflow")\n'''
    text = replace_once(text, old_anchor_checks, new_anchor_checks)
    text = replace_once(text, '        "workflow_parent": W23_OLD,\n', '        "workflow_parent": W28_OLD,\n')

    verify_function = '''\n\ndef verify_v26_engineering_failure() -> None:\n    dispatch = api_get(f"actions/runs/{V26_DISPATCH_RUN}")\n    if (\n        dispatch.get("head_branch") != V26_DISPATCH_BRANCH\n        or dispatch.get("head_sha") != V26_DISPATCH_COMMIT\n        or dispatch.get("event") != "push"\n        or dispatch.get("status") != "completed"\n        or dispatch.get("conclusion") != "success"\n        or dispatch.get("run_attempt") != 1\n    ):\n        raise RuntimeError("v26 dispatch envelope identity drifted")\n\n    run_payload = api_get(f"actions/runs/{V26_BOUND_RUN}")\n    if (\n        run_payload.get("head_sha") != W26_OLD\n        or run_payload.get("event") != "workflow_dispatch"\n        or run_payload.get("status") != "completed"\n        or run_payload.get("conclusion") != "failure"\n        or run_payload.get("run_attempt") != 1\n        or run_payload.get("display_title") != V26_BOUND_TITLE\n    ):\n        raise RuntimeError("v26 bound engineering failure identity drifted")\n    jobs = api_get(f"actions/runs/{V26_BOUND_RUN}/jobs?per_page=100")["jobs"]\n    shard_jobs = [job for job in jobs if str(job.get("name", "")).startswith("Parallel L1 core shard ")]\n    if len(shard_jobs) != 12 or any(job.get("conclusion") != "success" for job in shard_jobs):\n        raise RuntimeError("v26 did not preserve 12 successful recovered shard attestations")\n    for job in shard_jobs:\n        steps = {step.get("name"): step.get("conclusion") for step in job.get("steps", [])}\n        if (\n            steps.get("Recover exact Base22 shard bytes") != "success"\n            or steps.get("Compute exact chunk-preserving L1 shard") != "skipped"\n            or steps.get("Semantically attest exact L1 shard") != "success"\n        ):\n            raise RuntimeError("v26 recovered shard job semantics drifted")\n    central = [job for job in jobs if job.get("id") == V26_CENTRAL_JOB]\n    if len(central) != 1 or central[0].get("conclusion") != "failure":\n        raise RuntimeError("v26 central failure identity drifted")\n    steps = {step.get("name"): step.get("conclusion") for step in central[0].get("steps", [])}\n    if (\n        steps.get("Download exact same-run L1 shards") != "success"\n        or steps.get("Execute exact run binding") != "failure"\n        or steps.get("Upload sealed run export") != "skipped"\n    ):\n        raise RuntimeError("v26 central failure did not occur before sealed export")\n    continuation = [job for job in jobs if job.get("name") == "Request authenticated continuation"]\n    if len(continuation) != 1 or continuation[0].get("conclusion") != "skipped":\n        raise RuntimeError("v26 unexpectedly requested continuation")\n\n    remote = out("remote", "get-url", "origin")\n    observed = run("ls-remote", "--refs", remote, "refs/heads/codex/c6-dispatch/c6-v26*").stdout.strip()\n    expected_ref = f"{V26_DISPATCH_COMMIT}\\trefs/heads/{V26_DISPATCH_BRANCH}"\n    if observed != expected_ref:\n        raise RuntimeError("v26 dispatch ref set is not exactly the sole a0")\n'''
    verify_v27_function = '''

def verify_v27_orchestration_failure() -> None:
    base = api_get(f"actions/runs/{V27_BASE_BOUND_RUN}")
    if (
        base.get("head_sha") != W27_OLD
        or base.get("event") != "workflow_dispatch"
        or base.get("status") != "completed"
        or base.get("conclusion") != "success"
        or base.get("run_attempt") != 1
        or base.get("display_title") != "c6-bound-c6.base.l1-c6-v27-base-l1-a0"
    ):
        raise RuntimeError("v27 Base result identity drifted")
    base_jobs = api_get(f"actions/runs/{V27_BASE_BOUND_RUN}/jobs?per_page=100")["jobs"]
    shards = [job for job in base_jobs if str(job.get("name", "")).startswith("Parallel L1 core shard ")]
    if len(shards) != 12 or any(job.get("conclusion") != "success" for job in shards):
        raise RuntimeError("v27 Base does not retain 12 successful shard attestations")
    central = [job for job in base_jobs if job.get("id") == V27_BASE_CENTRAL_JOB]
    if len(central) != 1 or central[0].get("conclusion") != "success":
        raise RuntimeError("v27 Base central result identity drifted")
    artifacts = api_get(f"actions/runs/{V27_BASE_BOUND_RUN}/artifacts?per_page=100")["artifacts"]
    seals = [item for item in artifacts if item.get("name") == "c6-bound-c6-v27-base-l1-a0"]
    if (
        len(seals) != 1
        or seals[0].get("id") != V27_BASE_SEAL_ARTIFACT
        or seals[0].get("digest") != V27_BASE_SEAL_DIGEST
        or seals[0].get("expired") is not False
    ):
        raise RuntimeError("v27 Base sealed result identity drifted")

    stage = api_get(f"actions/runs/{V27_BASE_STAGE_RUN}")
    if (
        stage.get("head_sha") != LIVE_MAIN
        or stage.get("event") != "push"
        or stage.get("status") != "completed"
        or stage.get("conclusion") != "success"
        or stage.get("run_attempt") != 1
    ):
        raise RuntimeError("v27 Base stage authentication identity drifted")
    stage_artifacts = api_get(f"actions/runs/{V27_BASE_STAGE_RUN}/artifacts?per_page=100")["artifacts"]
    stage_status = [item for item in stage_artifacts if item.get("id") == V27_BASE_STAGE_STATUS_ARTIFACT]
    if (
        len(stage_status) != 1
        or stage_status[0].get("digest") != V27_BASE_STAGE_STATUS_DIGEST
        or stage_status[0].get("expired") is not False
    ):
        raise RuntimeError("v27 Base stage status artifact identity drifted")

    qualification = api_get(f"actions/runs/{V27_QUAL_BOUND_RUN}")
    if (
        qualification.get("head_sha") != W27_OLD
        or qualification.get("event") != "workflow_dispatch"
        or qualification.get("status") != "completed"
        or qualification.get("conclusion") != "success"
        or qualification.get("run_attempt") != 1
        or qualification.get("display_title") != "c6-bound-c6.s.qualification-c6-v27-s-qualification-a0"
    ):
        raise RuntimeError("v27 qualification workflow identity drifted")
    jobs = api_get(f"actions/runs/{V27_QUAL_BOUND_RUN}/jobs?per_page=100")["jobs"]
    by_name = {job.get("name"): job for job in jobs}
    if (
        by_name.get("Authenticate Base22 recovery source", {}).get("conclusion") != "success"
        or by_name.get("Execute frozen binding", {}).get("id") != V27_QUAL_RUN_JOB
        or by_name.get("Execute frozen binding", {}).get("conclusion") != "skipped"
        or by_name.get("Request authenticated continuation", {}).get("conclusion") != "skipped"
    ):
        raise RuntimeError("v27 qualification false-green job shape drifted")
    if api_get(f"actions/runs/{V27_QUAL_BOUND_RUN}/artifacts?per_page=100")["artifacts"]:
        raise RuntimeError("v27 qualification unexpectedly published an artifact")

    remote = out("remote", "get-url", "origin")
    observed = run("ls-remote", "--refs", remote, "refs/heads/codex/c6-dispatch/c6-v27*").stdout.strip().splitlines()
    expected = sorted([
        f"{V27_BASE_DISPATCH_COMMIT}\\trefs/heads/{V27_BASE_DISPATCH_BRANCH}",
        f"{V27_QUAL_DISPATCH_COMMIT}\\trefs/heads/{V27_QUAL_DISPATCH_BRANCH}",
    ])
    if sorted(observed) != expected:
        raise RuntimeError("v27 dispatch ref set differs from exact Base and qualification a0")
'''
    text = replace_once(
        text,
        "\ndef main() -> None:\n",
        verify_function + verify_v27_function + "\n\ndef main() -> None:\n",
    )
    text = replace_once(
        text,
        "    verify_v25_predispatch_evidence()\n",
        "    verify_v25_predispatch_evidence()\n    verify_v26_engineering_failure()\n    verify_v27_orchestration_failure()\n",
    )

    metadata_anchor = '        "prior_frozen_identities": {\n'
    metadata = (
        '        "superseded_engineering_v26": {\n'
        '            "P": P26,\n'
        '            "I_B": IB26,\n'
        '            "I_S": IS26,\n'
        '            "R": R26,\n'
        '            "workflow": W26_OLD,\n'
        '            "classification": "A_deterministic_cli_preregistration_path_typing_failure",\n'
        '            "dispatch_branch": V26_DISPATCH_BRANCH,\n'
        '            "dispatch_commit": V26_DISPATCH_COMMIT,\n'
        '            "dispatch_envelope_run_id": V26_DISPATCH_RUN,\n'
        '            "bound_run_id": V26_BOUND_RUN,\n'
        '            "recovered_core_evaluations": 3825,\n'
        '            "recovered_shard_attestations_success": 12,\n'
        '            "central_failed_job_id": V26_CENTRAL_JOB,\n'
        '            "sealed_economic_results": 0,\n'
        '            "sealed_checkpoints": 0,\n'
        '            "result_or_checkpoint_imported_records": 0,\n'
        '            "reruns": 0,\n'
        '            "reason": "The sole immutable v26 Base a0 recovered and semantically attested all 12 exact Base22 core shards without recomputing the 3825 core evaluations, then failed deterministically in the central recovered-L1 CLI because --preregistration was parsed as str while the path consumer required Path.read_bytes(). No sealed result or checkpoint was created; v26 is immutable forensic engineering evidence and is superseded by v27.",\n'
        '        },\n'
        '        "v27_base_valid_qualification_orchestration_failure": {\n'
        '            "P": P27,\n'
        '            "I_B": IB27,\n'
        '            "I_S": IS27,\n'
        '            "R": R27,\n'
        '            "workflow": W27_OLD,\n'
        '            "repair_workflow": W28_OLD,\n'
        '            "dispatch_workflow": W29,\n'
        '            "classification": "A_false_green_dependency_status_gating",\n'
        '            "base_dispatch_branch": V27_BASE_DISPATCH_BRANCH,\n'
        '            "base_dispatch_commit": V27_BASE_DISPATCH_COMMIT,\n'
        '            "base_dispatch_envelope_run_id": V27_BASE_DISPATCH_RUN,\n'
        '            "base_bound_run_id": V27_BASE_BOUND_RUN,\n'
        '            "base_central_job_id": V27_BASE_CENTRAL_JOB,\n'
        '            "base_seal_artifact_id": V27_BASE_SEAL_ARTIFACT,\n'
        '            "base_seal_artifact_digest": V27_BASE_SEAL_DIGEST,\n'
        '            "base_stage_run_id": V27_BASE_STAGE_RUN,\n'
        '            "base_stage_status_artifact_id": V27_BASE_STAGE_STATUS_ARTIFACT,\n'
        '            "base_stage_status_artifact_digest": V27_BASE_STAGE_STATUS_DIGEST,\n'
        '            "base_sealed_economic_results": 1,\n'
        '            "qualification_dispatch_branch": V27_QUAL_DISPATCH_BRANCH,\n'
        '            "qualification_dispatch_commit": V27_QUAL_DISPATCH_COMMIT,\n'
        '            "qualification_bound_run_id": V27_QUAL_BOUND_RUN,\n'
        '            "qualification_run_job_id": V27_QUAL_RUN_JOB,\n'
        '            "qualification_bound_job_conclusion": "skipped",\n'
        '            "qualification_artifacts": 0,\n'
        '            "result_or_checkpoint_imported_records": 0,\n'
        '            "reason": "The v27 Base result was sealed and fully authenticated by the stage consumer. Its sole immutable S-qualification successor then completed false-green without executing the bound job because the workflow omitted an explicit status-function override while depending on an intentionally skipped parallel_l1 job. This is not an economic rejection. W28 adds the cancellation-safe !cancelled() dependency gate and W29 rebinds immutable dispatch. v29 imports zero v27 result/checkpoint records and rebuilds the Base seal from fresh attestations of only the exact Base22 core shards.",\n'
        '        },\n'
        + metadata_anchor
    )
    text = replace_once(text, metadata_anchor, metadata)

    receipt_anchor = '        "base22_result_checkpoint_imported_records": 0,\n'
    receipt = receipt_anchor + (
        '        "v26_P": P26,\n'
        '        "v26_I_B": IB26,\n'
        '        "v26_I_S": IS26,\n'
        '        "v26_R": R26,\n'
        '        "v26_workflow": W26_OLD,\n'
        '        "v26_dispatch_branch": V26_DISPATCH_BRANCH,\n'
        '        "v26_dispatch_commit": V26_DISPATCH_COMMIT,\n'
        '        "v26_dispatch_envelope_run_id": V26_DISPATCH_RUN,\n'
        '        "v26_bound_run_id": V26_BOUND_RUN,\n'
        '        "v26_recovered_core_evaluations": 3825,\n'
        '        "v26_recovered_shard_attestations_success": 12,\n'
        '        "v26_central_failed_job_id": V26_CENTRAL_JOB,\n'
        '        "v26_sealed_economic_results": 0,\n'
        '        "v26_sealed_checkpoints": 0,\n'
        '        "v26_result_checkpoint_imported_records": 0,\n'
        '        "v26_reruns": 0,\n'
    )
    text = replace_once(text, receipt_anchor, receipt)

    receipt_v27_anchor = '        "v26_reruns": 0,\n'
    receipt_v27 = receipt_v27_anchor + (
        '        "v27_P": P27,\n'
        '        "v27_I_B": IB27,\n'
        '        "v27_I_S": IS27,\n'
        '        "v27_R": R27,\n'
        '        "v27_workflow": W27_OLD,\n'
        '        "v28_repair_workflow": W28_OLD,\n'
        '        "v29_dispatch_workflow": W29,\n'
        '        "v27_failure_classification": "A_false_green_dependency_status_gating",\n'
        '        "v27_base_dispatch_branch": V27_BASE_DISPATCH_BRANCH,\n'
        '        "v27_base_dispatch_commit": V27_BASE_DISPATCH_COMMIT,\n'
        '        "v27_base_dispatch_envelope_run_id": V27_BASE_DISPATCH_RUN,\n'
        '        "v27_base_bound_run_id": V27_BASE_BOUND_RUN,\n'
        '        "v27_base_central_job_id": V27_BASE_CENTRAL_JOB,\n'
        '        "v27_base_seal_artifact_id": V27_BASE_SEAL_ARTIFACT,\n'
        '        "v27_base_seal_artifact_digest": V27_BASE_SEAL_DIGEST,\n'
        '        "v27_base_stage_run_id": V27_BASE_STAGE_RUN,\n'
        '        "v27_base_stage_status_artifact_id": V27_BASE_STAGE_STATUS_ARTIFACT,\n'
        '        "v27_base_stage_status_artifact_digest": V27_BASE_STAGE_STATUS_DIGEST,\n'
        '        "v27_base_sealed_economic_results": 1,\n'
        '        "v27_qualification_dispatch_branch": V27_QUAL_DISPATCH_BRANCH,\n'
        '        "v27_qualification_dispatch_commit": V27_QUAL_DISPATCH_COMMIT,\n'
        '        "v27_qualification_bound_run_id": V27_QUAL_BOUND_RUN,\n'
        '        "v27_qualification_run_job_id": V27_QUAL_RUN_JOB,\n'
        '        "v27_qualification_bound_job_conclusion": "skipped",\n'
        '        "v27_qualification_artifacts": 0,\n'
        '        "v27_result_checkpoint_imported_records": 0,\n'
    )
    text = replace_once(text, receipt_v27_anchor, receipt_v27)

    old_purpose = (
        '        "Engineering-required v29 rebind after the Base22 GitHub runner interruption. "\n'
        '        "Reuse only the twelve explicitly authenticated successful Base22 core shard intermediates, "\n'
        '        "perform a second full byte-bound semantic validation in parallel, and preserve fixed Base/S economics, "\n'
        '        "765/3831/3875 manifests, official 17/958 gates, data, seeds, qualification and mechanical selection."\n'
    )
    new_purpose = (
        '        "Engineering-required v29 rebind after the v27 S-qualification workflow completed false-green "\n'
        '        "without executing its bound job. Preserve the authenticated v27 Base seal as forensic evidence "\n'
        '        "but import zero v27 result/checkpoint records; reuse only the twelve exact Base22 core shard "\n'
        '        "intermediates under fresh v29 attestations; and preserve fixed Base/S economics, 765/3831/3875 "\n'
        '        "manifests, official 17/958 gates, data, seeds, qualification and mechanical selection."\n'
    )
    text = replace_once(text, old_purpose, new_purpose)

    # Global version promotion changes the v25 historical reason suffix; restore it.
    text = text.replace(
        "v25 is retained as forensic pre-dispatch evidence and superseded by v29.",
        "v25 is retained as forensic pre-dispatch evidence and superseded by v26.",
    )

    if PR29_HEAD not in text or PR26_HEAD in text:
        raise SystemExit("v29 PR head replacement failed")
    if MAIN29 not in text or MAIN26 in text:
        raise SystemExit("v29 live-main replacement failed")
    for required in (
        "c6-causal-risk-closure-17x958-v29",
        "refs/heads/codex/c6-preregistration-v29",
        "refs/heads/codex/c6-base-v29",
        "refs/heads/codex/c6-s-v29",
        "refs/heads/codex/c6-evidence-v29",
        W29_REF,
        w29,
        W28,
        W27,
        W26,
        P26,
        IB26,
        IS26,
        R26,
        str(V26_BOUND_RUN),
        "superseded_engineering_v26",
        "v27_base_valid_qualification_orchestration_failure",
    ):
        if required not in text:
            raise SystemExit(f"missing v29 builder identity: {required}")

    Path("/tmp/build_c6_v29_freeze.py").write_text(text, encoding="utf-8")
    Path("c6-v29-anchor-receipt.txt").write_text(f"W29={w29}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
