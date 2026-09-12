"""Fail-closed boundaries for formal C6 publication and accepted incumbents."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from quantfusion.application import stress_artifacts, stress_metrics, stress_scenarios
from quantfusion.application.c6_release_acceptance import AB5_CANDIDATE_ID
from quantfusion.config.paths import MARKET_DATA_DIR, PROJECT_ROOT, REGIME_DATA_DIR


def _formal_candidate() -> tuple[
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
        candidate_id="C6-Base",
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
        "initial_baseline_gates": stress_metrics._initial_baseline_gates(rows, reference),
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
