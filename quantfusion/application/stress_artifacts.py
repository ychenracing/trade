"""Stress provenance, checkpoint validation, and artifact publication."""

# Validation deliberately presents one ValueError contract for malformed artifacts.
# ruff: noqa: TRY004

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

from quantfusion.application import stress_metrics, stress_scenarios
from quantfusion.config.paths import PROJECT_ROOT, VALIDATION_ARTIFACT_DIR

PROVENANCE_FIELDS = (
    "stress_contract_version",
    "source_revision",
    "source_fingerprint",
    "data_fingerprint",
    "scenario_signature",
    "run_signature",
    "scenario_count",
    "start_date",
    "end_date",
    "initial_capital",
    "engine",
    "deployment_policy",
)


def _artifact_path(path: Path) -> str:
    """Prefer a portable repository-relative path in persisted artifacts."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.stem}-", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary_name).replace(path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def _tree_fingerprint(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: str(item)):
        try:
            label = path.relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            label = path.resolve().as_posix()
        digest.update(label.encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _source_files() -> list[Path]:
    return list(PROJECT_ROOT.glob("*.py")) + list(
        (PROJECT_ROOT / "quantfusion").rglob("*.py")
    )


def _data_files(data_dir: Path, regime_data_dir: Path) -> list[Path]:
    return list(data_dir.glob("*.csv")) + list(regime_data_dir.glob("*.csv"))


def _build_provenance(
    scenarios: list[dict[str, Any]],
    data_dir: Path,
    regime_data_dir: Path,
    *,
    source_revision: str,
) -> dict[str, Any]:
    if len(source_revision) != 40 or any(
        character not in "0123456789abcdef" for character in source_revision
    ):
        raise ValueError("source_revision must be a lowercase 40-character Git SHA")
    source_fingerprint = _tree_fingerprint(_source_files())
    data_fingerprint = _tree_fingerprint(_data_files(data_dir, regime_data_dir))
    scenario_signature = stress_scenarios._scenario_signature(scenarios)
    payload = {
        "stress_contract_version": stress_metrics.STRESS_CONTRACT_VERSION,
        "source_revision": source_revision,
        "source_fingerprint": source_fingerprint,
        "data_fingerprint": data_fingerprint,
        "scenario_signature": scenario_signature,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return {
        **payload,
        "run_signature": hashlib.sha256(encoded).hexdigest(),
        "scenario_count": len(scenarios),
        "start_date": stress_metrics.START_DATE,
        "end_date": stress_metrics.END_DATE,
        "initial_capital": stress_metrics.INITIAL_CAPITAL,
        "engine": stress_metrics.ENGINE,
        "deployment_policy": stress_metrics.DEPLOYMENT_POLICY,
    }


def _run_signature(
    scenarios: list[dict[str, Any]],
    data_dir: Path,
    regime_data_dir: Path,
    *,
    source_revision: str,
) -> str:
    return str(
        _build_provenance(
            scenarios,
            data_dir,
            regime_data_dir,
            source_revision=source_revision,
        )["run_signature"]
    )


def _validated_checkpoint(
    payload: dict[str, Any],
    scenarios: list[dict[str, Any]],
    *,
    signature: str,
    provenance: dict[str, Any],
    diagnostic_selection: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Validate a checkpoint envelope and return its completed results."""
    if payload.get("signature") != signature:
        raise ValueError(
            "Stress checkpoint code, data, or scenario signature changed"
        )
    if payload.get("provenance") != provenance:
        raise ValueError("Stress checkpoint provenance changed")
    if (
        diagnostic_selection is not None
        and payload.get("selection") != diagnostic_selection
    ):
        raise ValueError("Stress diagnostic checkpoint selection changed")
    return _validated_checkpoint_results(payload, scenarios)


