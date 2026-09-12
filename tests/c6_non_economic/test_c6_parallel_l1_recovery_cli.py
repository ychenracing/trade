from __future__ import annotations

from pathlib import Path

from quantfusion.application.c6_diagnostics import build_parser


def test_parallel_recovery_identity_is_explicit_in_diagnostics_argv() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "--preregistration",
            "p.json",
            "--bindings-file",
            "r.json",
            "--binding-record-id",
            "c6.base.l1",
            "--source-revision",
            "b" * 40,
            "--parallel-evaluations",
            "shards",
            "--parallel-shard-count",
            "12",
            "--parallel-shard-source-revision",
            "a" * 40,
            "--parallel-validation-attestations-required",
            "--output",
            "out.json.gz",
        ]
    )
    assert args.preregistration == Path("p.json")
    assert args.source_revision == "b" * 40
    assert args.parallel_shard_source_revision == "a" * 40
    assert args.parallel_validation_attestations_required is True
