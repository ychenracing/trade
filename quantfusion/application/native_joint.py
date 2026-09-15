"""Source-bound current acceptance with immutable historical comparisons."""
from __future__ import annotations

from typing import Any, Mapping

from quantfusion.application.c6_contract import canonical_payload_hash
from quantfusion.config.paths import PROJECT_ROOT

CANDIDATE_ID = "native-default"
ORIGINAL_REFERENCE_PATH = PROJECT_ROOT / "artifacts/validation/candidates/stress-86fd22448b9aad9d5e6194c0c065c40d56d7bddd-rejected.json"
ORIGINAL_REFERENCE_PAYLOAD_SHA256 = "a08fb77de1f840bbb0d4adecb907f559ecead7dbea3c654761042465ff9bd5f7"
GATE_FAMILIES = (
    "absolute_hard_gates", "retained_robustness_hard_gates",
    "initial_baseline_gates", "promotion_gates",
)
COMPARABLE_FIELDS = (
    "stress_contract_version", "data_fingerprint", "scenario_signature",
    "scenario_count", "start_date", "end_date", "initial_capital", "engine",
    "deployment_policy",
)


def load_original_reference() -> dict[str, Any]:
    """Load the immutable wealth denominator, not the accepted incumbent."""
    from quantfusion.application.stress_artifacts import _load_initial_baseline_reference

    reference = _load_initial_baseline_reference(ORIGINAL_REFERENCE_PATH)
    if canonical_payload_hash(reference) != ORIGINAL_REFERENCE_PAYLOAD_SHA256:
        raise ValueError("Native original wealth reference content changed")
    return reference


def validate_references(
    provenance: Mapping[str, Any], original: Mapping[str, Any] | None,
    incumbent: Mapping[str, Any] | None,
) -> None:
    """Require two separately identified, comparable references for promotion."""
    if original is None or canonical_payload_hash(original) != ORIGINAL_REFERENCE_PAYLOAD_SHA256:
        raise ValueError("Native promotion requires the exact original wealth reference")
    if incumbent is None:
        raise ValueError("Native promotion requires an accepted incumbent")
    if incumbent.get("acceptance_status") != "accepted" or incumbent.get("canonical") is not True:
        raise ValueError("Native promotion requires an accepted canonical incumbent")
    for field in COMPARABLE_FIELDS:
        if any(reference.get(field) != provenance.get(field) for reference in (original, incumbent)):
            raise ValueError(f"Native reference comparison changed: {field}")


def receipt(
    artifact: Mapping[str, Any], original: Mapping[str, Any],
    incumbent: Mapping[str, Any],
) -> dict[str, Any]:
    """Describe recomputed economics; this is not a qualification/CI receipt."""
    checks = {name: artifact[name]["passed"] is True for name in GATE_FAMILIES}
    return {
        "kind": "native_joint_economic_acceptance", "candidate_id": CANDIDATE_ID,
        "economic_contract": artifact["economic_contract"],
        "original_contract_passed": all(
            gate["passed"] for gate in artifact["original_contract_assessment"].values()
        ),
        "source_revision": artifact["source_revision"],
        "source_fingerprint": artifact["source_fingerprint"],
        "data_fingerprint": artifact["data_fingerprint"],
        "scenario_signature": artifact["scenario_signature"],
        "original_reference_payload_sha256": canonical_payload_hash(original),
        "incumbent_payload_sha256": canonical_payload_hash(incumbent),
        "waivers_used": False, "gate_families": checks, "passed": all(checks.values()),
    }
