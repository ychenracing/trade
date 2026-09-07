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


def _square(value: int) -> dict[str, int]:
    return {"square": value * value}


def test_checkpoint_serializes_once_per_map_not_per_chunk(tmp_path, monkeypatch):
    ids = [f"scenario/{index}" for index in range(3)]
    path = tmp_path / "child-checkpoint.json"
    checkpoint = bound.DiagnosticCheckpoint(
        path, ids, "a" * 64, budget_seconds=60, chunk_size=1
    )
    calls = 0
    original = checkpoint.save

    def counted_save() -> None:
        nonlocal calls
        calls += 1
        original()

    monkeypatch.setattr(checkpoint, "save", counted_save)
    actual = checkpoint.map(_square, [0, 1, 2], ids, workers=1)

    assert list(actual) == [_square(index) for index in range(3)]
    assert calls == 1
    assert path.is_file()


def test_checkpoint_serializes_once_before_graceful_exit(tmp_path, monkeypatch):
    ids = [f"scenario/{index}" for index in range(3)]
    path = tmp_path / "child-checkpoint.json"
    checkpoint = bound.DiagnosticCheckpoint(
        path, ids, "a" * 64, budget_seconds=0, chunk_size=1
    )
    calls = 0
    original = checkpoint.save

    def counted_save() -> None:
        nonlocal calls
        calls += 1
        original()

    monkeypatch.setattr(checkpoint, "save", counted_save)
    with pytest.raises(SystemExit) as stopped:
        checkpoint.map(_square, [0, 1, 2], ids, workers=1)

    assert stopped.value.code == 75
    assert calls == 1
    assert path.is_file()


@pytest.mark.parametrize("suffix", [".json", ".json.gz"])
def test_checkpoint_progress_decodes_each_record_once(tmp_path, monkeypatch, suffix):
    from quantfusion.io.c6_stream import FileArray
    ids = [f"scenario/{index}" for index in range(3)]
    path = tmp_path / ("checkpoint" + suffix)
    checkpoint = bound.DiagnosticCheckpoint(path, ids, "a" * 64, chunk_size=1)
    checkpoint.map(_square, [0, 1, 2], ids, workers=1)
    decoded = []
    original = FileArray.__iter__

    def counted_records(self):
        for record in original(self):
            decoded.append(record["item_id"])
            yield record

    monkeypatch.setattr(FileArray, "__iter__", counted_records)
    assert bound.checkpoint_progress(
        path, stage="L1", binding_signature="a" * 64, item_ids=ids
    ) == ids
    assert decoded == ids


@pytest.mark.parametrize("suffix", [".json", ".json.gz"])
def test_checkpoint_does_not_resave_without_new_map_records(tmp_path, monkeypatch, suffix):
    ids = [f"scenario/{index}" for index in range(3)]
    path = tmp_path / ("checkpoint" + suffix)
    checkpoint = bound.DiagnosticCheckpoint(path, ids, "a" * 64, chunk_size=1)
    checkpoint.map(_square, [0], ids[:1], workers=1)
    before = path.read_bytes()

    def unexpected_save():
        pytest.fail("completed records were already saved by the preceding map")

    monkeypatch.setattr(checkpoint, "save", unexpected_save)
    assert list(checkpoint.map(_square, [], [], workers=1)) == []
    checkpoint.deadline = 0
    with pytest.raises(SystemExit) as stopped:
        checkpoint.map(_square, [1, 2], ids[1:], workers=1)
    assert stopped.value.code == 75
    assert path.read_bytes() == before


@pytest.mark.parametrize("suffix", [".json", ".json.gz"])
@pytest.mark.parametrize("workers", [1, 2])
def test_checkpoint_graceful_resume_matches_uninterrupted_bytes(tmp_path, suffix, workers):
    ids = [f"scenario/{index}" for index in range(6)]
    tasks = list(range(6))
    full_path = tmp_path / ("full" + suffix)
    full = bound.DiagnosticCheckpoint(full_path, ids, "b" * 64, chunk_size=2)
    full.map(_square, tasks[:4], ids[:4], workers=workers)
    full.map(_square, tasks[4:], ids[4:], workers=workers)

    resumed_path = tmp_path / ("resumed" + suffix)
    first = bound.DiagnosticCheckpoint(
        resumed_path, ids, "a" * 64, budget_seconds=0, chunk_size=2
    )
    with pytest.raises(SystemExit) as stopped:
        first.map(_square, tasks[:4], ids[:4], workers=workers)
    assert stopped.value.code == 75
    assert bound.checkpoint_progress(
        resumed_path, stage="L1", binding_signature="a" * 64, item_ids=ids
    ) == ids[:2]
    resumed = bound.DiagnosticCheckpoint(
        resumed_path, ids, "b" * 64, resume_signature="a" * 64, chunk_size=2
    )
    # A resumed prefix must not invoke the worker again.
    assert list(resumed.map(_square, [None, None, 2, 3], ids[:4], workers=workers)) == [
        _square(index) for index in range(4)
    ]
    assert list(resumed.map(_square, tasks[4:], ids[4:], workers=workers)) == [
        _square(index) for index in range(4, 6)
    ]
    assert resumed_path.read_bytes() == full_path.read_bytes()


