from __future__ import annotations

from copy import deepcopy

import pytest

from quantfusion.application import c6_diagnostics, stress
from quantfusion.application import stress_scenarios
from quantfusion.application.c6_contract import (
    ContractError,
    candidate_spec,
    load_preregistration,
)
from quantfusion.application.c6_parallel_l1 import core_l1_tasks
from quantfusion.config.engine import default_engine_config
from quantfusion.engine.replay import (
    ProductionReplayEngine,
    c6_diagnostic_engine_config,
)
from quantfusion.engine.universe import BacktestEngine


def _request(intervention_id: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "intervention_id": intervention_id,
        "recording_mode": "DEFAULT",
        "scenario_id": "random-20260807-03-004",
        "diagnostic_noncanonical": True,
        "allow_publication": False,
    }


def test_candidate_identity_is_distinct_and_maps_to_existing_sources() -> None:
    assert candidate_spec("C6-Base") == {
        "role": "base", "source_alias": "I_B",
        "intervention_id": "C6_BASE", "account_risk_budget_enabled": False,
    }
    assert candidate_spec("C6-Base+S")["source_alias"] == "I_S"
    assert candidate_spec("C6-Base+AB5") == {
        "role": "base", "source_alias": "I_B",
        "intervention_id": "C6_BASE_AB5", "account_risk_budget_enabled": True,
    }
    assert candidate_spec("C6-Base+AB5+S") == {
        "role": "s", "source_alias": "I_S",
        "intervention_id": "C6_BASE_AB5_PLUS_S", "account_risk_budget_enabled": True,
    }
    with pytest.raises(ContractError, match="candidate"):
        candidate_spec("C6-Base+AB5-typo")


def test_only_ab5_interventions_enable_the_existing_budget_path() -> None:
    ordinary = default_engine_config()
    for intervention in ("C6_BASE", "C6_BASE_PLUS_S"):
        request = _request(intervention)
        assert ProductionReplayEngine.validate_c6_diagnostic_request(request) == request
        assert c6_diagnostic_engine_config({}, request).get(
            "account_risk_budget_enabled", False
        ) is False
    for intervention in ("C6_BASE_AB5", "C6_BASE_AB5_PLUS_S"):
        request = _request(intervention)
        assert ProductionReplayEngine.validate_c6_diagnostic_request(request) == request
        assert c6_diagnostic_engine_config({}, request)[
            "account_risk_budget_enabled"
        ] is True
    with pytest.raises(ValueError, match="own evidence identity"):
        c6_diagnostic_engine_config(
            {**ordinary, "account_risk_budget_enabled": True},
            _request("C6_BASE"),
        )


def test_ab5_variants_keep_full_base_and_s_feature_sets() -> None:
    engine = object.__new__(BacktestEngine)
    for intervention, expected in (
        ("C6_BASE_AB5", {"F0", "F1", "U"}),
        ("C6_BASE_AB5_PLUS_S", {"F0", "F1", "U", "S"}),
    ):
        engine._c6_diagnostic_request = {"intervention_id": intervention}
        assert {
            feature
            for feature in ("F0", "F1", "U", "S")
            if engine._c6_feature_enabled(feature)
        } == expected


def test_ab6_has_a_noncanonical_diagnostic_identity_not_a_formal_candidate() -> None:
    request = _request("C6_BASE_AB6")
    assert ProductionReplayEngine.validate_c6_diagnostic_request(request) == request
    assert c6_diagnostic_engine_config({}, request)[
        "account_risk_budget_enabled"
    ] is True
    engine = object.__new__(BacktestEngine)
    engine._c6_diagnostic_request = request
    assert {
        feature
        for feature in ("F0", "F1", "U", "S")
        if engine._c6_feature_enabled(feature)
    } == {"F0", "F1", "U"}
    with pytest.raises(ContractError, match="candidate"):
        candidate_spec("C6-Base+AB6")


def test_ab7_has_a_noncanonical_diagnostic_identity_not_a_formal_candidate() -> None:
    request = _request("C6_BASE_AB7")
    assert ProductionReplayEngine.validate_c6_diagnostic_request(request) == request
    assert c6_diagnostic_engine_config({}, request)[
        "account_risk_budget_enabled"
    ] is True
    with pytest.raises(ContractError, match="candidate"):
        candidate_spec("C6-Base+AB7")


def test_formal_ab5_l1_uses_binding_candidate_without_changing_scenarios() -> None:
    prereg = load_preregistration(
        "artifacts/diagnostics/c6-preregistration.json"
    )
    ab5 = deepcopy(prereg)
    base_manifest = ab5["scenario_manifests"]["L1_BASE_EVALUATION_MANIFEST"]
    base_manifest["core_variant_order"][-1] = "C6-Base+AB5"
    binding = {"record_id": "c6.base.l1", "candidate_id": "C6-Base+AB5"}

    ids, tasks = core_l1_tasks(ab5, binding)

    assert len(ids) == 3825
    assert len(tasks) == 3825
    assert ids[-1] == "evaluation/C6-Base+AB5::random-20260827-15-050"
    assert tasks[-1][0] == "C6-Base+AB5"


def test_formal_ab5_witness_reproduces_locked_path_and_budget_orders() -> None:
    scenario = next(
        item
        for item in stress_scenarios._multi_seed_scenarios(
            random_samples=50,
            permutation_samples=50,
            seeds=(20260807, 20260817, 20260827),
        )
        if item["scenario_id"] == "random-20260807-03-004"
    )

    result = c6_diagnostics._l1_evaluate(
        ("C6-Base+AB5", scenario, "DEFAULT")
    )
    budget_orders = [
        row for row in result["orders"]
        if "account_budget" in str(row.get("reason", ""))
    ]

    assert result["variant_id"] == "C6-Base+AB5"
    assert result["official_metrics"]["max_drawdown"] == pytest.approx(
        -0.17796519775099098
    )
    assert result["official_metrics"]["total_return"] == pytest.approx(
        2.6051501539157518
    )
    assert len(budget_orders) == 32
    assert budget_orders[0]["decision_timestamp"] == "2025-09-04"


def test_formal_official_runner_accepts_only_explicit_candidate_identity() -> None:
    parser = stress.build_argument_parser()
    args = parser.parse_args(
        ["--source-revision", "a" * 40, "--candidate-id", "C6-Base+AB5"]
    )
    assert args.candidate_id == "C6-Base+AB5"
    legacy = parser.parse_args(["--source-revision", "a" * 40])
    assert legacy.candidate_id == "C6-Base"
