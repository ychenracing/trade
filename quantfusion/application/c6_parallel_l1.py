"""Deterministic parallel precomputation for frozen C6 L1 evaluations.

Shards only compute independent core evaluation records. The trusted
aggregate process revalidates every record and reconstructs the exact
frozen order before predicates, W0-W5, controls, or no-drift checks.
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from quantfusion.application.c6_contract import (
    canonical_payload_hash,
)
from quantfusion.io.c6_stream import FileArray, load_object

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


def _manifest_hash(ids: Sequence[str]) -> str:
    return hashlib.sha256("".join(f"{item}\n" for item in ids).encode()).hexdigest()


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


def merge_shard_payloads(
    root: Path,
    *,
    expected_item_ids: Sequence[str],
    source_revision: str,
    record_id: str,
    shard_count: int,
    chunk_size: int,
    prereg: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    from quantfusion.application.c6_bound_run import validate_checkpoint_item

    ids = list(expected_item_ids)
    expected_hash = _manifest_hash(ids)
    paths = sorted(root.rglob("shard.json.gz"))
    if len(paths) != shard_count:
        raise ValueError("parallel L1 shard set is incomplete")
    by_id: dict[str, dict[str, Any]] = {}
    seen_shards: set[int] = set()
    for path in paths:
        payload = load_object(path, array_fields=frozenset({"records"}))
        if not isinstance(payload, dict) or set(payload) != _SHARD_KEYS:
            raise ValueError("parallel L1 shard schema is invalid")
        index = payload["shard_index"]
        if (
            payload["schema_version"] != 1
            or payload["kind"] != "c6_l1_parallel_shard"
            or payload["source_revision"] != source_revision
            or payload["record_id"] != record_id
            or type(index) is not int
            or index in seen_shards
            or payload["shard_count"] != shard_count
            or payload["chunk_size"] != chunk_size
            or payload["core_item_count"] != len(ids)
            or payload["core_item_sha256"] != expected_hash
        ):
            raise ValueError("parallel L1 shard identity is invalid")
        expected_shard = partition_item_ids(
            ids, index, shard_count, chunk_size=chunk_size
        )
        records = payload["records"]
        if not isinstance(records, (list, FileArray)):
            raise ValueError("parallel L1 shard records are invalid")
        observed_shard: list[str] = []
        for item in records:
            if (
                not isinstance(item, dict)
                or set(item) != _RECORD_KEYS
                or item["item_kind"] != "evaluation"
                or item["result_schema"] != "evaluation_record"
                or not isinstance(item["result"], dict)
                or item["result_sha256"] != canonical_payload_hash(item["result"])
                or item["item_id"] in by_id
            ):
                raise ValueError("parallel L1 record hash/schema is invalid")
            observed_shard.append(item["item_id"])
            if prereg is not None:
                validate_checkpoint_item(item, prereg)
            by_id[item["item_id"]] = item["result"]
        if observed_shard != expected_shard:
            raise ValueError("parallel L1 shard does not contain its exact partition")
        seen_shards.add(index)
    if seen_shards != set(range(shard_count)) or set(by_id) != set(ids):
        raise ValueError("parallel L1 shard union is incomplete")
    return [by_id[item] for item in ids]


def load_parallel_evaluations(
    root: Path,
    *,
    prereg: Mapping[str, Any],
    binding: Mapping[str, Any],
    source_revision: str,
    shard_count: int,
) -> list[dict[str, Any]]:
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
    )