#!/usr/bin/env python3
"""Validate and assemble the temporary exact-958 sharded recovery evidence."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import shutil
from typing import Any

from quantfusion.application import stress_artifacts, stress_scenarios
from quantfusion.config.paths import MARKET_DATA_DIR, REGIME_DATA_DIR

EXPECTED_FAMILIES = Counter(
    prefix=17,
    leave_one_out=17,
    add_one=24,
    random_subset=750,
    permutation=150,
)


def _full_plan() -> list[dict[str, Any]]:
    scenarios = stress_scenarios._multi_seed_scenarios(
        random_samples=stress_scenarios.DEFAULT_RANDOM_SAMPLES,
        permutation_samples=stress_scenarios.DEFAULT_PERMUTATION_SAMPLES,
        seeds=stress_scenarios.DEFAULT_SEEDS,
    )
    ids = [str(item["scenario_id"]) for item in scenarios]
    if len(scenarios) != 958 or len(set(ids)) != 958:
        raise RuntimeError(
            f"formal plan is not 958/958 unique: {len(scenarios)}/{len(set(ids))}"
        )
    families = Counter(str(item["scenario_type"]) for item in scenarios)
    if families != EXPECTED_FAMILIES:
        raise RuntimeError(f"unexpected family counts: {families!r}")
    return scenarios


def _selected_plan(
    full: list[dict[str, Any]], shard: int, shard_count: int
) -> list[dict[str, Any]]:
    selected, formal_complete = stress_scenarios.select_scenarios(
        full,
        scenario_id=None,
        scenario_type=None,
        shard_index=shard,
        shard_count=shard_count,
        scenario_ids=None,
    )
    if formal_complete or not selected:
        raise RuntimeError(f"shard {shard} unexpectedly formed a formal/empty plan")
    return selected


def _expected_selection(shard: int, shard_count: int) -> dict[str, Any]:
    return {
        "scenario_id": None,
        "scenario_ids_file": None,
        "scenario_ids": None,
        "scenario_type": None,
        "shard_index": shard,
        "shard_count": shard_count,
    }


def _provenance(
    scenarios: list[dict[str, Any]], source_sha: str
) -> dict[str, Any]:
    return stress_artifacts._build_provenance(
        scenarios,
        MARKET_DATA_DIR.resolve(),
        REGIME_DATA_DIR.resolve(),
        source_revision=source_sha,
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _validate_finite_metrics(item: dict[str, Any]) -> None:
    if item.get("deployment_policy") != "production_daily_replay":
        raise RuntimeError(
            f"non-production replay result: {item.get('scenario_id')!r}"
        )
    for key in ("total_return", "max_drawdown", "sharpe", "calmar"):
        try:
            value = float(item[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"missing/non-numeric {key} in {item.get('scenario_id')!r}"
            ) from exc
        if not math.isfinite(value):
            raise RuntimeError(
                f"non-finite {key} in {item.get('scenario_id')!r}: {value!r}"
            )


def _load_validated_shard(
    path: Path,
    *,
    shard: int,
    shard_count: int,
    source_sha: str,
    full: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"shard payload is not an object: {path}")
    expected_selection = _expected_selection(shard, shard_count)
    fixed_fields = {
        "artifact_status": "diagnostic",
        "formal_plan_complete": False,
        "canonical": False,
        "full_scenario_count": 958,
        "source_revision": source_sha,
        "selection": expected_selection,
    }
    for field, expected_value in fixed_fields.items():
        if payload.get(field) != expected_value:
            raise RuntimeError(
                f"shard {shard} field {field} changed: "
                f"{payload.get(field)!r} != {expected_value!r}"
            )

    selected = _selected_plan(full, shard, shard_count)
    expected_ids = [str(item["scenario_id"]) for item in selected]
    expected_provenance = _provenance(selected, source_sha)
    for field in stress_artifacts.PROVENANCE_FIELDS:
        if payload.get(field) != expected_provenance[field]:
            raise RuntimeError(f"shard {shard} provenance mismatch: {field}")

    results = payload.get("results")
    if not isinstance(results, list):
        raise RuntimeError(f"shard {shard} results are not a list")
    if len(results) != payload.get("scenario_count"):
        raise RuntimeError(f"shard {shard} result count mismatch")
    actual_ids = [str(item.get("scenario_id", "")) for item in results]
    if actual_ids != expected_ids:
        raise RuntimeError(f"shard {shard} exact membership/order changed")
    validated = stress_artifacts._validated_checkpoint_results(
        {"results": results}, selected
    )
    if set(validated) != set(expected_ids):
        raise RuntimeError(f"shard {shard} checkpoint validation lost results")
    for item in results:
        _validate_finite_metrics(item)
    return results


def preflight(source_sha: str, shard_count: int) -> None:
    full = _full_plan()
    selected = _selected_plan(full, 0, shard_count)
    full_provenance = _provenance(full, source_sha)
    empty = {
        "signature": full_provenance["run_signature"],
        "provenance": full_provenance,
        "completed": 0,
        "scenario_count": len(full),
        "results": [],
    }
    validated = stress_artifacts._validated_checkpoint(
        empty,
        full,
        signature=str(full_provenance["run_signature"]),
        provenance=full_provenance,
        diagnostic_selection=None,
    )
    if validated:
        raise RuntimeError("empty formal checkpoint did not validate as empty")
    shard_provenance = _provenance(selected, source_sha)
    if shard_provenance["scenario_count"] != len(selected):
        raise RuntimeError("shard provenance scenario count mismatch")


def validate_shard(
    path: Path, shard: int, shard_count: int, source_sha: str
) -> None:
    _load_validated_shard(
        path,
        shard=shard,
        shard_count=shard_count,
        source_sha=source_sha,
        full=_full_plan(),
    )


def aggregate(
    input_dir: Path, output: Path, shard_count: int, source_sha: str
) -> None:
    full = _full_plan()
    expected = {str(item["scenario_id"]): item for item in full}
    files = sorted(input_dir.glob("shard-*.json"))
    if len(files) != shard_count:
        raise RuntimeError(
            f"expected {shard_count} shard JSON files, found {len(files)}"
        )

    seen_shards: set[int] = set()
    combined: dict[str, dict[str, Any]] = {}
    for path in files:
        try:
            shard = int(path.stem.split("-")[-1])
        except ValueError as exc:
            raise RuntimeError(f"invalid shard filename: {path}") from exc
        if not 0 <= shard < shard_count or shard in seen_shards:
            raise RuntimeError(f"duplicate/out-of-range shard file: {path}")
        seen_shards.add(shard)
        results = _load_validated_shard(
            path,
            shard=shard,
            shard_count=shard_count,
            source_sha=source_sha,
            full=full,
        )
        for raw in results:
            scenario_id = str(raw["scenario_id"])
            if scenario_id not in expected or scenario_id in combined:
                raise RuntimeError(f"unknown/duplicate scenario: {scenario_id}")
            definition = expected[scenario_id]
            for key, value in definition.items():
                if raw.get(key) != _jsonable(value):
                    raise RuntimeError(
                        f"scenario definition changed: {scenario_id}/{key}"
                    )
            item = dict(raw)
            item.pop("diagnostic_telemetry", None)
            combined[scenario_id] = item

    if seen_shards != set(range(shard_count)):
        raise RuntimeError(f"incomplete shard set: {sorted(seen_shards)}")
    if set(combined) != set(expected):
        raise RuntimeError("combined result IDs do not equal the formal plan")
    results = [combined[scenario_id] for scenario_id in sorted(combined)]
    families = Counter(str(item["scenario_type"]) for item in results)
    if families != EXPECTED_FAMILIES:
        raise RuntimeError(f"combined family counts changed: {families!r}")

    provenance = _provenance(full, source_sha)
    checkpoint = {
        "signature": provenance["run_signature"],
        "provenance": provenance,
        "completed": 958,
        "scenario_count": 958,
        "results": results,
    }
    validated = stress_artifacts._validated_checkpoint(
        checkpoint,
        full,
        signature=str(provenance["run_signature"]),
        provenance=provenance,
        diagnostic_selection=None,
    )
    if len(validated) != 958 or set(validated) != set(expected):
        raise RuntimeError("reconstructed formal checkpoint is incomplete")
    stress_artifacts._atomic_json(output, checkpoint)


def validate_final(
    summary_path: Path, candidate_copy: Path, source_sha: str
) -> None:
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("source_revision") != source_sha:
        raise RuntimeError("summary source revision mismatch")
    if summary.get("scenario_count") != 958:
        raise RuntimeError("summary scenario count mismatch")
    if summary.get("unique_scenario_ids") != 958:
        raise RuntimeError("summary unique scenario count mismatch")
    exit_status = summary.get("formal_exit_status")
    if exit_status not in (0, 2):
        raise RuntimeError(f"unexpected formal exit status: {exit_status!r}")

    candidate_path = Path(str(summary["candidate_path"]))
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
    if digest != summary.get("candidate_sha256"):
        raise RuntimeError("candidate checksum mismatch")
    if candidate.get("source_revision") != source_sha:
        raise RuntimeError("candidate source revision mismatch")
    if candidate.get("scenario_count") != 958:
        raise RuntimeError("candidate scenario count mismatch")
    results = candidate.get("results")
    if not isinstance(results, list):
        raise RuntimeError("candidate results are not a list")
    ids = [str(item.get("scenario_id", "")) for item in results]
    if len(results) != 958 or len(set(ids)) != 958:
        raise RuntimeError("candidate is not 958/958 unique")
    if set(ids) != {str(item["scenario_id"]) for item in _full_plan()}:
        raise RuntimeError("candidate IDs do not equal the current formal plan")
    families = Counter(str(item["scenario_type"]) for item in results)
    if families != EXPECTED_FAMILIES:
        raise RuntimeError(f"candidate family counts changed: {families!r}")
    for item in results:
        if "diagnostic_telemetry" in item:
            raise RuntimeError(
                f"diagnostic telemetry leaked into {item.get('scenario_id')!r}"
            )
        _validate_finite_metrics(item)
    for name in (
        "absolute_hard_gates",
        "retained_robustness_hard_gates",
    ):
        gates = candidate.get(name)
        if not isinstance(gates, dict) or not isinstance(gates.get("passed"), bool):
            raise RuntimeError(f"missing/invalid gate family: {name}")
    if exit_status == 2:
        if summary.get("acceptance_status") != "rejected":
            raise RuntimeError("exit status 2 must retain rejected status")
        if summary.get("canonical") is not False:
            raise RuntimeError("rejected result must remain non-canonical")
    else:
        if summary.get("acceptance_status") != "accepted":
            raise RuntimeError("exit status 0 must retain accepted status")
        if summary.get("canonical") is not True:
            raise RuntimeError("accepted result must be canonical")
    candidate_copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(candidate_path, candidate_copy)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--source-sha", required=True)
    common.add_argument("--shard-count", type=int, default=12)

    subparsers.add_parser("preflight", parents=[common])

    shard_parser = subparsers.add_parser("validate-shard", parents=[common])
    shard_parser.add_argument("--path", type=Path, required=True)
    shard_parser.add_argument("--shard", type=int, required=True)

    aggregate_parser = subparsers.add_parser("aggregate", parents=[common])
    aggregate_parser.add_argument("--input-dir", type=Path, required=True)
    aggregate_parser.add_argument("--output", type=Path, required=True)

    final_parser = subparsers.add_parser("validate-final", parents=[common])
    final_parser.add_argument("--summary", type=Path, required=True)
    final_parser.add_argument("--candidate-copy", type=Path, required=True)

    args = parser.parse_args()
    if args.shard_count <= 0:
        raise SystemExit("--shard-count must be positive")
    if args.command == "preflight":
        preflight(args.source_sha, args.shard_count)
    elif args.command == "validate-shard":
        validate_shard(args.path, args.shard, args.shard_count, args.source_sha)
    elif args.command == "aggregate":
        aggregate(args.input_dir, args.output, args.shard_count, args.source_sha)
    elif args.command == "validate-final":
        validate_final(args.summary, args.candidate_copy, args.source_sha)
    else:  # pragma: no cover
        raise SystemExit(f"unsupported command: {args.command}")


if __name__ == "__main__":
    main()