def _validated_checkpoint_results(
    payload: dict[str, Any], scenarios: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Reject stale, duplicated, malformed, or scenario-mismatched progress."""
    raw_results = payload.get("results", [])
    if not isinstance(raw_results, list):
        raise ValueError("Stress checkpoint results must be a list")
    expected = {str(item["scenario_id"]): item for item in scenarios}
    completed: dict[str, dict[str, Any]] = {}
    for item in raw_results:
        if not isinstance(item, dict):
            raise ValueError("Stress checkpoint result must be an object")
        scenario_id = str(item.get("scenario_id", ""))
        if scenario_id not in expected:
            raise ValueError(
                f"Stress checkpoint contains unknown scenario {scenario_id}"
            )
        if scenario_id in completed:
            raise ValueError(f"Stress checkpoint duplicates scenario {scenario_id}")
        for key, value in expected[scenario_id].items():
            if item.get(key) != value:
                raise ValueError(
                    f"Stress checkpoint scenario definition changed: {scenario_id}"
                )
        for key in ("total_return", "max_drawdown", "sharpe", "calmar"):
            value = item.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"Stress checkpoint {scenario_id} has invalid {key}")
            if not math.isfinite(float(value)):
                raise ValueError(
                    f"Stress checkpoint {scenario_id} has non-finite {key}"
                )
        for key in (
            "total_trades",
            "sleeve_fill_count",
            "date_symbol_side_count",
            "max_concurrent_symbols",
        ):
            value = item.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"Stress checkpoint {scenario_id} has invalid {key}")
        attribution = item.get("reason_attribution")
        if not isinstance(attribution, dict) or set(attribution) != set(
            stress_metrics.ATTRIBUTION_CATEGORIES
        ):
            raise ValueError(
                f"Stress checkpoint {scenario_id} has invalid reason_attribution"
            )
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in attribution.values()
            )
            or sum(attribution.values()) != item["total_trades"]
        ):
            raise ValueError(
                f"Stress checkpoint {scenario_id} has invalid reason_attribution"
            )
        if not isinstance(item.get("terminal_risk_lock"), bool):
            raise ValueError(
                f"Stress checkpoint {scenario_id} has invalid terminal_risk_lock"
            )
        if item.get("deployment_policy") != stress_metrics.DEPLOYMENT_POLICY:
            raise ValueError(
                f"Stress checkpoint {scenario_id} was not a production replay"
            )
        completed[scenario_id] = item
    return completed


def _validate_publish_candidate(
    prefix_artifact: dict[str, Any],
    universe_artifact: dict[str, Any],
    *,
    scenarios: list[dict[str, Any]],
    provenance: dict[str, Any],
    incumbent: dict[str, Any] | None,
    initial_baseline_reference: dict[str, Any] | None,
) -> None:
    """Fail closed before retaining or publishing a completed stress run."""
    if (
        provenance.get("stress_contract_version")
        != stress_metrics.STRESS_CONTRACT_VERSION
    ):
        raise ValueError("Stress candidate has an invalid contract version")
    for field in PROVENANCE_FIELDS:
        if field not in provenance or any(
            artifact.get(field) != provenance[field]
            for artifact in (prefix_artifact, universe_artifact)
        ):
            raise ValueError(f"Stress candidate provenance changed: {field}")
    results = universe_artifact.get("results")
    if not isinstance(results, list):
        raise ValueError("Stress candidate results must be a list")
    completed = _validated_checkpoint_results({"results": results}, scenarios)
    expected_ids = {str(item["scenario_id"]) for item in scenarios}
    if set(completed) != expected_ids or len(results) != len(scenarios):
        raise ValueError("Stress candidate did not complete the exact scenario plan")
    if universe_artifact.get("scenario_count") != len(scenarios):
        raise ValueError("Stress candidate did not complete the exact scenario plan")
    if (
        universe_artifact.get("trade_count_semantics")
        != stress_metrics.TRADE_COUNT_SEMANTICS
    ):
        raise ValueError("Stress candidate has invalid trade_count_semantics")
    expected_seeds = list(
        dict.fromkeys(int(item["seed"]) for item in scenarios if "seed" in item)
    )
    if universe_artifact.get("seeds") != expected_seeds:
        raise ValueError("Stress candidate seeds changed")
    prefix_scenarios = [item for item in scenarios if item["scenario_type"] == "prefix"]
    prefix_results = prefix_artifact.get("results")
    if not isinstance(prefix_results, list):
        raise ValueError("Stress candidate prefix results must be a list")
    completed_prefixes = _validated_checkpoint_results(
        {"results": prefix_results}, prefix_scenarios
    )
    expected_prefix_ids = {str(item["scenario_id"]) for item in prefix_scenarios}
    if set(completed_prefixes) != expected_prefix_ids or len(prefix_results) != len(
        prefix_scenarios
    ):
        raise ValueError("Stress candidate did not complete the prefix scenario plan")
    expected_absolute_gates = stress_metrics._absolute_hard_gates(results)
    if universe_artifact.get("absolute_hard_gates") != expected_absolute_gates:
        raise ValueError("Stress candidate absolute hard gates changed")
    expected_retained_gates = stress_metrics._retained_robustness_hard_gates(results)
    if (
        universe_artifact.get("retained_robustness_hard_gates")
        != expected_retained_gates
    ):
        raise ValueError("Stress candidate retained robustness hard gates changed")
    expected_diagnostics = stress_metrics._robustness_diagnostics(results)
    if universe_artifact.get("robustness_diagnostics") != expected_diagnostics:
        raise ValueError("Stress candidate robustness diagnostics changed")
    expected_promotion_gates = stress_metrics._promotion_gates(results, incumbent)
    if universe_artifact.get("promotion_gates") != expected_promotion_gates:
        raise ValueError("Stress candidate promotion gates changed")
    expected_initial_gates = stress_metrics._initial_baseline_gates(
        results, initial_baseline_reference
    )
    if universe_artifact.get("initial_baseline_gates") != expected_initial_gates:
        raise ValueError("Stress candidate initial baseline gates changed")


def _load_incumbent(path: Path) -> dict[str, Any] | None:
    """Load an optional accepted incumbent with current trade semantics."""
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read incumbent stress artifact: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Cannot read incumbent stress artifact: {path}")
    if (
        payload.get("stress_contract_version")
        != stress_metrics.STRESS_CONTRACT_VERSION
    ):
        return None
    stress_metrics._current_incumbent_by_id(payload)
    return payload


def _load_initial_baseline_reference(path: Path) -> dict[str, Any]:
    """Load a retained artifact for one-time economic comparison only."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read initial baseline reference: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Cannot read initial baseline reference: {path}")
    stress_metrics._transition_reference_by_id(payload)
    return payload


