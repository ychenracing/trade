from __future__ import annotations

from pathlib import Path

import pytest

from quantfusion.application.c6_contract import canonical_payload_hash
from quantfusion.application.c6_parallel_l1 import (
    attest_shard_validation,
    merge_shard_payloads,
    shard_payload,
)
from quantfusion.io.c6_stream import load_object, write_json


def _record(item_id: str, value: int) -> dict[str, object]:
    result = {"value": value}
    return {
        "item_id": item_id,
        "item_kind": "evaluation",
        "result_schema": "evaluation_record",
        "result_sha256": canonical_payload_hash(result),
        "result": result,
    }


def _write_one(root: Path, ids: list[str]) -> Path:
    payload = shard_payload(
        source_revision="a" * 40,
        record_id="c6.base.l1",
        shard_index=0,
        shard_count=1,
        chunk_size=2,
        core_item_ids=ids,
        records=[_record(item, index) for index, item in enumerate(ids)],
    )
    path = root / "artifact-0" / "shard.json.gz"
    path.parent.mkdir(parents=True)
    write_json(path, payload)
    return path


def test_attestation_runs_semantic_validation_once_and_binds_exact_bytes(
    tmp_path: Path, monkeypatch
) -> None:
    ids = [f"evaluation/item-{index:03d}" for index in range(8)]
    path = _write_one(tmp_path, ids)
    calls: list[str] = []

    from quantfusion.application import c6_bound_run

    def semantic(item, prereg):
        assert prereg == {"contract": "current"}
        calls.append(item["item_id"])

    monkeypatch.setattr(c6_bound_run, "validate_checkpoint_item", semantic)
    attestation = attest_shard_validation(
        path,
        expected_item_ids=ids,
        shard_source_revision="a" * 40,
        validator_source_revision="b" * 40,
        preregistration_sha256="c" * 64,
        record_id="c6.base.l1",
        shard_count=1,
        chunk_size=2,
        prereg={"contract": "current"},
    )
    assert calls == ids
    assert attestation["record_count"] == len(ids)
    assert attestation["shard_index"] == 0
    assert attestation["shard_source_revision"] == "a" * 40
    assert attestation["validator_source_revision"] == "b" * 40
    assert attestation["preregistration_sha256"] == "c" * 64
    assert len(attestation["shard_file_sha256"]) == 64


def test_attested_merge_does_not_repeat_semantics_and_rejects_byte_drift(
    tmp_path: Path, monkeypatch
) -> None:
    ids = [f"evaluation/item-{index:03d}" for index in range(8)]
    path = _write_one(tmp_path, ids)
    from quantfusion.application import c6_bound_run

    monkeypatch.setattr(c6_bound_run, "validate_checkpoint_item", lambda item, prereg: None)
    attestation = attest_shard_validation(
        path,
        expected_item_ids=ids,
        shard_source_revision="a" * 40,
        validator_source_revision="b" * 40,
        preregistration_sha256="c" * 64,
        record_id="c6.base.l1",
        shard_count=1,
        chunk_size=2,
        prereg={"contract": "current"},
    )
    write_json(path.with_name("validation.json"), attestation)

    def duplicate_semantics_forbidden(item, prereg):
        raise AssertionError("central merge repeated semantic validation")

    monkeypatch.setattr(c6_bound_run, "validate_checkpoint_item", duplicate_semantics_forbidden)
    results = merge_shard_payloads(
        tmp_path,
        expected_item_ids=ids,
        source_revision="a" * 40,
        record_id="c6.base.l1",
        shard_count=1,
        chunk_size=2,
        prereg={"contract": "current"},
        attestations_required=True,
        validator_source_revision="b" * 40,
        preregistration_sha256="c" * 64,
    )
    assert results == [{"value": index} for index in range(len(ids))]

    payload = load_object(path)
    payload["records"][0]["result"] = {"value": 999}
    payload["records"][0]["result_sha256"] = canonical_payload_hash(
        payload["records"][0]["result"]
    )
    write_json(path, payload, replace=True)
    with pytest.raises(ValueError, match="attestation"):
        merge_shard_payloads(
            tmp_path,
            expected_item_ids=ids,
            source_revision="a" * 40,
            record_id="c6.base.l1",
            shard_count=1,
            chunk_size=2,
            prereg={"contract": "current"},
            attestations_required=True,
            validator_source_revision="b" * 40,
            preregistration_sha256="c" * 64,
        )
