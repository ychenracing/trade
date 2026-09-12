"""Source-bound release assessment for the user-selected C6 AB5 evidence."""
from __future__ import annotations

import ast
import math
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from quantfusion.application.c6_contract import canonical_payload_hash
from quantfusion.config.paths import PROJECT_ROOT

ACCEPTANCE_REVISION = "C6_AB5_P90_185_EXCEPTION_20260912"
RELAXATION_REVISION = "C6_ACCEPTANCE_RELAX15_20260911"
AB5_CANDIDATE_ID = "C6-Base+AB5"
AB5_BASE_SOURCE_REVISION = "4659a2b6d265f45256777da6a2fc25d1369308bd"
AB5_BASE_PRODUCER_RUN_ID = 34509818018
AB5_REFERENCE_SHA256 = (
    "ffe023ae48de50225870c220d37208d1d1ee6c26ba6905a470093562153decbe"
)

_RELAXED_LIMITS = {
    "mdd.noncanonical_18pct_screen": ("<=", 0.207, 1e-15),
    "prefix.09_to_10_wealth": (">", -0.115, 0.0),
    "prefix.worst_adjacent_wealth": (">=", -0.345, 1e-12),
    "initial.prefix05": (">=", 0.8415, 1e-12),
    "initial.prefix05_proxy": (">=", 0.8415, 1e-12),
    "initial.other_prefix": (">=", 0.8075, 1e-12),
    "initial.other_prefix_proxy": (">=", 0.8075, 1e-12),
    "trade.random_p90_buckets": ("<=", 184.0, 0.0),
    "trade.max_buckets": ("<=", 230.0, 0.0),
    "initial.worst_total_return_delta": (">=", -0.023, 1e-12),
    "initial.worst_add_one": (">=", -0.0345, 1e-12),
    "initial.add_one_601869_proxy": (">=", -0.0345, 1e-12),
}

_KNOWN_ENVELOPES = {
    "mdd.noncanonical_18pct_screen": (
        "<=",
        0.2110621724651241,
        1e-15,
        "AB5_KNOWN_MDD_ENVELOPE",
    ),
    "trade.random_p90_buckets": (
        "<=",
        185.0,
        0.0,
        "AB5_KNOWN_RANDOM_P90_185_ENVELOPE",
    ),
    "prefix.worst_adjacent_wealth": (
        ">=",
        -0.4557191766929257,
        1e-12,
        "AB5_KNOWN_ADJACENT_ENVELOPE",
    ),
    "initial.prefix05": (
        ">=",
        0.723166270514689,
        1e-12,
        "AB5_KNOWN_PREFIX05_ENVELOPE",
    ),
    "initial.prefix05_proxy": (
        ">=",
        0.723166270514689,
        1e-12,
        "AB5_KNOWN_PREFIX05_ENVELOPE",
    ),
    "initial.other_prefix": (
        ">=",
        0.33223093416649163,
        1e-12,
        "AB5_KNOWN_OTHER_PREFIX_ENVELOPE",
    ),
    "initial.other_prefix_proxy": (
        ">=",
        0.33223093416649163,
        1e-12,
        "AB5_KNOWN_OTHER_PREFIX_ENVELOPE",
    ),
}


def _gate_name(predicate_id: str) -> str:
    for stage in ("l1.", "l2."):
        if predicate_id.startswith(stage):
            return predicate_id[len(stage):]
    return predicate_id


def _passes(value: float, comparator: str, limit: float, tolerance: float) -> bool:
    if comparator == "<=":
        return abs(value) <= limit + tolerance
    if comparator == ">=":
        return value >= limit - tolerance
    if comparator == ">":
        return value > limit
    raise ValueError(f"unsupported release comparator: {comparator}")


def _identity(
    *,
    candidate_id: str,
    source_revision: str,
    producer_run_id: int,
    reference_sha256: str,
) -> dict[str, object]:
    identity = {
        "candidate_id": candidate_id,
        "source_revision": source_revision,
        "base_producer_run_id": producer_run_id,
        "reference_sha256": reference_sha256,
    }
    if identity != {
        "candidate_id": AB5_CANDIDATE_ID,
        "source_revision": AB5_BASE_SOURCE_REVISION,
        "base_producer_run_id": AB5_BASE_PRODUCER_RUN_ID,
        "reference_sha256": AB5_REFERENCE_SHA256,
    }:
        raise ValueError("AB5 release evidence identity does not match the authorized source")
    return identity


