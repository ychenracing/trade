"""Selection-critical, non-economic qualification of the frozen C6 S candidate."""

from __future__ import annotations

import argparse
import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from quantfusion.application.c6_contract import (
    file_sha256,
    load_preregistration,
    load_run_bindings,
    select_binding,
    strict_json_load,
)

from quantfusion.io.c6_stream import FileArray, load_object, write_json


from quantfusion.application.c6_predicates import _CRITERIA, _qualify, _fill_valid as _fill_valid


def qualify_base_payload(
    base_payload: Mapping[str, Any],
    *,
    base_producer_identity: Mapping[str, Any],
    P: Mapping[str, Any],
    R_revision: str,
    I_B: Mapping[str, Any],
    I_S: Mapping[str, Any],
    checkpointed: bool = False,
) -> dict[str, Any]:
    evaluations = base_payload.get("evaluations")
    if not isinstance(evaluations, (list, FileArray)):
        raise ValueError("Base producer evaluations must be an array")
    selected = [
        {"scenario_id": item["scenario_id"], "official_metrics": item["official_metrics"],
         "causal_matrix": {key: item["causal_matrix"][key] for key in ("event_timeline", "s_evidence")}} for item in evaluations
        if isinstance(item, Mapping) and item.get("variant_id") == "C6-Base"
    ]
    scenario_ids = [item.get("scenario_id") for item in selected]
    if (
        len(selected) != 765
        or len(set(scenario_ids)) != 765
        or not all(isinstance(item, str) and item for item in scenario_ids)
    ):
        raise ValueError("Base producer must contain exactly 765 unique scenarios")
    residual = sorted(
        (
            item for item in selected
            if abs(float(item["official_metrics"]["max_drawdown"]))
            > 0.18 + 1e-15
        ),
        key=lambda item: item["scenario_id"],
    )
    if not residual:
        raise ValueError("empty residual set forbids S qualification")
    ids = [item["scenario_id"] for item in residual]
    if checkpointed:
        from quantfusion.application.c6_bound_run import DiagnosticCheckpoint
        item_ids = [f"qualification/{item}" for item in ids]
        checkpoint = DiagnosticCheckpoint.from_environment(item_ids)
        results = list(checkpoint.map(_qualify, residual, item_ids, workers=1))
    else:
        results = [_qualify(item) for item in residual]
    return {
        "schema_version": 2,
        "kind": "c6_s_qualification",
        "diagnostic_noncanonical": True,
        "complete": True,
        "base_producer_identity": dict(base_producer_identity),
        "P": dict(P),
        "R_revision": R_revision,
        "I_B": dict(I_B),
        "I_S": dict(I_S),
        "residual_ids": ids,
        "residual_ids_sha256": hashlib.sha256(
            "".join(f"{item}\n" for item in ids).encode()
        ).hexdigest(),
        "results": results,
        "all_passed": all(item["passed"] for item in results),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Qualify the frozen C6 S candidate")
    parser.add_argument("--preregistration", required=True)
    parser.add_argument("--bindings-file", required=True)
    parser.add_argument("--binding-record-id", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--producer-export", required=True)
    parser.add_argument("--producer-artifact-sha256", required=True)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    prereg = load_preregistration(args.preregistration, repository=Path.cwd())
    bindings = load_run_bindings(args.bindings_file)
    binding = next(
        item for item in bindings["binding_records"]
        if item["record_id"] == args.binding_record_id
    )
    selected = select_binding(
        bindings, binding["workflow_binding_id"],
        candidate_id=binding["candidate_id"],
    )
    if (
        selected is not binding
        or binding["record_id"] != "c6.s.qualification"
        or binding["source_revision"] != args.source_revision
    ):
        raise ValueError("CLI identity does not select S qualification")
    producer = Path(args.producer_export)
    if file_sha256(producer / "payload.json") != args.producer_artifact_sha256:
        raise ValueError("Base producer artifact SHA-256 mismatch")
    manifest = strict_json_load(producer / "manifest.json")
    base_identity = {
        "artifact_full_byte_sha256": args.producer_artifact_sha256,
        "attempt_id": manifest["attempt_id"],
        "binding_id": manifest["binding_id"],
        "logical_run_id": manifest["logical_run_id"],
        "workflow_run_id": manifest["workflow_run_id"],
    }
    if (
        base_identity["binding_id"] != "c6.base.l1"
        or base_identity["logical_run_id"] != next(record["logical_run_id"] for record in bindings["binding_records"] if record["record_id"] == "c6.base.l1")
        or manifest["candidate_id"] != "C6-Base"
        or manifest["source_revision"]
        != bindings["implementations"]["I_B"]["commit"]
    ):
        raise ValueError("Base producer identity does not match qualification")
    result = qualify_base_payload(
        load_object(producer / "payload.json"),
        base_producer_identity=base_identity,
        P=bindings["P"],
        R_revision=manifest["run_bindings_revision"],
        I_B=bindings["implementations"]["I_B"],
        I_S=bindings["implementations"]["I_S"],
        checkpointed=True,
    )
    qualification = prereg["S_QUALIFICATION_RUN"]
    expected = qualification["criteria"]
    frozen = [
        {"id": criterion_id, "input_paths": list(paths)}
        for criterion_id, paths, _ in _CRITERIA
    ]
    actual = [
        {"id": item["id"], "input_paths": item["input_paths"]}
        for item in expected
    ]
    failures = qualification["failure_reason_enum_by_criterion"]
    frozen_failures = {
        criterion_id: failure
        for criterion_id, _, failure in _CRITERIA
    }
    if actual != frozen or failures != frozen_failures:
        raise ValueError("P qualification criteria differ from implementation")
    output = Path(args.output)
    if output.exists():
        raise ValueError("qualification output path already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
