from __future__ import annotations

from pathlib import Path

OLD_PR_HEAD = "5cede8cd8916f33fbf701d454a3a939a8352c0d3"
NEW_PR_HEAD = "80b4fc401ae1689ccc648c85c032bd32e0861e35"
OLD_LIVE_MAIN = "de01f669a901da92d1819f22a8e76da8f551e2fd"
NEW_LIVE_MAIN = "594298fca61dd50d811d6c4f94843c9b47b5a05c"
W21 = "f9da08afebf22b3dc03a1fb3a0ec351a79adf42c"
W23 = "8db7981012416d6a8fe4c4394e68051d8c293336"
W26 = "f77d7d4f91e3816d5cde705cbb2f50255aa9200e"
P23 = "2cfe3775d12f8e4dea0b82220b45c8a4598fb2a9"
IB23 = "322c3fce34eff6ce0e2ca12b2c455178257358ed"
IS23 = "b2007568b84d5abea224c3d316794d15509fc366"
R23 = "fc72ae96b6c03418e35046b5a377ef5a02f5d27b"
P24 = "f7400c0fff0718a0ac083f32f99b2fe96054647e"
IB24 = "f6aeb467b2aa70703a69a8b4053ec13aae90e2bc"
IS24 = "c10f748bd012eda3b1e3ab37f74ac734d76913ce"
R24 = "5204e90924533558a546f13a9c2f93b017898eb5"
P25 = "d6e76889e36f9efdc2685eb43aa57dff50ac1521"
IB25 = "f811cf8d0f0093b3148900bd0fbb96a9e67f9b51"
IS25 = "3d890d6d501c77a48a9ce46047d504b4f4838b46"
R25 = "71748de130d31d132e69999645e3285f81a651f9"
V24_INTEGRATION_RUN = 34149676167
V24_CENTRAL_JOB = 101839667216
CENTRAL_PROOF_RUN = 34163703732
CENTRAL_PROOF_HEAD = "40989faaa52d35d0edc51be9039ce6c4589068b7"
EXACT_CI_RUN = 34164530161
V25_DISPATCH_BRANCH = "codex/c6-dispatch/c6-v25-base-l1/c6.base.l1/a0"
V25_DISPATCH_COMMIT = "f5a4a98afbb603957e86a77cb1695899a6a99632"
V25_DISPATCH_RUN = 34166096907
V25_DISPATCH_JOB = 101877301635


def replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise SystemExit(f"expected one builder token, got {text.count(old)}: {old!r}")
    return text.replace(old, new)