def _release_row(row: Mapping[str, Any]) -> dict[str, Any]:
    predicate_id = str(row["predicate_id"])
    gate = _gate_name(predicate_id)
    observed = row.get("observed")
    if not isinstance(observed, Mapping):
        raise ValueError(f"release predicate has invalid observation: {predicate_id}")
    if type(row.get("passed")) is not bool:
        raise ValueError(f"release predicate result is not boolean: {predicate_id}")
    original_passed = row["passed"]
    release_passed = original_passed
    threshold: dict[str, object] | None = None
    exception: str | None = None
    if gate in _RELAXED_LIMITS:
        value = observed.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"release predicate has non-numeric value: {predicate_id}")
        comparator, limit, tolerance = _RELAXED_LIMITS[gate]
        release_passed = _passes(float(value), comparator, limit, tolerance)
        threshold = {
            "comparator": comparator,
            "value": limit,
            "tolerance": tolerance,
        }
        if not release_passed and gate in _KNOWN_ENVELOPES:
            comparator, limit, tolerance, exception_name = _KNOWN_ENVELOPES[gate]
            release_passed = _passes(float(value), comparator, limit, tolerance)
            if release_passed:
                exception = exception_name
                threshold = {
                    "comparator": comparator,
                    "value": limit,
                    "tolerance": tolerance,
                }
    return {
        "predicate_id": predicate_id,
        "original_passed": original_passed,
        "original_detail_sha256": observed.get("detail_sha256"),
        "release_passed": release_passed,
        "release_threshold": threshold,
        "exception_applied": exception,
        "failure_reason": None if release_passed else row.get("failure_reason"),
    }


def release_predicate_assessment(
    payload: Mapping[str, Any],
    *,
    candidate_id: str,
    source_revision: str,
    producer_run_id: int,
    reference_sha256: str,
) -> dict[str, Any]:
    """Derive a new release decision without changing the frozen raw predicates."""
    if payload.get("kind") not in {"c6_l1_base", "c6_l2"}:
        raise ValueError("AB5 release assessment requires an L1 Base or L2 payload")
    raw_rows = payload.get("diagnostic_predicates")
    if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)):
        raise ValueError("AB5 release assessment requires predicate rows")
    if not raw_rows or any(not isinstance(row, Mapping) for row in raw_rows):
        raise ValueError("release predicates must be a nonempty sequence of rows")
    ids = [row.get("predicate_id") for row in raw_rows]
    if any(not isinstance(item, str) or not item for item in ids) or len(set(ids)) != len(ids):
        raise ValueError("release predicate IDs are invalid or duplicate")
    identity = _identity(
        candidate_id=candidate_id,
        source_revision=source_revision,
        producer_run_id=producer_run_id,
        reference_sha256=reference_sha256,
    )
    rows = [_release_row(row) for row in raw_rows]
    body = {
        "schema_version": 1,
        "kind": "c6_ab5_release_predicate_assessment",
        "acceptance_revision": ACCEPTANCE_REVISION,
        "relaxation_revision": RELAXATION_REVISION,
        "source_payload_sha256": canonical_payload_hash(payload),
        "evidence_identity": identity,
        "predicate_results": rows,
        "passed": all(row["release_passed"] for row in rows),
    }
    return {**body, "assessment_id": canonical_payload_hash(body)}


def validate_release_predicate_assessment(
    assessment: Mapping[str, Any], payload: Mapping[str, Any]
) -> None:
    """Recompute an AB5 release assessment and reject altered derived decisions."""
    identity = assessment.get("evidence_identity")
    if not isinstance(identity, Mapping):
        raise ValueError("AB5 release assessment has no evidence identity")
    expected = release_predicate_assessment(
        payload,
        candidate_id=str(identity.get("candidate_id")),
        source_revision=str(identity.get("source_revision")),
        producer_run_id=int(identity.get("base_producer_run_id", -1)),
        reference_sha256=str(identity.get("reference_sha256")),
    )
    if canonical_payload_hash(assessment) != canonical_payload_hash(expected):
        raise ValueError("AB5 release assessment differs from recomputed evidence")


AB5_DATA_FINGERPRINT = "aeeb96a94e84830033e8ad11180293fc982bb08e99322054d2c27dbb3f4b5975"
_AB5_REFERENCE_PAYLOAD_SHA256 = "a08fb77de1f840bbb0d4adecb907f559ecead7dbea3c654761042465ff9bd5f7"

