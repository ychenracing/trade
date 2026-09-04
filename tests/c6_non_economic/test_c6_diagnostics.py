"""Non-economic contracts for the frozen C6 diagnostic surface."""
from __future__ import annotations
# ruff: noqa: E501
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from quantfusion.application import c6_diagnostics
from quantfusion.engine.replay import ProductionReplayEngine
from quantfusion.engine.universe import BacktestEngine
from quantfusion.risk.overlay.policy import CrossMarketOverlay
def _manifest(ids: list[str]) -> dict[str, object]:
    encoded = "".join(f"{item}\n" for item in ids).encode()
    return {
        "count": len(ids),
        "unique_count": len(ids),
        "sha256": __import__("hashlib").sha256(encoded).hexdigest(),
        "ids": ids,
    }
def test_manifest_identity_is_sorted_unique_and_fail_closed() -> None:
    manifest = _manifest(["a", "b"])
    assert c6_diagnostics.validate_manifest_identity(["a", "b"], manifest) == ["a", "b"]
    for ids, message in ((["b", "a"], "order"), (["a", "a"], "duplicate")):
        with pytest.raises(ValueError, match=message):
            c6_diagnostics.validate_manifest_identity(ids, manifest)
def test_official_breach_uses_the_official_running_peak() -> None:
    breach = c6_diagnostics.first_official_mdd_breach(
        [
            {"timestamp": "2026-01-01", "equity": 100.0},
            {"timestamp": "2026-01-02", "equity": 120.0},
            {"timestamp": "2026-01-03", "equity": 98.39},
        ]
    )
    assert breach is not None
    assert breach["sample_ordinal"] == 2
    assert breach["peak_owner"] == "official_running_peak"
    assert breach["drawdown"] == pytest.approx(98.39 / 120.0 - 1.0)
def test_cli_matches_the_exact_r_bound_shape() -> None:
    parser = c6_diagnostics.build_parser()
    args = parser.parse_args(
        [
            "--preregistration", "P.json", "--bindings-file", "R.json",
            "--binding-record-id", "c6.base.l1", "--source-revision", "a" * 40,
            "--output", "result.json",
        ]
    )
    assert args.binding_record_id == "c6.base.l1"
    assert args.producer_export is None and args.base_producer_export is None
    with pytest.raises(SystemExit):
        parser.parse_args(["--layer", "L1"])
def test_diagnostic_request_is_closed_and_production_signature_unchanged() -> None:
    production = inspect.signature(ProductionReplayEngine.run)
    assert {"intervention", "diagnostic_request", "trace_sink"}.isdisjoint(
        production.parameters
    )
    request = {
        "schema_version": 1, "intervention_id": "C6_BASE",
        "recording_mode": "DEFAULT", "scenario_id": "prefix-05",
        "diagnostic_noncanonical": True, "allow_publication": False,
    }
    assert ProductionReplayEngine.validate_c6_diagnostic_request(request) == request
    for key, value in (
        ("schema_version", 2), ("intervention_id", "TYPO"),
        ("recording_mode", "TRACE"), ("scenario_id", ""),
        ("diagnostic_noncanonical", False), ("allow_publication", True),
    ):
        with pytest.raises(ValueError, match=key):
            ProductionReplayEngine.validate_c6_diagnostic_request({**request, key: value})
    with pytest.raises(ValueError, match="extra"):
        ProductionReplayEngine.validate_c6_diagnostic_request({**request, "candidate": "x"})
    with pytest.raises(ValueError, match="no-drift"):
        ProductionReplayEngine.validate_c6_diagnostic_request({**request, "recording_mode": "ON", "scenario_id": "prefix-04"})
def test_ablation_map_is_exact_and_production_defaults_full_on() -> None:
    engine = object.__new__(BacktestEngine)
    assert all(engine._c6_feature_enabled(item) for item in ("F0", "F1", "U"))
    expected = {
        "BASELINE": set(), "F0_ONLY": {"F0"}, "F0_F1": {"F0", "F1"},
        "U_ONLY": {"U"}, "C6_BASE": {"F0", "F1", "U"},
        "W3_REAL_INTENTS_FIXED_REFERENCE_U": {"F0", "F1", "U"},
        "W4_FULL_BASE_PRODUCTION_POOL_RELATIVE": {"F0", "F1"},
    }
    for intervention, enabled in expected.items():
        engine._c6_diagnostic_request = {"intervention_id": intervention}
        assert {item for item in ("F0", "F1", "U") if engine._c6_feature_enabled(item)} == enabled
def test_s_counterpart_is_same_scenario_only() -> None:
    assert c6_diagnostics.base_counterpart_id("C6-Base+S::prefix-05") == "C6-Base::prefix-05"
    with pytest.raises(ValueError, match="prefix"):
        c6_diagnostics.base_counterpart_id("C6-Base::prefix-05")