def main() -> None:
    source = Path(".github/scripts/build_c6_v23_freeze.py")
    text = source.read_text(encoding="utf-8")

    text = replace_once(text, f'PR_HEAD = "{OLD_PR_HEAD}"', f'PR_HEAD = "{NEW_PR_HEAD}"')
    text = replace_once(text, f'LIVE_MAIN = "{OLD_LIVE_MAIN}"', f'LIVE_MAIN = "{NEW_LIVE_MAIN}"')
    text = replace_once(
        text,
        'p["frozen_at"] = "2026-09-07T17:32:30Z"',
        'p["frozen_at"] = "2026-09-07T22:25:00Z"',
    )

    # v23 builder is the verified structural template. Promote its current-version
    # identities directly to v26, while reintroducing W23 as immutable lineage below.
    text = text.replace("v23", "v26")
    text = text.replace("W23", "W26")
    text = replace_once(
        text,
        f'W26 = "{W23}"',
        f'W23_OLD = "{W23}"\nW26 = "{W26}"',
    )

    constants_anchor = 'R22 = "ea6dfa36b9796df7e4078d6f8d88b539a54072e3"\n'
    constants = constants_anchor + (
        f'P23 = "{P23}"\nIB23 = "{IB23}"\nIS23 = "{IS23}"\nR23 = "{R23}"\n'
        f'P24 = "{P24}"\nIB24 = "{IB24}"\nIS24 = "{IS24}"\nR24 = "{R24}"\n'
        f'P25 = "{P25}"\nIB25 = "{IB25}"\nIS25 = "{IS25}"\nR25 = "{R25}"\n'
        f'V24_INTEGRATION_RUN = {V24_INTEGRATION_RUN}\nV24_CENTRAL_JOB = {V24_CENTRAL_JOB}\n'
        f'CENTRAL_PROOF_RUN = {CENTRAL_PROOF_RUN}\nCENTRAL_PROOF_HEAD = "{CENTRAL_PROOF_HEAD}"\n'
        f'EXACT_CI_RUN = {EXACT_CI_RUN}\n'
        f'V25_DISPATCH_BRANCH = "{V25_DISPATCH_BRANCH}"\n'
        f'V25_DISPATCH_COMMIT = "{V25_DISPATCH_COMMIT}"\n'
        f'V25_DISPATCH_RUN = {V25_DISPATCH_RUN}\nV25_DISPATCH_JOB = {V25_DISPATCH_JOB}\n'
    )
    text = replace_once(text, constants_anchor, constants)

    expected_anchor = '        "refs/heads/codex/c6-evidence-v22": R22,\n'
    expected = expected_anchor + (
        '        "refs/heads/codex/c6-preregistration-v23": P23,\n'
        '        "refs/heads/codex/c6-base-v23": IB23,\n'
        '        "refs/heads/codex/c6-s-v23": IS23,\n'
        '        "refs/heads/codex/c6-evidence-v23": R23,\n'
        '        "refs/heads/codex/c6-preregistration-v24": P24,\n'
        '        "refs/heads/codex/c6-base-v24": IB24,\n'
        '        "refs/heads/codex/c6-s-v24": IS24,\n'
        '        "refs/heads/codex/c6-evidence-v24": R24,\n'
        '        "refs/heads/codex/c6-preregistration-v25": P25,\n'
        '        "refs/heads/codex/c6-base-v25": IB25,\n'
        '        "refs/heads/codex/c6-s-v25": IS25,\n'
        '        "refs/heads/codex/c6-evidence-v25": R25,\n'
        '        "refs/heads/codex/c6-v23-workflow-anchor": W23_OLD,\n'
    )
    text = replace_once(text, expected_anchor, expected)

    old_anchor_checks = '''    if out("rev-parse", f"{W26}^") != W21:\n        raise RuntimeError("W26 is not direct child of W21")\n    if changed_paths(W21, W26) != [".github/workflows/c6-bound-economic.yml"]:\n        raise RuntimeError("W26 changed more than the bound workflow")\n'''
    new_anchor_checks = '''    if out("rev-parse", f"{W23_OLD}^") != W21:\n        raise RuntimeError("W23 is not direct child of W21")\n    if changed_paths(W21, W23_OLD) != [".github/workflows/c6-bound-economic.yml"]:\n        raise RuntimeError("W23 changed more than the bound workflow")\n    if out("rev-parse", f"{W26}^") != W23_OLD:\n        raise RuntimeError("W26 is not direct child of W23")\n    if changed_paths(W23_OLD, W26) != [".github/workflows/c6-dispatch.yml"]:\n        raise RuntimeError("W26 changed more than the dispatch workflow")\n'''
    text = replace_once(text, old_anchor_checks, new_anchor_checks)

    text = replace_once(text, '        "workflow_parent": W21,\n', '        "workflow_parent": W23_OLD,\n')

    verify_function = '''\n\ndef verify_v25_predispatch_evidence() -> None:\n    ci = api_get(f"actions/runs/{EXACT_CI_RUN}")\n    if (\n        ci.get("head_sha") != PR_HEAD\n        or ci.get("event") != "pull_request"\n        or ci.get("status") != "completed"\n        or ci.get("conclusion") != "success"\n    ):\n        raise RuntimeError("current PR exact-head CI is not green")\n\n    proof = api_get(f"actions/runs/{CENTRAL_PROOF_RUN}")\n    if (\n        proof.get("head_sha") != CENTRAL_PROOF_HEAD\n        or proof.get("status") != "completed"\n        or proof.get("conclusion") != "success"\n        or proof.get("run_attempt") != 1\n    ):\n        raise RuntimeError("disk-backed central proof is not green")\n\n    dispatch = api_get(f"actions/runs/{V25_DISPATCH_RUN}")\n    if (\n        dispatch.get("head_branch") != V25_DISPATCH_BRANCH\n        or dispatch.get("head_sha") != V25_DISPATCH_COMMIT\n        or dispatch.get("event") != "push"\n        or dispatch.get("status") != "completed"\n        or dispatch.get("conclusion") != "failure"\n        or dispatch.get("run_attempt") != 1\n    ):\n        raise RuntimeError("v25 dispatch-envelope failure identity drifted")\n    jobs = api_get(f"actions/runs/{V25_DISPATCH_RUN}/jobs?per_page=100")["jobs"]\n    exact = [job for job in jobs if job.get("id") == V25_DISPATCH_JOB]\n    if len(exact) != 1 or exact[0].get("conclusion") != "failure":\n        raise RuntimeError("v25 failed dispatch job identity drifted")\n    steps = {step.get("name"): step.get("conclusion") for step in exact[0].get("steps", [])}\n    if steps.get("Validate immutable request") != "failure" or steps.get("Dispatch bound workflow") != "skipped":\n        raise RuntimeError("v25 failure was not pre-economic dispatch validation")\n\n    remote = out("remote", "get-url", "origin")\n    expected_ref = f"{V25_DISPATCH_COMMIT}\\trefs/heads/{V25_DISPATCH_BRANCH}"\n    observed = run(\n        "ls-remote", "--refs", remote, "refs/heads/codex/c6-dispatch/c6-v25*"\n    ).stdout.strip()\n    if observed != expected_ref:\n        raise RuntimeError("v25 dispatch ref set is not exactly the single failed a0 envelope")\n\n    bound_runs = api_get("actions/workflows/c6-bound-economic.yml/runs?event=workflow_dispatch&per_page=100")["workflow_runs"]\n    forbidden_title = "c6-bound-c6.base.l1-c6-v25-base-l1-a0"\n    if any(run.get("display_title") == forbidden_title for run in bound_runs):\n        raise RuntimeError("v25 unexpectedly acquired a bound economic run")\n'''
    text = replace_once(text, "\ndef main() -> None:\n", verify_function + "\n\ndef main() -> None:\n")
    text = replace_once(
        text,
        "    verify_base22_recovery_artifacts()\n",
        "    verify_base22_recovery_artifacts()\n    verify_v25_predispatch_evidence()\n",
    )

    metadata_anchor = '        "prior_frozen_identities": {\n'
    metadata = (
        '        "superseded_predispatch_v25": {\n'
        '            "P": P25,\n'
        '            "I_B": IB25,\n'
        '            "I_S": IS25,\n'
        '            "R": R25,\n'
        '            "workflow": W23_OLD,\n'
        '            "economic_dispatches": 0,\n'
        '            "sealed_economic_results": 0,\n'
        '            "result_or_checkpoint_imported_records": 0,\n'
        '            "classification": "A_dispatch_anchor_validation_failure",\n'
        '            "dispatch_branch": V25_DISPATCH_BRANCH,\n'
        '            "dispatch_commit": V25_DISPATCH_COMMIT,\n'
        '            "dispatch_envelope_run_id": V25_DISPATCH_RUN,\n'
        '            "failed_job_id": V25_DISPATCH_JOB,\n'
        '            "bound_economic_runs": 0,\n'
        '            "reason": "The sole immutable v25 a0 envelope failed closed before dispatch because W23 still hard-coded the historical v21 trusted workflow ref. No bound economic workflow, checkpoint, or result was created; v25 is retained as forensic pre-dispatch evidence and superseded by v26.",\n'
        '        },\n'
        + metadata_anchor
    )
    text = replace_once(text, metadata_anchor, metadata)

    receipt_anchor = '        "base22_result_checkpoint_imported_records": 0,\n'
    receipt = receipt_anchor + (
        '        "v25_P": P25,\n'
        '        "v25_I_B": IB25,\n'
        '        "v25_I_S": IS25,\n'
        '        "v25_R": R25,\n'
        '        "v25_workflow": W23_OLD,\n'
        '        "v25_economic_dispatches": 0,\n'
        '        "v25_sealed_economic_results": 0,\n'
        '        "v25_result_checkpoint_imported_records": 0,\n'
        '        "v25_dispatch_branch": V25_DISPATCH_BRANCH,\n'
        '        "v25_dispatch_commit": V25_DISPATCH_COMMIT,\n'
        '        "v25_dispatch_envelope_run_id": V25_DISPATCH_RUN,\n'
        '        "v25_dispatch_failed_job_id": V25_DISPATCH_JOB,\n'
        '        "v25_bound_economic_runs": 0,\n'
        '        "disk_backed_central_proof_run_id": CENTRAL_PROOF_RUN,\n'
        '        "disk_backed_central_proof_conclusion": "success",\n'
        '        "exact_head_ci_run_id": EXACT_CI_RUN,\n'
    )
    text = replace_once(text, receipt_anchor, receipt)

    if NEW_PR_HEAD not in text or OLD_PR_HEAD in text:
        raise SystemExit("v26 builder PR head replacement failed")
    if NEW_LIVE_MAIN not in text or OLD_LIVE_MAIN in text:
        raise SystemExit("v26 builder live-main replacement failed")
    for stale in (
        'refs/heads/codex/c6-preregistration-v23": p_sha',
        'refs/heads/codex/c6-base-v23": ib_sha',
        'refs/heads/codex/c6-s-v23": is_sha',
        'refs/heads/codex/c6-evidence-v23": r_sha',
    ):
        if stale in text:
            raise SystemExit(f"v23 output target survived: {stale}")
    for required in (
        "c6-causal-risk-closure-17x958-v26",
        "refs/heads/codex/c6-preregistration-v26",
        "refs/heads/codex/c6-base-v26",
        "refs/heads/codex/c6-s-v26",
        "refs/heads/codex/c6-evidence-v26",
        "codex/c6-v26-workflow-anchor",
        W26,
        W23,
        P25,
        IB25,
        IS25,
        R25,
        str(V25_DISPATCH_RUN),
        "superseded_predispatch_v25",
    ):
        if required not in text:
            raise SystemExit(f"missing v26 builder identity: {required}")

    output = Path("/tmp/build_c6_v26_freeze.py")
    output.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
