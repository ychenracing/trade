from __future__ import annotations

from pathlib import Path

OLD_PR_HEAD = "7da0f7bfac490e022eba10a945119a3a8b90c1bc"
NEW_PR_HEAD = "80b4fc401ae1689ccc648c85c032bd32e0861e35"
W23_REF = "codex/c6-v23-workflow-anchor"
P24 = "f7400c0fff0718a0ac083f32f99b2fe96054647e"
IB24 = "f6aeb467b2aa70703a69a8b4053ec13aae90e2bc"
IS24 = "c10f748bd012eda3b1e3ab37f74ac734d76913ce"
R24 = "5204e90924533558a546f13a9c2f93b017898eb5"
V24_INTEGRATION_RUN = 34149676167
V24_INTEGRATION_HEAD = "5302e5818a9e4a687148deb827c1ea76413cd5a9"
V24_CENTRAL_JOB = 101839667216
CENTRAL_PROOF_RUN = 34163703732
CENTRAL_PROOF_HEAD = "40989faaa52d35d0edc51be9039ce6c4589068b7"
EXACT_CI_RUN = 34164530161


def replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise SystemExit(f"expected one builder token, got {text.count(old)}: {old!r}")
    return text.replace(old, new)


def main() -> None:
    source = Path("/tmp/build_c6_v24_freeze.py")
    text = source.read_text(encoding="utf-8")

    text = replace_once(
        text,
        f'PR_HEAD = "{OLD_PR_HEAD}"',
        f'PR_HEAD = "{NEW_PR_HEAD}"',
    )
    text = replace_once(
        text,
        'p["frozen_at"] = "2026-09-07T17:50:00Z"',
        'p["frozen_at"] = "2026-09-07T21:53:00Z"',
    )

    placeholder = "__C6_RETAIN_W23_WORKFLOW_REF__"
    if text.count(W23_REF) < 2:
        raise SystemExit("W23 workflow ref is unexpectedly absent from v24 builder")
    text = text.replace(W23_REF, placeholder)
    text = text.replace("v24", "v25")
    text = text.replace(placeholder, W23_REF)

    constants_anchor = 'R23 = "fc72ae96b6c03418e35046b5a377ef5a02f5d27b"\n'
    constants = constants_anchor + (
        f'P24 = "{P24}"\n'
        f'IB24 = "{IB24}"\n'
        f'IS24 = "{IS24}"\n'
        f'R24 = "{R24}"\n'
        f'V24_INTEGRATION_RUN = {V24_INTEGRATION_RUN}\n'
        f'V24_INTEGRATION_HEAD = "{V24_INTEGRATION_HEAD}"\n'
        f'V24_CENTRAL_JOB = {V24_CENTRAL_JOB}\n'
        f'CENTRAL_PROOF_RUN = {CENTRAL_PROOF_RUN}\n'
        f'CENTRAL_PROOF_HEAD = "{CENTRAL_PROOF_HEAD}"\n'
        f'EXACT_CI_RUN = {EXACT_CI_RUN}\n'
    )
    text = replace_once(text, constants_anchor, constants)

    expected_anchor = '        "refs/heads/codex/c6-evidence-v23": R23,\n'
    expected = expected_anchor + (
        '        "refs/heads/codex/c6-preregistration-v24": P24,\n'
        '        "refs/heads/codex/c6-base-v24": IB24,\n'
        '        "refs/heads/codex/c6-s-v24": IS24,\n'
        '        "refs/heads/codex/c6-evidence-v24": R24,\n'
    )
    text = replace_once(text, expected_anchor, expected)

    verify_function = '''\n\ndef verify_v24_predispatch_evidence() -> None:\n    ci = api_get(f"actions/runs/{EXACT_CI_RUN}")\n    if (\n        ci.get("head_sha") != PR_HEAD\n        or ci.get("event") != "pull_request"\n        or ci.get("status") != "completed"\n        or ci.get("conclusion") != "success"\n    ):\n        raise RuntimeError("current PR exact-head CI is not green")\n\n    integration = api_get(f"actions/runs/{V24_INTEGRATION_RUN}")\n    if (\n        integration.get("head_branch") != "codex/c6-v24-real-shard-integration-20260908"\n        or integration.get("head_sha") != V24_INTEGRATION_HEAD\n        or integration.get("status") != "completed"\n        or integration.get("conclusion") != "failure"\n        or integration.get("run_attempt") != 1\n    ):\n        raise RuntimeError("v24 read-only integration identity drifted")\n    jobs = api_get(f"actions/runs/{V24_INTEGRATION_RUN}/jobs?per_page=100")["jobs"]\n    observed: dict[int, tuple[str | None, str | None]] = {}\n    prefix = "Attest recovered Base22 shard "\n    suffix = " under I_B24"\n    for job in jobs:\n        name = str(job.get("name", ""))\n        if name.startswith(prefix) and name.endswith(suffix):\n            raw_index = name[len(prefix):-len(suffix)]\n            if raw_index.isdigit():\n                observed[int(raw_index)] = (job.get("status"), job.get("conclusion"))\n    if observed != {index: ("completed", "success") for index in range(12)}:\n        raise RuntimeError("v24 did not retain exactly twelve successful semantic attestations")\n    central = [job for job in jobs if job.get("id") == V24_CENTRAL_JOB]\n    if len(central) != 1 or central[0].get("conclusion") != "failure":\n        raise RuntimeError("v24 central interruption identity drifted")\n\n    proof = api_get(f"actions/runs/{CENTRAL_PROOF_RUN}")\n    if (\n        proof.get("head_sha") != CENTRAL_PROOF_HEAD\n        or proof.get("status") != "completed"\n        or proof.get("conclusion") != "success"\n        or proof.get("run_attempt") != 1\n    ):\n        raise RuntimeError("disk-backed real central proof is not green")\n    proof_jobs = api_get(f"actions/runs/{CENTRAL_PROOF_RUN}/jobs?per_page=100")["jobs"]\n    if len(proof_jobs) != 1 or proof_jobs[0].get("name") != "central-proof" or proof_jobs[0].get("conclusion") != "success":\n        raise RuntimeError("disk-backed real central proof job identity drifted")\n\n    remote = out("remote", "get-url", "origin")\n    dispatch = run("ls-remote", "--refs", remote, "refs/heads/codex/c6-dispatch/c6-v24*")\n    if dispatch.stdout.strip():\n        raise RuntimeError("v24 unexpectedly acquired an economic dispatch ref")\n'''
    text = replace_once(text, "\ndef main() -> None:\n", verify_function + "\n\ndef main() -> None:\n")
    text = replace_once(
        text,
        "    verify_base22_recovery_artifacts()\n",
        "    verify_base22_recovery_artifacts()\n    verify_v24_predispatch_evidence()\n",
    )

    metadata_anchor = '        "superseded_predispatch_v23": {\n'
    metadata = (
        '        "superseded_predispatch_v24": {\n'
        '            "P": P24,\n'
        '            "I_B": IB24,\n'
        '            "I_S": IS24,\n'
        '            "R": R24,\n'
        '            "workflow": W23,\n'
        '            "economic_dispatches": 0,\n'
        '            "sealed_economic_results": 0,\n'
        '            "result_or_checkpoint_imported_records": 0,\n'
        '            "classification": "A_read_only_central_verification_interruption",\n'
        '            "read_only_integration_run_id": V24_INTEGRATION_RUN,\n'
        '            "successful_semantic_attestation_jobs": 12,\n'
        '            "failed_central_job_id": V24_CENTRAL_JOB,\n'
        '            "failed_central_python_traceback": False,\n'
        '            "central_only_proof_run_id": CENTRAL_PROOF_RUN,\n'
        '            "central_only_proof_conclusion": "success",\n'
        '            "central_only_proof_economic_dispatches": 0,\n'
        '            "recovered_base22_core_evaluations": 3825,\n'
        '            "reason": "All twelve exact Base22 shards passed byte-bound semantic/formula/execution-facts attestation under v24; the eager central verification was interrupted by the hosted runner before any economic dispatch. A disk-backed central-only proof on the same attested bytes subsequently completed successfully, so immutable v24 remains unexecuted and superseded by v25.",\n'
        '        },\n'
        + metadata_anchor
    )
    text = replace_once(text, metadata_anchor, metadata)

    receipt_anchor = '        "base22_result_checkpoint_imported_records": 0,\n'
    receipt = receipt_anchor + (
        '        "v24_P": P24,\n'
        '        "v24_I_B": IB24,\n'
        '        "v24_I_S": IS24,\n'
        '        "v24_R": R24,\n'
        '        "v24_economic_dispatches": 0,\n'
        '        "v24_sealed_economic_results": 0,\n'
        '        "v24_result_checkpoint_imported_records": 0,\n'
        '        "v24_read_only_integration_run_id": V24_INTEGRATION_RUN,\n'
        '        "v24_successful_semantic_attestations": 12,\n'
        '        "v24_failed_central_job_id": V24_CENTRAL_JOB,\n'
        '        "disk_backed_central_proof_run_id": CENTRAL_PROOF_RUN,\n'
        '        "disk_backed_central_proof_conclusion": "success",\n'
        '        "exact_head_ci_run_id": EXACT_CI_RUN,\n'
    )
    text = replace_once(text, receipt_anchor, receipt)

    if NEW_PR_HEAD not in text or OLD_PR_HEAD in text:
        raise SystemExit("v25 builder PR head replacement failed")
    for stale in (
        'refs/heads/codex/c6-preregistration-v24": p_sha',
        'refs/heads/codex/c6-base-v24": ib_sha',
        'refs/heads/codex/c6-s-v24": is_sha',
        'refs/heads/codex/c6-evidence-v24": r_sha',
    ):
        if stale in text:
            raise SystemExit(f"v24 output target survived: {stale}")
    for required in (
        "c6-causal-risk-closure-17x958-v25",
        "refs/heads/codex/c6-preregistration-v25",
        "refs/heads/codex/c6-base-v25",
        "refs/heads/codex/c6-s-v25",
        "refs/heads/codex/c6-evidence-v25",
        W23_REF,
        P24,
        IB24,
        IS24,
        R24,
        str(V24_INTEGRATION_RUN),
        str(CENTRAL_PROOF_RUN),
        str(EXACT_CI_RUN),
        "superseded_predispatch_v24",
    ):
        if required not in text:
            raise SystemExit(f"missing v25 builder identity: {required}")

    output = Path("/tmp/build_c6_v25_freeze.py")
    output.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
