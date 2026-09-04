from __future__ import annotations
# ruff: noqa: E501
from pathlib import Path
import pytest
from quantfusion.application.c6_bound_run import (
    BoundRunError,
    ExclusiveLease,
    build_digest,
    build_parser,
    child_environment,
    resolve_attempt_paths,
    runtime_binding_signature,
    seal_export,
    validate_result_payload,
)
from quantfusion.application.c6_contract import canonical_payload_hash, strict_json_load
TOKEN_1 = "0000000000000001"
TOKEN_2 = "0000000000000002"
def test_v2_attempt_paths_are_derived_only_from_r_identity() -> None:
    paths = resolve_attempt_paths(
        {"logical_run_id": "c6-v5-base-l1", "stage": "L1"}, "r1-aabbccddeeff"
    )
    assert paths["payload"].as_posix().endswith("r1-aabbccddeeff/payload.json")
    assert paths["digest"].name == "digest.json"
    assert paths["child_checkpoint"].name == "child-checkpoint.bin"
    with pytest.raises(BoundRunError, match="attempt_id"):
        resolve_attempt_paths(
            {"logical_run_id": "c6-v5-base-l1", "stage": "L1"}, "../escape"
        )
def test_runtime_signature_and_v2_digest_bind_exact_bytes() -> None:
    payload = {
        "record_signature": "a" * 64,
        "run_bindings_revision": "b" * 40,
        "attempt_id": "a0",
        "workflow_run_id": "123",
        "resume_from": "",
        "resume_workflow_run_id": "",
        "fencing_token_sha256": "c" * 64,
        "direct_producer_identity": {},
        "transitive_base_producer_identity": {},
        "D": None,
        "C": None,
        "selection_status": "unselected",
        "item_manifest_count": 1,
        "item_manifest_sha256": "d" * 64,
    }
    signature = runtime_binding_signature(payload)
    artifact = b'{"ok":true}\n'
    digest = build_digest(
        stage="L1",
        record_id="c6.base.l1",
        binding_signature=signature,
        p_identity={"commit": "e" * 40},
        r_revision="b" * 40,
        source_revision="f" * 40,
        source_tree="1" * 40,
        d_identity=None,
        implementation=None,
        artifact_path="payload.json",
        artifact_bytes=artifact,
        payload_schema={"name": "L1_base_payload", "version": 2},
        payload={"ok": True},
        exit_code=0,
    )
    assert digest["schema_version"] == 2
    assert digest["artifact_full_byte_sha256"] == canonical_payload_hash({"ok": True})
    assert digest["canonical_result_payload_sha256"] == canonical_payload_hash(
        {"ok": True}
    )
def test_result_schema_rejects_nested_extra_keys() -> None:
    definitions = {"root": {"exact_keys": ["items"], "field_types": {"items": "child array"}}, "child": {"exact_keys": ["value"], "field_types": {"value": "integer"}}}
    with pytest.raises(BoundRunError, match="extra"):
        validate_result_payload({"items": [{"value": 1, "extra": 2}]}, {"canonical_payload_schema": {"name": "root"}}, {"schema_catalog": {"definitions": definitions}})
def _attempt_identity() -> dict[str, object]:
    return {
        "candidate_id": "C6-Base",
        "repository": "owner/repo",
        "workflow_run_id": "123",
        "workflow_run_attempt": "1",
        "resume_from": "",
        "resume_workflow_run_id": "",
        "d_commit": "",
        "d_selection_blob_oid": "",
        "d_selection_file_sha256": "",
        "producer_identity": {
            "artifact_full_byte_sha256": "",
            "attempt_id": "",
            "binding_id": "",
            "logical_run_id": "",
            "workflow_run_id": "",
        },
        "runner_image_os": "ubuntu24",
        "runner_image_version": "20260901.1",
        "python_version": "3.12.11",
    }
def test_lease_is_exclusive_and_rejects_stale_writer(tmp_path: Path) -> None:
    lease_path = tmp_path / "lease.json"
    first = ExclusiveLease.acquire(
        lease_path,
        logical_run_id="fixture",
        attempt_id="attempt-1",
        fencing_token=TOKEN_1,
        fencing_sequence=1,
    )
    with pytest.raises(BoundRunError, match="active lease"):
        ExclusiveLease.acquire(
            lease_path,
            logical_run_id="fixture",
            attempt_id="attempt-2",
            fencing_token=TOKEN_2,
            fencing_sequence=2,
        )
    first.release(terminal=True, checkpoint_id="checkpoint-1")
    second = ExclusiveLease.acquire(
        lease_path,
        logical_run_id="fixture",
        attempt_id="attempt-2",
        fencing_token=TOKEN_2,
        fencing_sequence=2,
        resume_from="checkpoint-1",
    )
    with pytest.raises(BoundRunError, match="stale fenced writer"):
        first.assert_current()
    second.assert_current()
