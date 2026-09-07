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


def test_partition_is_exact_balanced_and_preserves_original_worker_chunks():
    ids = [f"evaluation/item-{index:03d}" for index in range(97)]
    chunk_size = 10
    shards = [
        partition_item_ids(ids, index, 4, chunk_size=chunk_size)
        for index in range(4)
    ]
    assert sorted(item for shard in shards for item in shard) == sorted(ids)
    assert len({item for shard in shards for item in shard}) == len(ids)
    assert max(map(len, shards)) - min(map(len, shards)) <= chunk_size
    owner = {item: shard for shard, items in enumerate(shards) for item in items}
    for start in range(0, len(ids), chunk_size):
        original_chunk = ids[start : start + chunk_size]
        assert len({owner[item] for item in original_chunk}) == 1
        shard = owner[original_chunk[0]]
        selected = [item for item in shards[shard] if item in set(original_chunk)]
        assert selected == original_chunk


@pytest.mark.parametrize("workers", [1, 2])
def test_fresh_pool_map_preserves_order_across_chunks(workers):
    tasks = list(range(13))
    assert fresh_pool_map(_square, tasks, workers=workers, chunk_size=4) == [
        _square(item) for item in tasks
    ]


def _write_shards(
    root: Path, ids: list[str], *, shard_count: int = 3, chunk_size: int = 2
) -> None:
    for index in range(shard_count):
        selected = partition_item_ids(
            ids, index, shard_count, chunk_size=chunk_size
        )
        payload = shard_payload(
            source_revision="a" * 40,
            record_id="c6.base.l1",
            shard_index=index,
            shard_count=shard_count,
            chunk_size=chunk_size,
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
        chunk_size=2,
    )
    assert results == [{"value": index} for index in range(len(ids))]


def test_merge_parallel_validation_reassembles_exact_manifest_order(tmp_path):
    ids = [f"evaluation/item-{index:03d}" for index in range(17)]
    _write_shards(tmp_path, ids)
    results = merge_shard_payloads(
        tmp_path,
        expected_item_ids=ids,
        source_revision="a" * 40,
        record_id="c6.base.l1",
        shard_count=3,
        chunk_size=2,
        validation_workers=2,
    )
    assert results == [{"value": index} for index in range(len(ids))]


def test_merge_streams_records_array_instead_of_decoding_whole_shard(
    tmp_path, monkeypatch
):
    ids = [f"evaluation/item-{index:03d}" for index in range(8)]
    _write_shards(tmp_path, ids, shard_count=1, chunk_size=2)

    from quantfusion.application import c6_parallel_l1 as parallel
    from quantfusion.io.c6_stream import load_object as real_load_object

    def bounded_load(path, **kwargs):
        return real_load_object(path, record_limit=512, **kwargs)

    monkeypatch.setattr(parallel, "load_object", bounded_load)
    results = parallel.merge_shard_payloads(
        tmp_path,
        expected_item_ids=ids,
        source_revision="a" * 40,
        record_id="c6.base.l1",
        shard_count=1,
        chunk_size=2,
    )
    assert results == [{"value": index} for index in range(len(ids))]


@pytest.mark.parametrize(
    "mutation", ["missing", "wrong-shard", "bad-hash", "wrong-chunk"]
)
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
        elif mutation == "bad-hash":
            payload["records"][0]["result"]["value"] = 999
        else:
            payload["chunk_size"] = 3
        write_json(path, payload, replace=True)
    with pytest.raises(ValueError):
        merge_shard_payloads(
            tmp_path,
            expected_item_ids=ids,
            source_revision="a" * 40,
            record_id="c6.base.l1",
            shard_count=3,
            chunk_size=2,
        )
