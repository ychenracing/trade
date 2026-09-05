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


def test_streamed_json_preserves_canonical_bytes_and_indexes_records(tmp_path):
    from quantfusion.io.c6_stream import FileArray, canonical_chunks, load_object, write_json
    from quantfusion.application.c6_contract import canonical_json_bytes
    payload = {"evaluations": [{"text": "中文\\\"\n", "number": -0.0, "nested": [1, True, None]}, {}], "complete": True}
    path = tmp_path / "payload.json"
    path.write_bytes(canonical_json_bytes(payload))
    loaded = load_object(path, chunk_size=1)
    assert isinstance(loaded["evaluations"], FileArray)
    assert loaded["evaluations"][0] == payload["evaluations"][0]
    assert list(loaded["evaluations"][1:]) == [{}]
    assert b"".join(canonical_chunks(loaded)) == canonical_json_bytes(payload)
    output = tmp_path / "copy.json"
    write_json(output, loaded)
    assert output.read_bytes() == path.read_bytes()
    with pytest.raises(FileExistsError):
        write_json(output, loaded)


@pytest.mark.parametrize("raw", [
    b'{"evaluations":[{"a":1,"a":2}]}', b'{"a":1,"a":2}',
    b'{"evaluations":[NaN]}', b'{"evaluations":[1e999]}',
    b'{"evaluations":[1,]}', b'{"evaluations":[', b'{"a":1} extra',
    b'{"a":12x}', b'{"a":1,}', b'{"evaluations":["\xff"]}',
])
def test_streamed_json_rejects_corruption(tmp_path, raw):
    from quantfusion.io.c6_stream import load_object
    path = tmp_path / "bad.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        load_object(path, chunk_size=2)


def test_streamed_json_bounds_each_record_and_does_not_cache_values(tmp_path):
    from quantfusion.io.c6_stream import FileArray, load_object
    path = tmp_path / "payload.json"
    path.write_text('{"evaluations":[{"text":"' + 'x' * 200 + '"}]}')
    with pytest.raises(ValueError, match="record limit"):
        load_object(path, record_limit=100, chunk_size=7)
    records = load_object(path)["evaluations"]
    assert isinstance(records, FileArray)
    first = records[0]
    first["text"] = "mutated"
    assert records[0]["text"] == "x" * 200


def test_streamed_json_large_array_has_bounded_live_memory(tmp_path):
    import hashlib
    import tracemalloc
    from quantfusion.io.c6_stream import canonical_chunks, content_hash, load_object
    path = tmp_path / "large.json"
    record = b'{"text":"' + b'x' * (256 * 1024) + b'"}'
    with path.open("wb") as stream:
        stream.write(b'{"evaluations":[')
        for index in range(320):
            stream.write((b',' if index else b'') + record)
        stream.write(b']}\n')
    tracemalloc.start()
    try:
        payload = load_object(path)
        digest = hashlib.sha256()
        for chunk in canonical_chunks(payload):
            digest.update(chunk)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(payload["evaluations"]) == 320
    assert digest.hexdigest() == content_hash(path)
    assert path.stat().st_size > 80 * 1024 * 1024
    assert peak < 8 * 1024 * 1024


def test_streamed_archive_rejects_unsafe_members_and_size_before_extraction(tmp_path):
    import zipfile
    from quantfusion.io.c6_stream import extract_archive
    for index, name in enumerate(("../payload.json", "/payload.json", "a/b", "a\\b", ".hidden")):
        archive = tmp_path / f"unsafe-{index}.zip"
        with zipfile.ZipFile(archive, "w") as stream:
            stream.writestr(name, b'{}')
        with pytest.raises(ValueError, match="unsafe"):
            extract_archive(archive, tmp_path / f"out-{index}")
    archive = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as stream:
        stream.writestr("payload.json", b'x' * 10000)
    with pytest.raises(ValueError, match="expanded"):
        extract_archive(archive, tmp_path / "bomb-out", limit=1000)
    assert not (tmp_path / "bomb-out").exists()


