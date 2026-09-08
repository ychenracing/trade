from __future__ import annotations

import os
import subprocess
from pathlib import Path

PR26_HEAD = "80b4fc401ae1689ccc648c85c032bd32e0861e35"
PR27_HEAD = "fa3f76bef5ba52ef05cd4f3f43273fd9742edf6f"
MAIN26 = "594298fca61dd50d811d6c4f94843c9b47b5a05c"
MAIN27 = "aafaa3458c517e229db5c8ff918a58772c6298db"
W23 = "8db7981012416d6a8fe4c4394e68051d8c293336"
W26 = "f77d7d4f91e3816d5cde705cbb2f50255aa9200e"
W27_REF = "codex/c6-v27-workflow-anchor"
P26 = "a56a97b56cf1479b46dcc258416255f6ac19b571"
IB26 = "2d7b6e2a153dcb3bc359f0ca75908e6a0c7e9631"
IS26 = "8c83a242481f510f3d5cba977de4628903011c62"
R26 = "85e7b3158f130e2f475de1bd67081ac3629739f1"
EXACT_CI_26 = 34164530161
EXACT_CI_27 = 34174540260
V26_DISPATCH_BRANCH = "codex/c6-dispatch/c6-v26-base-l1/c6.base.l1/a0"
V26_DISPATCH_COMMIT = "b40d1a14f757c4703dae327a4c2e2ec0547ef261"
V26_DISPATCH_RUN = 34170560344
V26_BOUND_RUN = 34170566443
V26_CENTRAL_JOB = 101891187197
V26_BOUND_TITLE = "c6-bound-c6.base.l1-c6-v26-base-l1-a0"


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


def create_w27() -> str:
    remote = out("git", "remote", "get-url", "origin")
    existing = run("git", "ls-remote", "--refs", remote, f"refs/heads/{W27_REF}").stdout.strip()
    if existing:
        sha, ref = existing.split("\t")
        if ref != f"refs/heads/{W27_REF}":
            raise SystemExit("unexpected W27 ref response")
        if out("git", "rev-parse", f"{sha}^") != W26:
            raise SystemExit("existing W27 is not direct child of W26")
        changed = out("git", "diff", "--name-only", W26, sha).splitlines()
        if changed != [".github/workflows/c6-dispatch.yml"]:
            raise SystemExit("existing W27 changed more than dispatch workflow")
        return sha

    if out("git", "rev-parse", f"{W26}^") != W23:
        raise SystemExit("W26 is not direct child of W23")
    if out("git", "diff", "--name-only", W23, W26).splitlines() != [".github/workflows/c6-dispatch.yml"]:
        raise SystemExit("W26 lineage changed unexpectedly")

    worktree = Path("/tmp/c6-w27-anchor")
    if worktree.exists():
        run("rm", "-rf", str(worktree))
    run("git", "worktree", "add", "--detach", str(worktree), W26)
    path = worktree / ".github/workflows/c6-dispatch.yml"
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        'expected_workflow_ref = "codex/c6-v26-workflow-anchor"',
        'expected_workflow_ref = "codex/c6-v27-workflow-anchor"',
    )
    path.write_text(text, encoding="utf-8")
    run("git", "add", ".github/workflows/c6-dispatch.yml", cwd=worktree)
    tree = out("git", "write-tree", cwd=worktree)
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "github-actions[bot]",
            "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
            "GIT_COMMITTER_NAME": "github-actions[bot]",
            "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
        }
    )
    commit = subprocess.run(
        ["git", "commit-tree", tree, "-p", W26],
        input="fix(c6): bind immutable dispatch to v27 workflow anchor\n",
        text=True,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    ).stdout.strip()
    if out("git", "rev-parse", f"{commit}^") != W26:
        raise SystemExit("new W27 parent mismatch")
    if out("git", "diff", "--name-only", W26, commit).splitlines() != [".github/workflows/c6-dispatch.yml"]:
        raise SystemExit("new W27 changed more than dispatch workflow")
    bound_old = out("git", "show", f"{W26}:.github/workflows/c6-bound-economic.yml")
    bound_new = out("git", "show", f"{commit}:.github/workflows/c6-bound-economic.yml")
    if bound_old != bound_new:
        raise SystemExit("bound economic workflow changed in W27")
    run("git", "push", remote, f"{commit}:refs/heads/{W27_REF}")
    observed = run("git", "ls-remote", "--refs", remote, f"refs/heads/{W27_REF}").stdout.strip()
    if observed != f"{commit}\trefs/heads/{W27_REF}":
        raise SystemExit("W27 remote ref did not bind exact commit")
    return commit


