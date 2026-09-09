from __future__ import annotations

from pathlib import Path

from quantfusion.application.c6_contract import canonical_payload_hash
from quantfusion.application.c6_parallel_l1 import merge_shard_payloads, shard_payload
from quantfusion.io.c6_stream import write_json


def _record(item_id: str, value: int) -> dict[str, object]:
    result = {"value": value}
    return {
        "item_id": item_id,
        "item_kind": "evaluation",
        "result_schema": "evaluation_record",
        "result_sha256": canonical_payload_hash(result),
        "result": result,
    }


def test_merge_consumes_each_shard_record_sequence_once(tmp_path: Path, monkeypatch) -> None:
    ids = [f"evaluation/item-{index:03d}" for index in range(8)]
    payload = shard_payload(
        source_revision="a" * 40,
        record_id="c6.base.l1",
        shard_index=0,
        shard_count=1,
        chunk_size=2,
        core_item_ids=ids,
        records=[_record(item, index) for index, item in enumerate(ids)],
    )
    path = tmp_path / "artifact-0" / "shard.json.gz"
    path.parent.mkdir(parents=True)
    write_json(path, payload)

    from quantfusion.application import c6_parallel_l1 as parallel
    from quantfusion.io.c6_stream import load_object as real_load_object

    iterations = {"count": 0}

    class CountingList(list):
        def __iter__(self):
            iterations["count"] += 1
            return super().__iter__()

    def counted_load(path: Path, **kwargs):
        loaded = real_load_object(path, **kwargs)
        loaded["records"] = CountingList(list(loaded["records"]))
        return loaded

    monkeypatch.setattr(parallel, "load_object", counted_load)
    results = merge_shard_payloads(
        tmp_path,
        expected_item_ids=ids,
        source_revision="a" * 40,
        record_id="c6.base.l1",
        shard_count=1,
        chunk_size=2,
    )

    assert results == [{"value": index} for index in range(len(ids))]
    assert iterations["count"] == 1