def test_sealed_disk_payload_roundtrips_through_actual_archive_reader(tmp_path, monkeypatch):
    import io
    import zipfile
    from contextlib import contextmanager
    from quantfusion.application import c6_bound_run as runner
    source = tmp_path / "payload.json"
    source.write_bytes(b'{"evaluations":[{"value":1}]}\n')
    root = tmp_path / "sealed"
    seal_export(root, kind="result", source_revision="a" * 40, run_bindings_revision="b" * 40,
                workflow_revision="c" * 40, binding_id="c6.fixture", logical_run_id="fixture",
                attempt_id="a0", fencing_token=TOKEN_1, attempt_identity=_attempt_identity(),
                files={"payload.json": source, "digest.json": b'{}\n'})
    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, "w") as archive:
        for path in root.iterdir():
            archive.write(path, path.name)
    @contextmanager
    def response(*args, **kwargs):
        yield io.BytesIO(zipped.getvalue())
    store = runner.GitHubActionsLeaseStore("owner/repo", "fixture")
    monkeypatch.setattr(store, "_response", response)
    export = store._export(123, {"id": 1, "expired": False, "archive_download_url": "https://api.github.com/fixture"})
    try:
        assert isinstance(export.files["payload.json"], Path)
        assert export.files["payload.json"].read_bytes() == source.read_bytes()
        assert runner._json_content(export.files["payload.json"])["evaluations"][0] == {"value": 1}
    finally:
        export.close()

def _checkpoint_square(value: int) -> dict:
    return {"square": value * value}


def test_child_checkpoint_resumes_exact_prefix_without_recomputation(tmp_path) -> None:
    from quantfusion.application import c6_bound_run as runner
    assert hasattr(runner, "DiagnosticCheckpoint"), "children need an actual checkpoint executor"
    path = tmp_path / "child.json"
    ids = [f"scenario/{i}" for i in range(5)]
    first = runner.DiagnosticCheckpoint(path, ids, "a" * 64, budget_seconds=0, chunk_size=2)
    with pytest.raises(SystemExit) as stopped:
        first.map(_checkpoint_square, list(range(5)), ids, workers=1)
    assert stopped.value.code == 75
    saved = strict_json_load(path)
    assert [item["item_id"] for item in saved["completed_items"]] == ids[:2]
    assert runner.checkpoint_progress(path.read_bytes(), stage="L2", binding_signature="a" * 64, item_ids=ids) == ids[:2]
    second = runner.DiagnosticCheckpoint(path, ids, "b" * 64, resume_signature="a" * 64)
    # Poison completed inputs: resumed work must never evaluate these again.
    resumed = second.map(_checkpoint_square, [None, None, 2, 3, 4], ids, workers=1)
    assert list(resumed) == [_checkpoint_square(i) for i in range(5)]
    assert strict_json_load(path)["binding_signature"] == "b" * 64
    with pytest.raises(runner.BoundRunError):
        runner.DiagnosticCheckpoint(path, ids[::-1], "c" * 64, resume_signature="b" * 64)
    with pytest.raises(runner.BoundRunError):
        runner.DiagnosticCheckpoint(path, ids, "c" * 64)


def test_producer_dependency_uses_frozen_record_instead_of_version_literal() -> None:
    from quantfusion.application import c6_bound_run as runner
    assert hasattr(runner, "producer_dependency"), "producer identity must be derived from R"
    records = [{"record_id": "c6.base.l1", "workflow_binding_id": "c6.base.l1", "logical_run_id": "c6-v10-base-l1"}]
    assert runner.producer_dependency("c6.s.qualification", records) == ("c6.base.l1", "c6-v10-base-l1")
    with pytest.raises(runner.BoundRunError):
        runner.producer_dependency("c6.s.qualification", records + records)


