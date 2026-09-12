"""Fail-closed boundaries for formal C6 publication and accepted incumbents."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from quantfusion.application import stress_artifacts, stress_metrics, stress_scenarios
from quantfusion.application import c6_release_acceptance as release
from quantfusion.application.c6_release_acceptance import AB5_CANDIDATE_ID
from quantfusion.config.paths import MARKET_DATA_DIR, PROJECT_ROOT, REGIME_DATA_DIR


def _formal_candidate(*, ab5: bool = False) -> tuple[
    list[dict[str, object]], dict[str, object], dict[str, object], dict[str, object]
]:
    scenarios = stress_scenarios._multi_seed_scenarios(
        random_samples=50,
        permutation_samples=50,
        seeds=(20260807, 20260817, 20260827),
    )
    reference = json.loads(
        (
            PROJECT_ROOT
            / "artifacts/validation/candidates/stress-86fd22448b9aad9d5e6194c0c065c40d56d7bddd-rejected.json"
        ).read_text(encoding="utf-8")
    )
    total_return = max(float(row["total_return"]) for row in reference["results"]) + 1.0
    rows = [
        {
            **scenario,
            "symbol_count": len(scenario["symbols"]),
            "total_return": total_return,
            "max_drawdown": -0.10,
            "sharpe": 1.0,
            "calmar": 1.0,
            "total_trades": 10,
            "sleeve_fill_count": 10,
            "date_symbol_side_count": 10,
            "max_concurrent_symbols": len(scenario["symbols"]),
            "terminal_risk_lock": False,
            "reason_attribution": {
                key: 10 if key == "initial_entry" else 0
                for key in stress_metrics.ATTRIBUTION_CATEGORIES
            },
            "deployment_policy": "production_daily_replay",
        }
        for scenario in scenarios
    ]
    provenance = stress_artifacts._build_provenance(
        scenarios,
        MARKET_DATA_DIR,
        REGIME_DATA_DIR,
        source_revision="a" * 40,
        candidate_id=AB5_CANDIDATE_ID if ab5 else "C6-Base",
    )
    prefix_rows = [deepcopy(row) for row in rows if row["scenario_type"] == "prefix"]
    prefix = {**provenance, "results": prefix_rows}
    universe = {
        **provenance,
        "results": rows,
        "scenario_count": len(scenarios),
        "trade_count_semantics": stress_metrics.TRADE_COUNT_SEMANTICS,
        "seeds": list(stress_scenarios.DEFAULT_SEEDS),
        "absolute_hard_gates": stress_metrics._absolute_hard_gates(rows),
        "retained_robustness_hard_gates": stress_metrics._retained_robustness_hard_gates(rows),
        "robustness_diagnostics": stress_metrics._robustness_diagnostics(rows),
        "promotion_gates": stress_metrics._promotion_gates(rows, None),
        "initial_baseline_gates": stress_metrics._initial_baseline_gates(rows, reference if ab5 else None),
    }
    return scenarios, provenance, prefix, universe


def test_publish_candidate_rejects_prefix_universe_record_disagreement() -> None:
    scenarios, provenance, prefix, universe = _formal_candidate()
    prefix["results"][0]["total_return"] += 1.0

    with pytest.raises(ValueError, match="prefix.*universe|universe.*prefix"):
        stress_artifacts._validate_publish_candidate(
            prefix,
            universe,
            scenarios=scenarios,
            provenance=provenance,
            incumbent=None,
            initial_baseline_reference=None,
        )


def _minimal_incumbent(candidate_id: str) -> dict[str, object]:
    return {
        "stress_contract_version": stress_metrics.STRESS_CONTRACT_VERSION,
        "trade_count_semantics": stress_metrics.TRADE_COUNT_SEMANTICS,
        "acceptance_status": "accepted",
        "canonical": True,
        "baseline_kind": "initial_current_contract",
        "candidate_id": candidate_id,
        "results": [{"scenario_id": "prefix-01", "scenario_type": "prefix"}],
    }


def test_ab5_incumbent_requires_valid_release_assessment(tmp_path) -> None:
    path = tmp_path / "incumbent.json"
    path.write_text(json.dumps(_minimal_incumbent(AB5_CANDIDATE_ID)), encoding="utf-8")

    with pytest.raises(ValueError, match="release assessment|release_acceptance"):
        stress_artifacts._load_incumbent(path)

    ordinary = _minimal_incumbent("C6-Base")
    path.write_text(json.dumps(ordinary), encoding="utf-8")
    assert stress_artifacts._load_incumbent(path) == ordinary


def test_consistent_prefix_projection_accepts_independent_order() -> None:
    scenarios, provenance, prefix, universe = _formal_candidate()
    for rows in (prefix["results"], list(reversed(prefix["results"]))):
        stress_artifacts._validate_publish_candidate(
            {**prefix, "results": rows}, universe, scenarios=scenarios,
            provenance=provenance, incumbent=None, initial_baseline_reference=None,
        )


def _accepted_ab5():
    _, _, _, native = _formal_candidate(ab5=True)
    source = native["source_revision"]
    evidence = {
        "assessment": release.release_formal_assessment(native),
        "source_binding": {
            "kind": "ab5_economic_dependency_equivalence",
            "execution_source_revision": source,
            "execution_tree": "b" * 40,
            "base_source_revision": release.AB5_BASE_SOURCE_REVISION,
            "release_plumbing_paths": [],
            "whole_tree_A": None,
            "candidate_id": AB5_CANDIDATE_ID,
            "base_producer_run_id": release.AB5_BASE_PRODUCER_RUN_ID,
            "reference_sha256": release.AB5_REFERENCE_SHA256,
            "data_fingerprint": release.AB5_DATA_FINGERPRINT,
        },
        "L2": {
            "execution_source_revision": source,
            "l2_scenario_count": 77,
            "l2_assessment_id": "c" * 64,
            "l2_evidence_sha256": "d" * 64,
            "selection_id": "e" * 64,
            "economic_producer": deepcopy(release.AB5_L2_REUSE),
        },
    }
    assert evidence["assessment"]["passed"] is True
    return {**native, "release_acceptance": evidence, "acceptance_status": "accepted",
            "canonical": True, "baseline_kind": "initial_current_contract"}


def _read_incumbent(tmp_path, payload):
    path = tmp_path / "incumbent.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return stress_artifacts._load_incumbent(path)


def test_ab5_incumbent_accepts_valid_historical_source_without_checkout_binding(tmp_path):
    import subprocess

    payload = _accepted_ab5()
    assert payload["source_revision"] != subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True).strip()
    assert _read_incumbent(tmp_path, payload) == payload


@pytest.mark.parametrize("damage", [
    "missing", "rejected_assessment", "assessment_hash", "metric", "source",
    "source_binding", "reference_binding", "l2_binding", "l2_missing", "candidate",
    "native_gate_forged", "unwaived",
])
def test_ab5_incumbent_rejects_corruption_and_unwaived_failures(tmp_path, damage):
    payload = _accepted_ab5()
    attachment = payload["release_acceptance"]
    if damage == "missing":
        del payload["release_acceptance"]
    elif damage == "rejected_assessment":
        attachment["assessment"]["passed"] = False
    elif damage == "assessment_hash":
        attachment["assessment"]["assessment_id"] = "0" * 64
    elif damage == "metric":
        payload["results"][0]["total_return"] += 1
    elif damage == "source":
        payload["source_revision"] = "0" * 40
    elif damage == "source_binding":
        attachment["source_binding"]["execution_source_revision"] = "0" * 40
    elif damage == "reference_binding":
        attachment["source_binding"]["reference_sha256"] = "0" * 64
    elif damage == "l2_binding":
        attachment["L2"]["execution_source_revision"] = "0" * 40
    elif damage == "l2_missing":
        del attachment["L2"]
    elif damage == "candidate":
        payload["candidate_id"] = "C6-Base"
    else:
        if damage == "native_gate_forged":
            payload["absolute_hard_gates"]["observed"]["all_worst_drawdown"] = -.15
        else:
            payload["absolute_hard_gates"]["observed"]["all_worst_drawdown"] = -.22
        native = {key: value for key, value in payload.items() if key not in {
            "release_acceptance", "acceptance_status", "canonical", "baseline_kind"}}
        attachment["assessment"] = release.release_formal_assessment(native)
    with pytest.raises(ValueError):
        _read_incumbent(tmp_path, payload)


def test_prefix_disagreement_is_rejected_before_any_retention_write(tmp_path, monkeypatch):
    scenarios, provenance, prefix, universe = _formal_candidate()
    prefix["results"][0]["sharpe"] += 1
    monkeypatch.setattr(stress_artifacts, "VALIDATION_ARTIFACT_DIR", tmp_path)
    with pytest.raises(ValueError, match="prefix.*universe|universe.*prefix"):
        stress_artifacts._publish_formal_artifacts(
            prefix, universe, scenarios=scenarios, provenance=provenance,
            incumbent=None, formal_plan_complete=True,
        )
    assert not list(tmp_path.iterdir())
