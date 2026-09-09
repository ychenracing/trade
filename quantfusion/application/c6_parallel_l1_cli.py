"""Executable CLI for deterministic frozen C6 L1 parallel shards."""

from __future__ import annotations

import argparse
from pathlib import Path

from quantfusion.application.c6_contract import (
    canonical_payload_hash,
    load_preregistration,
    load_run_bindings,
    select_binding,
)
from quantfusion.application.c6_parallel_l1 import (
    core_l1_tasks,
    fresh_pool_map,
    partition_item_ids,
    shard_payload,
)
from quantfusion.io.c6_stream import write_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preregistration", required=True, type=Path)
    parser.add_argument("--bindings-file", required=True, type=Path)
    parser.add_argument("--binding-record-id", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--shard-index", required=True, type=int)
    parser.add_argument("--shard-count", required=True, type=int)
    parser.add_argument("--workers", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if len(args.source_revision) != 40 or any(
        c not in "0123456789abcdef" for c in args.source_revision
    ):
        raise ValueError("source_revision must be a lowercase Git SHA")
    prereg = load_preregistration(args.preregistration, repository=Path.cwd())
    bindings = load_run_bindings(args.bindings_file)
    binding = next(
        item
        for item in bindings["binding_records"]
        if item["record_id"] == args.binding_record_id
    )
    selected = select_binding(
        bindings, binding["workflow_binding_id"], candidate_id=binding["candidate_id"]
    )
    if (
        selected is not binding
        or binding["stage"] != "L1"
        or binding["source_revision"] != args.source_revision
    ):
        raise ValueError("parallel CLI identity does not select one exact L1 binding")
    ids, tasks = core_l1_tasks(prereg, binding)
    chunk_size = int(binding["runtime"]["checkpoint_every"])
    shard_ids = partition_item_ids(
        ids, args.shard_index, args.shard_count, chunk_size=chunk_size
    )
    by_id = dict(zip(ids, tasks, strict=True))
    shard_tasks = [by_id[item] for item in shard_ids]
    from quantfusion.application.c6_diagnostics import _l1_evaluate

    results = fresh_pool_map(
        _l1_evaluate,
        shard_tasks,
        workers=args.workers,
        chunk_size=chunk_size,
    )
    from quantfusion.application.c6_bound_run import validate_checkpoint_item

    records = []
    for item_id, result in zip(shard_ids, results, strict=True):
        item = {
            "item_id": item_id,
            "item_kind": "evaluation",
            "result_schema": "evaluation_record",
            "result_sha256": canonical_payload_hash(result),
            "result": result,
        }
        validate_checkpoint_item(item, prereg)
        records.append(item)
    write_json(
        args.output,
        shard_payload(
            source_revision=args.source_revision,
            record_id=args.binding_record_id,
            shard_index=args.shard_index,
            shard_count=args.shard_count,
            chunk_size=chunk_size,
            core_item_ids=ids,
            records=records,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