@pytest.mark.parametrize("write_checkpoint", [True, False])
def test_bound_exit_75_is_success_only_after_valid_checkpoint_handoff(tmp_path, monkeypatch, write_checkpoint):
    import hashlib
    import json
    import platform
    from types import SimpleNamespace
    from quantfusion.application import c6_bound_run as runner
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("C6_FENCING_TOKEN", TOKEN_1)
    ids = ["scenario/one", "scenario/two"]
    identity = {"commit": "a" * 40, "tree": "b" * 40, "blob": "c" * 40, "sha256": "d" * 64}
    binding = {"record_id": "c6.base.l1", "logical_run_id": "fixture", "stage": "L1", "source_tree": "e" * 40,
               "candidate_id": "C6-Base", "record_signature": "f" * 64,
               "item_manifest_contract": {"count": 2, "sha256": hashlib.sha256(b"scenario/one\nscenario/two\n").hexdigest()},
               "runtime_late_slots": [], "argv": ["python", "-m", "quantfusion.application.c6_diagnostics"],
               "exit_semantics": {"checkpoint_incomplete_exit_code": 75, "terminal_success_exit_codes": [0], "terminal_rejected_exit_codes": []}}
    args = SimpleNamespace(producer_identity_json=json.dumps(_attempt_identity()["producer_identity"]), candidate_id="C6-Base",
                           repository="owner/repo", workflow_run_id="123", workflow_run_attempt=1, resume_from="", resume_workflow_run_id="",
                           d_commit="", d_selection_blob_oid="", d_selection_file_sha256="", runner_image_os="ubuntu24", runner_image_version="fixture",
                           python_version=platform.python_version(), bindings_file=tmp_path / "R.json", binding_id="c6.base.l1",
                           source_revision="a" * 40, run_bindings_revision="b" * 40, workflow_revision="c" * 40,
                           logical_run_id="fixture", attempt_id="a0", require_durable_lease=False)
    # Substitute immutable-input/API setup and the child process only. Exercise
    # production lease acquisition, checkpoint validation, sealing and exit status.
    monkeypatch.setattr(runner, "load_preregistration", lambda *a, **k: {})
    monkeypatch.setattr(runner, "load_run_bindings", lambda *a: {"P": identity, "binding_records": [binding]})
    monkeypatch.setattr(runner, "select_binding", lambda *a, **k: binding)
    monkeypatch.setattr(runner, "file_sha256", lambda *a: identity["sha256"])
    monkeypatch.setattr(runner, "_git_text", lambda argv, **k: identity["tree"] if argv[0] == "rev-parse" else ("100644 blob " + identity["blob"] + " path") if argv[0] == "ls-tree" else identity["blob"])
    monkeypatch.setattr(runner, "validate_runtime_identity", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_verify_checkout", lambda **k: None)
    monkeypatch.setattr(runner, "execution_item_ids", lambda *a: ids)
    def child(argv, **kwargs):
        if write_checkpoint:
            env = kwargs["env"]
            checkpoint = runner.DiagnosticCheckpoint(Path(env["C6_BOUND_CHECKPOINT_PATH"]), ids, env["C6_BOUND_SIGNATURE"], budget_seconds=0, chunk_size=1)
            with pytest.raises(SystemExit) as stopped:
                checkpoint.map(_checkpoint_square, [1, 2], ids, workers=1)
            assert stopped.value.code == 75
        return SimpleNamespace(returncode=75)
    monkeypatch.setattr(runner.subprocess, "run", child)
    if write_checkpoint:
        assert runner.execute_bound_binding(args) == 0
        manifest = strict_json_load("artifacts/checkpoints/c6/sealed-export/manifest.json")
        assert manifest["kind"] == "checkpoint" and manifest["sealed"] is True
        assert set(manifest["files"]) == {"checkpoint.json", "child-checkpoint.bin"}
    else:
        with pytest.raises(BoundRunError, match="without a valid sealed checkpoint"):
            runner.execute_bound_binding(args)
        assert not Path("artifacts/checkpoints/c6/sealed-export").exists()


def test_durable_history_restores_sealed_child_and_hands_off_lease(tmp_path, monkeypatch) -> None:
    import hashlib
    from quantfusion.application import c6_bound_run as runner
    from quantfusion.application.c6_contract import canonical_json_bytes
    ids = [f"scenario/{i}" for i in range(3)]
    path = tmp_path / "original-child"
    checkpoint = runner.DiagnosticCheckpoint(path, ids, "a" * 64, chunk_size=1, budget_seconds=0)
    with pytest.raises(SystemExit):
        checkpoint.map(_checkpoint_square, [0, 1, 2], ids, workers=1)
    child = path.read_bytes()
    hash_ids = lambda values: hashlib.sha256("".join(x + "\n" for x in values).encode()).hexdigest()
    wrapper = {"schema_version": 2, "kind": "c6_bound_checkpoint", "status": "checkpointed_incomplete",
               "record_id": "fixture", "binding_signature": "a" * 64, "source_revision": "a" * 40,
               "logical_run_id": "fixture", "attempt_id": "a0", "workflow_run_id": "123",
               "fencing_sequence": 123, "fencing_token_sha256": hashlib.sha256(TOKEN_1.encode()).hexdigest(),
               "resume_from": "", "resume_workflow_run_id": "", "child_checkpoint_kind": "c6_diagnostic_shard_v2",
               "child_checkpoint_path": "child-checkpoint.bin", "child_checkpoint_byte_size": len(child),
               "child_checkpoint_full_byte_sha256": hashlib.sha256(child).hexdigest(),
               "item_manifest_count": len(ids), "item_manifest_sha256": hash_ids(ids), "completed_item_ids": ids[:1],
               "completed_item_ids_sha256": hash_ids(ids[:1]), "next_item_ordinal": 1, "created_at": "2026-09-05T00:00:00Z"}
    raw = canonical_json_bytes(wrapper)
    checkpoint_id = hashlib.sha256(raw).hexdigest()
    export = tmp_path / "export"
    manifest = seal_export(export, kind="checkpoint", source_revision="a" * 40, run_bindings_revision="b" * 40,
                           workflow_revision="c" * 40, binding_id="c6.fixture", logical_run_id="fixture", attempt_id="a0",
                           fencing_token=TOKEN_1, attempt_identity=_attempt_identity(),
                           files={"checkpoint.json": raw, "child-checkpoint.bin": child})
    remote = runner.RemoteExport(123, manifest, (export / "manifest.json").read_bytes(),
                                 {"checkpoint.json": raw, "child-checkpoint.bin": child})
    history = runner.GitHubActionsLeaseStore("owner/repo", "fixture-token")
    prior = {"id": 123, "display_title": "c6-bound-c6.fixture-fixture-a0", "event": "workflow_dispatch",
             "head_branch": "anchor", "head_sha": "c" * 40, "run_attempt": 1, "status": "completed", "created_at": "2026-09-05T00:00:00Z"}
    current = {**prior, "id": 124, "status": "in_progress"}
    # Only the remote API boundary is substituted; the production restore validates
    # the real sealed bytes, exact item prefix, predecessor identity and fence.
    monkeypatch.setattr(history, "_pages", lambda path, key: [prior] if key == "workflow_runs" else [{"name": "c6-bound-fixture-a0"}])
    monkeypatch.setattr(history, "_json", lambda url, label: current if url.endswith("/124") else prior)
    monkeypatch.setattr(history, "_export", lambda run_id, artifact: remote)
    binding = {"record_id": "fixture", "workflow_binding_id": "c6.fixture", "logical_run_id": "fixture", "source_revision": "a" * 40,
               "workflow": {"revision": "c" * 40, "dispatch_ref": "anchor"}, "stage": "L2",
               "attempt_policy": {"dispatch_deadline_utc": "2099-01-01T00:00:00Z"}}
    restored, lease_path = tmp_path / "restored", tmp_path / "lease"
    history.restore(binding=binding, current_run_id=124, resume_from=checkpoint_id, resume_workflow_run_id="123",
                    checkpoint_path=restored, lease_path=lease_path, run_bindings_revision="b" * 40, item_ids=ids)
    lease = ExclusiveLease.acquire(lease_path, logical_run_id="fixture", attempt_id="r1-aabbccddeeff",
                                   fencing_token=TOKEN_2, fencing_sequence=124, resume_from=checkpoint_id)
    lease.assert_current()
    resumed = runner.DiagnosticCheckpoint(restored, ids, "b" * 64, resume_signature="a" * 64)
    assert list(resumed.map(_checkpoint_square, [None, 1, 2], ids, workers=1)) == [_checkpoint_square(i) for i in range(3)]
    prior["status"] = "in_progress"
    with pytest.raises(BoundRunError, match="prior logical-run workflow identity"):
        history.restore(binding=binding, current_run_id=124, resume_from=checkpoint_id, resume_workflow_run_id="123",
                        checkpoint_path=tmp_path / "forbidden", lease_path=tmp_path / "forbidden-lease", run_bindings_revision="b" * 40, item_ids=ids)


def test_official_checkpoint_sorted_storage_preserves_execution_prefix() -> None:
    from quantfusion.application.c6_bound_run import checkpoint_progress
    from quantfusion.application.c6_contract import canonical_json_bytes
    ids = ["scenario/prefix-05", "scenario/add-one-01", "scenario/random-01"]
    payload = {"signature": "a" * 64, "provenance": {}, "completed": 2,
               "scenario_count": 3, "results": [{"scenario_id": "add-one-01"}, {"scenario_id": "prefix-05"}]}
    assert checkpoint_progress(canonical_json_bytes(payload), stage="L4", binding_signature="b" * 64, item_ids=ids) == ids[:2]


def test_official_bound_budget_yields_only_after_a_durable_incomplete_chunk(monkeypatch) -> None:
    from quantfusion.application import stress
    assert hasattr(stress, "_bound_budget_expired"), "official runner must yield before platform cancellation"
    monkeypatch.setenv("C6_BOUND_CHECKPOINT_PATH", "checkpoint.json")
    assert stress._bound_budget_expired(0, 901, completed=10, total=100)
    assert not stress._bound_budget_expired(0, 899, completed=10, total=100)
    assert not stress._bound_budget_expired(0, 901, completed=100, total=100)
    monkeypatch.delenv("C6_BOUND_CHECKPOINT_PATH")
    assert not stress._bound_budget_expired(0, 901, completed=10, total=100)


def _cloud_checkpoint_smoke(stage: str, directory: Path) -> None:
    """Exercise real process pools and sealed artifact transfer using synthetic rows."""
    import hashlib
    import json
    from quantfusion.application.c6_bound_run import DiagnosticCheckpoint, checkpoint_progress
    directory.mkdir(parents=True, exist_ok=True)
    ids = [f"scenario/{i}" for i in range(5)]
    path = directory / "child-checkpoint.bin"
    if stage == "checkpoint":
        checkpoint = DiagnosticCheckpoint(path, ids, "a" * 64, budget_seconds=0, chunk_size=2)
        try:
            checkpoint.map(dict, [{"ordinal": i} for i in range(5)], ids, workers=2)
        except SystemExit as stopped:
            assert stopped.code == 75
        else:
            raise AssertionError("fixture must checkpoint before completion")
        assert checkpoint_progress(path.read_bytes(), stage="L2", binding_signature="a" * 64, item_ids=ids) == ids[:2]
        seal_export(directory / "export", kind="checkpoint", source_revision="a" * 40,
                    run_bindings_revision="b" * 40, workflow_revision="c" * 40,
                    binding_id="c6.fixture", logical_run_id="fixture", attempt_id="attempt-1",
                    fencing_token=TOKEN_1, attempt_identity=_attempt_identity(),
                    files={"child-checkpoint.bin": path.read_bytes()})
    elif stage == "resume":
        manifest = strict_json_load(directory / "manifest.json")
        assert manifest["kind"] == "checkpoint" and manifest["sealed"] is True
        assert set(manifest["files"]) == {"child-checkpoint.bin"}
        assert manifest["files"]["child-checkpoint.bin"] == hashlib.sha256(path.read_bytes()).hexdigest()
        checkpoint = DiagnosticCheckpoint(path, ids, "b" * 64, resume_signature="a" * 64)
        actual = checkpoint.map(dict, [None, None, *({"ordinal": i} for i in range(2, 5))], ids, workers=2)
        expected = [{"ordinal": i} for i in range(5)]
        assert list(actual) == expected
        (directory / "verified.json").write_text(json.dumps({"passed": True, "completed": len(actual), "resumed_without_recompute": True}))
    else:
        raise ValueError("unknown fixture stage")
def test_v2_attempt_paths_are_derived_only_from_r_identity() -> None:
    paths = resolve_attempt_paths(
        {"logical_run_id": "c6-v6-base-l1", "stage": "L1"}, "r1-aabbccddeeff"
    )
    assert paths["payload"].as_posix().endswith("r1-aabbccddeeff/payload.json")
    assert paths["digest"].name == "digest.json"
    assert paths["child_checkpoint"].name == "child-checkpoint.bin"
    with pytest.raises(BoundRunError, match="attempt_id"):
        resolve_attempt_paths(
            {"logical_run_id": "c6-v6-base-l1", "stage": "L1"}, "../escape"
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


@pytest.mark.parametrize('invalid', [True, -1, float('nan'), '2'])
def test_machine_payload_schema_rejects_invalid_nested_numbers(invalid):
    definition = {'exact_keys': ['items'], 'field_types': {'items': 'two child objects'},
                  'wire_schema': {'type': 'object', 'properties': {'items': {'type': 'array', 'minItems': 2, 'maxItems': 2, 'items': {'type': 'integer', 'minimum': 0}}},
                                  'required': ['items'], 'additionalProperties': False}}
    prereg = {'schema_catalog': {'schema_version': 2, 'definitions': {'root': definition}}}
    binding = {'canonical_payload_schema': {'name': 'root'}}
    validate_result_payload({'items': [0, 2]}, binding, prereg)
    with pytest.raises(BoundRunError):
        validate_result_payload({'items': [0, invalid]}, binding, prereg)
    with pytest.raises(BoundRunError):
        validate_result_payload({'items': [0]}, binding, prereg)


def test_machine_payload_schema_is_closed_and_unknown_rules_fail():
    definition = {'wire_schema': {'type': 'string', 'pattern': '^[a-f0-9]{4}$'}}
    prereg = {'schema_catalog': {'schema_version': 2, 'definitions': {'root': definition}}}
    binding = {'canonical_payload_schema': {'name': 'root'}}
    with pytest.raises(BoundRunError):
        validate_result_payload('INVALID', binding, prereg)
    definition['wire_schema']['uncheckedRule'] = True
    with pytest.raises(BoundRunError, match='unsupported'):
        validate_result_payload('abcd', binding, prereg)
    definition['wire_schema'] = {'anyOf': [{'type': 'string'}, {'type': 'integer', 'uncheckedRule': True}]}
    with pytest.raises(BoundRunError, match='unsupported'):
        validate_result_payload('abcd', binding, prereg)
    definition['wire_schema'] = {'anyOf': [{'type': 'string'}, {'type': 'integer'}]}
    validate_result_payload('abcd', binding, prereg)


def test_qualification_criteria_descriptor_is_validated_as_an_array() -> None:
    definitions = {
        "root": {
            "exact_keys": ["results"],
            "field_types": {"results": "per_residual array"},
        },
        "per_residual": {
            "exact_keys": ["criteria"],
            "field_types": {"criteria": "exact seven criterion_result objects"},
        },
        "criterion_result": {
            "exact_keys": ["passed"],
            "field_types": {"passed": "boolean"},
        },
    }
    validate_result_payload(
        {"results": [{"criteria": [{"passed": True}]}]},
        {"canonical_payload_schema": {"name": "root"}},
        {"schema_catalog": {"definitions": definitions}},
    )
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
        logical_run_id="c6-v6-base-l1",
        attempt_id="a0",
        fencing_token=TOKEN_1,
        resume_from="",
    )
    assert environment == {
        "PATH": "/bin",
        "LANG": "C.UTF-8",
        "C6_BOUND_CHECKPOINT_PATH": "artifacts/checkpoints/c6/run/checkpoint.json",
        "C6_BOUND_SIGNATURE": "s" * 64,
        "C6_BOUND_LOGICAL_RUN_ID": "c6-v6-base-l1",
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
            "--logical-run-id", "c6-v6-base-l1",
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


def test_d_authentication_binds_producer_bytes_digest_manifest_and_claims(tmp_path):
    from copy import deepcopy
    import hashlib
    from quantfusion.application import c6_bound_run as runner
    from quantfusion.application.c6_contract import canonical_json_bytes
    record = {'record_id': 'c6.base.l1', 'workflow_binding_id': 'c6.base.l1', 'candidate_id': 'C6-Base',
              'logical_run_id': 'base', 'stage': 'L1', 'source_revision': 'a' * 40, 'source_tree': 'b' * 40,
              'canonical_payload_schema': {'name': 'fixture'}}
    bindings = {'P': {'commit': 'c' * 40}, 'binding_records': [record]}
    rows = [{'predicate_id': 'gate', 'passed': True}]
    payload = {'evaluations': [{'variant_id': 'C6-Base', 'scenario_id': 'a', 'official_metrics': {'max_drawdown': -.1}}],
               'diagnostic_predicates': rows}
    raw = canonical_json_bytes(payload)
    digest = {'record_id': 'c6.base.l1', 'P': bindings['P'], 'R_revision': 'r' * 40,
              'source_revision': record['source_revision'], 'source_tree': record['source_tree'],
              'artifact_path': runner.resolve_attempt_paths(record, 'a0')['payload'].as_posix(),
              'artifact_byte_size': len(raw), 'artifact_full_byte_sha256': hashlib.sha256(raw).hexdigest(),
              'canonical_result_payload_sha256': canonical_payload_hash(payload), 'exit_code': 0}
    manifest = {'source_revision': record['source_revision']}
    manifest_bytes = canonical_json_bytes(manifest)
    claim = {key: digest[key] for key in ('record_id', 'artifact_path', 'artifact_byte_size', 'artifact_full_byte_sha256', 'canonical_result_payload_sha256')}
    claim.update(candidate_id='C6-Base', logical_run_id='base', attempt_id='a0', workflow_run_id='123',
                 manifest_full_byte_sha256=hashlib.sha256(manifest_bytes).hexdigest())
    selection = {'base_l1': claim, 'base_l1_predicates': rows, 'residual_ids': [], 's_qualification': None,
                 'base_plus_s_l1': None, 'base_plus_s_l1_predicates': None}
    prereg = {'schema_catalog': {'definitions': {'fixture': {'exact_keys': ['evaluations', 'diagnostic_predicates']}}}}
    class Store:
        def producer(self, identity, **kwargs):
            assert kwargs['expected_record'] == identity['binding_id'] == 'c6.base.l1'
            assert kwargs['expected_logical_run'] == 'base'
            assert kwargs['run_bindings_revision'] == 'r' * 40
            return runner.RemoteExport(123, manifest, manifest_bytes, {'payload.json': raw, 'digest.json': canonical_json_bytes(digest)})
    store = Store()
    runner.authenticate_selection_producers(selection, store, bindings, 'r' * 40, {}, prereg, ['a'], tmp_path)
    for field, value in [('artifact_byte_size', len(raw) + 1), ('manifest_full_byte_sha256', '0' * 64), ('artifact_path', 'wrong.json')]:
        altered = deepcopy(selection)
        altered['base_l1'][field] = value
        with pytest.raises(BoundRunError, match='sealed producer'):
            runner.authenticate_selection_producers(altered, store, bindings, 'r' * 40, {}, prereg, ['a'], tmp_path)
    digest['source_revision'] = '0' * 40
    with pytest.raises(BoundRunError, match='sealed producer'):
        runner.authenticate_selection_producers(selection, store, bindings, 'r' * 40, {}, prereg, ['a'], tmp_path)


def test_producer_logical_identity_is_rejected_before_network():
    from quantfusion.application import c6_bound_run as runner
    store = runner.GitHubActionsLeaseStore('owner/repo', 'synthetic-token')
    identity = {'artifact_full_byte_sha256': 'a' * 64, 'attempt_id': 'a0', 'binding_id': 'c6.base.l1',
                'logical_run_id': 'wrong', 'workflow_run_id': '123'}
    with pytest.raises(BoundRunError, match='logical identity'):
        store.producer(identity, expected_record='c6.base.l1', expected_logical_run='base',
                       workflow={}, run_bindings_revision='r' * 40, destination=Path('/nonexistent'))


def test_l4_gate_requires_complete_passing_l2_for_identical_d_and_c():
    from copy import deepcopy
    from quantfusion.application import c6_bound_run as runner
    from quantfusion.application.c6_contract import canonical_json_bytes
    d, c = {'commit': 'd' * 40}, {'commit': 'c' * 40}
    prereg = {'diagnostic_predicate_manifests': {'L2_APPLICABLE_DIAGNOSTIC_PREDICATES': [{'id': 'one'}, {'id': 'two'}]}}
    digest = {'D': d, 'C': c}
    rows = [{'predicate_id': 'one', 'passed': True}, {'predicate_id': 'two', 'passed': True}]
    def exported(changed_digest, changed_rows):
        return runner.RemoteExport(123, {}, b'{}', {'digest.json': canonical_json_bytes(changed_digest),
            'payload.json': canonical_json_bytes({'diagnostic_predicates': changed_rows})})
    runner.validate_l2_gate(exported(digest, rows), d, c, prereg)
    for mutation in ('missing', 'failed', 'truthy', 'D', 'C'):
        new_digest, new_rows = deepcopy(digest), deepcopy(rows)
        if mutation == 'missing':
            new_rows.pop()
        elif mutation in {'D', 'C'}:
            new_digest[mutation] = {'commit': '0' * 40}
        else:
            new_rows[0]['passed'] = 1 if mutation == 'truthy' else False
        with pytest.raises(BoundRunError, match='L4 requires'):
            runner.validate_l2_gate(exported(new_digest, new_rows), d, c, prereg)


def _factual_ledger():
    rows = []
    fills = []
    for ordinal, (date, side, shares, price) in enumerate((('2026-01-05', 'BUY', 200, 10.), ('2026-01-06', 'SELL', 100, 11.))):
        identity = dict(state_index=0, sleeve_name='fast', strategy_name='trend', symbol='A', side=side)
        rows.append(dict(order_ordinal=ordinal, execution_timestamp=date, requested_shares=shares,
                         authorized_shares=shares, filled_shares=shares, **identity))
        fills.append(dict(fill_ordinal=ordinal, order_ordinal=ordinal, timestamp=date, shares=shares,
                          price=price, notional=shares*price, fee=1., **identity))
    return dict(orders=rows, fills=fills, action_lifecycle=[],
                warm_boundary={'sleeve_cash':[{'cash':4000.}]},
                cash_series=[{'timestamp':'2026-01-05','state_index':0,'cash':1999.}, {'timestamp':'2026-01-06','state_index':0,'cash':3098.}],
                position_series=[dict(timestamp=date,state_index=0,strategy_name='trend',symbol='A',shares=shares,mark=mark,market_value=shares*mark)
                                 for date,shares,mark in [('2026-01-05',200,10.),('2026-01-06',100,11.)]],
                equity_series=[{'timestamp':date,'equity':value} for date,value in [('2026-01-05',3999.),('2026-01-06',4198.)]],
                drawdown_series=[{'timestamp':date,'equity':value,'running_peak':value,'drawdown':0.} for date,value in [('2026-01-05',3999.),('2026-01-06',4198.)]],
                official_metrics={'terminal_wealth':4198.,'total_return':4198./4000.-1.,'max_drawdown':0.,'total_trades':2,'sleeve_fill_count':2,'date_symbol_side_count':2,'reason_attribution':{'initial_entry':1,'strategy_exit':1}})


@pytest.mark.parametrize('mutation', ['none','fill_book','fill_shares','fill_notional','duplicate_fill','order_fill','position_shares','position_value','cash','equity','running_peak','drawdown','terminal','return','mdd'])
def test_factual_evidence_reconciles_primitive_ledgers(mutation):
    from quantfusion.application.c6_bound_run import validate_execution_facts
    payload = _factual_ledger()
    if mutation == 'none':
        validate_execution_facts(payload, complete_path=True)
        return
    targets = {'fill_book':('fills','state_index',1),'fill_shares':('fills','shares',300),
               'fill_notional':('fills','notional',2001.),'order_fill':('orders','filled_shares',100),
               'position_shares':('position_series','shares',100),'position_value':('position_series','market_value',2001.),
               'cash':('cash_series','cash',2000.),'equity':('equity_series','equity',4000.),
               'running_peak':('drawdown_series','running_peak',4000.),'drawdown':('drawdown_series','drawdown',-.01)}
    if mutation in targets:
        path,key,value=targets[mutation]
        payload[path][0][key]=value
    elif mutation == 'duplicate_fill':
        payload['fills'].append(dict(payload['fills'][0]))
    else:
        payload['official_metrics'][{'terminal':'terminal_wealth','return':'total_return','mdd':'max_drawdown'}[mutation]] += .1
    with pytest.raises(BoundRunError):
        validate_execution_facts(payload, complete_path=True)


def test_l2_predicate_receipts_are_recomputed_and_respect_frozen_numeric_tolerance():
    from copy import deepcopy
    from quantfusion.application.c6_predicates import _predicate_rows, validate_predicate_results
    prereg = strict_json_load('artifacts/diagnostics/c6-preregistration.json')
    rows = [dict(scenario_id=f'prefix-{i:02d}',scenario_type='prefix',total_return=0.,max_drawdown=-.1,seed=None) for i in range(1,18)]
    rows += [dict(rows[4],scenario_id='add-one-05-synthetic',scenario_type='add_one',base_size=5)]
    common = dict(total_return=0.,max_drawdown=-.1,sharpe=0.,calmar=0.,total_trades=0,sleeve_fill_count=0,
                  date_symbol_side_count=0,reason_attribution={},max_concurrent_symbols=0,terminal_risk_lock=False)
    rows += [dict(common,scenario_id=f'permutation-synthetic-{i}',scenario_type='permutation',seed=1,sharpe=i*5e-13) for i in range(2)]
    for row in rows:
        for key, value in common.items():
            row.setdefault(key, value)
        row['execution_receipts'] = {'orders': [], 'fills': [], 'exposure_series': []}
        row['diagnostic_telemetry'] = dict(planned_risk_sell_shares=0, retained_risk_sell_shares=0,
            suppressed_risk_sell_shares=0, filled_risk_sell_shares=0, first_executable_open=None,
            same_open_offset_shares=0, carried_conflict_count=0, cluster_substitution_count=0,
            cash_days=0, max_gross_ratio=0., max_cluster_weight=0., mdd_slack=.18-.1,
            near_18pct=False, terminal_lock_count=0)
    reference = {'results': deepcopy(rows)}
    ids = [row['scenario_id'] for row in rows]
    prereg['scenario_manifests']['L2_EXACT_SCENARIO_IDS']['ids'] = ids
    specs = prereg['diagnostic_predicate_manifests']['L2_APPLICABLE_DIAGNOSTIC_PREDICATES']
    predicates = _predicate_rows(specs,rows,reference,ids)
    assert predicates[-1]['passed'] is True
    payload = {'kind':'c6_l2','results':rows,'diagnostic_predicates':predicates}
    validate_predicate_results(payload,prereg,reference)
    for mutation in ('passed','detail_hash','metric','missing'):
        forged = deepcopy(payload)
        if mutation == 'passed':
            forged['diagnostic_predicates'][0]['passed'] = False
        elif mutation == 'detail_hash':
            forged['diagnostic_predicates'][0]['observed']['detail_sha256'] = '0'*64
        elif mutation == 'metric':
            forged['results'][0]['max_drawdown'] = -.25
        else:
            forged['diagnostic_predicates'].pop()
        with pytest.raises(ValueError):
            validate_predicate_results(forged,prereg,reference)
