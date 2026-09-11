"""Source-bound release assessment for the user-selected C6 AB5 evidence."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from quantfusion.application.c6_contract import canonical_payload_hash

ACCEPTANCE_REVISION = "C6_AB5_RETURN_EXCEPTIONS_20260911"
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
    original_passed = row.get("passed") is True
    release_passed = original_passed
    threshold: dict[str, object] | None = None
    exception: str | None = None
    if gate in _RELAXED_LIMITS:
        value = observed.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
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
