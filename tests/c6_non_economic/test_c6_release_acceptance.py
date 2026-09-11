from __future__ import annotations

from copy import deepcopy

import pytest

from quantfusion.application.c6_release_acceptance import (
    AB5_BASE_SOURCE_REVISION,
    AB5_REFERENCE_SHA256,
    release_predicate_assessment,
    validate_release_predicate_assessment,
)
from quantfusion.application.c6_predicates import (
    validate_attached_release_assessment,
)


def _predicate(predicate_id: str, value: float, *, passed: bool = False) -> dict[str, object]:
    return {
        "predicate_id": predicate_id,
        "passed": passed,
        "observed": {
            "value": value,
            "reference_value": None,
            "threshold": "original frozen comparator",
            "failure_count": 1 if not passed else 0,
            "failed_item_ids": [] if passed else ["witness"],
            "detail_sha256": "1" * 64,
        },
        "comparator": "original frozen comparator",
        "tolerance": 1e-12,
        "failure_reason": None if passed else "ORIGINAL_GATE_FAILED",
    }


def _payload(*rows: dict[str, object], kind: str = "c6_l1_base") -> dict[str, object]:
    return {
        "schema_version": 2,
        "kind": kind,
        "diagnostic_predicates": list(rows),
    }


def _assess(payload: dict[str, object]) -> dict[str, object]:
    return release_predicate_assessment(
        payload,
        candidate_id="C6-Base+AB5",
        source_revision=AB5_BASE_SOURCE_REVISION,
        producer_run_id=34509818018,
        reference_sha256=AB5_REFERENCE_SHA256,
    )


def test_exact_user_accepted_ab5_envelopes_pass_without_mutating_raw_rows() -> None:
    raw = _payload(
        _predicate("l1.mdd.noncanonical_18pct_screen", 0.2110621724651241),
        _predicate("l1.initial.prefix05_proxy", 0.723166270514689),
        _predicate("l1.initial.other_prefix_proxy", 0.33223093416649163),
        _predicate("l1.prefix.worst_adjacent_wealth", -0.4557191766929257),
    )
    before = deepcopy(raw)

    assessment = _assess(raw)

    assert raw == before
    assert assessment["passed"] is True
    assert [row["exception_applied"] for row in assessment["predicate_results"]] == [
        "AB5_KNOWN_MDD_ENVELOPE",
        "AB5_KNOWN_PREFIX05_ENVELOPE",
        "AB5_KNOWN_OTHER_PREFIX_ENVELOPE",
        "AB5_KNOWN_ADJACENT_ENVELOPE",
    ]
    validate_release_predicate_assessment(assessment, raw)


def test_result_outside_known_ab5_envelope_fails_closed() -> None:
    raw = _payload(
        _predicate("l1.mdd.noncanonical_18pct_screen", 0.211062172465126)
    )

    assessment = _assess(raw)

    assert assessment["passed"] is False
    assert assessment["predicate_results"][0]["release_passed"] is False
    assert assessment["predicate_results"][0]["exception_applied"] is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate_id", "C6-Base"),
        ("source_revision", "0" * 40),
        ("producer_run_id", 34509818019),
        ("reference_sha256", "0" * 64),
    ],
)
def test_release_assessment_rejects_mismatched_evidence_identity(
    field: str, value: object
) -> None:
    kwargs: dict[str, object] = {
        "candidate_id": "C6-Base+AB5",
        "source_revision": AB5_BASE_SOURCE_REVISION,
        "producer_run_id": 34509818018,
        "reference_sha256": AB5_REFERENCE_SHA256,
    }
    kwargs[field] = value

    with pytest.raises(ValueError, match="AB5 release evidence identity"):
        release_predicate_assessment(
            _payload(_predicate("l1.metrics.finite", False, passed=True)),
            **kwargs,
        )


def test_relax15_predicates_use_one_time_thresholds_without_blanket_waiver() -> None:
    raw = _payload(
        _predicate("l2.prefix.09_to_10_wealth", -0.114),
        _predicate("l2.initial.worst_add_one", -0.034),
        kind="c6_l2",
    )

    assessment = _assess(raw)

    assert assessment["passed"] is True
    assert [row["release_threshold"] for row in assessment["predicate_results"]] == [
        {"comparator": ">", "value": -0.115, "tolerance": 0.0},
        {"comparator": ">=", "value": -0.0345, "tolerance": 1e-12},
    ]
    assert all(
        row["exception_applied"] is None
        for row in assessment["predicate_results"]
    )


def test_non_relaxed_integrity_failure_remains_failed() -> None:
    assessment = _assess(
        _payload(_predicate("l1.metrics.finite", False, passed=False))
    )

    assert assessment["passed"] is False
    assert assessment["predicate_results"][0]["release_passed"] is False


def test_assessment_validation_detects_tampering() -> None:
    raw = _payload(_predicate("l1.metrics.finite", True, passed=True))
    assessment = _assess(raw)
    assessment["predicate_results"][0]["release_passed"] = False

    with pytest.raises(ValueError, match="differs from recomputed"):
        validate_release_predicate_assessment(assessment, raw)


def test_predicate_payload_validates_attached_release_assessment() -> None:
    raw = _payload(_predicate("l1.metrics.finite", True, passed=True))
    payload = {**raw, "release_acceptance": _assess(raw)}

    validate_attached_release_assessment(payload)

    payload["diagnostic_predicates"][0]["passed"] = False
    with pytest.raises(ValueError, match="differs from recomputed"):
        validate_attached_release_assessment(payload)
