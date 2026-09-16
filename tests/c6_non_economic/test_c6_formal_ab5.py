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
from quantfusion.research.c6_runtime import (
    c6_diagnostic_engine_config,
    runtime_policy_for_intervention,
    validate_c6_diagnostic_request,
)


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
        assert validate_c6_diagnostic_request(request) == request
        assert c6_diagnostic_engine_config({}, request).get(
            "account_risk_budget_enabled", False
        ) is False
    for intervention in ("C6_BASE_AB5", "C6_BASE_AB5_PLUS_S"):
        request = _request(intervention)
        assert validate_c6_diagnostic_request(request) == request
        assert c6_diagnostic_engine_config({}, request)[
            "account_risk_budget_enabled"
        ] is True
    with pytest.raises(ValueError, match="own evidence identity"):
        c6_diagnostic_engine_config(
            {**ordinary, "account_risk_budget_enabled": True},
            _request("C6_BASE"),
        )


def test_ab5_variants_keep_full_base_and_s_feature_sets() -> None:
    for intervention, expected in (
        ("C6_BASE_AB5", {"F0", "F1", "U"}),
        ("C6_BASE_AB5_PLUS_S", {"F0", "F1", "U", "S"}),
    ):
        policy = runtime_policy_for_intervention(intervention)
        enabled = {
            name
            for name, value in {
                "F0": policy.state_local_books,
                "F1": policy.block_retained_defensive_rebuy,
                "U": policy.use_fixed_reference_scores,
                "S": policy.overlay_s_enabled,
            }.items()
            if value
        }
        assert enabled == expected


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


def test_current_ab5_witness_reconciles_metrics_and_causal_budget_orders() -> None:
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
    # C6 labels select interventions, not a checkout of historical source.
    # PR101 changed holding/risk allocation; check today's path independently.
    values = [row["equity"] for row in result["equity_series"]]
    peak = values[0]
    worst = 0.
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.)
    assert result["official_metrics"]["max_drawdown"] == pytest.approx(worst)
    assert result["official_metrics"]["total_return"] == pytest.approx(values[-1] / 2_000_000 - 1.)
    assert result["official_metrics"]["total_trades"] == len(result["fills"])
    assert budget_orders
    assert budget_orders[0]["decision_timestamp"] == "2025-09-04"
    for order in budget_orders:
        assert order["side"] == "SELL"
        assert 0 <= order["filled_shares"] <= order["authorized_shares"]
        if order["filled_shares"]:
            assert order["execution_timestamp"] > order["decision_timestamp"]



def test_formal_runner_defaults_native_and_retains_explicit_historical_identity() -> None:
    parser = stress.build_argument_parser()
    args = parser.parse_args(
        ["--source-revision", "a" * 40, "--candidate-id", "C6-Base+AB5"]
    )
    assert args.candidate_id == "C6-Base+AB5"
    default = parser.parse_args(["--source-revision", "a" * 40])
    assert default.candidate_id == "native-default"


def test_formal_checkpoint_signature_binds_actual_candidate(tmp_path) -> None:
    from quantfusion.application import stress_artifacts

    scenario = stress_scenarios._multi_seed_scenarios(
        random_samples=1, permutation_samples=1, seeds=(20260807,)
    )[0]
    market, regime = tmp_path / 'market', tmp_path / 'regime'
    market.mkdir()
    regime.mkdir()
    kwargs = {'source_revision': 'a' * 40}
    ordinary = stress_artifacts._build_provenance(
        [scenario], market, regime, candidate_id='C6-Base', **kwargs
    )
    ab5 = stress_artifacts._build_provenance(
        [scenario], market, regime, candidate_id='C6-Base+AB5', **kwargs
    )
    assert ab5['candidate_id'] == 'C6-Base+AB5'
    assert ordinary['run_signature'] != ab5['run_signature']
    assert ordinary['source_fingerprint'] == ab5['source_fingerprint']
    assert ordinary['data_fingerprint'] == ab5['data_fingerprint']
    assert ordinary['scenario_signature'] == ab5['scenario_signature']
    checkpoint = {'signature': ordinary['run_signature'], 'provenance': ordinary,
                  'results': [], 'completed': 0, 'scenario_count': 1}
    with pytest.raises(ValueError, match='signature changed'):
        stress_artifacts._validated_checkpoint(
            checkpoint, [scenario], signature=ab5['run_signature'],
            provenance=ab5, diagnostic_selection=None,
        )
    assert stress_artifacts._run_signature(
        [scenario], market, regime, candidate_id='C6-Base+AB5', **kwargs
    ) == ab5['run_signature']


def test_formal_provenance_rejects_unknown_candidate(tmp_path) -> None:
    from quantfusion.application import stress_artifacts

    with pytest.raises(ContractError, match='candidate'):
        stress_artifacts._build_provenance(
            [], tmp_path, tmp_path, source_revision='a' * 40,
            candidate_id='C6-Base+AB5-typo',
        )
