"""Semantically validate one frozen C6 L1 shard and attest its exact bytes."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from quantfusion.application.c6_contract import (
    load_preregistration,
    load_run_bindings,
    select_binding,
)
from quantfusion.application.c6_parallel_l1 import (
    attest_shard_validation,
    core_l1_tasks,
)
from quantfusion.io.c6_stream import write_json


def _git_sha(value: str, label: str) -> str:
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a lowercase 40-character Git SHA")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preregistration", required=True, type=Path)
    parser.add_argument("--bindings-file", required=True, type=Path)
    parser.add_argument("--binding-record-id", required=True)
    parser.add_argument("--validator-source-revision", required=True)
    parser.add_argument("--shard-source-revision", required=True)
    parser.add_argument("--shard-index", required=True, type=int)
    parser.add_argument("--shard-count", required=True, type=int)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    validator_source = _git_sha(args.validator_source_revision, "validator_source_revision")
    shard_source = _git_sha(args.shard_source_revision, "shard_source_revision")
    if args.shard_count < 1 or args.shard_index < 0 or args.shard_index >= args.shard_count:
        raise ValueError("invalid shard index/count")
    if args.output.exists():
        raise ValueError("validation output path already exists")

    preregistration_bytes = args.preregistration.read_bytes()
    preregistration_sha256 = hashlib.sha256(preregistration_bytes).hexdigest()
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
        or binding["source_revision"] != validator_source
    ):
        raise ValueError("validation CLI identity does not select one exact L1 binding")

    ids, _ = core_l1_tasks(prereg, binding)
    attestation = attest_shard_validation(
        args.input,
        expected_item_ids=ids,
        shard_source_revision=shard_source,
        validator_source_revision=validator_source,
        preregistration_sha256=preregistration_sha256,
        record_id=args.binding_record_id,
        shard_count=args.shard_count,
        chunk_size=int(binding["runtime"]["checkpoint_every"]),
        prereg=prereg,
    )
    if attestation["shard_index"] != args.shard_index:
        raise ValueError("validation CLI shard index does not match shard payload")
    write_json(args.output, attestation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