def main() -> None:
    # Reuse the already-tested v26 adapter to obtain the complete current builder,
    # then advance only the versioned identities and verified v26 failure evidence.
    run("python", ".github/scripts/build_c6_v26_from_v23.py")
    source = Path("/tmp/build_c6_v26_freeze.py")
    text = source.read_text(encoding="utf-8")
    w27 = create_w27()

    text = replace_once(text, f'PR_HEAD = "{PR26_HEAD}"', f'PR_HEAD = "{PR27_HEAD}"')
    text = replace_once(text, f'LIVE_MAIN = "{MAIN26}"', f'LIVE_MAIN = "{MAIN27}"')
    text = replace_once(text, f'EXACT_CI_RUN = {EXACT_CI_26}', f'EXACT_CI_RUN = {EXACT_CI_27}')
    text = replace_once(
        text,
        'p["frozen_at"] = "2026-09-07T22:25:00Z"',
        'p["frozen_at"] = "2026-09-08T00:54:00Z"',
    )

    text = text.replace("v26", "v27")
    text = text.replace("W26", "W27")
    text = replace_once(
        text,
        f'W27 = "{W26}"',
        f'W26_OLD = "{W26}"\nW27 = "{w27}"',
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
    )
    text = replace_once(text, constants_anchor, constants)

    expected_anchor = '        "refs/heads/codex/c6-evidence-v25": R25,\n'
    expected = expected_anchor + (
        '        "refs/heads/codex/c6-preregistration-v26": P26,\n'
        '        "refs/heads/codex/c6-base-v26": IB26,\n'
        '        "refs/heads/codex/c6-s-v26": IS26,\n'
        '        "refs/heads/codex/c6-evidence-v26": R26,\n'
        '        "refs/heads/codex/c6-v26-workflow-anchor": W26_OLD,\n'
    )
    text = replace_once(text, expected_anchor, expected)

    old_anchor_checks = '''    if out("rev-parse", f"{W23_OLD}^") != W21:\n        raise RuntimeError("W23 is not direct child of W21")\n    if changed_paths(W21, W23_OLD) != [".github/workflows/c6-bound-economic.yml"]:\n        raise RuntimeError("W23 changed more than the bound workflow")\n    if out("rev-parse", f"{W27}^") != W23_OLD:\n        raise RuntimeError("W27 is not direct child of W23")\n    if changed_paths(W23_OLD, W27) != [".github/workflows/c6-dispatch.yml"]:\n        raise RuntimeError("W27 changed more than the dispatch workflow")\n'''
    new_anchor_checks = '''    if out("rev-parse", f"{W23_OLD}^") != W21:\n        raise RuntimeError("W23 is not direct child of W21")\n    if changed_paths(W21, W23_OLD) != [".github/workflows/c6-bound-economic.yml"]:\n        raise RuntimeError("W23 changed more than the bound workflow")\n    if out("rev-parse", f"{W26_OLD}^") != W23_OLD:\n        raise RuntimeError("W26 is not direct child of W23")\n    if changed_paths(W23_OLD, W26_OLD) != [".github/workflows/c6-dispatch.yml"]:\n        raise RuntimeError("W26 changed more than the dispatch workflow")\n    if out("rev-parse", f"{W27}^") != W26_OLD:\n        raise RuntimeError("W27 is not direct child of W26")\n    if changed_paths(W26_OLD, W27) != [".github/workflows/c6-dispatch.yml"]:\n        raise RuntimeError("W27 changed more than the dispatch workflow")\n'''
    text = replace_once(text, old_anchor_checks, new_anchor_checks)
    text = replace_once(text, '        "workflow_parent": W23_OLD,\n', '        "workflow_parent": W26_OLD,\n')

    verify_function = '''\n\ndef verify_v26_engineering_failure() -> None:\n    dispatch = api_get(f"actions/runs/{V26_DISPATCH_RUN}")\n    if (\n        dispatch.get("head_branch") != V26_DISPATCH_BRANCH\n        or dispatch.get("head_sha") != V26_DISPATCH_COMMIT\n        or dispatch.get("event") != "push"\n        or dispatch.get("status") != "completed"\n        or dispatch.get("conclusion") != "success"\n        or dispatch.get("run_attempt") != 1\n    ):\n        raise RuntimeError("v26 dispatch envelope identity drifted")\n\n    run_payload = api_get(f"actions/runs/{V26_BOUND_RUN}")\n    if (\n        run_payload.get("head_sha") != W26_OLD\n        or run_payload.get("event") != "workflow_dispatch"\n        or run_payload.get("status") != "completed"\n        or run_payload.get("conclusion") != "failure"\n        or run_payload.get("run_attempt") != 1\n        or run_payload.get("display_title") != V26_BOUND_TITLE\n    ):\n        raise RuntimeError("v26 bound engineering failure identity drifted")\n    jobs = api_get(f"actions/runs/{V26_BOUND_RUN}/jobs?per_page=100")["jobs"]\n    shard_jobs = [job for job in jobs if str(job.get("name", "")).startswith("Parallel L1 core shard ")]\n    if len(shard_jobs) != 12 or any(job.get("conclusion") != "success" for job in shard_jobs):\n        raise RuntimeError("v26 did not preserve 12 successful recovered shard attestations")\n    for job in shard_jobs:\n        steps = {step.get("name"): step.get("conclusion") for step in job.get("steps", [])}\n        if (\n            steps.get("Recover exact Base22 shard bytes") != "success"\n            or steps.get("Compute exact chunk-preserving L1 shard") != "skipped"\n            or steps.get("Semantically attest exact L1 shard") != "success"\n        ):\n            raise RuntimeError("v26 recovered shard job semantics drifted")\n    central = [job for job in jobs if job.get("id") == V26_CENTRAL_JOB]\n    if len(central) != 1 or central[0].get("conclusion") != "failure":\n        raise RuntimeError("v26 central failure identity drifted")\n    steps = {step.get("name"): step.get("conclusion") for step in central[0].get("steps", [])}\n    if (\n        steps.get("Download exact same-run L1 shards") != "success"\n        or steps.get("Execute exact run binding") != "failure"\n        or steps.get("Upload sealed run export") != "skipped"\n    ):\n        raise RuntimeError("v26 central failure did not occur before sealed export")\n    continuation = [job for job in jobs if job.get("name") == "Request authenticated continuation"]\n    if len(continuation) != 1 or continuation[0].get("conclusion") != "skipped":\n        raise RuntimeError("v26 unexpectedly requested continuation")\n\n    remote = out("remote", "get-url", "origin")\n    observed = run("ls-remote", "--refs", remote, "refs/heads/codex/c6-dispatch/c6-v26*").stdout.strip()\n    expected_ref = f"{V26_DISPATCH_COMMIT}\\trefs/heads/{V26_DISPATCH_BRANCH}"\n    if observed != expected_ref:\n        raise RuntimeError("v26 dispatch ref set is not exactly the sole a0")\n'''
    text = replace_once(text, "\ndef main() -> None:\n", verify_function + "\n\ndef main() -> None:\n")
    text = replace_once(
        text,
        "    verify_v25_predispatch_evidence()\n",
        "    verify_v25_predispatch_evidence()\n    verify_v26_engineering_failure()\n",
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

    # Global version promotion changes the v25 historical reason suffix; restore it.
    text = text.replace(
        "v25 is retained as forensic pre-dispatch evidence and superseded by v27.",
        "v25 is retained as forensic pre-dispatch evidence and superseded by v26.",
    )

    if PR27_HEAD not in text or PR26_HEAD in text:
        raise SystemExit("v27 PR head replacement failed")
    if MAIN27 not in text or MAIN26 in text:
        raise SystemExit("v27 live-main replacement failed")
    for required in (
        "c6-causal-risk-closure-17x958-v27",
        "refs/heads/codex/c6-preregistration-v27",
        "refs/heads/codex/c6-base-v27",
        "refs/heads/codex/c6-s-v27",
        "refs/heads/codex/c6-evidence-v27",
        W27_REF,
        w27,
        W26,
        P26,
        IB26,
        IS26,
        R26,
        str(V26_BOUND_RUN),
        "superseded_engineering_v26",
    ):
        if required not in text:
            raise SystemExit(f"missing v27 builder identity: {required}")

    Path("/tmp/build_c6_v27_freeze.py").write_text(text, encoding="utf-8")
    Path("c6-v27-anchor-receipt.txt").write_text(f"W27={w27}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