def _declining_frame() -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=5)
    return pd.DataFrame(
        {
            "open": [12.0, 11.5, 11.0, 10.0, 9.8],
            "high": [12.0, 11.5, 11.0, 10.0, 9.8],
            "low": [12.0, 11.5, 11.0, 10.0, 9.8],
            "close": [12.0, 11.5, 11.0, 10.0, 9.8],
            "volume": [1_000_000] * 5,
        },
        index=index,
    )


def test_base_observes_complete_s_evidence_without_s_action() -> None:
    frame = _declining_frame()
    positions = {
        symbol: {
            "trend": SimpleNamespace(
                shares=9_000,
                entry_price=11.0,
                highest_close_since_entry=12.0,
                entry_date="2025-12-01",
            )
        }
        for symbol in ("601869", "002384")
    }
    sleeve = SimpleNamespace(
        sleeve_name="fast",
        positions=positions,
        cash=20_000.0,
        _remaining_adv_capacity=lambda *args: 100_000,
        _opening_limit_state=lambda *args: None,
    )
    risk_symbols = (
        "300308", "300502", "688008", "688072", "002409", "688256",
        "601869", "002384",
    )
    state = SimpleNamespace(
        sleeve=sleeve,
        data_map={symbol: frame for symbol in ("601869", "002384")},
        all_dates=list(frame.index),
        pending=[],
    )
    overlay = CrossMarketOverlay(
        risk_frames={symbol: frame for symbol in risk_symbols}
    )
    overlay._risk_level = 1
    overlay._assets_history = [220_000.0, 215_000.0, 210_000.0, 200_000.0]

    evidence = overlay.observe_c6_s_evidence(
        [state],
        pd.Timestamp("2026-01-04"),
        3,
        {"601869": 10.0, "002384": 10.0},
        200_000.0,
        0.05,
        lambda symbol: 0.0 if symbol == "601869" else 1.0,
    )

    definitions = json.loads(
        Path("artifacts/diagnostics/c6-preregistration.json").read_text()
    )["schema_catalog"]["definitions"]
    assert set(evidence) == set(definitions["s_evidence"]["exact_keys"])
    assert set(evidence["coverage"]) == set(
        definitions["qualification_coverage"]["exact_keys"]
    )
    assert set(evidence["leave_held_components_out"]) == set(
        definitions["qualification_leave_held"]["exact_keys"]
    )
    assert set(evidence["fillability"]) == set(
        definitions["qualification_fillability"]["exact_keys"]
    )
    assert set(evidence["shortfall"]) == set(
        definitions["qualification_shortfall"]["exact_keys"]
    )
    assert state.pending == []
    assert evidence["worst_cluster"] == "optical"
    assert evidence["stressed_cluster_set"] == sorted(
        evidence["stressed_cluster_set"]
    )
    assert evidence["coverage"]["coverage_passed"] is True
    assert evidence["leave_held_components_out"]["passed"] is True
    assert evidence["legacy_gate_open"] is False
    assert evidence["early_sell_required"] is True
    assert evidence["planned_shares"] == 2_000
    assert evidence["executable_lot_shares"] == 2_000
    assert evidence["scheduled_execution_batch"]["execution_open"] == "2026-01-05"

    overlay._c6_diagnostic_evidence_enabled = True
    overlay._risk_level_day = 3
    overlay._assets_history = [220_000.0, 215_000.0, 210_000.0]
    actions = overlay.evaluate(
        [state],
        pd.Timestamp("2026-01-04"),
        3,
        200_000.0,
        210_526.31578947368,
        lambda symbol: 0.0 if symbol == "601869" else 1.0,
    )
    assert actions == ()
    assert overlay.c6_s_evidence["early_sell_required"] is True
    assert state.pending == []


def test_s_evidence_is_finalized_against_the_official_breach_sample() -> None:
    evidence = c6_diagnostics._empty_s_evidence()
    evidence.update(
        {
            "first_causal_stressed_cluster_close": "2026-01-04",
            "first_early_sell_required_close": "2026-01-04",
            "scheduled_execution_batch": {
                "decision_close": "2026-01-04",
                "execution_open": "2026-01-05",
                "calendar_ordinal": 4,
            },
            "pre_trade_open_drawdown": -0.10,
        }
    )
    finalized = c6_diagnostics.finalize_s_evidence(
        evidence,
        [
            {"timestamp": "2026-01-01", "equity": 100.0},
            {"timestamp": "2026-01-04", "equity": 95.0},
            {"timestamp": "2026-01-05", "equity": 79.0},
        ],
        ["2026-01-01", "2026-01-04", "2026-01-05"],
    )
    assert finalized["lead_batch_count"] == 1
    assert finalized["official_sample_relation"] == "OPEN_MARK_GAP_NOT_OFFICIAL_SAMPLE"
    assert finalized["identical_valuation_instant_proven"] is False
