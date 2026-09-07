"""Deterministic parallel precomputation for frozen C6 L1 evaluations.

Shards compute independent core evaluation records. Semantic validation may be
performed in dedicated shard jobs and bound to the exact compressed shard bytes;
the aggregate process still validates every record hash/schema/partition and the
exact frozen union/order before predicates, controls, or no-drift checks.
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from quantfusion.application.c6_contract import canonical_payload_hash
from quantfusion.io.c6_stream import FileArray, MultiFileArray, load_object

_SHARD_KEYS = {
    "schema_version",
    "kind",
    "source_revision",
    "record_id",
    "shard_index",
    "shard_count",
    "chunk_size",
    "core_item_count",
    "core_item_sha256",
    "records",
}
_RECORD_KEYS = {"item_id", "item_kind", "result_schema", "result_sha256", "result"}
_ATTESTATION_KEYS = {
    "schema_version",
    "kind",
    "record_id",
    "shard_index",
    "shard_count",
    "chunk_size",
    "record_count",
    "record_ids_sha256",
    "shard_source_revision",
    "validator_source_revision",
    "preregistration_sha256",
    "shard_file_sha256",
}


def _manifest_hash(ids: Sequence[str]) -> str:
    return hashlib.sha256("".join(f"{item}\n" for item in ids).encode()).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def partition_item_ids(
    item_ids: Sequence[str], shard_index: int, shard_count: int, *, chunk_size: int
) -> list[str]:
    """Assign whole original process chunks to shards without splitting a chunk."""
    if (
        shard_count < 1
        or shard_index < 0
        or shard_index >= shard_count
        or chunk_size < 1
    ):
        raise ValueError("invalid L1 shard index/count/chunk")
    ids = list(item_ids)
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("parallel L1 manifest must be nonempty and unique")
    return [
        item
        for ordinal, item in enumerate(ids)
        if (ordinal // chunk_size) % shard_count == shard_index
    ]


def validate_manifest_identity(
    actual_ids: Sequence[str], frozen_manifest: Mapping[str, Any]
) -> list[str]:
    """Validate the frozen sorted manifest without depending on diagnostics."""
    ids = list(actual_ids)
    if any(
        not isinstance(item, str) or not item or "\n" in item or "\r" in item
        for item in ids
    ):
        raise ValueError("manifest IDs must be non-empty single-line strings")
    if len(set(ids)) != len(ids):
        raise ValueError("manifest contains duplicate IDs")
    if ids != sorted(ids):
        raise ValueError("manifest ID order is not lexicographic")
    count = frozen_manifest.get("count")
    unique_count = frozen_manifest.get("unique_count", count)
    if count != len(ids) or unique_count != len(ids):
        raise ValueError("manifest count does not match frozen identity")
    embedded = frozen_manifest.get("ids")
    if embedded is not None and embedded != ids:
        raise ValueError("manifest IDs do not match frozen order")
    digest = hashlib.sha256("".join(f"{item}\n" for item in ids).encode()).hexdigest()
    if frozen_manifest.get("sha256") != digest:
        raise ValueError("manifest SHA-256 does not match frozen identity")
    return ids


def fresh_pool_map(
    worker: Callable[[Any], Any], tasks: Sequence[Any], *, workers: int, chunk_size: int
) -> list[Any]:
    """Preserve the existing fresh-pool-per-chunk process lifetime."""
    if workers < 1 or chunk_size < 1:
        raise ValueError("workers and chunk_size must be positive")
    results: list[Any] = []
    for offset in range(0, len(tasks), chunk_size):
        batch = list(tasks[offset : offset + chunk_size])
        if workers == 1:
            results.extend(map(worker, batch))
        else:
            with ProcessPoolExecutor(max_workers=workers) as executor:
                results.extend(executor.map(worker, batch))
    return results


def core_l1_tasks(
    prereg: Mapping[str, Any], binding: Mapping[str, Any]
) -> tuple[list[str], list[tuple[str, Mapping[str, Any], str]]]:
    from quantfusion.application import stress_scenarios

    manifests = prereg["scenario_manifests"]
    scenario_ids = Path(
        manifests["L1_ECONOMIC_SCENARIO_IDS"]["path"]
    ).read_text().splitlines()
    validate_manifest_identity(
        scenario_ids, manifests["L1_ECONOMIC_SCENARIO_IDS"]
    )
    plan = stress_scenarios._multi_seed_scenarios(
        random_samples=50,
        permutation_samples=50,
        seeds=(20260807, 20260817, 20260827),
    )
    by_id: dict[str, Mapping[str, Any]] = {}
    for scenario in plan:
        if not isinstance(scenario, dict):
            raise ValueError("L1 scenario plan contains a non-object record")
        scenario_id = scenario.get("scenario_id")
        if type(scenario_id) is not str or scenario_id in by_id:
            raise ValueError("L1 scenario plan identity is invalid or duplicated")
        by_id[scenario_id] = scenario

    base = binding["candidate_id"] == "C6-Base"
    variant_values = (
        manifests["L1_BASE_EVALUATION_MANIFEST"]["core_variant_order"]
        if base
        else ["C6-Base+S"]
    )
    if not isinstance(variant_values, list):
        raise ValueError("L1 core variant order must be a list")
    variants: list[str] = []
    for variant in variant_values:
        if type(variant) is not str:
            raise ValueError("L1 core variant order contains a non-string identity")
        variants.append(variant)

    tasks: list[tuple[str, Mapping[str, Any], str]] = []
    ids: list[str] = []
    for variant in variants:
        for scenario_id in scenario_ids:
            scenario = by_id.get(scenario_id)
            if scenario is None:
                raise ValueError("L1 scenario manifest references an unknown scenario")
            tasks.append((variant, scenario, "DEFAULT"))
            ids.append(f"evaluation/{variant}::{scenario_id}")
    return ids, tasks


def shard_payload(
    *,
    source_revision: str,
    record_id: str,
    shard_index: int,
    shard_count: int,
    chunk_size: int,
    core_item_ids: Sequence[str],
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    ids = list(core_item_ids)
    return {
        "schema_version": 1,
        "kind": "c6_l1_parallel_shard",
        "source_revision": source_revision,
        "record_id": record_id,
        "shard_index": shard_index,
        "shard_count": shard_count,
        "chunk_size": chunk_size,
        "core_item_count": len(ids),
        "core_item_sha256": _manifest_hash(ids),
        "records": list(records),
    }


def _validate_shard_header(
    payload: Mapping[str, Any],
    *,
    expected_item_ids: Sequence[str],
    source_revision: str,
    record_id: str,
    shard_count: int,
    chunk_size: int,
) -> int:
    ids = list(expected_item_ids)
    index = payload.get("shard_index")
    if (
        set(payload) != _SHARD_KEYS
        or payload.get("schema_version") != 1
        or payload.get("kind") != "c6_l1_parallel_shard"
        or payload.get("source_revision") != source_revision
        or payload.get("record_id") != record_id
        or type(index) is not int
        or index < 0
        or index >= shard_count
        or payload.get("shard_count") != shard_count
        or payload.get("chunk_size") != chunk_size
        or payload.get("core_item_count") != len(ids)
        or payload.get("core_item_sha256") != _manifest_hash(ids)
    ):
        raise ValueError("parallel L1 shard identity is invalid")
    return index


def _validate_record(
    item: Any,
    *,
    seen_ids: set[str] | None = None,
    verify_result_hash: bool = True,
) -> tuple[str, dict[str, Any]]:
    if (
        not isinstance(item, dict)
        or set(item) != _RECORD_KEYS
        or item["item_kind"] != "evaluation"
        or item["result_schema"] != "evaluation_record"
        or not isinstance(item["item_id"], str)
        or not isinstance(item["result"], dict)
        or (seen_ids is not None and item["item_id"] in seen_ids)
    ):
        raise ValueError("parallel L1 record hash/schema is invalid")
    if verify_result_hash and item["result_sha256"] != canonical_payload_hash(
        item["result"]
    ):
        raise ValueError("parallel L1 record hash/schema is invalid")
    return item["item_id"], item["result"]


def attest_shard_validation(
    path: Path,
    *,
    expected_item_ids: Sequence[str],
    shard_source_revision: str,
    validator_source_revision: str,
    preregistration_sha256: str,
    record_id: str,
    shard_count: int,
    chunk_size: int,
    prereg: Mapping[str, Any],
) -> dict[str, Any]:
    """Semantically validate one exact shard and attest the compressed bytes."""
    from quantfusion.application.c6_bound_run import validate_checkpoint_item

    if (
        len(shard_source_revision) != 40
        or len(validator_source_revision) != 40
        or len(preregistration_sha256) != 64
    ):
        raise ValueError("parallel L1 validation identity is malformed")
    ids = list(expected_item_ids)
    payload = load_object(path, array_fields=frozenset({"records"}))
    if not isinstance(payload, dict):
        raise ValueError("parallel L1 shard schema is invalid")
    index = _validate_shard_header(
        payload,
        expected_item_ids=ids,
        source_revision=shard_source_revision,
        record_id=record_id,
        shard_count=shard_count,
        chunk_size=chunk_size,
    )
    expected_shard = partition_item_ids(
        ids, index, shard_count, chunk_size=chunk_size
    )
    records = payload["records"]
    if not isinstance(records, (list, FileArray)):
        raise ValueError("parallel L1 shard records are invalid")
    observed: list[str] = []
    for item in records:
        item_id, _ = _validate_record(item)
        observed.append(item_id)
        validate_checkpoint_item(item, prereg)
    if observed != expected_shard:
        raise ValueError("parallel L1 shard does not contain its exact partition")
    return {
        "schema_version": 1,
        "kind": "c6_l1_semantic_validation_attestation",
        "record_id": record_id,
        "shard_index": index,
        "shard_count": shard_count,
        "chunk_size": chunk_size,
        "record_count": len(observed),
        "record_ids_sha256": _manifest_hash(observed),
        "shard_source_revision": shard_source_revision,
        "validator_source_revision": validator_source_revision,
        "preregistration_sha256": preregistration_sha256,
        "shard_file_sha256": _file_sha256(path),
    }


def _verify_attestation(
    path: Path,
    attestation: Any,
    *,
    expected_shard: Sequence[str],
    shard_source_revision: str,
    validator_source_revision: str,
    preregistration_sha256: str,
    record_id: str,
    shard_index: int,
    shard_count: int,
    chunk_size: int,
) -> None:
    if not isinstance(attestation, dict) or set(attestation) != _ATTESTATION_KEYS:
        raise ValueError("parallel L1 validation attestation is invalid")
    expected = {
        "schema_version": 1,
        "kind": "c6_l1_semantic_validation_attestation",
        "record_id": record_id,
        "shard_index": shard_index,
        "shard_count": shard_count,
        "chunk_size": chunk_size,
        "record_count": len(expected_shard),
        "record_ids_sha256": _manifest_hash(expected_shard),
        "shard_source_revision": shard_source_revision,
        "validator_source_revision": validator_source_revision,
        "preregistration_sha256": preregistration_sha256,
        "shard_file_sha256": _file_sha256(path),
    }
    if attestation != expected:
        raise ValueError("parallel L1 validation attestation does not bind exact shard bytes")


def merge_shard_payloads(
    root: Path,
    *,
    expected_item_ids: Sequence[str],
    source_revision: str,
    record_id: str,
    shard_count: int,
    chunk_size: int,
    prereg: Mapping[str, Any] | None = None,
    attestations_required: bool = False,
    validator_source_revision: str | None = None,
    preregistration_sha256: str | None = None,
) -> Sequence[dict[str, Any]]:
    from quantfusion.application.c6_bound_run import validate_checkpoint_item

    if attestations_required and (
        validator_source_revision is None or preregistration_sha256 is None
    ):
        raise ValueError("parallel L1 attestation identities are required")
    ids = list(expected_item_ids)
    paths = sorted(root.rglob("shard.json.gz"))
    if len(paths) != shard_count:
        raise ValueError("parallel L1 shard set is incomplete")
    by_id: dict[str, dict[str, Any]] = {}
    by_ref: dict[str, tuple[Path, tuple[int, int]]] = {}
    seen_ids: set[str] = set()
    seen_shards: set[int] = set()
    for path in paths:
        payload = load_object(path, array_fields=frozenset({"records"}))
        if not isinstance(payload, dict):
            raise ValueError("parallel L1 shard schema is invalid")
        index = _validate_shard_header(
            payload,
            expected_item_ids=ids,
            source_revision=source_revision,
            record_id=record_id,
            shard_count=shard_count,
            chunk_size=chunk_size,
        )
        if index in seen_shards:
            raise ValueError("parallel L1 shard identity is invalid")
        expected_shard = partition_item_ids(
            ids, index, shard_count, chunk_size=chunk_size
        )
        if attestations_required:
            attestation = load_object(path.with_name("validation.json"))
            _verify_attestation(
                path,
                attestation,
                expected_shard=expected_shard,
                shard_source_revision=source_revision,
                validator_source_revision=str(validator_source_revision),
                preregistration_sha256=str(preregistration_sha256),
                record_id=record_id,
                shard_index=index,
                shard_count=shard_count,
                chunk_size=chunk_size,
            )
        records = payload["records"]
        if not isinstance(records, (list, FileArray)):
            raise ValueError("parallel L1 shard records are invalid")
        observed_shard: list[str] = []
        if attestations_required and not isinstance(records, FileArray):
            raise ValueError("attested parallel L1 requires indexed shard records")
        spans = records.spans if isinstance(records, FileArray) else [None] * len(records)
        for span, item in zip(spans, records):
            item_id, result = _validate_record(
                item,
                seen_ids=seen_ids,
                verify_result_hash=not attestations_required,
            )
            observed_shard.append(item_id)
            seen_ids.add(item_id)
            if prereg is not None and not attestations_required:
                validate_checkpoint_item(item, prereg)
            if attestations_required:
                assert isinstance(span, tuple)
                by_ref[item_id] = (path, span)
            else:
                by_id[item_id] = result
        if observed_shard != expected_shard:
            raise ValueError("parallel L1 shard does not contain its exact partition")
        seen_shards.add(index)
    if seen_shards != set(range(shard_count)) or seen_ids != set(ids):
        raise ValueError("parallel L1 shard union is incomplete")
    if attestations_required:
        return MultiFileArray([by_ref[item] for item in ids]).project("result")
    return [by_id[item] for item in ids]


def load_parallel_evaluations(
    root: Path,
    *,
    prereg: Mapping[str, Any],
    binding: Mapping[str, Any],
    source_revision: str,
    shard_count: int,
    attestations_required: bool = False,
    validator_source_revision: str | None = None,
    preregistration_sha256: str | None = None,
) -> Sequence[dict[str, Any]]:
    ids, _ = core_l1_tasks(prereg, binding)
    chunk_size = int(binding["runtime"]["checkpoint_every"])
    return merge_shard_payloads(
        root,
        expected_item_ids=ids,
        source_revision=source_revision,
        record_id=str(binding["record_id"]),
        shard_count=shard_count,
        chunk_size=chunk_size,
        prereg=prereg,
        attestations_required=attestations_required,
        validator_source_revision=validator_source_revision,
        preregistration_sha256=preregistration_sha256,
    )
