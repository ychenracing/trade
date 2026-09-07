from __future__ import annotations

from pathlib import Path
import pytest

from quantfusion.application.c6_contract import canonical_payload_hash
from quantfusion.application.c6_parallel_l1 import (
    fresh_pool_map,
    merge_shard_payloads,
    partition_item_ids,
    shard_payload,
)
from quantfusion.io.c6_stream import write_json


def _square(value: int) -> dict[str, int]:
    return {"square": value * value}


def _record(item_id: str, value: int) -> dict[str, object]:
    result = {"value": value}
    return {
        "item_id": item_id,
        "item_kind": "evaluation",
        "result_schema": "evaluation_record",
        "result_sha256": canonical_payload_hash(result),
        "result": result,
    }


def test_partition_is_exact_balanced_and_order_preserving():
    ids = [f"evaluation/item-{index:03d}" for index in range(37)]
    shards = [partition_item_ids(ids, index, 12) for index in range(12)]
    assert sorted(item for shard in shards for item in shard) == sorted(ids)
    assert len({item for shard in shards for item in shard}) == len(ids)
    assert max(map(len, shards)) - min(map(len, shards)) <= 1
    for index, shard in enumerate(shards):
        assert shard == [item for ordinal, item in enumerate(ids) if ordinal % 12 == index]


@pytest.mark.parametrize("workers", [1, 2])
def test_fresh_pool_map_preserves_order_across_chunks(workers):
    tasks = list(range(13))
    assert fresh_pool_map(_square, tasks, workers=workers, chunk_size=4) == [
        _square(item) for item in tasks
    ]


def _write_shards(root: Path, ids: list[str], *, shard_count: int = 3) -> None:
    for index in range(shard_count):
        selected = partition_item_ids(ids, index, shard_count)
        payload = shard_payload(
            source_revision="a" * 40,
            record_id="c6.base.l1",
            shard_index=index,
            shard_count=shard_count,
            core_item_ids=ids,
            records=[_record(item, ids.index(item)) for item in selected],
        )
        path = root / f"artifact-{index}" / "shard.json.gz"
        path.parent.mkdir(parents=True)
        write_json(path, payload)


def test_merge_reassembles_exact_manifest_order(tmp_path):
    ids = [f"evaluation/item-{index:03d}" for index in range(17)]
    _write_shards(tmp_path, ids)
    results = merge_shard_payloads(
        tmp_path,
        expected_item_ids=ids,
        source_revision="a" * 40,
        record_id="c6.base.l1",
        shard_count=3,
    )
    assert results == [{"value": index} for index in range(len(ids))]


@pytest.mark.parametrize("mutation", ["missing", "wrong-shard", "bad-hash"])
def test_merge_fails_closed_on_incomplete_or_corrupt_shards(tmp_path, mutation):
    ids = [f"evaluation/item-{index:03d}" for index in range(9)]
    _write_shards(tmp_path, ids)
    if mutation == "missing":
        (tmp_path / "artifact-2" / "shard.json.gz").unlink()
    else:
        from quantfusion.io.c6_stream import load_object
        path = tmp_path / "artifact-1" / "shard.json.gz"
        payload = load_object(path)
        if mutation == "wrong-shard":
            payload["records"][0]["item_id"] = ids[0]
        else:
            payload["records"][0]["result"]["value"] = 999
        write_json(path, payload, replace=True)
    with pytest.raises(ValueError):
        merge_shard_payloads(
            tmp_path,
            expected_item_ids=ids,
            source_revision="a" * 40,
            record_id="c6.base.l1",
            shard_count=3,
        )
