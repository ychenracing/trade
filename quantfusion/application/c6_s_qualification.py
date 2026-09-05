"""Selection-critical, non-economic qualification of the frozen C6 S candidate."""

from __future__ import annotations

import argparse
import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from quantfusion.application.c6_contract import (
    canonical_json_bytes,
    load_preregistration,
    load_run_bindings,
    select_binding,
    strict_json_load,
)


_CRITERIA = (
    (
        "q1_base_breach_exists",
        (
            "base.evaluation.official_metrics.max_drawdown",
            "base.evaluation.causal_matrix.event_timeline.first_official_mdd_breach.timestamp",
        ),
        "Q1_BASE_BREACH_MISSING",
    ),
    (
        "q2_causal_stress_evidence_precedes_breach",
        (
            "base.evaluation.causal_matrix.s_evidence.first_causal_stressed_cluster_close",
            "base.evaluation.causal_matrix.event_timeline.first_official_mdd_breach.timestamp",
        ),
        "Q2_CAUSAL_EVIDENCE_NOT_EARLY",
    ),
    (
        "q3_dominant_cluster_is_stressed_and_over_cap",
        (
            "base.evaluation.causal_matrix.s_evidence.worst_cluster",
            "base.evaluation.causal_matrix.s_evidence.worst_cluster_weight",
            "base.evaluation.causal_matrix.s_evidence.stressed_cluster_set",
        ),
        "Q3_CLUSTER_NOT_STRESSED_OR_OVER_CAP",
    ),
    (
        "q4_complete_independent_evidence",
        (
            "base.evaluation.causal_matrix.s_evidence.coverage",
            "base.evaluation.causal_matrix.s_evidence.leave_held_components_out",
        ),
        "Q4_EVIDENCE_INCOMPLETE",
    ),
    (
        "q5_scheduled_prebreach_nonzero_lot",
        (
            "base.evaluation.causal_matrix.s_evidence.first_early_sell_required_close",
            "base.evaluation.causal_matrix.s_evidence.risk_level",
            "base.evaluation.causal_matrix.s_evidence.portfolio_fast_return",
            "base.evaluation.causal_matrix.s_evidence.existing_concentration_eligible",
            "base.evaluation.causal_matrix.s_evidence.cluster_symbol_count",
            "base.evaluation.causal_matrix.s_evidence.minimum_cluster_size",
            "base.evaluation.causal_matrix.s_evidence.legacy_gate_open",
            "base.evaluation.causal_matrix.s_evidence.early_sell_required",
            "base.evaluation.causal_matrix.s_evidence.scheduled_execution_batch",
            "base.evaluation.causal_matrix.s_evidence.planned_shares",
            "base.evaluation.causal_matrix.s_evidence.executable_lot_shares",
        ),
        "Q5_NO_PREBREACH_EXECUTABLE_ACTION",
    ),
    (
        "q6_action_strictly_precedes_breach_sample",
        (
            "base.evaluation.causal_matrix.s_evidence.scheduled_execution_batch.execution_open",
            "base.evaluation.causal_matrix.event_timeline.first_official_mdd_breach.timestamp",
            "base.evaluation.causal_matrix.s_evidence.lead_batch_count",
        ),
        "Q6_ACTION_NOT_BEFORE_BREACH",
    ),
    (
        "q7_official_sample_gap_not_proven_unavoidable",
        (
            "base.evaluation.causal_matrix.event_timeline.first_official_mdd_breach.timestamp",
            "base.evaluation.causal_matrix.s_evidence.official_sample_relation",
            "base.evaluation.causal_matrix.s_evidence.pre_trade_open_drawdown",
            "base.evaluation.causal_matrix.s_evidence.identical_valuation_instant_proven",
        ),
        "Q7_GAP_PROVEN_UNAVOIDABLE",
    ),
)


def _resolve(evaluation: Mapping[str, Any], path: str) -> Any:
    prefix = "base.evaluation."
    if not path.startswith(prefix):
        raise ValueError(f"qualification path has invalid root: {path}")
    value: Any = evaluation
    for segment in path[len(prefix):].split("."):
        if value is None:
            return None
        if not isinstance(value, Mapping) or segment not in value:
            raise ValueError(f"qualification path does not resolve: {path}")
        value = value[segment]
    return value


