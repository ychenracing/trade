"""Non-economic identity checks for the sealed C6 baseline rebuild."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from quantfusion.application import stress_artifacts, stress_scenarios
from quantfusion.config.paths import MARKET_DATA_DIR, PROJECT_ROOT, REGIME_DATA_DIR


PREREG_V1 = PROJECT_ROOT / "artifacts/diagnostics/c6-baseline-rebuild-preregistration.json"
PREREG_V2 = PROJECT_ROOT / "artifacts/diagnostics/c6-baseline-rebuild-preregistration-v2.json"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_strict(path: Path) -> dict[str, Any]:
    payload = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )
    assert isinstance(payload, dict)
    return payload


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _text_sha256(ids: list[str]) -> str:
    return hashlib.sha256(("\n".join(ids) + "\n").encode("utf-8")).hexdigest()


def _formal_plan() -> list[dict[str, Any]]:
    return stress_scenarios._multi_seed_scenarios(
        random_samples=stress_scenarios.DEFAULT_RANDOM_SAMPLES,
        permutation_samples=stress_scenarios.DEFAULT_PERMUTATION_SAMPLES,
        seeds=stress_scenarios.DEFAULT_SEEDS,
    )


def test_preregistration_supersession_changes_only_the_test_path() -> None:
    v1 = _load_strict(PREREG_V1)
    v2 = _load_strict(PREREG_V2)
    assert _sha256(PREREG_V1) == v2["supersedes"]["sha256"]
    assert v2["unchanged_frozen_economic_contract"]["economic_contract_changed"] is False
    expected = v1["expected_outputs"]
    frozen = v2["unchanged_frozen_economic_contract"]
    for key in (
        "failure_count",
        "failure_ids_text_sha256",
        "boundary_count",
        "boundary_ids_text_sha256",
        "control_count",
        "control_ids_text_sha256",
        "cohort_count",
        "cohort_text_sha256",
    ):
        assert frozen[key] == expected[key]
    assert frozen["transition_reference_sha256"] == v1["transition_reference"]["sha256"]
    assert v2["resolution"]["authorized_writes_after_this_commit"] == [
        "artifacts/diagnostics/c6-current-baseline-cohort.txt",
        "artifacts/diagnostics/c6-baseline-rebuild.json",
        "tests/c6_non_economic/test_baseline_rebuild.py",
        "README.md",
        "docs/ARCHITECTURE.md",
        "docs/VALIDATION.md",
    ]


def test_transition_reference_matches_frozen_formal_identity() -> None:
    prereg = _load_strict(PREREG_V1)
    historical = prereg["historical_cohort"]
    historical_cohort_path = PROJECT_ROOT / historical["path"]
    historical_analysis_path = PROJECT_ROOT / historical["analysis_path"]
    reference_path = PROJECT_ROOT / prereg["transition_reference"]["path"]
    assert _sha256(historical_cohort_path) == historical["sha256"]
    assert _sha256(historical_analysis_path) == historical["analysis_sha256"]
    historical_ids = historical_cohort_path.read_text(encoding="utf-8").splitlines()
    assert len(historical_ids) == len(set(historical_ids)) == 281
    historical_analysis = _load_strict(historical_analysis_path)
    for field in (
        "source_revision",
        "source_fingerprint",
        "data_fingerprint",
        "scenario_signature",
    ):
        assert historical_analysis[field] == historical[field]
    assert _sha256(reference_path) == prereg["transition_reference"]["sha256"]
    reference = stress_artifacts._load_initial_baseline_reference(reference_path)
    plan = _formal_plan()
    by_id = {str(item["scenario_id"]): item for item in reference["results"]}
    assert len(plan) == len(by_id) == 958
    assert set(by_id) == {str(item["scenario_id"]) for item in plan}
    assert all(
        by_id[str(item["scenario_id"])]["scenario_type"] == item["scenario_type"]
        for item in plan
    )
    identity = prereg["current_checkout_identity"]
    assert stress_artifacts._tree_fingerprint(
        stress_artifacts._data_files(MARKET_DATA_DIR, REGIME_DATA_DIR)
    ) == identity["data_fingerprint"]
    assert stress_scenarios._scenario_signature(plan) == identity["scenario_signature"]
    assert reference["source_fingerprint"] == identity["source_fingerprint"]
    assert reference["data_fingerprint"] == identity["data_fingerprint"]
    assert reference["scenario_signature"] == identity["scenario_signature"]
    assert reference["artifact_status"] == "current_candidate"
    assert reference["acceptance_status"] == "rejected"
    assert reference["canonical"] is False


def test_rebuilt_evidence_matches_the_frozen_derivation() -> None:
    prereg = _load_strict(PREREG_V1)
    evidence_path = PROJECT_ROOT / prereg["expected_outputs"]["evidence_path"]
    cohort_path = PROJECT_ROOT / prereg["expected_outputs"]["cohort_path"]
    evidence = _load_strict(evidence_path)
    cohort_ids = cohort_path.read_text(encoding="utf-8").splitlines()
    reference = _load_strict(PROJECT_ROOT / prereg["transition_reference"]["path"])
    by_id = {str(item["scenario_id"]): item for item in reference["results"]}
    ordered_ids = [str(item["scenario_id"]) for item in _formal_plan()]
    derivation = prereg["cohort_derivation"]
    tolerance = float(derivation["official_numeric_tolerance"])
    limit = float(derivation["official_max_drawdown"])
    floor = float(derivation["boundary_floor"])
    failures = sorted(
        scenario_id
        for scenario_id in ordered_ids
        if abs(float(by_id[scenario_id]["max_drawdown"])) > limit + tolerance
    )
    boundaries = sorted(
        scenario_id
        for scenario_id in ordered_ids
        if floor
        <= abs(float(by_id[scenario_id]["max_drawdown"]))
        <= limit + tolerance
    )
    eligible_controls = [
        scenario_id
        for scenario_id in ordered_ids
        if abs(float(by_id[scenario_id]["max_drawdown"])) < floor
    ]
    controls = eligible_controls[: int(derivation["control_count"])]
    expected_cohort = sorted(set(failures) | set(boundaries) | set(controls))
    expected = prereg["expected_outputs"]
    assert controls == derivation["control_ids_in_canonical_order"]
    assert len(expected_cohort) == len(failures) + len(boundaries) + len(controls)
    assert len(failures) == expected["failure_count"]
    assert _text_sha256(failures) == expected["failure_ids_text_sha256"]
    assert len(boundaries) == expected["boundary_count"]
    assert _text_sha256(boundaries) == expected["boundary_ids_text_sha256"]
    assert len(controls) == expected["control_count"]
    assert _text_sha256(sorted(controls)) == expected["control_ids_text_sha256"]
    assert len(eligible_controls) == expected["safe_eligible_count"]
    assert cohort_ids == expected_cohort
    assert _sha256(cohort_path) == expected["cohort_text_sha256"]
    assert evidence == {
        "schema_version": 1,
        "kind": "c6_current_contract_baseline_rebuild",
        "status": "trusted_transition_reference_compatible",
        "preregistration": {
            "v1_commit": "4cd65fe12fde6bb404fecc7b872123205dab2367",
            "v1_sha256": _sha256(PREREG_V1),
            "v2_commit": "19ace3753f33d04fc1864e11a4d68a55dcb4686d",
            "v2_sha256": _sha256(PREREG_V2),
        },
        "historical_cohort": prereg["historical_cohort"],
        "transition_reference": prereg["transition_reference"],
        "current_checkout_identity": prereg["current_checkout_identity"],
        "compatibility": {
            "official_loader": "pass",
            "exact_scenario_ids_and_families": True,
            "historical_cohort_sha256": "match",
            "historical_cohort_count_and_uniqueness": "281_of_281",
            "historical_analysis_sha256": "match",
            "historical_analysis_provenance": "match",
            "transition_reference_sha256": "match",
            "source_fingerprint": "match",
            "data_fingerprint": "match",
            "scenario_signature": "match",
        },
        "derivation": {
            "failure_ids": failures,
            "failure_count": len(failures),
            "failure_ids_text_sha256": _text_sha256(failures),
            "boundary_ids": boundaries,
            "boundary_count": len(boundaries),
            "boundary_ids_text_sha256": _text_sha256(boundaries),
            "control_ids_in_canonical_order": controls,
            "control_count": len(controls),
            "control_ids_text_sha256": _text_sha256(sorted(controls)),
            "safe_eligible_count": len(eligible_controls),
            "cohort_count": len(expected_cohort),
            "cohort_text_sha256": _text_sha256(expected_cohort),
        },
        "outputs": {
            "cohort_path": prereg["expected_outputs"]["cohort_path"],
            "cohort_order": "lexicographic_scenario_id",
        },
        "publication": {
            "new_backtest_performed": False,
            "economic_source_changed": False,
            "data_or_scenario_changed": False,
            "historical_artifact_changed": False,
            "accepted": False,
            "canonical": False,
            "use": "diagnostic_transition_reference_for_a_new_c6_checkpoint_and_preregistration",
        },
    }


def test_rebuilt_baseline_identity_is_documented_consistently() -> None:
    marker = (
        '<!-- C6_BASELINE_REBUILD_META: {"reference_scenarios": 958, '
        '"cohort_scenarios": 765, "failures": 649, "boundaries": 110, '
        '"controls": 6} -->'
    )
    statement = (
        "C6 基线兼容性重建保留旧 281 场景证据不变，并从唯一完整的当前 17 股/958 "
        "场景 rejected transition reference 确定性派生 765 个场景：649 个失败、110 "
        "个 17%—18% 边界和 6 个对照；未运行新回测，也未建立 accepted canonical 基线。"
    )
    for relative in ("README.md", "docs/ARCHITECTURE.md", "docs/VALIDATION.md"):
        text = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        assert marker in text
        assert statement in text
