"""Small, lossless scenario metric tables; original ledgers remain external.

This review/export utility does not change runtime artifact loading. Repeated
scenario definitions come from the frozen plan; all measured columns are kept.
Run ``python -m scripts.release_tables DIRECTORY OUTPUT.json`` to reconstruct
and independently validate the accepted native artifact.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections import defaultdict
from pathlib import Path

from quantfusion.application import native_joint, stress_artifacts, stress_scenarios
from quantfusion.application.c6_contract import canonical_payload_hash


def _plan():
    return stress_scenarios._multi_seed_scenarios(
        random_samples=50,
        permutation_samples=50,
        seeds=stress_scenarios.DEFAULT_SEEDS,
    )


def _write(path, payload):
    raw = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode()
    if len(raw) >= 65536:
        raise ValueError("Review table exceeds the small-file boundary")
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def write_tables(payload, directory):
    """Export the full metrics by family/seed, without duplicated plan text."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    plan = _plan()
    results = payload["results"]
    completed = stress_artifacts._validated_checkpoint_results(payload, plan)
    if len(completed) != len(plan):
        raise ValueError("Incomplete scenario plan")
    if results != sorted(results, key=lambda row: row["scenario_id"]):
        raise ValueError("Unexpected result ordering")
    definitions = {row["scenario_id"]: row for row in plan}
    groups = defaultdict(list)
    for row in results:
        definition = definitions[row["scenario_id"]]
        metrics = {key: value for key, value in row.items() if key not in definition}
        for reason, count in metrics.pop("reason_attribution").items():
            metrics["reason_attribution." + reason] = count
        group = row["scenario_type"]
        if "seed" in row:
            group += "-" + str(row["seed"])
        groups[group].append({"scenario_id": row["scenario_id"], **metrics})
    files = {}
    for group, rows in groups.items():
        columns = ["scenario_id", *sorted(set(rows[0]) - {"scenario_id"})]
        if any(set(row) != set(columns) for row in rows):
            raise ValueError("Inconsistent metric columns")
        name = group + ".json"
        files[name] = _write(
            directory / name,
            {
                "columns": columns,
                "rows": [[row[key] for key in columns] for row in rows],
            },
        )
    _write(
        directory / "index.json",
        {
            "schema_version": 1,
            "tables": files,
            "payload_sha256": canonical_payload_hash(payload),
            "metadata": {
                key: value for key, value in payload.items() if key != "results"
            },
        },
    )


def read_tables(directory):
    """Verify bytes, exact plan and semantic payload identity before use."""
    directory = Path(directory)
    index = json.loads((directory / "index.json").read_text())
    if index["schema_version"] != 1:
        raise ValueError("Unsupported review table schema")
    plan = _plan()
    definitions = {row["scenario_id"]: row for row in plan}
    results = []
    for name, expected_hash in index["tables"].items():
        if Path(name).name != name or not name.endswith(".json"):
            raise ValueError("Invalid table path")
        raw = (directory / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_hash:
            raise ValueError("Review table hash changed")
        table = json.loads(raw)
        columns = table["columns"]
        if len(set(columns)) != len(columns):
            raise ValueError("Duplicate metric columns")
        for values in table["rows"]:
            if len(values) != len(columns):
                raise ValueError("Metric row width changed")
            metrics = dict(zip(columns, values))
            scenario_id = metrics.pop("scenario_id")
            if scenario_id not in definitions:
                raise ValueError("Unknown scenario")
            reasons = {
                key.split(".", 1)[1]: metrics.pop(key)
                for key in list(metrics)
                if key.startswith("reason_attribution.")
            }
            results.append(
                {**definitions[scenario_id], **metrics, "reason_attribution": reasons}
            )
    payload = {
        **index["metadata"],
        "results": sorted(results, key=lambda row: row["scenario_id"]),
    }
    if len(stress_artifacts._validated_checkpoint_results(payload, plan)) != len(plan):
        raise ValueError("Incomplete scenario plan")
    if canonical_payload_hash(payload) != index["payload_sha256"]:
        raise ValueError("Reconstructed payload hash changed")
    return payload


def export_native(directory, output):
    """Validate native acceptance before atomically replacing the export."""
    artifact = read_tables(directory)
    if (
        artifact.get("candidate_id") != native_joint.CANDIDATE_ID
        or "native_joint_acceptance" not in artifact
    ):
        raise ValueError("Export requires a native candidate and its receipt")
    output = Path(output)
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        candidate = Path(temporary) / "candidate.json"
        candidate.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n")
        if stress_artifacts._load_incumbent(candidate) != artifact:
            raise ValueError("Export did not validate as an accepted native artifact")
        candidate.replace(output)
    return artifact


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    artifact = export_native(args.directory, args.output)
    print(
        f"Verified {len(artifact['results'])} scenarios: "
        f"{canonical_payload_hash(artifact)}"
    )
