"""Non-economic contracts for the frozen C6 diagnostic surface."""
from __future__ import annotations
# ruff: noqa: E501
import inspect
import pytest
from quantfusion.application import c6_diagnostics
from quantfusion.engine.replay import ProductionReplayEngine
from quantfusion.engine.universe import BacktestEngine
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
