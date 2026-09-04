from __future__ import annotations
# ruff: noqa: E501
import hashlib
import json
import subprocess
from pathlib import Path
import pytest
from quantfusion.application.c6_contract import (
    ContractError,
    binding_identity,
    canonical_json_bytes,
    canonical_payload_hash,
    economic_tree_hash,
    load_preregistration,
    load_run_bindings,
    manifest_identity,
    render_bound_argv,
    strict_json_loads,
)
REPOSITORY = Path(__file__).resolve().parents[2]
PREREGISTRATION = REPOSITORY / "artifacts/diagnostics/c6-preregistration.json"
def test_strict_json_rejects_duplicate_keys_and_nonfinite_numbers() -> None:
    with pytest.raises(ContractError, match="duplicate JSON key"):
        strict_json_loads('{"x":1,"x":2}')
    with pytest.raises(ContractError, match="non-finite"):
        strict_json_loads('{"x":NaN}')
    with pytest.raises(ContractError, match="non-finite"):
        canonical_json_bytes({"x": float("inf")})
    with pytest.raises(ContractError, match="object keys must be strings"):
        canonical_json_bytes({1: "x"})
def test_canonical_payload_hash_uses_the_frozen_serialization() -> None:
    payload = {"z": [2, 1], "é": "值", "a": {"ok": True}}
    expected = (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    assert canonical_json_bytes(payload) == expected
    assert canonical_payload_hash(payload) == hashlib.sha256(expected).hexdigest()
def test_manifest_identity_is_sorted_unique_and_lf_terminated() -> None:
    identity = manifest_identity(["z", "a"])
    assert identity == {
        "count": 2,
        "unique_count": 2,
        "sha256": hashlib.sha256(b"a\nz\n").hexdigest(),
    }
    with pytest.raises(ContractError, match="duplicate manifest ID"):
        manifest_identity(["same", "same"])
def test_preregistration_rejects_extra_root_keys(tmp_path: Path) -> None:
    prereg = {
        "schema_version": 1,
        "kind": "synthetic",
        "status": "frozen",
    }
    path = tmp_path / "P.json"
    path.write_text(json.dumps({**prereg, "surprise": True}), encoding="utf-8")
    with pytest.raises(ContractError, match="unexpected keys"):
        load_preregistration(
            path,
            expected_keys={"schema_version", "kind", "status"},
        )
def test_frozen_preregistration_version_matches_authority_branch() -> None:
    preregistration = load_preregistration(PREREGISTRATION, repository=REPOSITORY)
    assert preregistration["schema_version"] == 2
    prefix = "c6-causal-risk-closure-17x958-"
    experiment_id = preregistration["experiment_id"]
    assert experiment_id.startswith(prefix)
    version = experiment_id.removeprefix(prefix)
    assert version.startswith("v") and version[1:].isdigit()
    assert not version.startswith("v0")
    assert preregistration["authority"]["branch"] == (
        f"codex/c6-causal-risk-closure-{version}"
    )
    assert len(preregistration["run_templates"]["binding_specs"]) == 7
    assert preregistration["checkpoint_and_lease_protocol"]["schema_version"] == 2
    definitions = preregistration["schema_catalog"]["definitions"]
    assert "base_evaluation_id" in definitions["common_prefix_comparison"]["exact_keys"]
    assert "base_evaluation_id" in definitions["no_effect_comparison"]["exact_keys"]
    assert definitions["no_effect_comparison"]["field_types"]["item_kind"] == (
        "literal evaluation"
    )
def test_bound_argv_accepts_only_declared_late_slots() -> None:
    template = ["python", "-m", "fixture", "--source", "{source_revision}"]
    assert render_bound_argv(
        template,
        {"source_revision": "a" * 40},
        allowed_slots={"source_revision"},
    ) == ["python", "-m", "fixture", "--source", "a" * 40]
    with pytest.raises(ContractError, match="unknown late-bound slot"):
        render_bound_argv(
            template,
            {"source_revision": "a" * 40, "threshold": "0.19"},
            allowed_slots={"source_revision"},
        )
def test_run_binding_has_an_exact_schema_and_self_consistent_identity(
    tmp_path: Path,
) -> None:
    p_identity = {
        "commit": "f" * 40,
        "tree": "1" * 40,
        "blob": "2" * 40,
        "sha256": "3" * 64,
    }
    workflow: dict[str, object] = {}
    implementations = {
        "I_B": {"commit": "a" * 40, "tree": "b" * 40, "required_blobs": {}},
        "I_S": {"commit": "c" * 40, "tree": "d" * 40, "required_blobs": {}},
    }
    binding = {
        "record_id": "c6.fixture.0",
        "workflow_binding_id": "c6.fixture",
        "candidate_id": "C6-Base",
        "logical_run_id": "fixture-0",
        "initial_attempt_id": "a0",
        "stage": "L1",
        "source_alias": "I_B",
        "source_revision": "a" * 40,
        "source_tree": "b" * 40,
        "source_blob_identities": {},
        "P": p_identity,
        "workflow": workflow,
        "entrypoint": "quantfusion.application.c6_diagnostics",
        "argv": ["python", "-m", "fixture", "--output", "{OUTPUT_PATH}"],
        "runtime_late_slots": ["OUTPUT_PATH"],
        "resolved_inputs": {},
        "scenario_manifest_identity": {
            "name": "FIXTURE_IDS",
            "count": 1,
            "unique_count": 1,
            "sha256": "d" * 64,
        },
        "synthetic_control_manifest_identity": None,
        "evaluation_manifest_identity": None,
        "item_manifest_contract": {},
        "producer_policy": "none",
        "decision_policy": "none",
        "runtime": {},
        "attempt_policy": {},
        "paths": {},
        "canonical_payload_schema": {},
        "exit_semantics": {},
        "record_signature": "",
    }
    record_ids = (
        "c6.base.l1", "c6.s.qualification", "c6.base_plus_s.l1",
        "c6.base.selected.l2", "c6.base_plus_s.selected.l2",
        "c6.base.selected.l4", "c6.base_plus_s.selected.l4",
    )
    records = []
    for index, record_id in enumerate(record_ids):
        candidate = "C6-Base" if record_id.startswith("c6.base.") else "C6-Base+S"
        alias = "I_B" if candidate == "C6-Base" else "I_S"
        implementation = implementations[alias]
        record = {
            **binding,
            "record_id": record_id,
            "logical_run_id": f"fixture-{index}",
            "candidate_id": candidate,
            "source_alias": alias,
            "source_revision": implementation["commit"],
            "source_tree": implementation["tree"],
        }
        record["record_signature"] = binding_identity(record)
        records.append(record)
    payload = {
        "schema_version": 2,
        "kind": "c6_run_bindings",
        "status": "frozen_before_economic_dispatch",
        "P": p_identity,
        "workflow": workflow,
        "implementations": implementations,
        "binding_records": records,
        "cross_record_invariants": {},
        "selection_validator": {},
        "serialization": "canonical_json_v2",
    }
    path = tmp_path / "R.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_run_bindings(path, allowed_late_slots={"OUTPUT_PATH"}) == payload
    payload["binding_records"][0]["threshold"] = 0.19
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ContractError, match="unexpected keys"):
        load_run_bindings(path, allowed_late_slots={"OUTPUT_PATH"})
    with pytest.raises(ContractError, match="undeclared placeholder"):
        render_bound_argv(
            ["python", "{threshold}"],
            {"threshold": "0.19"},
            allowed_slots={"source_revision"},
        )
def test_economic_tree_hash_uses_exact_git_records_and_allowlist(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "fixture@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Fixture"], cwd=tmp_path, check=True
    )
    (tmp_path / "economic.txt").write_text("fixed\n", encoding="utf-8")
    (tmp_path / "report.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "first"], cwd=tmp_path, check=True)
    first = economic_tree_hash("HEAD", ["report.txt"], repository=tmp_path)
    (tmp_path / "report.txt").write_text("two\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "report"], cwd=tmp_path, check=True)
    assert economic_tree_hash("HEAD", ["report.txt"], repository=tmp_path) == first
    (tmp_path / "economic.txt").write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "economic"], cwd=tmp_path, check=True)
    assert economic_tree_hash("HEAD", ["report.txt"], repository=tmp_path) != first
    with pytest.raises(ContractError, match="repo-relative"):
        economic_tree_hash("HEAD", ["../report.txt"], repository=tmp_path)
