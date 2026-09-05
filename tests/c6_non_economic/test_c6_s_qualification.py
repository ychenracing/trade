"""Non-economic qualification contracts for the frozen C6 S candidate."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from quantfusion.application import c6_diagnostics, c6_s_qualification
from quantfusion.application.c6_bound_run import validate_result_payload
from quantfusion.application.c6_predicates import validate_qualification_results


def _base_payload() -> dict:
    records = []
    for index in range(765):
        scenario = f"scenario-{index:03d}"
        evidence = c6_diagnostics._empty_s_evidence()
        maximum = -0.19 if index == 0 else -0.10
        breach = "2026-01-05" if index == 0 else None
        if index == 0:
            evidence.update(
                {
                    "first_causal_stressed_cluster_close": "2026-01-03",
                    "worst_cluster": "optical",
                    "worst_cluster_weight": 0.9,
                    "stressed_cluster_set": ["optical"],
                    "first_early_sell_required_close": "2026-01-04",
                    "risk_level": 1,
                    "portfolio_fast_return": -0.05,
                    "existing_concentration_eligible": True,
                    "cluster_symbol_count": 2,
                    "legacy_gate_open": False,
                    "early_sell_required": True,
                    "scheduled_execution_batch": {
                        "decision_close": "2026-01-04",
                        "execution_open": "2026-01-05",
                        "calendar_ordinal": 4,
                    },
                    "lead_batch_count": 1,
                    "pre_trade_open_drawdown": -0.10,
                    "official_sample_relation": "OPEN_MARK_GAP_NOT_OFFICIAL_SAMPLE",
                    "planned_shares": 2_000,
                    "executable_lot_shares": 2_000,
                }
            )
            evidence["coverage"].update(
                {
                    "observed_count": 8,
                    "observed_industries": 4,
                    "decision_timestamp": "2026-01-04",
                    "latest_source_timestamp": "2026-01-04",
                    "freshness_passed": True,
                    "coverage_passed": True,
                    "unmapped_weight": 0.0,
                    "unmapped_passed": True,
                }
            )
            evidence["leave_held_components_out"].update(
                {
                    "target_cluster": "optical",
                    "removed_components": ["300308"],
                    "remaining_components": ["002409", "601869", "688008", "688072"],
                    "observed_count": 4,
                    "observed_industries": 4,
                    "recomputed_fast_return": -0.07,
                    "recomputed_declining_ratio": 0.75,
                    "recomputed_stressed_cluster_set": ["optical"],
                    "freshness_passed": True,
                    "coverage_passed": True,
                    "same_evidence_preserved": True,
                    "passed": True,
                }
            )
            evidence["fillability"].update(
                {
                    "t_plus_one_passed": True,
                    "open_available": True,
                    "not_suspended": True,
                    "not_limit_blocked": True,
                    "adv_capacity_shares": 2_000,
                    "nonzero_executable_lot": True,
                }
            )
            evidence["shortfall"] = {"shares": 0, "reason": "NONE"}
            evidence["book_fillability"] = [{
                "state_index": 0, "sleeve_name": "fast", "strategy_name": "trend", "symbol": "601869",
                "current_shares": 9000, "inventory_before_shares": 9000, "suppression_winner_reason": None,
                "planned_shares": 2000, "raw_adv_capacity_shares": 10000,
                "capacity_shares": 2000, "executable_shares": 2000, "t_plus_one_passed": True,
                "open_available": True, "not_suspended": True, "not_limit_blocked": True}]
            row = evidence["book_fillability"][0]
            evidence["queue_fillability"] = [{**{key: value for key, value in row.items()
                                                if key not in {"current_shares", "planned_shares", "suppression_winner_reason"}},
                                               "requested_shares": 2000, "is_s_proposal": True,
                                               "signal_date": "2026-01-04", "reason": "concentration_trim:cluster=optical"}]
        records.append(
            {
                "variant_id": "C6-Base",
                "scenario_id": scenario,
                "official_metrics": {"max_drawdown": maximum},
                "causal_matrix": {
                    "event_timeline": {
                        "first_official_mdd_breach": {"timestamp": breach}
                    },
                    "s_evidence": evidence,
                },
            }
        )
    return {"evaluations": records}


def _identities() -> dict:
    def implementation(character: str) -> dict:
        return {
            "commit": character * 40,
            "tree": "3" * 40,
            "required_blobs": {},
            "comparison_base_commit": "4" * 40,
            "comparison_base_tree": "5" * 40,
            "first_parent_ancestor": True,
            "merge_commit_count": 0,
            "changed_paths": [],
            "added_lines": 0,
            "deleted_lines": 0,
        }

    return {
        "base_producer_identity": {
            "artifact_full_byte_sha256": "a" * 64,
            "attempt_id": "a0",
            "binding_id": "c6.base.l1",
            "logical_run_id": "c6-v6-base-l1",
            "workflow_run_id": "123",
        },
        "P": {"commit": "b" * 40, "tree": "c" * 40, "blob": "d" * 40, "sha256": "e" * 64},
        "R_revision": "f" * 40,
        "I_B": implementation("1"),
        "I_S": implementation("2"),
    }


def test_complete_residual_qualification_recomputes_all_seven_criteria(tmp_path) -> None:
    result = c6_s_qualification.qualify_base_payload(
        _base_payload(), **_identities()
    )
    assert result["residual_ids"] == ["scenario-000"]
    assert result["all_passed"] is True
    row = result["results"][0]
    assert [item["criterion_id"] for item in row["criteria"]] == [
        f"q{index}_{suffix}"
        for index, suffix in (
            (1, "base_breach_exists"),
            (2, "causal_stress_evidence_precedes_breach"),
            (3, "dominant_cluster_is_stressed_and_over_cap"),
            (4, "complete_independent_evidence"),
            (5, "scheduled_prebreach_nonzero_lot"),
            (6, "action_strictly_precedes_breach_sample"),
            (7, "official_sample_gap_not_proven_unavoidable"),
        )
    ]
    assert all(item["passed"] for item in row["criteria"])
    from quantfusion.application.c6_bound_run import DiagnosticCheckpoint, BoundRunError
    prereg = json.loads(Path("artifacts/diagnostics/c6-preregistration.json").read_text())
    ids = ["qualification/" + row["scenario_id"]]
    path = tmp_path / "synthetic-qualified.json.gz"
    checkpoint = DiagnosticCheckpoint(path, ids, "synthetic-v1", prereg=prereg)
    checkpoint.map(c6_diagnostics._identity, [row], ids, workers=1)
    restored = DiagnosticCheckpoint(path, ids, "synthetic-v2", resume_signature="synthetic-v1", prereg=prereg)
    assert restored.map(c6_diagnostics._identity, [row], ids, workers=1)[0] == row
    invalid = deepcopy(row)
    invalid["criteria"][2]["passed"] = False
    bad = DiagnosticCheckpoint(tmp_path / "bad.gz", ids, "synthetic-bad", prereg=prereg)
    with pytest.raises(BoundRunError, match="qualification"):
        bad.map(c6_diagnostics._identity, [invalid], ids, workers=1)
    assert not bad.path.exists()
    validate_result_payload(
        result,
        {
            "canonical_payload_schema": {
                "name": "S_QUALIFICATION_RUN.output_schema"
            }
        },
        json.loads(
            Path("artifacts/diagnostics/c6-preregistration.json").read_text()
        ),
    )


def test_nested_pass_booleans_are_not_trusted() -> None:
    payload = _base_payload()
    evidence = payload["evaluations"][0]["causal_matrix"]["s_evidence"]
    evidence["coverage"]["observed_count"] = 1
    evidence["coverage"]["coverage_passed"] = True
    evidence["leave_held_components_out"]["passed"] = True
    result = c6_s_qualification.qualify_base_payload(
        payload, **_identities()
    )
    row = result["results"][0]
    q4 = row["criteria"][3]
    assert q4["passed"] is False
    assert q4["failure_reason"] == "Q4_EVIDENCE_INCOMPLETE"
    assert result["all_passed"] is False


def test_missing_early_evidence_is_a_sealed_failure_not_an_error() -> None:
    payload = _base_payload()
    payload["evaluations"][0]["causal_matrix"]["s_evidence"] = (
        c6_diagnostics._empty_s_evidence()
    )

    result = c6_s_qualification.qualify_base_payload(
        payload, **_identities()
    )

    assert result["all_passed"] is False
    row = result["results"][0]
    assert row["passed"] is False
    assert row["failure_reasons"]
    assert row["criteria"][5]["observed_values"]["execution_open"] is None


def test_base_slice_must_be_exact_unique_and_complete() -> None:
    payload = _base_payload()
    payload["evaluations"].append(deepcopy(payload["evaluations"][0]))
    try:
        c6_s_qualification.qualify_base_payload(payload, **_identities())
    except ValueError as exc:
        assert "765 unique" in str(exc)
    else:
        raise AssertionError("duplicate Base scenario was accepted")


def test_provisional_s_fillability_cannot_qualify_before_final_queue():
    payload = _base_payload()
    payload['evaluations'][0]['causal_matrix']['s_evidence']['queue_fillability'] = None
    result = c6_s_qualification.qualify_base_payload(payload, **_identities())
    assert result['all_passed'] is False
    assert result['results'][0]['criteria'][4]['passed'] is False


def test_consumer_recomputes_qualification_from_authenticated_base():
    base = _base_payload()
    qualification = c6_s_qualification.qualify_base_payload(base, **_identities())
    validate_qualification_results(qualification, base)
    mutations = [
        lambda result: result.update(all_passed=False),
        lambda result: result['residual_ids'].clear(),
        lambda result: result['results'][0]['criteria'][4]['observed_values'].update(planned_shares=1),
        lambda result: result['results'][0].update(passed=False),
    ]
    for mutate in mutations:
        forged = deepcopy(qualification)
        mutate(forged)
        with pytest.raises(ValueError, match='qualification'):
            validate_qualification_results(forged, base)
    changed = deepcopy(base)
    changed['evaluations'][1]['official_metrics']['max_drawdown'] = -.20
    with pytest.raises(ValueError, match='coverage'):
        validate_qualification_results(qualification, changed)
    changed = deepcopy(base)
    changed['evaluations'][0]['causal_matrix']['s_evidence']['coverage']['observed_count'] = 1
    with pytest.raises(ValueError, match='recomputed'):
        validate_qualification_results(qualification, changed)