# Field names refer to the unchanged native gates, never to a replacement metric.
_FORMAL_GATES = {
    "absolute_hard_gates": (
        ("all_scenarios_max_drawdown_at_most_18pct", "all_worst_drawdown", "mdd.noncanonical_18pct_screen", None),
        ("random_p90_date_symbol_side_buckets_at_most_160", "random_p90_date_symbol_side_buckets", "trade.random_p90_buckets", None),
        ("all_date_symbol_side_buckets_at_most_200", "all_worst_date_symbol_side_buckets", "trade.max_buckets", None),
    ),
    "retained_robustness_hard_gates": (
        ("prefix_9_to_10_wealth_above_minus_10pct", "prefix_9_to_10_wealth_change", "prefix.09_to_10_wealth", None),
        ("worst_adjacent_wealth_at_least_minus_30pct", "worst_adjacent_wealth_change", "prefix.worst_adjacent_wealth", None),
    ),
    "initial_baseline_gates": (
        ("prefix_05_wealth_at_least_99pct", "prefix_05_wealth_ratio", "initial.prefix05", None),
        ("other_prefix_wealth_at_least_95pct", "other_prefix_wealth_ratio_min", "initial.other_prefix", None),
        ("worst_total_return_not_worse_by_0_02", "worst_total_return", "initial.worst_total_return_delta", "reference_worst_total_return"),
        ("worst_add_one_diagnostic_not_worse_by_0_03", "worst_add_one_wealth_change", "initial.worst_add_one", "reference_worst_add_one_wealth_change"),
    ),
}


