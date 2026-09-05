"""Native scenario structures cross real diagnostic and checkpoint boundaries."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from quantfusion.application import c6_diagnostics as diagnostic
from quantfusion.application import c6_bound_run as bound
from quantfusion.application import stress_scenarios, stress_metrics
from quantfusion.io.c6_stream import load_object, write_json


@pytest.fixture(scope="module")
def prereg():
    return json.loads(Path("artifacts/diagnostics/c6-preregistration.json").read_text())


def native_scenarios():
    return stress_scenarios._multi_seed_scenarios(
        random_samples=50, permutation_samples=50, seeds=(20260807, 20260817, 20260827))


def test_all_native_metadata_uses_actual_ordered_symbols(prereg):
    ids = set(Path(prereg["scenario_manifests"]["L1_ECONOMIC_SCENARIO_IDS"]["path"]).read_text().splitlines())
    definitions = prereg["schema_catalog"]["definitions"]
    checked = []
    for scenario in native_scenarios():
        if scenario["scenario_id"] not in ids:
            continue
        original = deepcopy(scenario)
        metadata = diagnostic._scenario_metadata(scenario)
        bound.validate_wire_value(metadata, {"$ref": "#/$defs/scenario_definition"}, definitions)
        assert metadata["symbol_count"] == len(scenario["symbols"])
        assert metadata["symbols"] == scenario["symbols"]
        assert scenario == original
        checked.append(scenario["scenario_id"])
    assert len(checked) == len(ids) == 765


@pytest.mark.parametrize("patch", [
    {"symbols": None}, {"symbols": "300308"}, {"symbols": []},
    {"symbols": ["300308", "300308"]}, {"symbols": [300308]},
    {"symbols": ["bad"]}, {"symbol_count": None}, {"symbol_count": True},
    {"symbol_count": 0}, {"symbol_count": 1.0}, {"symbol_count": "1"},
    {"scenario_id": ""}, {"scenario_id": None},
])
def test_metadata_rejects_malformed_or_inconsistent_inputs(patch):
    scenario = {**native_scenarios()[0], **patch}
    with pytest.raises(ValueError):
        diagnostic._scenario_metadata(scenario)


@pytest.fixture
def synthetic_market(tmp_path, monkeypatch):
    from quantfusion.config import paths
    from quantfusion.config.overlay import RISK_BASKET
    from quantfusion.config.portfolio import PortfolioPolicy
    from quantfusion.config.regime import REGIME_INDEX_FILES
    dates = pd.bdate_range("2024-01-01", "2026-01-09")
    close = pd.Series([10 + i * .02 for i in range(len(dates))], index=dates)
    frame = pd.DataFrame({"open": close, "high": close * 1.001,
                          "low": close * .999, "close": close, "volume": 10000000.})
    frame.index.name = "date"
    codes = set(stress_scenarios.ORDERED_CODES) | set(PortfolioPolicy().regime_symbols) | set(RISK_BASKET) | set(REGIME_INDEX_FILES.values())
    for code in codes:
        frame.to_csv(tmp_path / f"{code}.csv")
    monkeypatch.setattr(paths, "MARKET_DATA_DIR", tmp_path)
    monkeypatch.setattr(paths, "REGIME_DATA_DIR", tmp_path)
    monkeypatch.setattr(stress_metrics, "START_DATE", "2026-01-05")
    monkeypatch.setattr(stress_metrics, "END_DATE", "2026-01-09")
    return tmp_path


def synthetic_prereg(prereg, directory):
    """Bind generated inputs as a distinct test contract, never as formal data."""
    from quantfusion.application import stress_artifacts
    result = deepcopy(prereg)
    result["experiment_id"] = "synthetic-recovery-boundary-only"
    properties = result["schema_catalog"]["definitions"]["data_identity"]["wire_schema"]["properties"]
    properties["data_fingerprint"] = {"const": stress_artifacts._tree_fingerprint(stress_artifacts._data_files(directory, directory))}
    properties["scenario_signature"] = {"const": stress_scenarios._scenario_signature(native_scenarios())}
    dates = pd.bdate_range(stress_metrics.START_DATE, stress_metrics.END_DATE)
    properties["calendar_hash"] = {"const": hashlib.sha256("".join(str(date.date()) + "\n" for date in dates).encode()).hexdigest()}
    return result


def test_real_producer_compressed_checkpoint_and_consumer(prereg, synthetic_market):
    prereg = synthetic_prereg(prereg, synthetic_market)
    scenario = next(s for s in native_scenarios() if s["scenario_id"] == "prefix-05")
    definitions = prereg["schema_catalog"]["definitions"]
    variants = ["baseline", "F0-only", "F0+F1", "U-only", "C6-Base", "C6-Base+S"]
    rows = [diagnostic._l1_evaluate((v, scenario, "DEFAULT")) for v in variants]
    for row in rows:
        bound.validate_wire_value(row, {"$ref": "#/$defs/evaluation_record"}, definitions)
        assert row["fills"], "generated market must exercise execution"
    ids = [f"evaluation/{r['evaluation_id']}" for r in rows]
    path = synthetic_market / "synthetic-checkpoint.json.gz"
    checkpoint = bound.DiagnosticCheckpoint(path, ids, "synthetic-v1", prereg=prereg)
    checkpoint.map(diagnostic._identity, rows, ids, workers=1)
    restored = bound.DiagnosticCheckpoint(path, ids, "synthetic-v2", resume_signature="synthetic-v1", prereg=prereg)
    restored_rows = list(restored.map(lambda _: pytest.fail("completed work was recomputed"), rows, ids, workers=1))
    assert restored_rows == rows
    l2 = diagnostic._l2_evaluate(scenario)
    bound.validate_wire_value(l2, {"$ref": "#/$defs/L2_result"}, definitions)
    l2_item = {"item_id": "scenario/" + l2["scenario_id"], "result_schema": "L2_result", "result": l2}
    bound.validate_checkpoint_item(l2_item, prereg)
    l2["diagnostic_telemetry"]["mdd_slack"] += 1
    with pytest.raises(bound.BoundRunError, match="L2 formulas"):
        bound.validate_checkpoint_item(l2_item, prereg)
    invalid = deepcopy(rows[0])
    invalid["scenario_definition"]["symbol_count"] = None
    bad = bound.DiagnosticCheckpoint(synthetic_market / "bad.json.gz", ids[:1], "synthetic-bad", prereg=prereg)
    with pytest.raises(bound.BoundRunError, match="symbol_count"):
        bad.map(diagnostic._identity, [invalid], ids[:1], workers=1)
    assert len(bad.items) == 0
    assert not bad.path.exists()
    # Even a correctly rehashed malformed sealed item must fail semantic restore.
    payload = load_object(path)
    payload["completed_items"] = list(payload["completed_items"])
    payload["completed_items"][0]["result"] = invalid
    payload["completed_items"][0]["result_sha256"] = bound.canonical_payload_hash(invalid)
    write_json(path, payload, replace=True)
    with pytest.raises(bound.BoundRunError, match="symbol_count"):
        bound.DiagnosticCheckpoint(path, ids, "synthetic-v3", resume_signature="synthetic-v1", prereg=prereg)


def test_complete_native_w_group_passes_real_record_consumer(prereg, synthetic_market):
    prereg = synthetic_prereg(prereg, synthetic_market)
    scenario = next(s for s in native_scenarios() if s["scenario_id"] == "add-one-13-601869")
    variants = prereg["scenario_manifests"]["L1_BASE_EVALUATION_MANIFEST"]["causal_intervention_order"]
    rows = [diagnostic._l1_evaluate((v, scenario, "DEFAULT")) for v in variants]
    diagnostic._attach_interventions(rows)
    for row in rows:
        bound.validate_wire_value(row, {"$ref": "#/$defs/evaluation_record"}, prereg["schema_catalog"]["definitions"])
    assert len(rows) == 6
    diagnostic._attribution(rows)


def test_retained_rows_native_publication_crosses_l4_consumer(prereg, tmp_path, monkeypatch):
    """Exercise publication with retained rows; never invoke the official backtest."""
    from quantfusion.application import stress_artifacts
    source = Path(prereg["transition_reference"]["path"])
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    assert before == prereg["transition_reference"]["sha256"]
    reference = json.loads(source.read_text())
    artifact = deepcopy(reference)
    # The historical run had no transition reference. The C6 invocation does.
    # Use the native gate and publisher to obtain that output shape, without replay.
    for key in ("artifact_status", "acceptance_status", "canonical", "rejection_reasons"):
        artifact.pop(key)
    artifact["initial_baseline_gates"] = stress_metrics._initial_baseline_gates(artifact["results"], reference)
    provenance = {key: artifact[key] for key in stress_artifacts.PROVENANCE_FIELDS}
    prefix = {**provenance, "results": [row for row in artifact["results"] if row["scenario_type"] == "prefix"]}
    monkeypatch.setattr(stress_artifacts, "VALIDATION_ARTIFACT_DIR", tmp_path)
    published = stress_artifacts._publish_formal_artifacts(
        prefix, artifact, scenarios=native_scenarios(), provenance=provenance,
        incumbent=None, formal_plan_complete=True, establish_initial_baseline=True,
        initial_baseline_reference=reference)
    assert not published  # Known retained rejection is not promoted by this test.
    artifact = load_object(tmp_path / "candidates" / f"stress-{provenance['source_revision']}-rejected.json")
    binding = {"canonical_payload_schema": {"name": "official_L4_payload", "version": 2}}
    bound.validate_result_payload(artifact, binding, prereg)
    projected_hash = bound.canonical_payload_hash(bound.result_payload(artifact, "L4", prereg))
    output = tmp_path / "native-official.json"
    write_json(output, artifact)
    restored = load_object(output)
    bound.validate_result_payload(restored, binding, prereg)
    assert bound.canonical_payload_hash(bound.result_payload(restored, "L4", prereg)) == projected_hash
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_qualification_checkpoint_validates_formulas_before_completed_prefix(prereg, tmp_path):
    from quantfusion.application.c6_predicates import _qualify
    base = {"scenario_id": "synthetic-residual", "official_metrics": {"max_drawdown": -.19},
            "causal_matrix": {"s_evidence": diagnostic._empty_s_evidence(),
                              "event_timeline": {"first_official_mdd_breach": {"timestamp": "2026-01-05"}}}}
    row = _qualify(base)
    ids = ["qualification/synthetic-residual"]
    path = tmp_path / "qualification.json.gz"
    checkpoint = bound.DiagnosticCheckpoint(path, ids, "synthetic-qualification", prereg=prereg)
    checkpoint.map(diagnostic._identity, [row], ids, workers=1)
    restored = bound.DiagnosticCheckpoint(path, ids, "synthetic-resume", resume_signature="synthetic-qualification", prereg=prereg)
    assert restored.map(diagnostic._identity, [row], ids, workers=1)[0] == row
    for mutation in ("passed", "scenario_id"):
        invalid = deepcopy(row)
        invalid[mutation] = not row[mutation] if mutation == "passed" else "wrong-residual"
        bad = bound.DiagnosticCheckpoint(tmp_path / (mutation + ".gz"), ids, "synthetic-invalid", prereg=prereg)
        with pytest.raises(bound.BoundRunError, match="qualification"):
            bad.map(diagnostic._identity, [invalid], ids, workers=1)
        assert not bad.items and not bad.path.exists()
