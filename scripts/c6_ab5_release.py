"""One source-bound AB5 release: authenticated existing Base -> native fixed L2.

This narrow owner-authorized revision does not impersonate old R/D, replay Base,
search parameters, or publish a diagnostic subset as formal acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import zipfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from quantfusion.application import c6_diagnostics, c6_predicates, c6_release_acceptance as release
from quantfusion.application import stress_artifacts, stress_metrics, stress_scenarios
from quantfusion.application.c6_bound_run import validate_result_payload, validate_wire_value
from quantfusion.application.c6_contract import canonical_payload_hash
from quantfusion.config.paths import MARKET_DATA_DIR, PROJECT_ROOT, REGIME_DATA_DIR

_D_ZIP_SHA256 = "786e1d9c3926bbb0ebe69d12cd6f05412332e94bd7aba08c9b8983161de670b4"
_REFERENCE = PROJECT_ROOT / "artifacts/validation/candidates/stress-86fd22448b9aad9d5e6194c0c065c40d56d7bddd-rejected.json"


def read_base_receipt(path: Path) -> dict:
    if hashlib.sha256(path.read_bytes()).hexdigest() != _D_ZIP_SHA256:
        raise ValueError("AB5 selection receipt archive is not the authenticated native artifact")
    with zipfile.ZipFile(path) as archive:
        raw = archive.read("c6-selection.json")
    if hashlib.sha256(raw).hexdigest() != release.AB5_ORIGINAL_D_SHA256:
        raise ValueError("AB5 selection receipt bytes differ")
    return release.derive_base_release_selection(json.loads(raw))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection-receipt", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    if args.workers < 1 or args.workers > 12:
        raise ValueError("workers must be in 1..12")
    selection = read_base_receipt(args.selection_receipt)
    if selection["selected_candidate"] != release.AB5_CANDIDATE_ID:
        raise ValueError("AB5 Base retains an unwaived release failure")
    full_plan = stress_scenarios._multi_seed_scenarios(
        random_samples=50, permutation_samples=50, seeds=(20260807, 20260817, 20260827))
    provenance = stress_artifacts._build_provenance(
        full_plan, MARKET_DATA_DIR, REGIME_DATA_DIR,
        source_revision=args.source_revision, candidate_id=release.AB5_CANDIDATE_ID)
    reference = stress_artifacts._load_initial_baseline_reference(_REFERENCE)
    proof = stress_artifacts.validate_ab5_release_request(provenance, reference)
    prereg = release.ab5_preregistration()
    manifest = prereg["scenario_manifests"]["L2_EXACT_SCENARIO_IDS"]
    ids = c6_diagnostics.validate_manifest_identity(manifest["ids"], manifest)
    by_id = {row["scenario_id"]: row for row in full_plan}
    if len(ids) != 77 or len(by_id) != 958:
        raise ValueError("AB5 exact L2/formal population differs")
    out = args.output_dir.resolve()
    if out == PROJECT_ROOT or out.is_relative_to(PROJECT_ROOT / "artifacts/validation"):
        raise ValueError("L2 output must be outside the formal publication namespace")
    out.mkdir(parents=True, exist_ok=True)
    signature = canonical_payload_hash({"source": proof, "selection_id": selection["selection_id"], "manifest": manifest})
    checkpoint = out / "l2-checkpoint.json"
    stress_artifacts._atomic_json(out / "release-preflight.json", {
        "source_binding": proof, "selection": selection, "provenance": provenance,
        "L2_manifest": manifest, "formal_scenarios": len(full_plan), "economic_dispatches": 0})
    print(f"AB5 release preflight passed; source={args.source_revision}; selection={selection['selection_id']}", flush=True)
    if args.preflight_only:
        return 0
    completed = []
    if checkpoint.exists():
        saved = json.loads(checkpoint.read_text())
        if saved.get("signature") != signature:
            raise ValueError("AB5 L2 checkpoint source/selection/manifest changed")
        completed = saved["results"]
        if [row["scenario_id"] for row in completed] != ids[:len(completed)]:
            raise ValueError("AB5 L2 checkpoint coverage is not an exact prefix")
    definitions = prereg["schema_catalog"]["definitions"]
    for row in completed:
        validate_wire_value(row, {"$ref": "#/$defs/L2_result"}, definitions)
        c6_predicates.validate_l2_telemetry(row)
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        for start in range(len(completed), len(ids), 10):
            tasks = [(release.AB5_CANDIDATE_ID, by_id[item]) for item in ids[start:start+10]]
            for row in executor.map(c6_diagnostics._l2_evaluate, tasks):
                validate_wire_value(row, {"$ref": "#/$defs/L2_result"}, definitions)
                c6_predicates.validate_l2_telemetry(row)
                completed.append(row)
            stress_artifacts._atomic_json(checkpoint, {"signature": signature, "results": completed})
            print(f"L2 checkpoint {len(completed)}/{len(ids)}", flush=True)
    summary = stress_metrics._summary(completed)
    for key in ("trades_worst", "date_symbol_side_buckets_worst", "sleeve_fills_worst"):
        summary[key] = int(summary[key])
    raw = {"schema_version": 2, "kind": "c6_l2", "diagnostic_noncanonical": True,
           "scenario_manifest": {"name": "L2_EXACT_SCENARIO_IDS", "count": 77, "unique_count": 77, "sha256": manifest["sha256"]},
           "summary": summary, "results": completed,
           "diagnostic_predicates": c6_predicates._predicate_rows(
               prereg["diagnostic_predicate_manifests"]["L2_APPLICABLE_DIAGNOSTIC_PREDICATES"], completed, reference, ids)}
    validate_result_payload(raw, {"canonical_payload_schema": {"name": "L2_payload"}}, prereg)
    assessment = release.release_predicate_assessment(
        raw, candidate_id=release.AB5_CANDIDATE_ID, source_revision=release.AB5_BASE_SOURCE_REVISION,
        producer_run_id=release.AB5_BASE_PRODUCER_RUN_ID, reference_sha256=release.AB5_REFERENCE_SHA256)
    evidence = {"kind": "c6_ab5_release_l2_evidence", "schema_version": 1,
                "execution_source_revision": args.source_revision, "selection": selection,
                "raw_l2": raw, "assessment": assessment, "source_binding": proof, "provenance": provenance,
                "workflow_run_id": os.environ.get("GITHUB_RUN_ID"), "python_version": platform.python_version()}
    # Preserve a true rejection too; it must never launch the official successor.
    stress_artifacts._atomic_json(out / "l2-evidence.json", evidence)
    print(json.dumps({"l2_passed": assessment["passed"], "assessment": assessment}, ensure_ascii=False), flush=True)
    if not assessment["passed"]:
        return 2
    stress_artifacts.validate_release_l2_evidence(evidence, source_revision=args.source_revision, reference=reference)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