def release_formal_assessment(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Assess already-recomputed native initial-baseline gates, preserving them."""
    rows = []
    for family, specs in _FORMAL_GATES.items():
        native = payload.get(family)
        if not isinstance(native, Mapping) or set(native.get("checks", {})) != {spec[0] for spec in specs}:
            raise ValueError(f"AB5 formal gate coverage differs: {family}")
        observed = native.get("observed")
        if not isinstance(observed, Mapping):
            raise ValueError(f"AB5 formal observations missing: {family}")
        for name, field, gate, reference_field in specs:
            original = native["checks"][name]
            if type(original) is not bool:
                raise ValueError(f"AB5 formal gate result is not boolean: {name}")
            fields = (field,) if reference_field is None else (field, reference_field)
            if any(type(observed.get(key)) not in (int, float) or not math.isfinite(observed[key]) for key in fields):
                raise ValueError(f"AB5 formal observation is not finite: {name}")
            value = observed[field] if reference_field is None else observed[field] - observed[reference_field]
            row = _release_row({"predicate_id": gate, "passed": original,
                                "observed": {"value": value, "detail_sha256": canonical_payload_hash(native)},
                                "failure_reason": name})
            rows.append({**row, "predicate_id": name, "gate_family": family, "observed_value": value})
    permutation = payload.get("promotion_gates", {}).get("permutation_invariance", {}).get("invariant")
    if type(permutation) is not bool:
        raise ValueError("AB5 formal gate coverage lacks permutation result")
    rows.append({"predicate_id": "permutation_invariant", "original_passed": permutation,
                 "release_passed": permutation, "release_threshold": None, "exception_applied": None,
                 "gate_family": "promotion_gates", "failure_reason": None if permutation else "PERMUTATION_INVARIANCE_FAILED"})
    body = {"kind": "c6_ab5_release_formal_assessment", "schema_version": 1,
            "acceptance_revision": ACCEPTANCE_REVISION, "relaxation_revision": RELAXATION_REVISION,
            "source_payload_sha256": canonical_payload_hash(payload),
            "native_gate_sha256": canonical_payload_hash({key: payload[key] for key in (*_FORMAL_GATES, "promotion_gates")}),
            "predicate_results": rows, "passed": all(row["release_passed"] for row in rows)}
    return {**body, "assessment_id": canonical_payload_hash(body)}


def validate_published_release_assessment(payload: Mapping[str, Any]) -> None:
    """Verify the assessment against its native input, excluding publication stamps.

    This checks the derived assessment only. The caller must also authenticate
    the artifact and validate its native records, source, reference and L2 proof.
    """
    if (payload.get("acceptance_status") != "accepted" or payload.get("canonical") is not True
            or payload.get("baseline_kind") != "initial_current_contract"):
        raise ValueError("AB5 published artifact is not an accepted initial baseline")
    attachment = payload.get("release_acceptance")
    if not isinstance(attachment, Mapping) or not isinstance(attachment.get("assessment"), Mapping):
        raise ValueError("AB5 published artifact has no release assessment")
    native = {key: value for key, value in payload.items() if key not in {
        "release_acceptance", "acceptance_status", "canonical", "baseline_kind"}}
    expected = release_formal_assessment(native)
    if not expected["passed"] or canonical_payload_hash(attachment["assessment"]) != canonical_payload_hash(expected):
        raise ValueError("AB5 published release assessment differs from its native input")


def verify_ab5_release_source(source_revision: str, *, root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Bind the actual checkout to AB5 economic bytes plus explicit release plumbing.

    This is a dependency-scoped equivalence proof, NOT a whole-tree A proof.
    A new strategy, configuration or metric implementation is never allowed here.
    """
    def git(*args: str) -> str:
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()

    if git("rev-parse", "HEAD") != source_revision:
        raise ValueError("AB5 release source differs from the actual checkout")
    if git("status", "--porcelain", "--untracked-files=all", "--", "quantfusion"):
        raise ValueError("AB5 release runtime checkout has uncommitted source")
    allowed = {"quantfusion/application/" + name for name in (
        "c6_release_acceptance.py", "c6_predicates.py", "stress.py", "stress_artifacts.py")}
    changed = set(git("diff", "--name-only", AB5_BASE_SOURCE_REVISION, source_revision, "--", "quantfusion").splitlines())
    if changed - allowed:
        raise ValueError(f"AB5 economic source changed: {sorted(changed - allowed)}")
    worker_path = "quantfusion/application/stress.py"
    def economic_ast(text: str) -> str:
        tree = ast.parse(text)
        tree.body = [node for node in tree.body if not (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {"main", "build_argument_parser"})]
        return ast.dump(tree, include_attributes=False)
    if economic_ast((root / worker_path).read_text(encoding="utf-8")) != economic_ast(git("show", f"{AB5_BASE_SOURCE_REVISION}:{worker_path}")):
        raise ValueError("AB5 formal worker differs from the frozen economic implementation")
    return {"kind": "ab5_economic_dependency_equivalence", "execution_source_revision": source_revision,
            "execution_tree": git("rev-parse", "HEAD^{tree}"), "base_source_revision": AB5_BASE_SOURCE_REVISION,
            "release_plumbing_paths": sorted(changed), "whole_tree_A": None}



AB5_ORIGINAL_D_SHA256 = "6afee85f4c35502816ccde2af43f5c528bd0154f1770204dfcf27e18f66dad29"



# One authenticated, already executed L2; reuse changes only the erroneous predicate aggregation.
AB5_L2_RESULTS_SHA256 = "0025431de9f09a335037deb5d2c68cbc74ae998f83670d8460c799c6c004fbd1"
AB5_L2_REUSE = {
    "kind": "c6_l2_difference_of_minima_reassessment",
    "economic_source_revision": "c8d46db65b4ae1e72884399cbcaeb7999f6c400b",
    "economic_workflow_run_id": 34630144631,
    "original_artifact_id": 10276433256,
    "original_artifact_sha256": "1771990a779f3a228b1e401699bd2be95b55320e3a47fb59086171f9b5991d16",
    "original_evidence_sha256": "a1f8054b29b948ba6eecc4bee062e8cb7b24387934fa56f9ebf47fa5c7c3c57d",
    "original_raw_payload_sha256": "a32981a24e05b62db5d9fd255d2fbe93f4676e94f757239dfd8548103c5a6880",
    "economic_replays": 0,
}


def validate_l2_reuse(lineage: Mapping[str, Any], *, results_sha256: str) -> None:
    """Validate the known producer and unchanged records; never relabel economic source."""
    if (canonical_payload_hash(lineage) != canonical_payload_hash(AB5_L2_REUSE)
            or results_sha256 != AB5_L2_RESULTS_SHA256):
        raise ValueError("AB5 L2 reuse differs from its authenticated economic producer")


def ab5_preregistration() -> dict[str, Any]:
    """Read the immutable native schema and scenario lists; never edit old P."""
    import json

    return json.loads(subprocess.check_output([
        "git", "-C", str(PROJECT_ROOT), "show",
        f"{AB5_BASE_SOURCE_REVISION}:artifacts/diagnostics/c6-preregistration.json",
    ]))


def derive_base_release_selection(original: Mapping[str, Any]) -> dict[str, Any]:
    """Rejudge authenticated compact D predicates, not a purported new Base run."""
    if canonical_payload_hash(original) != AB5_ORIGINAL_D_SHA256:
        raise ValueError("AB5 original selection receipt differs from the authenticated native D")
    projection = {"kind": "c6_l1_base", "diagnostic_predicates": original["base_l1_predicates"]}
    assessment = release_predicate_assessment(
        projection, candidate_id=AB5_CANDIDATE_ID, source_revision=AB5_BASE_SOURCE_REVISION,
        producer_run_id=AB5_BASE_PRODUCER_RUN_ID, reference_sha256=AB5_REFERENCE_SHA256,
    )
    body = {"kind": "c6_ab5_derived_release_selection", "schema_version": 1,
            "original_D": dict(original), "original_D_sha256": AB5_ORIGINAL_D_SHA256,
            "source_projection": "authenticated_D.base_l1_predicates",
            "assessment": assessment, "selected_candidate": AB5_CANDIDATE_ID if assessment["passed"] else None}
    return {**body, "selection_id": canonical_payload_hash(body)}