def test_wire_validator_preserves_mapping_and_strict_scalar_types():
    from collections import UserDict
    from types import MappingProxyType
    schema = {"type": "object", "required": ["count"], "additionalProperties": False,
              "properties": {"count": {"type": "integer", "minimum": 0}}}
    for factory in (dict, UserDict, MappingProxyType):
        bound.validate_wire_value(factory({"count": 1}), factory(schema), {})
        for invalid in (True, None, 1.0, -1, "1"):
            with pytest.raises(bound.BoundRunError):
                bound.validate_wire_value(factory({"count": invalid}), factory(schema), {})
    for invalid in ([], None, True, {"count": 1, "extra": 2}):
        with pytest.raises(bound.BoundRunError):
            bound.validate_wire_value(invalid, schema, {})


def test_history_validation_reuses_only_identical_authenticated_records(prereg, tmp_path, monkeypatch):
    from quantfusion.application.c6_predicates import _qualify
    ids = [f"qualification/synthetic-{index}" for index in range(3)]
    rows = [_qualify({"scenario_id": item.removeprefix("qualification/"),
                     "official_metrics": {"max_drawdown": -.19},
                     "causal_matrix": {"s_evidence": diagnostic._empty_s_evidence(),
                                       "event_timeline": {"first_official_mdd_breach": {
                                           "timestamp": "2026-01-05"}}}}) for item in ids]
    paths = []
    for count in range(1, 4):
        path = tmp_path / f"prefix-{count}.json.gz"
        checkpoint = bound.DiagnosticCheckpoint(path, ids, "a" * 64, prereg=prereg)
        checkpoint.map(diagnostic._identity, rows[:count], ids[:count], workers=1)
        paths.append(path)
    calls = []
    original = bound.validate_checkpoint_item

    def counted_validation(item, contract):
        calls.append(item["item_id"])
        original(item, contract)

    monkeypatch.setattr(bound, "validate_checkpoint_item", counted_validation)
    validated = {}
    for count, path in enumerate(paths, start=1):
        assert bound.checkpoint_progress(
            path, stage="L1", binding_signature="a" * 64, item_ids=ids,
            prereg=prereg, _validated_records=validated,
        ) == ids[:count]
    assert calls == ids  # Not 1 + 2 + 3 recursive schema/formula validations.
    assert len(validated) == len(ids)

    # A changed value with the old claimed hash must still fail before reuse.
    corrupt = load_object(paths[-1])
    corrupt["completed_items"] = list(corrupt["completed_items"])
    item = corrupt["completed_items"][0]
    item["result"]["passed"] = not item["result"]["passed"]
    damaged = tmp_path / "damaged.json.gz"
    write_json(damaged, corrupt)
    with pytest.raises(bound.BoundRunError, match="hash/schema"):
        bound.checkpoint_progress(damaged, stage="L1", binding_signature="a" * 64,
                                  item_ids=ids, prereg=prereg, _validated_records=validated)
    assert calls == ids

    # Rehashing an invalid formula cannot turn it into a cached valid record.
    item["result_sha256"] = bound.canonical_payload_hash(item["result"])
    write_json(damaged, corrupt, replace=True)
    for _ in range(2):
        with pytest.raises(bound.BoundRunError, match="qualification formulas"):
            bound.checkpoint_progress(damaged, stage="L1", binding_signature="a" * 64,
                                      item_ids=ids, prereg=prereg, _validated_records=validated)
    assert calls == ids + [ids[0], ids[0]]
    assert len(validated) == len(ids)  # Failed validations never populate reuse state.

    calls.clear()
    assert bound.checkpoint_progress(paths[-1], stage="L1", binding_signature="a" * 64,
                                     item_ids=ids, prereg=prereg) == ids
    assert calls == ids  # Default/independent calls never inherit prior validation.