def _rejection_reasons(
    universe_artifact: dict[str, Any],
    *,
    incumbent: dict[str, Any] | None,
    establish_initial_baseline: bool,
) -> list[dict[str, str]]:
    reasons = [
        {"gate_family": "absolute_hard_gates", "gate": str(name)}
        for name, passed in universe_artifact["absolute_hard_gates"]["checks"].items()
        if not passed
    ]
    reasons.extend(
        {"gate_family": "retained_robustness_hard_gates", "gate": str(name)}
        for name, passed in universe_artifact["retained_robustness_hard_gates"][
            "checks"
        ].items()
        if not passed
    )
    promotion = universe_artifact["promotion_gates"]
    permutation = promotion.get("permutation_invariance", {})
    if not permutation.get("invariant"):
        reasons.append(
            {"gate_family": "promotion_gates", "gate": "permutation_invariant"}
        )
    if incumbent is None and not establish_initial_baseline:
        reasons.append(
            {
                "gate_family": "initial_baseline",
                "gate": "explicit_establish_initial_baseline_action",
            }
        )
    elif incumbent is None:
        reasons.extend(
            {"gate_family": "initial_baseline_gates", "gate": str(name)}
            for name, passed in universe_artifact["initial_baseline_gates"]
            .get("checks", {})
            .items()
            if not passed
        )
    elif promotion.get("passed") is not True:
        reasons.extend(
            {"gate_family": "promotion_gates", "gate": str(name)}
            for name, passed in promotion.get("checks", {}).items()
            if not passed and name != "permutation_invariant"
        )
    return reasons


def _publish_formal_artifacts(
    prefix_artifact: dict[str, Any],
    universe_artifact: dict[str, Any],
    *,
    scenarios: list[dict[str, Any]],
    provenance: dict[str, Any],
    incumbent: dict[str, Any] | None,
    formal_plan_complete: bool,
    establish_initial_baseline: bool = False,
    initial_baseline_reference: dict[str, Any] | None = None,
) -> bool:
    """Retain complete failures; publish canonical files only after acceptance."""
    if not formal_plan_complete:
        raise ValueError(
            "A diagnostic stress selection cannot publish formal artifacts"
        )
    if not stress_scenarios.is_canonical_scenario_plan(scenarios):
        raise ValueError(
            "Formal publication requires the exact canonical scenario plan"
        )
    if incumbent is not None and (
        establish_initial_baseline or initial_baseline_reference is not None
    ):
        raise ValueError(
            "Cannot establish an initial baseline when a current-contract incumbent exists"
        )
    if incumbent is None and (
        establish_initial_baseline != (initial_baseline_reference is not None)
    ):
        raise ValueError(
            "Initial baseline establishment requires both the explicit action and reference"
        )
    _validate_publish_candidate(
        prefix_artifact,
        universe_artifact,
        scenarios=scenarios,
        provenance=provenance,
        incumbent=incumbent,
        initial_baseline_reference=initial_baseline_reference,
    )
    route_accepted = (
        stress_metrics._promotion_accepted(universe_artifact["promotion_gates"])
        if incumbent is not None
        else establish_initial_baseline
        and universe_artifact["initial_baseline_gates"]["passed"] is True
        and universe_artifact["promotion_gates"]["permutation_invariance"][
            "invariant"
        ]
    )
    accepted = (
        universe_artifact["absolute_hard_gates"]["passed"]
        and universe_artifact["retained_robustness_hard_gates"]["passed"]
        and route_accepted
    )
    if not accepted:
        candidate = {
            **universe_artifact,
            "artifact_status": "current_candidate",
            "acceptance_status": "rejected",
            "canonical": False,
            "rejection_reasons": _rejection_reasons(
                universe_artifact,
                incumbent=incumbent,
                establish_initial_baseline=establish_initial_baseline,
            ),
        }
        source_revision = str(provenance["source_revision"])
        _atomic_json(
            VALIDATION_ARTIFACT_DIR
            / "candidates"
            / f"stress-{source_revision}-rejected.json",
            candidate,
        )
        return False
    prefix_artifact = {
        **prefix_artifact,
        "acceptance_status": "accepted",
        "canonical": True,
    }
    universe_artifact = {
        **universe_artifact,
        "acceptance_status": "accepted",
        "canonical": True,
    }
    if incumbent is None:
        prefix_artifact["baseline_kind"] = "initial_current_contract"
        universe_artifact["baseline_kind"] = "initial_current_contract"
    _atomic_json(VALIDATION_ARTIFACT_DIR / "prefix_stress.json", prefix_artifact)
    _atomic_json(VALIDATION_ARTIFACT_DIR / "universe_stress.json", universe_artifact)
    return True