def _ordered_unique(values: object) -> bool:
    return (
        isinstance(values, list)
        and all(isinstance(value, str) and value for value in values)
        and values == sorted(values)
        and len(values) == len(set(values))
    )


def _coverage_valid(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    try:
        freshness = (
            value["latest_source_timestamp"] == value["decision_timestamp"]
        )
        coverage = (
            value["observed_count"] >= 4
            and value["observed_industries"] >= 3
        )
        unmapped = value["unmapped_weight"] < 0.05
        return (
            value["minimum_observed"] == 4
            and value["minimum_observed_industries"] == 3
            and value["freshness_max_sessions"] == 0
            and value["unmapped_limit"] == 0.05
            and value["freshness_passed"] is freshness
            and value["coverage_passed"] is coverage
            and value["unmapped_passed"] is unmapped
            and freshness and coverage and unmapped
        )
    except (KeyError, TypeError):
        return False


def _leave_valid(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    try:
        removed = value["removed_components"]
        remaining = value["remaining_components"]
        stressed = value["recomputed_stressed_cluster_set"]
        if not all(_ordered_unique(item) for item in (removed, remaining, stressed)):
            return False
        coverage = (
            value["observed_count"] >= 4
            and value["observed_industries"] >= 3
        )
        same = value["target_cluster"] in stressed
        common = (
            value["minimum_observed"] == 4
            and value["minimum_observed_industries"] == 3
            and value["fast_return_threshold"] == -0.06
            and value["breadth_threshold"] == 0.60
            and value["coverage_passed"] is coverage
            and value["same_evidence_preserved"] is same
        )
        if value["mode"] == "disjoint_pass":
            passed = not removed and same
        elif value["mode"] == "recomputed":
            passed = (
                bool(removed)
                and value["freshness_passed"] is True
                and coverage
                and value["recomputed_fast_return"] <= -0.06
                and value["recomputed_declining_ratio"] >= 0.60
                and same
            )
        else:
            return False
        return common and value["passed"] is passed and passed
    except (KeyError, TypeError):
        return False


def _fill_valid(evidence: Mapping[str, Any]) -> bool:
    fill = evidence.get("fillability")
    shortfall = evidence.get("shortfall")
    if not isinstance(fill, Mapping) or not isinstance(shortfall, Mapping):
        return False
    try:
        planned = evidence["planned_shares"]
        executable = evidence["executable_lot_shares"]
        nonzero = (
            fill["t_plus_one_passed"] is True
            and fill["open_available"] is True
            and fill["not_suspended"] is True
            and fill["not_limit_blocked"] is True
            and fill["adv_capacity_shares"] >= fill["lot_size"]
            and executable >= fill["lot_size"]
            and executable <= planned
        )
        shares = max(planned - executable, 0)
        checks = (
            (fill["t_plus_one_passed"], "T_PLUS_ONE"),
            (fill["open_available"], "MISSING_OPEN"),
            (fill["not_suspended"], "SUSPENDED"),
            (fill["not_limit_blocked"], "LIMIT_BLOCKED"),
            (fill["adv_capacity_shares"] > 0, "ADV_ZERO"),
            (fill["adv_capacity_shares"] >= planned, "CAPACITY"),
            (executable >= fill["lot_size"], "SUB_LOT"),
        )
        reason = next((name for passed, name in checks if not passed), "NONE")
        return (
            fill["lot_size"] == 100
            and fill["nonzero_executable_lot"] is nonzero
            and shortfall == {"shares": shares, "reason": reason}
            and nonzero
        )
    except (KeyError, TypeError):
        return False


def _criterion_passes(
    criterion_id: str, evaluation: Mapping[str, Any]
) -> bool:
    evidence = evaluation["causal_matrix"]["s_evidence"]
    breach = evaluation["causal_matrix"]["event_timeline"][
        "first_official_mdd_breach"
    ]["timestamp"]
    if criterion_id == "q1_base_breach_exists":
        return (
            abs(float(evaluation["official_metrics"]["max_drawdown"]))
            > 0.18 + 1e-15
            and isinstance(breach, str) and bool(breach)
        )
    if criterion_id == "q2_causal_stress_evidence_precedes_breach":
        first = evidence["first_causal_stressed_cluster_close"]
        return isinstance(first, str) and isinstance(breach, str) and first < breach
    if criterion_id == "q3_dominant_cluster_is_stressed_and_over_cap":
        weight = evidence["worst_cluster_weight"]
        return (
            isinstance(evidence["worst_cluster"], str)
            and evidence["worst_cluster"] in evidence["stressed_cluster_set"]
            and isinstance(weight, (int, float))
            and not isinstance(weight, bool)
            and weight > 0.8
        )
    if criterion_id == "q4_complete_independent_evidence":
        return _coverage_valid(evidence["coverage"]) and _leave_valid(
            evidence["leave_held_components_out"]
        )
    if criterion_id == "q5_scheduled_prebreach_nonzero_lot":
        return (
            isinstance(evidence["first_early_sell_required_close"], str)
            and evidence["risk_level"] >= 1
            and evidence["portfolio_fast_return"] < 0
            and evidence["existing_concentration_eligible"] is True
            and evidence["cluster_symbol_count"] >= 2
            and evidence["minimum_cluster_size"] == 2
            and evidence["legacy_gate_open"] is False
            and evidence["early_sell_required"] is True
            and isinstance(evidence["scheduled_execution_batch"], Mapping)
            and evidence["planned_shares"] > 0
            and evidence["executable_lot_shares"] > 0
            and _fill_valid(evidence)
        )
    if criterion_id == "q6_action_strictly_precedes_breach_sample":
        schedule = evidence["scheduled_execution_batch"]
        return (
            isinstance(schedule, Mapping)
            and isinstance(breach, str)
            and schedule["execution_open"].split("T", 1)[0]
            <= breach.split("T", 1)[0]
            and evidence["lead_batch_count"] >= 1
        )
    if criterion_id == "q7_official_sample_gap_not_proven_unavoidable":
        return (
            isinstance(breach, str)
            and evidence["official_sample_relation"]
            != "UNAVOIDABLE_AT_OFFICIAL_SAMPLE"
            and not (
                evidence["official_sample_relation"]
                == "OPEN_MARK_GAP_NOT_OFFICIAL_SAMPLE"
                and evidence["identical_valuation_instant_proven"] is True
            )
        )
    raise ValueError(f"unknown qualification criterion: {criterion_id}")


def _qualify(evaluation: Mapping[str, Any]) -> dict[str, Any]:
    evidence = evaluation["causal_matrix"]["s_evidence"]
    breach = evaluation["causal_matrix"]["event_timeline"][
        "first_official_mdd_breach"
    ]["timestamp"]
    criteria = []
    for criterion_id, paths, failure in _CRITERIA:
        observed = {
            path.rsplit(".", 1)[-1]: _resolve(evaluation, path) for path in paths
        }
        passed = _criterion_passes(criterion_id, evaluation)
        criteria.append(
            {
                "criterion_id": criterion_id,
                "passed": passed,
                "input_paths": list(paths),
                "observed_values": observed,
                "failure_reason": None if passed else failure,
            }
        )
    failures = [
        item["failure_reason"] for item in criteria if not item["passed"]
    ]
    keys = (
        "first_causal_stressed_cluster_close", "worst_cluster",
        "worst_cluster_weight", "stressed_cluster_set", "coverage",
        "leave_held_components_out", "first_early_sell_required_close",
        "risk_level", "portfolio_fast_return",
        "existing_concentration_eligible", "cluster_symbol_count",
        "minimum_cluster_size", "legacy_gate_open", "early_sell_required",
        "scheduled_execution_batch", "lead_batch_count",
        "pre_trade_open_drawdown", "official_sample_relation",
        "identical_valuation_instant_proven", "planned_shares",
        "executable_lot_shares", "fillability", "shortfall",
    )
    return {
        "scenario_id": evaluation["scenario_id"],
        "criteria": criteria,
        "first_official_mdd_breach": breach,
        **{key: evidence[key] for key in keys},
        "passed": not failures,
        "failure_reasons": failures,
    }


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
    if not isinstance(evaluations, list):
        raise ValueError("Base producer evaluations must be an array")
    selected = [
        item for item in evaluations
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
        results = checkpoint.map(_qualify, residual, item_ids, workers=1)
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
    payload_bytes = (producer / "payload.json").read_bytes()
    if hashlib.sha256(payload_bytes).hexdigest() != args.producer_artifact_sha256:
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
        strict_json_load(producer / "payload.json"),
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
    output.write_bytes(canonical_json_bytes(result))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
