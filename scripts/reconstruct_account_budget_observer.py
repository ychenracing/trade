"""Reconstruct the exact observation-only candidate from preserved deterministic patches."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

MAIN_REVISION = "7d5efe5e4ea2159f82b9deb0ff4810730d8de15f"
OBSERVATION_DIR = Path("artifacts/diagnostics/account-budget-convergence")
OLD_CONTRACT_SHA256 = "12cc8e1e475b5c8dbf51c588c6f1b2251dd2fa78ae37896a3f3c45144fa260e7"
NEW_CONTRACT_SHA256 = "6f90ecaa310a5f3b54c7d74bf501987ad99b2707dd36628752734ab19c3e1e24"
FIXED_IDENTITY = {
    "GIT_AUTHOR_NAME": "Trade Evidence",
    "GIT_AUTHOR_EMAIL": "evidence@invalid.local",
    "GIT_AUTHOR_DATE": "2026-09-17T06:00:00Z",
    "GIT_COMMITTER_NAME": "Trade Evidence",
    "GIT_COMMITTER_EMAIL": "evidence@invalid.local",
    "GIT_COMMITTER_DATE": "2026-09-17T06:00:00Z",
}


def _run(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _assemble_observer_patch(repository: Path, output: Path) -> dict[str, object]:
    root = repository / OBSERVATION_DIR
    manifest = json.loads(
        (root / "observe-only-candidate.patch.manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assembled = bytearray()
    expected_offset = 0
    for item in manifest["parts"]:
        if item["offset"] != expected_offset:
            raise ValueError("observation patch part offset mismatch")
        data = (root / item["path"]).read_bytes()
        if len(data) != item["bytes"] or _sha256(data) != item["sha256"]:
            raise ValueError(f"observation patch part mismatch: {item['path']}")
        assembled.extend(data)
        expected_offset += len(data)
    if len(assembled) != manifest["total_bytes"]:
        raise ValueError("observation patch total length mismatch")
    if _sha256(assembled) != manifest["sha256"]:
        raise ValueError("observation patch total hash mismatch")
    output.write_bytes(assembled)
    return {
        "bytes": len(assembled),
        "sha256": _sha256(assembled),
        "parts": len(manifest["parts"]),
    }


def _replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise ValueError(f"expected one exact replacement in {path}: {old!r}")
    path.write_text(text.replace(old, new), encoding="utf-8")


def _apply_contract_revision(worktree: Path) -> dict[str, object]:
    contract_path = (
        worktree
        / "artifacts/diagnostics/no_waiver/production-primary/contract.json"
    )
    previous_path = contract_path.with_name("contract-before-nonrandom-mdd19.json")
    previous = contract_path.read_bytes()
    if _sha256(previous) != OLD_CONTRACT_SHA256:
        raise ValueError("unexpected pre-revision acceptance contract")
    previous_path.write_bytes(previous)

    contract = json.loads(previous)
    contract["authority"] = {
        "date": "2026-09-17",
        "user_instruction": "原18%非随机场景最大回撤门槛允许统一上浮最多1.0个百分点。",
        "approved_proposal": (
            "AB5 observation-only candidate may be evaluated under a uniform 19% "
            "absolute MDD limit for prefix, leave_one_out, add_one and permutation; "
            "random-subset incumbent-relative rules and every other gate remain unchanged."
        ),
        "scope": (
            "ychenracing/trade PR113 only; observation-only is the sole candidate; "
            "no third candidate, tuning, per-symbol or per-date exception"
        ),
    }
    limits = contract["formal"]["limits"]
    if limits.pop("mdd") != 0.18:
        raise ValueError("unexpected old non-random MDD limit")
    limits["nonrandom_mdd"] = 0.19
    limits["random_relative_reference_threshold"] = 0.18
    contract["changed_gates"]["all_scenario_mdd"] = (
        "19% uniformly applies to prefix/leave_one_out/add_one/permutation. "
        "Random subsets retain incumbent-relative maximum, P90 severity and count>18% "
        "non-deterioration; there is no random absolute19% gate."
    )
    retained = contract["retained_gates"]
    old_gate = "18% historical maximum MDD for every non-random family"
    retained[retained.index(old_gate)] = (
        "19% historical maximum MDD for every non-random family"
    )
    contract["deployment_revision"]["release_review"] = (
        "Matched main normal/adverse costs and registered cross-window pairs must be "
        "complete; main MDD must remain no worse than the fixed incumbent; disclose "
        "absolute losses, fees and workload."
    )
    contract["nonrandom_mdd_revision"] = {
        "date": "2026-09-17",
        "authority": (
            "Explicit owner instruction after observing prefix-01 MDD "
            "18.3413667487% under the old 18% limit."
        ),
        "previous_contract_path": "contract-before-nonrandom-mdd19.json",
        "previous_contract_sha256": OLD_CONTRACT_SHA256,
        "old_nonrandom_limit": 0.18,
        "new_nonrandom_limit": 0.19,
        "families": ["prefix", "leave_one_out", "add_one", "permutation"],
        "random_rules_unchanged": (
            "maximum, P90 severity and count above 18% must remain no worse than "
            "the paired fixed incumbent"
        ),
        "selection_disclosure": (
            "The owner changed risk preference after seeing prefix-01 at "
            "18.3413667487%; this is not an unseen-sample pass and does not rewrite "
            "the old-contract rejection."
        ),
        "no_other_gate_change": True,
    }
    revised = (
        json.dumps(contract, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    if _sha256(revised) != NEW_CONTRACT_SHA256:
        raise ValueError("revised acceptance contract hash mismatch")
    contract_path.write_bytes(revised)

    _replace_once(
        worktree / "quantfusion/application/production_pool.py",
        "'deployment_and_structured_sensitivity_drawdown_at_most_18pct':\n            scoped_worst <= .18 + metrics.DRAWDOWN_COMPARISON_TOLERANCE,",
        "'deployment_and_structured_sensitivity_drawdown_at_most_19pct':\n            scoped_worst <= .19 + metrics.DRAWDOWN_COMPARISON_TOLERANCE,",
    )
    _replace_once(
        worktree / "quantfusion/application/native_joint.py",
        f'PRIMARY_CONTRACT_SHA256 = "{OLD_CONTRACT_SHA256}"',
        f'PRIMARY_CONTRACT_SHA256 = "{NEW_CONTRACT_SHA256}"',
    )
    return {
        "previous_contract_sha256": OLD_CONTRACT_SHA256,
        "effective_contract_sha256": NEW_CONTRACT_SHA256,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--identity", type=Path, required=True)
    args = parser.parse_args()

    repository = args.repository.resolve()
    worktree = args.worktree.resolve()
    identity_path = args.identity.resolve()
    if worktree.exists():
        raise ValueError(f"worktree already exists: {worktree}")

    observer_patch = identity_path.parent / "observe-only-candidate.patch"
    observer_identity = _assemble_observer_patch(repository, observer_patch)
    _run(
        "git",
        "worktree",
        "add",
        "--detach",
        str(worktree),
        MAIN_REVISION,
        cwd=repository,
    )
    _run("git", "apply", str(observer_patch), cwd=worktree)
    contract_identity = _apply_contract_revision(worktree)
    _run("git", "add", "-A", cwd=worktree)
    tree = _run("git", "write-tree", cwd=worktree)
    env = {**os.environ, **FIXED_IDENTITY}
    commit = _run(
        "git",
        "commit-tree",
        tree,
        "-p",
        MAIN_REVISION,
        "-m",
        "Reconstruct AB5 observation-only candidate under nonrandom MDD 19%",
        cwd=worktree,
        env=env,
    )
    _run("git", "reset", "--hard", commit, cwd=worktree)
    if _run("git", "status", "--porcelain", cwd=worktree):
        raise RuntimeError("reconstructed candidate is not clean")

    contract_sha256 = _sha256(
        (
            worktree
            / "artifacts/diagnostics/no_waiver/production-primary/contract.json"
        ).read_bytes()
    )
    identity = {
        "schema_version": 1,
        "base_revision": MAIN_REVISION,
        "candidate_revision": commit,
        "candidate_tree": tree,
        "observer_patch": observer_identity,
        "contract_revision": contract_identity,
        "effective_contract_sha256": contract_sha256,
        "fixed_commit_identity": FIXED_IDENTITY,
    }
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    identity_path.write_text(
        json.dumps(identity, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(commit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
