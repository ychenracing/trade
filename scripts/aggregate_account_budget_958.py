"""Assemble independently replayed stress shards into one validated formal checkpoint."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from quantfusion.application import stress_artifacts, stress_scenarios
from quantfusion.application.native_joint import CANDIDATE_ID
from quantfusion.config.paths import MARKET_DATA_DIR, REGIME_DATA_DIR


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_fields(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: payload[key]
        for key in (
            "stress_contract_version",
            "source_revision",
            "candidate_id",
            "source_fingerprint",
            "data_fingerprint",
            "start_date",
            "end_date",
            "initial_capital",
            "engine",
            "deployment_policy",
        )
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    args = parser.parse_args()

    shard_paths = sorted(args.input.glob("result-*.json"))
    if not shard_paths:
        raise ValueError("no shard result files found")

    full_scenarios = stress_scenarios._multi_seed_scenarios(
        random_samples=stress_scenarios.DEFAULT_RANDOM_SAMPLES,
        permutation_samples=stress_scenarios.DEFAULT_PERMUTATION_SAMPLES,
        seeds=stress_scenarios.DEFAULT_SEEDS,
    )
    full_provenance = stress_artifacts._build_provenance(
        full_scenarios,
        MARKET_DATA_DIR,
        REGIME_DATA_DIR,
        source_revision=args.source_revision,
        candidate_id=CANDIDATE_ID,
    )
    expected_ids = {str(row["scenario_id"]) for row in full_scenarios}
    stable = _stable_fields(full_provenance)

    rows: dict[str, dict[str, Any]] = {}
    shard_manifest: list[dict[str, Any]] = []
    for path in shard_paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("artifact_status") != "diagnostic":
            raise ValueError(f"{path} is not a diagnostic shard")
        if payload.get("formal_plan_complete") is not False:
            raise ValueError(f"{path} incorrectly claims a complete plan")
        if _stable_fields(payload) != stable:
            raise ValueError(f"{path} source/data/runtime identity mismatch")
        shard_rows = payload.get("results")
        if not isinstance(shard_rows, list) or not shard_rows:
            raise ValueError(f"{path} has no results")
        ids: list[str] = []
        for row in shard_rows:
            scenario_id = str(row.get("scenario_id"))
            if scenario_id not in expected_ids:
                raise ValueError(f"unexpected scenario {scenario_id} in {path}")
            if scenario_id in rows:
                raise ValueError(f"duplicate scenario {scenario_id}")
            rows[scenario_id] = row
            ids.append(scenario_id)
        shard_manifest.append(
            {
                "path": path.name,
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
                "scenario_count": len(ids),
                "scenario_ids": sorted(ids),
            }
        )

    if set(rows) != expected_ids:
        missing = sorted(expected_ids - set(rows))
        extra = sorted(set(rows) - expected_ids)
        raise ValueError(f"incomplete formal plan missing={missing} extra={extra}")

    checkpoint = {
        "signature": full_provenance["run_signature"],
        "provenance": full_provenance,
        "completed": len(rows),
        "scenario_count": len(full_scenarios),
        "results": sorted(rows.values(), key=lambda row: str(row["scenario_id"])),
    }
    args.checkpoint.parent.mkdir(parents=True, exist_ok=True)
    args.checkpoint.write_text(
        json.dumps(checkpoint, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "source_revision": args.source_revision,
        "source_fingerprint": full_provenance["source_fingerprint"],
        "data_fingerprint": full_provenance["data_fingerprint"],
        "scenario_signature": full_provenance["scenario_signature"],
        "run_signature": full_provenance["run_signature"],
        "scenario_count": len(rows),
        "shards": shard_manifest,
        "checkpoint": {
            "path": args.checkpoint.name,
            "bytes": args.checkpoint.stat().st_size,
            "sha256": _sha256(args.checkpoint),
        },
    }
    args.manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    summary_keys = (
        "source_revision",
        "source_fingerprint",
        "data_fingerprint",
        "scenario_signature",
        "run_signature",
        "scenario_count",
    )
    print(json.dumps({key: manifest[key] for key in summary_keys}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