def test_sealed_export_is_immutable_and_manifest_is_complete(tmp_path: Path) -> None:
    export = tmp_path / "sealed-export"
    manifest = seal_export(
        export,
        kind="result",
        source_revision="a" * 40,
        run_bindings_revision="b" * 40,
        workflow_revision="c" * 40,
        binding_id="c6.fixture",
        logical_run_id="fixture",
        attempt_id="attempt-1",
        fencing_token=TOKEN_1,
        attempt_identity=_attempt_identity(),
        files={"result.json": b'{"ok":true}\n'},
    )
    assert manifest["sealed"] is True
    persisted = strict_json_load(export / "manifest.json")
    assert persisted == manifest
    assert set(persisted["files"]) == {"result.json"}
    with pytest.raises(BoundRunError, match="already exists"):
        seal_export(
            export,
            kind="result",
            source_revision="a" * 40,
            run_bindings_revision="b" * 40,
            workflow_revision="c" * 40,
            binding_id="c6.fixture",
            logical_run_id="fixture",
            attempt_id="attempt-1",
            fencing_token=TOKEN_1,
            attempt_identity=_attempt_identity(),
            files={"result.json": b"changed"},
        )
def test_sealed_export_rejects_path_escape(tmp_path: Path) -> None:
    with pytest.raises(BoundRunError, match="invalid export path"):
        seal_export(
            tmp_path / "sealed-export",
            kind="checkpoint",
            source_revision="a" * 40,
            run_bindings_revision="b" * 40,
            workflow_revision="c" * 40,
            binding_id="c6.fixture",
            logical_run_id="fixture",
            attempt_id="attempt-1",
            fencing_token=TOKEN_1,
            attempt_identity=_attempt_identity(),
            files={"../outside": b"no"},
        )
def test_child_environment_is_an_exact_allowlist_and_keeps_token_off_argv() -> None:
    environment = child_environment(
        {
            "PATH": "/bin",
            "LANG": "C.UTF-8",
            "GITHUB_TOKEN": "credential",
            "PYTHONPATH": "/untrusted",
            "C6_STALE": "forbidden",
        },
        checkpoint_path=Path("artifacts/checkpoints/c6/run/checkpoint.json"),
        signature="s" * 64,
        logical_run_id="c6-v5-base-l1",
        attempt_id="a0",
        fencing_token=TOKEN_1,
        resume_from="",
    )
    assert environment == {
        "PATH": "/bin",
        "LANG": "C.UTF-8",
        "C6_BOUND_CHECKPOINT_PATH": "artifacts/checkpoints/c6/run/checkpoint.json",
        "C6_BOUND_SIGNATURE": "s" * 64,
        "C6_BOUND_LOGICAL_RUN_ID": "c6-v5-base-l1",
        "C6_BOUND_ATTEMPT_ID": "a0",
        "C6_BOUND_FENCING_TOKEN": TOKEN_1,
        "C6_BOUND_RESUME_FROM": "",
    }
def test_wrapper_parser_matches_the_frozen_workflow_without_token_argv() -> None:
    args = build_parser().parse_args(
        [
            "--bindings-file", "R.json",
            "--binding-id", "c6.base.l1",
            "--candidate-id", "C6-Base",
            "--logical-run-id", "c6-v5-base-l1",
            "--attempt-id", "a0",
            "--resume-from", "",
            "--resume-workflow-run-id", "",
            "--d-commit", "",
            "--d-selection-blob-oid", "",
            "--d-selection-file-sha256", "",
            "--decision-checkout", "decision",
            "--producer-identity-json", '{"artifact_full_byte_sha256":"","attempt_id":"","binding_id":"","logical_run_id":"","workflow_run_id":""}',
            "--source-revision", "a" * 40,
            "--run-bindings-revision", "b" * 40,
            "--workflow-revision", "c" * 40,
            "--runner-image-os", "ubuntu24",
            "--runner-image-version", "20260901.1",
            "--python-version", "3.12.11",
            "--repository", "owner/repo",
            "--workflow-run-id", "123",
            "--workflow-run-attempt", "1",
            "--require-durable-lease",
        ]
    )
    assert args.binding_id == "c6.base.l1"
    assert not hasattr(args, "fencing_token")
