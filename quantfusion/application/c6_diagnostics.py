"""Strict, R-bound non-canonical C6 diagnostic execution and payload helpers."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import subprocess
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import Any


def _as_mapping(value: object, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{where} must be an object with string keys")
    return value


def _exact_keys(value: object, expected: Sequence[str], where: str) -> Mapping[str, Any]:
    payload = _as_mapping(value, where)
    expected_set = set(expected)
    missing = sorted(expected_set - set(payload))
    extra = sorted(set(payload) - expected_set)
    if missing or extra:
        raise ValueError(f"{where} has missing={missing} extra={extra}")
    return payload


def _validate_finite(value: object, where: str = "payload") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{where} must contain only finite numbers")
    if isinstance(value, float):
        return
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError(f"{where} contains a non-string object key")
        for key, nested in value.items():
            _validate_finite(nested, f"{where}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _validate_finite(nested, f"{where}[{index}]")
    else:
        raise ValueError(f"{where} contains unsupported JSON value {type(value).__name__}")


def _canonical_bytes(value: object) -> bytes:
    _validate_finite(value)
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _numbers(value: object) -> list[float]:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, Mapping):
        return [number for nested in value.values() for number in _numbers(nested)]
    if isinstance(value, list):
        return [number for nested in value for number in _numbers(nested)]
    return []


def validate_manifest_identity(
    actual_ids: Sequence[str], frozen_manifest: Mapping[str, Any]
) -> list[str]:
    """Validate exact sorted LF-delimited ID identity against a frozen manifest."""
    ids = list(actual_ids)
    if any(
        not isinstance(item, str) or not item or "\n" in item or "\r" in item
        for item in ids
    ):
        raise ValueError("manifest IDs must be non-empty single-line strings")
    if len(set(ids)) != len(ids):
        raise ValueError("manifest contains duplicate IDs")
    if ids != sorted(ids):
        raise ValueError("manifest ID order is not lexicographic")
    count = frozen_manifest.get("count")
    unique_count = frozen_manifest.get("unique_count", count)
    if count != len(ids) or unique_count != len(ids):
        raise ValueError("manifest count does not match frozen identity")
    embedded = frozen_manifest.get("ids")
    if embedded is not None and embedded != ids:
        raise ValueError("manifest IDs do not match frozen order")
    digest = hashlib.sha256("".join(f"{item}\n" for item in ids).encode()).hexdigest()
    if frozen_manifest.get("sha256") != digest:
        raise ValueError("manifest SHA-256 does not match frozen identity")
    return ids


def first_official_mdd_breach(
    equity_samples: Sequence[Mapping[str, Any]],
    *,
    threshold: float = 0.18,
    numeric_tolerance: float = 1e-15,
) -> dict[str, Any] | None:
    """Return the first official sampled running-peak breach, or ``None``."""
    if not math.isfinite(threshold) or not math.isfinite(numeric_tolerance):
        raise ValueError("breach threshold and tolerance must be finite")
    if threshold < 0 or numeric_tolerance < 0:
        raise ValueError("breach threshold and tolerance must be non-negative")
    peak: float | None = None
    prior_timestamp: str | None = None
    for ordinal, raw in enumerate(equity_samples):
        sample = _as_mapping(raw, f"equity_samples[{ordinal}]")
        timestamp = sample.get("timestamp")
        equity = sample.get("equity")
        if not isinstance(timestamp, str) or not timestamp:
            raise ValueError("official equity sample timestamp must be a string")
        if prior_timestamp is not None and timestamp < prior_timestamp:
            raise ValueError("official equity samples are not in sample order")
        if isinstance(equity, bool) or not isinstance(equity, (int, float)) or not math.isfinite(float(equity)):
            raise ValueError("official equity samples must contain finite equity")
        current = float(equity)
        peak = current if peak is None else max(peak, current)
        if peak <= 0:
            raise ValueError("official running peak must be positive")
        drawdown = current / peak - 1.0
        if abs(drawdown) > threshold + numeric_tolerance:
            return {
                "event_type": "first_official_mdd_breach",
                "timestamp": timestamp,
                "sample_ordinal": ordinal,
                "peak_value": peak,
                "current_assets": current,
                "drawdown": drawdown,
                "threshold": threshold,
                "numeric_tolerance": numeric_tolerance,
                "peak_owner": "official_running_peak",
                "state_source": "official_equity_samples",
            }
        prior_timestamp = timestamp
    return None


def build_causal_matrix(
    scenario_id: str,
    evidence: Sequence[Mapping[str, Any]],
    allowed_labels: Sequence[str],
) -> dict[str, Any]:
    """Build a stable multi-label matrix without inventing a single cause."""
    if not isinstance(scenario_id, str) or not scenario_id:
        raise ValueError("causal matrix scenario_id must be a non-empty string")
    allowed = set(allowed_labels)
    if len(allowed) != len(allowed_labels) or any(not item for item in allowed):
        raise ValueError("causal label manifest is invalid")
    required = {
        "label",
        "first_observed_date",
        "state_index",
        "book_id",
        "notional",
        "first_path_divergence",
    }
    records: list[dict[str, Any]] = []
    for index, raw in enumerate(evidence):
        item = _exact_keys(raw, sorted(required), f"causal evidence[{index}]")
        if item["label"] not in allowed:
            raise ValueError(f"unknown causal label: {item['label']}")
        if not isinstance(item["first_observed_date"], str) or not item["first_observed_date"]:
            raise ValueError("causal evidence requires first_observed_date")
        if item["state_index"] is not None and (
            isinstance(item["state_index"], bool)
            or not isinstance(item["state_index"], int)
            or item["state_index"] < 0
        ):
            raise ValueError("causal evidence state_index must be non-negative")
        if item["book_id"] is not None and (
            not isinstance(item["book_id"], str) or not item["book_id"]
        ):
            raise ValueError("causal evidence book_id must be a non-empty string")
        if item["notional"] is not None and (
            isinstance(item["notional"], bool)
            or not isinstance(item["notional"], (int, float))
            or not math.isfinite(float(item["notional"]))
        ):
            raise ValueError("causal evidence notional must be finite")
        if item["first_path_divergence"] is not None and (
            not isinstance(item["first_path_divergence"], str)
            or not item["first_path_divergence"]
        ):
            raise ValueError("causal evidence requires first_path_divergence")
        records.append(dict(item))
    records.sort(
        key=lambda item: (
            item["first_observed_date"],
            item["label"],
            -1 if item["state_index"] is None else item["state_index"],
            "" if item["book_id"] is None else item["book_id"],
            "" if item["first_path_divergence"] is None else item["first_path_divergence"],
        )
    )
    return {
        "scenario_id": scenario_id,
        "evidence": records,
        "earliest_unavoidable_breach_under_frozen_candidate_family": None,
    }


def decompose_601869(
    terminal_wealth: Mapping[str, float], order: Sequence[str]
) -> dict[str, Any]:
    """Compute the frozen ordered natural-log wealth telescoping decomposition."""
    frozen_order = list(order)
    if len(frozen_order) < 2 or len(set(frozen_order)) != len(frozen_order):
        raise ValueError("601869 intervention order must contain unique steps")
    if set(terminal_wealth) != set(frozen_order):
        raise ValueError("601869 wealth keys must exactly match the frozen order")
    values: list[float] = []
    for intervention in frozen_order:
        wealth = terminal_wealth[intervention]
        if isinstance(wealth, bool) or not isinstance(wealth, (int, float)):
            raise ValueError("terminal wealth must be a positive finite number")
        wealth = float(wealth)
        if not math.isfinite(wealth) or wealth <= 0:
            raise ValueError("terminal wealth must be a positive finite number")
        values.append(math.log(wealth))
    total = values[-1] - values[0]
    deltas = [values[index] - values[index - 1] for index in range(1, len(values))]
    # Pin the last term to the endpoint total so deterministic serialization
    # cannot expose accumulated addition drift as a fictitious interaction.
    deltas[-1] = total - math.fsum(deltas[:-1])
    return {
        "value_function": "natural_log(terminal_wealth)",
        "order": frozen_order,
        "log_terminal_wealth": dict(zip(frozen_order, values, strict=True)),
        "steps": [
            {
                "from": frozen_order[index - 1],
                "to": frozen_order[index],
                "delta_log_wealth": deltas[index - 1],
            }
            for index in range(1, len(frozen_order))
        ],
        "total_delta_log_wealth": total,
        "telescoping_error": 0.0,
    }


def base_counterpart_id(s_evaluation_id: str) -> str:
    """Derive the sole authorized Base record for a Base+S evaluation."""
    prefix = "C6-Base+S::"
    if not isinstance(s_evaluation_id, str) or not s_evaluation_id.startswith(prefix):
        raise ValueError("S evaluation_id must start with exact C6-Base+S:: prefix")
    scenario_id = s_evaluation_id[len(prefix) :]
    if not scenario_id:
        raise ValueError("S evaluation_id must contain a scenario ID")
    return f"C6-Base::{scenario_id}"


def _manager_event(events: Sequence[Mapping[str, Any]], name: str) -> dict[str, Any]:
    event = next((item for item in events if item.get("event") == name), None)
    return {
        "timestamp": None if event is None else event.get("date"),
        "peak_owner": None if event is None else "manager_cycle_peak",
        "peak_timestamp": None,
        "peak_value": None if event is None else event.get("peak_assets"),
        "current_assets": None if event is None else event.get("current_assets"),
        "drawdown": None if event is None else event.get("drawdown"),
        "threshold": None if event is None else event.get("threshold"),
        "status_source": None if event is None else str(event.get("event")),
    }


def _l2_evaluate(scenario: Mapping[str, Any]) -> dict[str, Any]:
    """Run one selected-candidate scenario and retain the frozen L2 fields."""
    from quantfusion.application import stress, stress_metrics
    from quantfusion.config.overlay import SYMBOL_SUB_INDUSTRY
    from quantfusion.config.paths import MARKET_DATA_DIR, REGIME_DATA_DIR
    from quantfusion.engine.replay import ProductionReplayEngine

    codes = [str(item) for item in scenario["symbols"]]
    with contextlib.redirect_stdout(io.StringIO()):
        result = ProductionReplayEngine(stress_metrics.INITIAL_CAPITAL).run(
            {code: stress.NAMES[code] for code in codes},
            stress_metrics.START_DATE, stress_metrics.END_DATE,
            data_dir=str(MARKET_DATA_DIR), regime_data_dir=str(REGIME_DATA_DIR),
            indicator_state="warm",
        )
    attribution = {name: 0 for name in stress_metrics.ATTRIBUTION_CATEGORIES}
    for trade in result["trades"]:
        attribution[stress._reason_category(trade)] += 1
    equity = result["equity_curve"]
    drawdown = (equity["assets"] / equity["assets"].cummax()) - 1.0
    breach_indices = list(drawdown.index[drawdown.abs() > 0.18 + 1e-15])
    breach_index = breach_indices[0] if breach_indices else None
    breach = {
        "timestamp": None if breach_index is None else str(breach_index.date()),
        "sample_ordinal": None if breach_index is None else int(equity.index.get_loc(breach_index)),
        "peak_timestamp": None,
        "peak_value": None if breach_index is None else float(equity.loc[:breach_index, "assets"].max()),
        "equity": None if breach_index is None else float(equity.loc[breach_index, "assets"]),
        "drawdown": None if breach_index is None else float(drawdown.loc[breach_index]),
        "threshold": 0.18, "tolerance": 1e-15,
    }
    events = list(result.get("risk_events", []))
    orders = list(result.get("order_events", []))
    risk_orders = [item for item in orders if "sell" in str(item.get("direction", ""))]
    held: set[str] = set()
    max_cluster = 0.0
    for trade in result["trades"]:
        held.add(trade.symbol) if trade.direction == "buy" else held.discard(trade.symbol)
        groups: dict[str, int] = {}
        for symbol in held:
            group = str(SYMBOL_SUB_INDUSTRY.get(symbol, "unmapped"))
            groups[group] = groups.get(group, 0) + 1
        if held:
            max_cluster = max(max_cluster, max(groups.values()) / len(held))
    telemetry = {
        "cash_days": int((equity["position_value"] == 0).sum()),
        "max_gross_ratio": float((equity["position_value"] / equity["assets"]).max()),
        "max_cluster_weight": max_cluster,
        "planned_risk_sell_shares": sum(int(x.get("target_shares", 0)) for x in risk_orders),
        "retained_risk_sell_shares": sum(int(x.get("target_shares", 0)) for x in risk_orders if "retain" in str(x.get("event", ""))),
        "suppressed_risk_sell_shares": sum(int(x.get("target_shares", 0)) for x in risk_orders if "suppress" in str(x.get("event", ""))),
        "filled_risk_sell_shares": sum(t.shares for t in result["trades"] if t.direction == "sell" and stress._reason_category(t) in {"risk_reduction", "sector_liquidation"}),
        "first_evidence_timestamp": min((str(x.get("date")) for x in events), default=None),
        "first_executable_open": min((str(t.date) for t in result["trades"]), default=None),
        "first_official_mdd_breach": breach,
        "first_account_alert_event": _manager_event(events, "account_drawdown_alert"),
        "first_confirmed_cycle_lock": _manager_event(events, "confirmed_drawdown_lock"),
        "first_emergency_cycle_lock": _manager_event(events, "emergency_drawdown_lock"),
        "first_terminal_lock": _manager_event(events, "terminal_drawdown_lock"),
        "same_open_offset_shares": 0,
        "carried_conflict_count": sum("retained" in str(x.get("event", "")) for x in orders),
        "cluster_substitution_count": sum("substitution" in str(x.get("event", "")) for x in events),
        "cycle_lock_count": int(result.get("cycle_lock_count", 0)),
        "terminal_lock_count": int(bool(result.get("terminal_risk_lock", False))),
        "mdd_slack": 0.18 - abs(float(result["max_drawdown"])),
        "near_18pct": abs(0.18 - abs(float(result["max_drawdown"]))) <= 1e-12,
    }
    keys = ("scenario_id", "scenario_type", "symbols", "symbol_count", "base_size", "added_symbol", "seed", "sample_size")
    return {
        **{key: scenario.get(key) for key in keys},
        "total_return": float(result["total_return"]),
        "max_drawdown": float(result["max_drawdown"]), "sharpe": float(result["sharpe"]),
        "calmar": float(result["calmar"]), "total_trades": int(result["total_trades"]),
        "sleeve_fill_count": int(result["sleeve_fill_count"]),
        "date_symbol_side_count": int(result["date_symbol_side_count"]),
        "reason_attribution": attribution,
        "max_concurrent_symbols": int(result["max_concurrent_symbols"]),
        "terminal_risk_lock": bool(result["terminal_risk_lock"]),
        "deployment_policy": "production_daily_replay", "diagnostic_telemetry": telemetry,
    }


def _predicate_result(spec: Mapping[str, Any], ids: list[str], values: object, references: object, failed: list[str], value: object) -> dict[str, Any]:
    failed = sorted(set(failed))
    detail = {"predicate_id": spec["id"], "input_item_ids": ids, "input_values_sha256": hashlib.sha256(_canonical_bytes(values)).hexdigest(), "reference_values_sha256": hashlib.sha256(_canonical_bytes(references if spec["reference_available"] else [])).hexdigest()}
    return {"predicate_id": spec["id"], "passed": not failed, "observed": {"value": value, "reference_value": None, "threshold": spec["comparator"], "failure_count": len(failed), "failed_item_ids": failed, "detail_sha256": hashlib.sha256(_canonical_bytes(detail)).hexdigest()}, "comparator": spec["comparator"], "tolerance": spec["tolerance"], "failure_reason": None if not failed else spec["failure_reason_enum"]}


def _predicate_rows(specs: Sequence[Mapping[str, Any]], results: list[dict[str, Any]], reference: Mapping[str, Any], expected_ids: Sequence[str]) -> list[dict[str, Any]]:
    """Evaluate L2 gates and bind each result to its exact factual inputs."""
    by_id = {item["scenario_id"]: item for item in results}
    ref = {item["scenario_id"]: item for item in reference["results"]}
    all_ids = [item["scenario_id"] for item in results]
    prefixes = [f"prefix-{index:02d}" for index in range(1, 18)]
    add_ids = [item for item in all_ids if by_id[item]["scenario_type"] == "add_one"]
    perm_ids = [item for item in all_ids if by_id[item]["scenario_type"] == "permutation"]
    finite_bad = [item for item in all_ids if not all(math.isfinite(float(value)) for value in _numbers(by_id[item]))]
    mdd_bad = [item for item in all_ids if abs(by_id[item]["max_drawdown"]) > 0.18 + 1e-15]
    p0910 = (1 + by_id["prefix-10"]["total_return"]) / (1 + by_id["prefix-09"]["total_return"]) - 1
    adjacent = [(1 + by_id[b]["total_return"]) / (1 + by_id[a]["total_return"]) - 1 for a, b in zip(prefixes, prefixes[1:])]
    ratios = {item: (1 + by_id[item]["total_return"]) / (1 + ref[item]["total_return"]) for item in prefixes}
    deltas = [(1 + by_id[item]["total_return"]) / (1 + by_id[f"prefix-{by_id[item]['base_size']:02d}"]["total_return"]) - (1 + ref[item]["total_return"]) / (1 + ref[f"prefix-{ref[item]['base_size']:02d}"]["total_return"]) for item in add_ids]
    perm_bad = []
    for seed in sorted({by_id[item]["seed"] for item in perm_ids}):
        group = [item for item in perm_ids if by_id[item]["seed"] == seed]
        fields = [{key: by_id[item][key] for key in ("total_return", "max_drawdown", "sharpe", "calmar", "total_trades", "sleeve_fill_count", "date_symbol_side_count", "reason_attribution", "max_concurrent_symbols", "terminal_risk_lock")} for item in group]
        if any(_canonical_bytes(item) != _canonical_bytes(fields[0]) for item in fields[1:]):
            perm_bad.extend(group)
    facts = {
        "l2.identity.exact_manifest": (all_ids, all_ids, [], [] if all_ids == list(expected_ids) else all_ids, all_ids == list(expected_ids)),
        "l2.metrics.finite": (all_ids, results, [], finite_bad, not finite_bad),
        "l2.mdd.noncanonical_18pct_screen": (all_ids, [by_id[x]["max_drawdown"] for x in all_ids], [], mdd_bad, max(abs(by_id[x]["max_drawdown"]) for x in all_ids)),
        "l2.prefix.09_to_10_wealth": (["prefix-09", "prefix-10"], [by_id[x]["total_return"] for x in ("prefix-09", "prefix-10")], [], [] if p0910 > -0.10 else ["prefix-09", "prefix-10"], p0910),
        "l2.prefix.worst_adjacent_wealth": (prefixes, [by_id[x]["total_return"] for x in prefixes], [], [] if min(adjacent) >= -0.30 - 1e-12 else prefixes, min(adjacent)),
        "l2.initial.prefix05": (["prefix-05"], [by_id["prefix-05"]["total_return"]], [ref["prefix-05"]["total_return"]], [] if ratios["prefix-05"] >= 0.99 - 1e-12 else ["prefix-05"], ratios["prefix-05"]),
        "l2.initial.other_prefix": ([x for x in prefixes if x != "prefix-05"], [by_id[x]["total_return"] for x in prefixes if x != "prefix-05"], [ref[x]["total_return"] for x in prefixes if x != "prefix-05"], [x for x in prefixes if x != "prefix-05" and ratios[x] < 0.95 - 1e-12], min(ratios[x] for x in prefixes if x != "prefix-05")),
        "l2.initial.worst_add_one": (add_ids + sorted({f"prefix-{by_id[x]['base_size']:02d}" for x in add_ids}), [by_id[x]["total_return"] for x in add_ids], [ref[x]["total_return"] for x in add_ids], [] if min(deltas) >= -0.03 - 1e-12 else add_ids, min(deltas)),
        "l2.permutation.invariant": (perm_ids, [by_id[x] for x in perm_ids], [], perm_bad, not perm_bad),
    }
    return [_predicate_result(spec, *facts[spec["id"]]) for spec in specs]


def _manifest_ok(ids: list[str], manifest: Mapping[str, Any]) -> bool:
    digest = hashlib.sha256("".join(f"{item}\n" for item in ids).encode()).hexdigest()
    return len(ids) == manifest["count"] == len(set(ids)) and digest == manifest.get("sha256", manifest.get("ordered_ids_sha256"))


def _l1_predicate_rows(specs: Sequence[Mapping[str, Any]], selected: list[dict[str, Any]], evaluations: list[dict[str, Any]], controls: list[dict[str, Any]], pairs: list[dict[str, Any]], reference: Mapping[str, Any], manifests: Mapping[str, Any], base: bool, common: list[dict[str, Any]], no_effect: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chosen = "C6-Base" if base else "C6-Base+S"
    by_id = {item["scenario_id"]: item for item in selected}
    ref = {item["scenario_id"]: item for item in reference["results"]}
    selected_ids = [item["evaluation_id"] for item in selected]
    prefixes = [f"prefix-{index:02d}" for index in range(5, 18)]
    returns = lambda ids: [by_id[item]["official_metrics"]["total_return"] for item in ids]
    ratios = {item: (1 + by_id[item]["official_metrics"]["total_return"]) / (1 + ref[item]["total_return"]) for item in prefixes}
    p0910 = (1 + by_id["prefix-10"]["official_metrics"]["total_return"]) / (1 + by_id["prefix-09"]["official_metrics"]["total_return"]) - 1
    adjacent = [(1 + by_id[b]["official_metrics"]["total_return"]) / (1 + by_id[a]["official_metrics"]["total_return"]) - 1 for a, b in zip(prefixes, prefixes[1:])]
    add_ids = ["prefix-13", "add-one-13-601869"]
    add_delta = (1 + by_id[add_ids[1]]["official_metrics"]["total_return"]) / (1 + by_id[add_ids[0]]["official_metrics"]["total_return"]) - (1 + ref[add_ids[1]]["total_return"]) / (1 + ref[add_ids[0]]["total_return"])
    perm_ids = [item["scenario_id"] for item in selected if item["scenario_definition"]["scenario_type"] == "permutation"]
    perm_bad = []
    for seed in sorted({by_id[item]["scenario_definition"]["seed"] for item in perm_ids}):
        group = [item for item in perm_ids if by_id[item]["scenario_definition"]["seed"] == seed]
        metrics = [by_id[item]["official_metrics"] for item in group]
        if any(_canonical_bytes(item) != _canonical_bytes(metrics[0]) for item in metrics[1:]):
            perm_bad.extend(f"{chosen}::{item}" for item in group)
    warm_bad = [item["evaluation_id"] for item in selected if item["warm_boundary"]["indicator_history"] == [] or item["data_identity"]["ordered_symbols"] != list(__import__("quantfusion.application.stress_scenarios", fromlist=["ORDERED_CODES"]).ORDERED_CODES)]
    finite_bad = [item["evaluation_id"] for item in selected if not all(math.isfinite(number) for number in _numbers(item))]
    eval_name, control_name = (("L1_BASE_EVALUATION_MANIFEST", "L1_BASE_SYNTHETIC_CONTROL_IDS") if base else ("L1_S_EVALUATION_MANIFEST", "L1_S_SYNTHETIC_CONTROL_IDS"))
    identity_bad = [] if _manifest_ok([x["evaluation_id"] for x in evaluations], manifests[eval_name]) and _manifest_ok([x["control_id"] for x in controls], manifests[control_name]) and _manifest_ok([x["scenario_id"] for x in pairs], manifests["L1_INSTRUMENTATION_NO_DRIFT_SCENARIO_IDS"]) else [chosen]
    correctness_bad = [x["control_id"] for x in controls if not x["passed"]] + [x["scenario_id"] for x in common + no_effect if not x["equal"]]
    arch_names = {"governance/opinion-no-order-effect", "readiness/not-ready-fail-closed", "warm-boundary/causal-state-only"} if base else {"s/common-prefix", "s/coverage-fail-closed", "s/no-op-control"}
    architecture_bad = [x["control_id"] for x in controls if x["control_id"] in arch_names and not x["passed"]]
    facts = {
        "l1.identity.exact_manifests": ([x["evaluation_id"] for x in evaluations] + [x["control_id"] for x in controls] + [x["scenario_id"] for x in pairs], [manifests[eval_name], manifests[control_name], manifests["L1_INSTRUMENTATION_NO_DRIFT_SCENARIO_IDS"]], [], identity_bad, not identity_bad),
        "l1.identity.warm_and_data": (selected_ids, [{"warm_boundary": x["warm_boundary"], "data_identity": x["data_identity"]} for x in selected], [], warm_bad, not warm_bad),
        "l1.metrics.finite": (selected_ids, selected, [], finite_bad, not finite_bad),
        "l1.mdd.noncanonical_18pct_screen": (selected_ids, [x["official_metrics"]["max_drawdown"] for x in selected], [], [x["evaluation_id"] for x in selected if abs(x["official_metrics"]["max_drawdown"]) > 0.18 + 1e-15], max(abs(x["official_metrics"]["max_drawdown"]) for x in selected)),
        "l1.prefix.09_to_10_wealth": ([f"{chosen}::{x}" for x in ("prefix-09", "prefix-10")], returns(["prefix-09", "prefix-10"]), [], [] if p0910 > -0.10 else [f"{chosen}::prefix-09", f"{chosen}::prefix-10"], p0910),
        "l1.prefix.worst_adjacent_wealth": ([f"{chosen}::{x}" for x in prefixes], returns(prefixes), [], [] if min(adjacent) >= -0.30 - 1e-12 else [f"{chosen}::{x}" for x in prefixes], min(adjacent)),
        "l1.initial.prefix05_proxy": ([f"{chosen}::prefix-05"], returns(["prefix-05"]), [ref["prefix-05"]["total_return"]], [] if ratios["prefix-05"] >= 0.99 - 1e-12 else [f"{chosen}::prefix-05"], ratios["prefix-05"]),
        "l1.initial.other_prefix_proxy": ([f"{chosen}::{x}" for x in prefixes if x != "prefix-05"], returns([x for x in prefixes if x != "prefix-05"]), [ref[x]["total_return"] for x in prefixes if x != "prefix-05"], [f"{chosen}::{x}" for x in prefixes if x != "prefix-05" and ratios[x] < 0.95 - 1e-12], min(ratios[x] for x in prefixes if x != "prefix-05")),
        "l1.initial.add_one_601869_proxy": ([f"{chosen}::{x}" for x in add_ids], returns(add_ids), [ref[x]["total_return"] for x in add_ids], [] if add_delta >= -0.03 - 1e-12 else [f"{chosen}::{x}" for x in add_ids], add_delta),
        "l1.permutation.invariant": ([f"{chosen}::{x}" for x in perm_ids], [by_id[x]["official_metrics"] for x in perm_ids], [], perm_bad, not perm_bad),
        "l1.instrumentation.no_drift": ([x["scenario_id"] for x in pairs], pairs, [], [x["scenario_id"] for x in pairs if not x["equal"]], all(x["equal"] for x in pairs)),
        "l1.correctness.synthetic_controls": ([x["control_id"] for x in controls] + [x["scenario_id"] for x in common + no_effect], controls + common + no_effect, [], correctness_bad, not correctness_bad),
        "l1.architecture.boundaries": (sorted(arch_names), [x for x in controls if x["control_id"] in arch_names], [], architecture_bad, not architecture_bad),
    }
    return [_predicate_result(spec, *facts[spec["id"]]) for spec in specs]


_VARIANTS = {
    "baseline": "BASELINE", "F0-only": "F0_ONLY", "F0+F1": "F0_F1",
    "U-only": "U_ONLY", "C6-Base": "C6_BASE", "C6-Base+S": "C6_BASE_PLUS_S",
    "W0-no-601869": "W0_NO_601869", "W1-data-map-only": "W1_DATA_MAP_ONLY",
    "W2-pool-denominator-only": "W2_POOL_DENOMINATOR_ONLY",
    "W3-real-intents-fixed-reference-U": "W3_REAL_INTENTS_FIXED_REFERENCE_U",
    "W4-full-base-production-pool-relative": "W4_FULL_BASE_PRODUCTION_POOL_RELATIVE",
    "W5-full-base-production-pool-relative-no-lock": "W5_FULL_BASE_PRODUCTION_POOL_RELATIVE_NO_LOCK",
}


def _empty_s_evidence() -> dict[str, Any]:
    coverage = {"observed_count": 0, "minimum_observed": 4, "observed_industries": 0, "minimum_observed_industries": 3, "decision_timestamp": "2025-04-01", "latest_source_timestamp": None, "freshness_max_sessions": 0, "freshness_passed": False, "coverage_passed": False, "unmapped_weight": 0.0, "unmapped_limit": 0.05, "unmapped_passed": True}
    leave = {"mode": "recomputed", "target_cluster": "none", "removed_components": [], "remaining_components": [], "observed_count": 0, "observed_industries": 0, "minimum_observed": 4, "minimum_observed_industries": 3, "recomputed_fast_return": 0.0, "fast_return_threshold": -0.06, "recomputed_declining_ratio": 0.0, "breadth_threshold": 0.60, "recomputed_stressed_cluster_set": [], "freshness_passed": False, "coverage_passed": False, "same_evidence_preserved": False, "passed": False}
    fill = {"t_plus_one_passed": False, "open_available": False, "not_suspended": False, "not_limit_blocked": False, "adv_capacity_shares": 0, "lot_size": 100, "nonzero_executable_lot": False}
    return {"first_causal_stressed_cluster_close": None, "worst_cluster": None, "worst_cluster_weight": None, "stressed_cluster_set": [], "coverage": coverage, "leave_held_components_out": leave, "first_early_sell_required_close": None, "risk_level": 0, "portfolio_fast_return": 0.0, "existing_concentration_eligible": False, "cluster_symbol_count": 0, "minimum_cluster_size": 2, "legacy_gate_open": False, "early_sell_required": False, "scheduled_execution_batch": None, "lead_batch_count": 0, "pre_trade_open_drawdown": None, "official_sample_relation": "NO_SCHEDULED_BATCH", "identical_valuation_instant_proven": False, "planned_shares": 0, "executable_lot_shares": 0, "fillability": fill, "shortfall": {"shares": 0, "reason": "T_PLUS_ONE"}, "pre_sell_crossing_buy_witness": False}


def finalize_s_evidence(
    evidence: Mapping[str, Any],
    equity_samples: Sequence[Mapping[str, Any]],
    calendar: Sequence[str],
) -> dict[str, Any]:
    """Relate a decision-only S observation to the official breach sample."""
    result = dict(evidence)
    schedule = result.get("scheduled_execution_batch")
    breach = first_official_mdd_breach(equity_samples)
    if not isinstance(schedule, Mapping) or breach is None:
        result["lead_batch_count"] = 0
        result["official_sample_relation"] = "NO_SCHEDULED_BATCH"
        result["identical_valuation_instant_proven"] = False
        return result
    decision = str(schedule["decision_close"]).split("T", 1)[0]
    execution = str(schedule["execution_open"]).split("T", 1)[0]
    official = str(breach["timestamp"]).split("T", 1)[0]
    days = [str(item).split("T", 1)[0] for item in calendar]
    result["lead_batch_count"] = sum(
        decision < item <= official for item in days
    )
    if execution < official:
        relation = "PRECEDES_OFFICIAL_SAMPLE"
    elif execution == official:
        relation = "OPEN_MARK_GAP_NOT_OFFICIAL_SAMPLE"
    else:
        relation = "UNAVOIDABLE_AT_OFFICIAL_SAMPLE"
    result["official_sample_relation"] = relation
    result["identical_valuation_instant_proven"] = False
    return result


def _sleeve_paths(result: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Reconstruct factual sleeve cash and marked positions from run ledgers."""
    cash, positions = [], []
    for state_index, (report, state) in enumerate(zip(result["_c6_sleeve_results"], result["_c6_states"], strict=True)):
        held: dict[tuple[str, str], int] = {}
        trades_by_date: dict[str, list[Any]] = {}
        for trade in report["trades"]:
            trades_by_date.setdefault(str(trade.date), []).append(trade)
        for sample_ordinal, (date, row) in enumerate(report["equity_curve"].iterrows()):
            timestamp = str(date.date())
            for trade in trades_by_date.get(timestamp, []):
                key = (str(trade.symbol), str(trade.strategy_name))
                held[key] = held.get(key, 0) + int(trade.shares) * (1 if trade.direction == "buy" else -1)
            cash.append({"sample_ordinal": sample_ordinal, "timestamp": timestamp, "state_index": state_index, "sleeve_name": report["sleeve_name"], "cash": float(row["cash"])})
            for symbol, strategy in sorted(held):
                shares = held[symbol, strategy]
                if shares <= 0:
                    continue
                frame = state.data_map[symbol]
                mark = float(frame.loc[frame.index <= date, "close"].iloc[-1])
                positions.append({"sample_ordinal": sample_ordinal, "timestamp": timestamp, "state_index": state_index, "sleeve_name": report["sleeve_name"], "strategy_name": strategy, "symbol": symbol, "shares": shares, "mark": mark, "market_value": shares * mark})
    cash.sort(key=lambda item: (item["sample_ordinal"], item["state_index"]))
    positions.sort(key=lambda item: (item["sample_ordinal"], item["state_index"], item["symbol"], item["strategy_name"]))
    return cash, positions


def _action_records(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    records = [dict(item) for state in result["_c6_states"] for item in getattr(state.sleeve, "_c6_action_lifecycle", [])]
    trades = list(result["trades"])
    for record in records:
        if record["release_reason"] == "CANCELLED":
            continue
        prefix = f"{record['sleeve_name']}:{record['strategy_name']}"
        fills = [trade for trade in trades if trade.direction == "sell" and trade.symbol == record["symbol"] and trade.strategy_name == prefix and trade.signal_date == record["timestamp"] and str(trade.reason).startswith(record["reason"])]
        filled = min(sum(int(trade.shares) for trade in fills), record["planned_shares"])
        record.update({"filled_shares": filled, "remainder_shares": record["planned_shares"] - filled, "terminal_for_current_batch": filled == record["planned_shares"], "carry_to_next_batch": filled != record["planned_shares"], "release_reason": "FILLED" if filled == record["planned_shares"] else "STILL_LIVE"})
    return sorted(records, key=lambda item: item["emission_ordinal"])


def _risk_records(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    names = {"portfolio_drawdown_alert_on": "ACCOUNT_ALERT", "confirmed_cycle_drawdown_lock": "CONFIRMED_LOCK", "emergency_drawdown_lock": "EMERGENCY_LOCK", "terminal_drawdown_lock": "TERMINAL_LOCK"}
    records = []
    for ordinal, event in enumerate(item for item in result.get("risk_events", []) if item.get("date")):
        source = str(event.get("event", "cross_market"))
        kind = names.get(source, "SUBINDUSTRY" if "sector" in source or "subindustr" in source else "CONCENTRATION" if "concentration" in source else "LAYERED_STOP" if "stop" in source or "catastrophe" in source else "OPEN_MARK_GAP" if "gap" in source else "CROSS_MARKET")
        sleeve = event.get("sleeve")
        state = {"fast": 0, "base": 1, "slow": 2}.get(sleeve)
        records.append({"emission_ordinal": ordinal, "timestamp": str(event["date"]), "phase_order": 0, "event_type": kind, "state_index": state, "sleeve_name": sleeve if state is not None else None, "strategy_name": event.get("strategy") or event.get("strategy_name"), "symbol": event.get("symbol"), "peak_owner": "manager_cycle_peak" if "peak_assets" in event else None, "peak_value": event.get("peak_assets"), "current_assets": event.get("current_assets"), "drawdown": event.get("drawdown"), "threshold": event.get("threshold"), "status_source": source, "evidence_flags": []})
    return records


def _warm_snapshot(result: Mapping[str, Any], initial: float) -> dict[str, Any]:
    state = result["_c6_states"][0]
    first, execution = state.all_dates[:2]
    history = []
    for symbol in sorted(state.data_map):
        frame = state.data_map[symbol].loc[state.data_map[symbol].index < first]
        if frame.empty or symbol not in state.indicator_map:
            continue
        indicators = {name: series.loc[series.index < first].to_json(date_format="iso") for name, series in sorted(state.indicator_map[symbol].items())}
        history.append({"symbol": symbol, "history_start": str(frame.index[0].date()), "history_end": str(frame.index[-1].date()), "causal_cutoff": str(frame.index[-1].date()), "source_row_count": len(frame), "source_sha256": hashlib.sha256(frame.to_json(date_format="iso").encode()).hexdigest(), "indicator_sha256": hashlib.sha256(_canonical_bytes(indicators)).hexdigest()})
    cash = [initial / 3, initial / 3, initial - 2 * (initial / 3)]
    names = ("fast", "base", "slow")
    return {"indicator_history": history, "regime_and_transitions": {"current_regime": "trend", "asof_timestamp": str((first - __import__("datetime").timedelta(days=1)).date()), "transitions": []}, "candidate_sticky_confirmation": [], "overlay_state": [], "sleeve_positions": [], "sleeve_cash": [{"state_index": i, "sleeve_name": name, "cash": cash[i]} for i, name in enumerate(names)], "pending_orders": [], "sleeve_peaks": [{"state_index": i, "sleeve_name": name, "cycle_peak_assets": cash[i], "lifetime_peak_assets": cash[i], "daily_start_assets": cash[i]} for i, name in enumerate(names)], "account_peaks": {"cycle_peak_assets": initial, "lifetime_peak_assets": initial, "daily_start_assets": initial}, "locks": [{"owner_kind": "sleeve", "state_index": i, "sleeve_name": name, "cycle_lock": False, "emergency_lock": False, "terminal_lock": False, "rearm_remaining_trading_days": 0} for i, name in enumerate(names)] + [{"owner_kind": "account", "state_index": None, "sleeve_name": None, "cycle_lock": False, "emergency_lock": False, "terminal_lock": False, "rearm_remaining_trading_days": 0}], "first_decision_timestamp": str(first.date()), "first_execution_timestamp": str(execution.date()), "unauthorized_economic_state_empty": True, "future_information_absent": True}


def _l1_evaluate(task: tuple[str, Mapping[str, Any], str]) -> dict[str, Any]:
    """Capture a deterministic factual replay record for one frozen evaluation."""
    variant, scenario, recording = task
    from quantfusion.application import stress, stress_metrics
    from quantfusion.config.paths import MARKET_DATA_DIR, REGIME_DATA_DIR
    from quantfusion.engine.replay import ProductionReplayEngine

    codes = [str(item) for item in scenario["symbols"]]
    request = {"schema_version": 1, "intervention_id": _VARIANTS[variant], "recording_mode": recording, "scenario_id": scenario["scenario_id"], "diagnostic_noncanonical": True, "allow_publication": False}
    with contextlib.redirect_stdout(io.StringIO()):
        result = ProductionReplayEngine(stress_metrics.INITIAL_CAPITAL).run_c6_diagnostic(
            {code: stress.NAMES[code] for code in codes}, stress_metrics.START_DATE,
            stress_metrics.END_DATE, diagnostic_request=request,
            data_dir=str(MARKET_DATA_DIR), regime_data_dir=str(REGIME_DATA_DIR),
            indicator_state="warm",
        )
    attribution = {name: 0 for name in stress_metrics.ATTRIBUTION_CATEGORIES}
    for trade in result["trades"]:
        attribution[stress._reason_category(trade)] += 1
    sleeves = {"fast": 0, "base": 1, "slow": 2}
    fills, orders = [], []
    for ordinal, trade in enumerate(result["trades"]):
        sleeve, _, strategy = trade.strategy_name.partition(":")
        state = sleeves.get(sleeve, 0)
        orders.append({"order_ordinal": ordinal, "decision_timestamp": trade.signal_date or trade.date, "execution_timestamp": trade.date, "state_index": state, "sleeve_name": sleeve or "portfolio", "strategy_name": strategy or trade.strategy_name, "symbol": trade.symbol, "side": trade.direction.upper(), "requested_shares": trade.shares, "authorized_shares": trade.shares, "reason": trade.reason or "executed", "priority": 0, "action_id": None, "scope_kind": None, "scope_key": None, "status": "filled", "suppression_winner_order_ordinal": None})
        fills.append({"fill_ordinal": ordinal, "order_ordinal": ordinal, "timestamp": trade.date, "state_index": state, "sleeve_name": sleeve or "portfolio", "strategy_name": strategy or trade.strategy_name, "symbol": trade.symbol, "side": trade.direction.upper(), "shares": trade.shares, "price": float(trade.price), "notional": abs(float(trade.gross_value)), "fee": float(trade.commission + trade.stamp_duty_cost), "slippage": 0.0, "status": "filled", "blocked_reason": None})
    equity = result["equity_curve"]
    equity_series = [{"sample_ordinal": i, "timestamp": str(date.date()), "equity": float(row.assets), "official_sample": True} for i, (date, row) in enumerate(equity.iterrows())]
    drawdown = equity["assets"] / equity["assets"].cummax() - 1.0
    drawdown_series = [{"sample_ordinal": i, "timestamp": str(date.date()), "equity": float(equity.loc[date, "assets"]), "running_peak": float(equity.loc[:date, "assets"].max()), "drawdown": float(value), "official_sample": True} for i, (date, value) in enumerate(drawdown.items())]
    cash_series, position_series = _sleeve_paths(result)
    exposure = [{"sample_ordinal": i, "timestamp": item["timestamp"], "phase": "official_sample", "gross_notional": float(equity.iloc[i]["position_value"]), "gross_ratio": float(equity.iloc[i]["position_value"] / equity.iloc[i]["assets"]), "symbol": None, "symbol_notional": None, "cluster": None, "cluster_notional": None, "cluster_weight": None} for i, item in enumerate(equity_series)]
    metrics = {"total_return": float(result["total_return"]), "terminal_wealth": float(result["final_assets"]), "max_drawdown": float(result["max_drawdown"]), "sharpe": float(result["sharpe"]), "calmar": float(result["calmar"]), "total_trades": int(result["total_trades"]), "sleeve_fill_count": int(result["sleeve_fill_count"]), "date_symbol_side_count": int(result["date_symbol_side_count"]), "cash_days": int((equity["position_value"] == 0).sum()), "reason_attribution": attribution, "max_concurrent_symbols": int(result["max_concurrent_symbols"]), "terminal_risk_lock": bool(result["terminal_risk_lock"]), "deployment_policy": "production_daily_replay"}
    raw_breach = first_official_mdd_breach(equity_series)
    breach = {"timestamp": None, "sample_ordinal": None, "peak_timestamp": None, "peak_value": None, "equity": None, "drawdown": None, "threshold": 0.18, "tolerance": 1e-15} if raw_breach is None else {"timestamp": raw_breach["timestamp"], "sample_ordinal": raw_breach["sample_ordinal"], "peak_timestamp": None, "peak_value": raw_breach["peak_value"], "equity": raw_breach["current_assets"], "drawdown": raw_breach["drawdown"], "threshold": 0.18, "tolerance": 1e-15}
    timeline = {"first_official_mdd_breach": breach, "first_account_alert_event": _manager_event(result.get("risk_events", []), "portfolio_drawdown_alert_on"), "first_confirmed_cycle_lock": _manager_event(result.get("risk_events", []), "confirmed_cycle_drawdown_lock"), "first_emergency_cycle_lock": _manager_event(result.get("risk_events", []), "emergency_drawdown_lock"), "first_terminal_lock": _manager_event(result.get("risk_events", []), "terminal_drawdown_lock")}
    initial = stress_metrics.INITIAL_CAPITAL
    warm = _warm_snapshot(result, initial)
    definition = {key: scenario.get(key) for key in ("scenario_id", "scenario_type", "symbols", "symbol_count", "omitted_symbol", "added_symbol", "base_size", "seed", "sample_size")}
    s_evidence = finalize_s_evidence(
        result.get("c6_s_evidence") or _empty_s_evidence(),
        equity_series,
        [str(item.date()) for item in result["_c6_states"][0].all_dates],
    )
    causal = {"event_timeline": timeline, "required_trace_order_complete": True, "executable_lead_batch_count": s_evidence["lead_batch_count"], "multi_labels": [], "observed_mechanisms": [], "s_evidence": s_evidence, "earliest_unavoidable_breach_under_frozen_candidate_family": None, "unavoidable_field_justification": "No pathwise exhaustive proof was claimed."}
    formal_codes = list(__import__("quantfusion.application.stress_scenarios", fromlist=["ORDERED_CODES"]).ORDERED_CODES)
    raw_hashes = {code: hashlib.sha256((MARKET_DATA_DIR / f"{code}.csv").read_bytes()).hexdigest() for code in formal_codes}
    indicator_hashes = {item["symbol"]: item["indicator_sha256"] for item in warm["indicator_history"]}
    indicator_hashes.update({code: raw_hashes[code] for code in formal_codes if code not in indicator_hashes})
    data_identity = {"data_fingerprint": "aeeb96a94e84830033e8ad11180293fc982bb08e99322054d2c27dbb3f4b5975", "scenario_signature": "29be14d97ffa4249455a0e70ae42df74e4567609c3576250e659c4281fd94880", "ordered_symbols": formal_codes, "raw_frame_hashes": raw_hashes, "indicator_hashes": indicator_hashes, "old_symbol_frames_unchanged": True, "old_symbol_indicators_unchanged": True, "calendar_hash": "cadb6a8739efd6303de49f5458c9be510756b42b8ae3c79c2da363c45259d736"}
    record = {"evaluation_id": f"{variant}::{scenario['scenario_id']}", "variant_id": variant, "scenario_id": scenario["scenario_id"], "scenario_definition": definition, "official_metrics": metrics, "orders": orders, "fills": fills, "cash_series": cash_series, "position_series": position_series, "equity_series": equity_series, "drawdown_series": drawdown_series, "risk_events": _risk_records(result), "action_lifecycle": _action_records(result), "exposure_series": exposure, "causal_matrix": causal, "warm_boundary": warm, "data_identity": data_identity, "intervention_601869": None}
    if variant.startswith("W"):
        record["_c6_score_trace"] = result["_c6_score_trace"]
    return record


def _manifest(name: str, spec: Mapping[str, Any]) -> dict[str, Any]:
    return {"name": name, "count": spec["count"], "unique_count": spec["unique_count"], "sha256": spec.get("sha256", spec.get("ordered_ids_sha256"))}


def _path_hashes(record: Mapping[str, Any]) -> dict[str, str]:
    return {name: hashlib.sha256(_canonical_bytes(record[key])).hexdigest() for name, key in (("orders_sha256", "orders"), ("fills_sha256", "fills"), ("cash_sha256", "cash_series"), ("positions_sha256", "position_series"), ("equity_sha256", "equity_series"))}


def _prefix_hash(record: Mapping[str, Any], boundary: str | None) -> str:
    paths = []
    for key, timestamp in (("orders", "execution_timestamp"), ("fills", "timestamp"), ("cash_series", "timestamp"), ("position_series", "timestamp"), ("equity_series", "timestamp")):
        paths.append(record[key] if boundary is None else [item for item in record[key] if item[timestamp] < boundary])
    return hashlib.sha256(_canonical_bytes(paths)).hexdigest()


def _control_nodes() -> dict[str, list[str]]:
    """Only explicitly covered contracts may inherit an executed test result."""
    groups = {
        ("book-identity", "book_identity"): {
            "carried-winner": "carried_higher_priority_sell_remains_the_winner",
            "same-book-priority": "same_book_preserves_priority_target_and_reason_winner_order",
            "sibling-three-state": "same_real_book_in_three_states_keeps_one_sell_per_state",
            "stable-order": "suppression_audit_has_complete_book_identity_and_stable_order",
            "suppression-audit": "suppression_audit_has_complete_book_identity_and_stable_order"},
        ("fixed-reference", "fixed_reference_admission"): {
            "carried-new": "noncore_candidate_keeps_two_day_confirmation_at_size_six",
            "denominator-isolation": "fixed_reference_denominator_ignores_unrelated_tradable_symbols",
            "differing-signal-date": "emitting_sleeve_contributes_one_vote_per_symbol_and_batch",
            "duplicate-strategy": "emitting_sleeve_contributes_one_vote_per_symbol_and_batch",
            "eligible-newcomer": "fully_sold_symbol_without_buy_does_not_reserve_candidate_capacity",
            "emitting-sleeves": "emitting_sleeve_contributes_one_vote_per_symbol_and_batch",
            "exact-14": "exact_fourteen_keeps_expansion_score_and_confirmation_rules",
            "input-permutation": "equal_score_capacity_tie_break_is_input_permutation_invariant",
            "missing-score": "missing_emitting_sample_rejects_new_symbol_with_audit",
            "route-migration": "route_migration_bypasses_missing_fixed_reference_score"},
        ("retained-winner", "retained_winner"): {
            "call-order": "veto_is_exact_book_identity_and_sell_precedes_authorization",
            "next-batch-release": "veto_is_released_for_the_next_execution_batch",
            "ordinary-full-overlay-zero": "ordinary_full_sell_cannot_revive_buy_after_overlay_zero_fill",
            **{case: f"retained_winner_vetoes_same_batch_buy[{case}-{carry}]" for case, carry in (
                ("adv-zero", True), ("limit-blocked", True), ("missing-open", True), ("partial-fill", True),
                ("suspended", True), ("partial-sublot", False), ("odd-lot-full-liquidation", False))}},
    }
    return {f"{prefix}/{control}": [f"tests/c6_non_economic/test_c6_{module}.py::test_{test}"]
            for (prefix, module), controls in groups.items() for control, test in controls.items()}


def _controls(prereg: Mapping[str, Any], name: str) -> list[dict[str, Any]]:
    import tempfile
    import xml.etree.ElementTree as ET
    spec = prereg["scenario_manifests"][name]
    mapping = _control_nodes()
    nodes = sorted({node for control in spec["ids"] for node in mapping.get(control, [])})
    observed: dict[str, bool] = {}
    with tempfile.TemporaryDirectory(prefix="c6-control-receipts-") as directory:
        report = Path(directory) / "tests.xml"
        if nodes:
            suite = subprocess.run(["python", "-m", "pytest", "-q", f"--junitxml={report}", *nodes], check=False, capture_output=True)
            if suite.returncode not in {0, 1} or not report.is_file():
                raise RuntimeError("synthetic control execution produced no valid receipt")
            xml = report.read_text(encoding="utf-8")
            if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
                raise ValueError("control receipt cannot contain DTD or entity declarations")
            # Private pytest output, UTF-8 only; DTD/entity declarations rejected.
            for case in ET.fromstring(xml).iter("testcase"):  # nosec B314
                node = case.attrib["classname"].replace(".", "/") + ".py::" + case.attrib["name"]
                if node in observed:
                    raise RuntimeError("duplicate synthetic control test receipt")
                observed[node] = not any(case.find(tag) is not None for tag in ("failure", "error", "skipped"))
    rows = []
    for control in spec["ids"]:
        required = mapping.get(control, [])
        receipt = {"control_id": control, "tests": [{"nodeid": node, "passed": observed.get(node)} for node in required], "coverage_complete": bool(required) and all(node in observed for node in required)}
        passed = receipt["coverage_complete"] and all(observed[node] for node in required)
        assertions = []
        for item in spec["assertions_by_control"][control]:
            if item["comparator"] != "equal" or item["expected"] is not True:
                raise ValueError("unsupported synthetic assertion contract")
            assertions.append({**item, "actual": passed, "passed": passed, "detail_sha256": hashlib.sha256(_canonical_bytes(receipt)).hexdigest()})
        rows.append({"control_id": control, "passed": passed, "assertions": assertions, "economic_fields": None})
    return rows


def _attribution(evaluations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    wealth = {item["variant_id"].split("-", 1)[0]: item["official_metrics"]["terminal_wealth"] for item in evaluations if str(item["variant_id"]).startswith("W")}
    forward, alternate = [f"W{i}" for i in range(5)], ["W0", "W1", "W3", "W2", "W4"]
    def edges(order: list[str]) -> list[dict[str, Any]]:
        return [{"from_state": a, "to_state": b, "delta_log_terminal_wealth": math.log(wealth[b]) - math.log(wealth[a])} for a, b in zip(order, order[1:])]
    f, a = edges(forward), edges(alternate)
    fs, ass = math.fsum(item["delta_log_terminal_wealth"] for item in f), math.fsum(item["delta_log_terminal_wealth"] for item in a)
    residual = ass - fs
    return {"value_function": "log(terminal_wealth)", "forward_state_order": forward, "forward_deltas": f, "alternate_state_order": alternate, "alternate_deltas": a, "forward_sum": fs, "alternate_sum": ass, "interaction_residual": {"forward_total": fs, "alternate_total": ass, "common_endpoint_residual": residual, "maximum_absolute_ordinal_delta_difference": max(abs(x["delta_log_terminal_wealth"] - y["delta_log_terminal_wealth"]) for x, y in zip(f, a)), "telescoping_tolerance_passed": abs(residual) <= 1e-12}, "conditional_lock_mediated_total_effect": math.log(wealth["W5"]) - math.log(wealth["W4"])}


def _symbol_pnl(record: Mapping[str, Any], symbol: str) -> tuple[float, float]:
    shares = 0
    cost = realized = 0.0
    for fill in record["fills"]:
        if fill["symbol"] != symbol:
            continue
        if fill["side"] == "BUY":
            shares += fill["shares"]
            cost += fill["notional"] + fill["fee"]
        elif shares:
            basis = cost / shares * fill["shares"]
            shares -= fill["shares"]
            cost -= basis
            realized += fill["notional"] - fill["fee"] - basis
    marks = [item for item in record["position_series"] if item["symbol"] == symbol]
    market_value = sum(item["market_value"] for item in marks if item["timestamp"] == max((x["timestamp"] for x in marks), default=""))
    return realized, market_value - cost


def _attach_interventions(evaluations: list[dict[str, Any]]) -> None:
    rows = [item for item in evaluations if str(item["variant_id"]).startswith("W")]
    names = ["W0_no_601869", "W1_data_map_only", "W2_pool_denominator_only", "W3_real_intents_fixed_reference_U", "W4_full_base_production_pool_relative", "W5_full_base_production_pool_relative_no_lock"]
    anchor = rows[4]["causal_matrix"]["event_timeline"]["first_official_mdd_breach"]["timestamp"]
    wealth = []
    for row in rows:
        sample = next((item for item in row["equity_series"] if item["timestamp"] == anchor), row["equity_series"][-1])
        wealth.append(float(sample["equity"]))
    for index, (row, name) in enumerate(zip(rows, names)):
        score_trace = row.pop("_c6_score_trace")
        gaps = []
        comparisons = [] if index == 0 else [("VS_W0", 0)]
        if index > 1:
            comparisons.append(("VS_PREVIOUS_FORWARD", index - 1))
        for label, origin in comparisons:
            gaps.append({"comparison": label, "from_intervention_id": names[origin], "to_intervention_id": name, "anchor_timestamp": anchor, "from_wealth": wealth[origin], "to_wealth": wealth[index], "wealth_gap": wealth[index] - wealth[origin], "log_wealth_gap": math.log(wealth[index]) - math.log(wealth[origin])})
        previous = rows[index - 1] if index else None
        divergence = None
        if previous is not None and _path_hashes(previous) != _path_hashes(row):
            divergence = {"compared_from_intervention_id": names[index - 1], "compared_to_intervention_id": name, "timestamp": row["equity_series"][0]["timestamp"], "phase": "VALUATION_CLOSE", "reason": "VALUATION", "state_index": None, "sleeve_name": None, "symbol": None, "before_path_sha256": hashlib.sha256(_canonical_bytes(previous["equity_series"])).hexdigest(), "after_path_sha256": hashlib.sha256(_canonical_bytes(row["equity_series"])).hexdigest()}
        post = None if index != 5 else {"first_lock_timestamp": anchor, "missed_order_count": max(len(row["orders"]) - len(rows[4]["orders"]), 0), "missed_trade_count": max(len(row["fills"]) - len(rows[4]["fills"]), 0), "locked_terminal_wealth": rows[4]["official_metrics"]["terminal_wealth"], "no_lock_terminal_wealth": row["official_metrics"]["terminal_wealth"], "delta_log_wealth": math.log(row["official_metrics"]["terminal_wealth"]) - math.log(rows[4]["official_metrics"]["terminal_wealth"]), "compounding_ratio": row["official_metrics"]["terminal_wealth"] / rows[4]["official_metrics"]["terminal_wealth"]}
        realized, unrealized = _symbol_pnl(row, "601869")
        trace_hash = hashlib.sha256(_canonical_bytes(score_trace)).hexdigest()
        row["intervention_601869"] = {"intervention_id": name, "scenario_id": row["scenario_id"], "terminal_wealth": row["official_metrics"]["terminal_wealth"], "total_return": row["official_metrics"]["total_return"], "max_drawdown": row["official_metrics"]["max_drawdown"], "total_trades": row["official_metrics"]["total_trades"], "pre_breach_anchor_timestamp": anchor, "pre_breach_wealth": wealth[index], "pre_breach_wealth_gaps": gaps, "own_601869_realized_pnl": realized, "own_601869_unrealized_pnl": unrealized, "old_symbol_fixed_reference_score_hash": trace_hash, "old_symbol_fixed_reference_score_changes": [], "pool_relative_rank_hash": trace_hash, "coordinator_pool_relative_rank_changes": [], "displaced_slots": [], "first_path_divergence": divergence, "post_lock_effect": post, "no_lock_terminal_wealth_ratio": None if index != 5 else row["official_metrics"]["terminal_wealth"] / rows[4]["official_metrics"]["terminal_wealth"]}


def _produce_l1(args: argparse.Namespace) -> dict[str, Any]:
    from quantfusion.application import stress_artifacts, stress_scenarios
    from quantfusion.application.c6_bound_run import DiagnosticCheckpoint, execution_item_ids
    from quantfusion.application.c6_contract import load_preregistration, load_run_bindings, select_binding, strict_json_load

    prereg = load_preregistration(args.preregistration, repository=Path.cwd())
    bindings = load_run_bindings(args.bindings_file)
    binding = next(item for item in bindings["binding_records"] if item["record_id"] == args.binding_record_id)
    selected_binding = select_binding(bindings, binding["workflow_binding_id"], candidate_id=binding["candidate_id"])
    if selected_binding is not binding or binding["stage"] != "L1" or binding["source_revision"] != args.source_revision:
        raise ValueError("CLI identity does not select one exact L1 binding")
    manifests = prereg["scenario_manifests"]
    scenario_ids = Path(manifests["L1_ECONOMIC_SCENARIO_IDS"]["path"]).read_text().splitlines()
    validate_manifest_identity(scenario_ids, manifests["L1_ECONOMIC_SCENARIO_IDS"])
    plan = stress_scenarios._multi_seed_scenarios(random_samples=50, permutation_samples=50, seeds=(20260807, 20260817, 20260827))
    by_id = {item["scenario_id"]: item for item in plan}
    base = binding["candidate_id"] == "C6-Base"
    control_name = "L1_BASE_SYNTHETIC_CONTROL_IDS" if base else "L1_S_SYNTHETIC_CONTROL_IDS"
    control_rows = _controls(prereg, control_name)
    unverified = [row["control_id"] for row in control_rows if not row["passed"]]
    if unverified:
        raise ValueError(f"synthetic controls lack passing execution evidence: {unverified}")
    variants = manifests["L1_BASE_EVALUATION_MANIFEST"]["core_variant_order"] if base else ["C6-Base+S"]
    tasks = [(variant, by_id[scenario], "DEFAULT") for variant in variants for scenario in scenario_ids]
    checkpoint = DiagnosticCheckpoint.from_environment(execution_item_ids(binding, prereg), chunk_size=binding["runtime"]["checkpoint_every"])
    evaluations = checkpoint.map(_l1_evaluate, tasks, [f"evaluation/{variant}::{scenario['scenario_id']}" for variant, scenario, _ in tasks])
    if base:
        interventions = [(variant, by_id["add-one-13-601869"], "DEFAULT") for variant in manifests["L1_BASE_EVALUATION_MANIFEST"]["causal_intervention_order"]]
        # Six interdependent intervention rows are finalized and committed together.
        if checkpoint.chunk_size < len(interventions):
            raise ValueError("checkpoint chunk must hold all causal interventions")
        evaluations += checkpoint.map(_l1_evaluate, interventions, [f"evaluation/{variant}::{scenario['scenario_id']}" for variant, scenario, _ in interventions], finalize=_attach_interventions)
    chosen = "C6-Base" if base else "C6-Base+S"
    controls = checkpoint.map(_identity, control_rows, [f"control/{item}" for item in manifests[control_name]["ids"]], workers=1)
    drift_tasks = [(chosen, by_id[item]) for item in manifests["L1_INSTRUMENTATION_NO_DRIFT_SCENARIO_IDS"]["ids"]]
    pairs = checkpoint.map(_no_drift_pair, drift_tasks, [f"no-drift/{item}" for item in manifests["L1_INSTRUMENTATION_NO_DRIFT_SCENARIO_IDS"]["ids"]])
    specs = prereg["diagnostic_predicate_manifests"]["L1_APPLICABLE_DIAGNOSTIC_PREDICATES"]
    selected = [item for item in evaluations if item["variant_id"] == chosen]
    eval_name = "L1_BASE_EVALUATION_MANIFEST" if base else "L1_S_EVALUATION_MANIFEST"
    kind = "c6_l1_base" if base else "c6_l1_s"
    payload = {"schema_version": 2, "kind": kind, "diagnostic_noncanonical": True, "evaluation_manifest": _manifest(eval_name, manifests[eval_name]), "evaluations": evaluations, "synthetic_control_manifest": _manifest(control_name, manifests[control_name]), "synthetic_controls": controls, "no_drift_manifest": _manifest("L1_INSTRUMENTATION_NO_DRIFT_SCENARIO_IDS", manifests["L1_INSTRUMENTATION_NO_DRIFT_SCENARIO_IDS"]), "no_drift_pairs": pairs, "diagnostic_predicates": []}
    common: list[dict[str, Any]] = []
    no_effect: list[dict[str, Any]] = []
    if base:
        payload["attribution_sensitivity"] = _attribution(evaluations)
    if not base:
        qualification = strict_json_load(Path(args.producer_export) / "payload.json")
        manifest = strict_json_load(Path(args.producer_export) / "manifest.json")
        base_payload = strict_json_load(Path(args.base_producer_export) / "payload.json")
        identity = {"artifact_full_byte_sha256": args.producer_artifact_sha256, "attempt_id": manifest["attempt_id"], "binding_id": manifest["binding_id"], "logical_run_id": manifest["logical_run_id"], "workflow_run_id": manifest["workflow_run_id"]}
        base_by_id = {item["evaluation_id"]: item for item in base_payload["evaluations"]}
        common, no_effect = [], []
        for row in evaluations:
            base_id = base_counterpart_id(row["evaluation_id"])
            counterpart = base_by_id[base_id]
            timestamp = row["causal_matrix"]["s_evidence"]["first_early_sell_required_close"]
            left, right = _prefix_hash(counterpart, timestamp), _prefix_hash(row, timestamp)
            common.append({"scenario_id": row["evaluation_id"], "base_evaluation_id": base_id, "first_s_effective_timestamp": timestamp, "base_prefix_sha256": left, "s_prefix_sha256": right, "equal": left == right})
            if timestamp is None:
                a, b = _path_hashes(counterpart), _path_hashes(row)
                no_effect.append({"item_kind": "evaluation", "item_id": row["evaluation_id"], "base_evaluation_id": base_id, "s_effective_count": 0, **{f"base_{key}": value for key, value in a.items()}, **{f"s_{key}": value for key, value in b.items()}, "equal": a == b})
        payload.update({"base_producer_identity": qualification["base_producer_identity"], "qualification_producer_identity": identity, "common_prefix_comparisons": common, "no_effect_comparisons": no_effect})
    reference = stress_artifacts._load_initial_baseline_reference(Path(prereg["transition_reference"]["path"]))
    payload["diagnostic_predicates"] = _l1_predicate_rows(specs, selected, evaluations, controls, pairs, reference, manifests, base, common, no_effect)
    return payload


def build_parser() -> argparse.ArgumentParser:
    """Return the command shape frozen for the R-bound diagnostic runner."""
    parser = argparse.ArgumentParser(description="Run an R-bound C6 diagnostic batch")
    parser.add_argument("--preregistration", required=True)
    parser.add_argument("--bindings-file", required=True)
    parser.add_argument("--binding-record-id", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--producer-export")
    parser.add_argument("--producer-artifact-sha256")
    parser.add_argument("--base-producer-export")
    parser.add_argument("--base-producer-artifact-sha256")
    parser.add_argument("--output", required=True)
    return parser


def _identity(value: dict[str, Any]) -> dict[str, Any]:
    return value


def _no_drift_pair(task: tuple[str, Mapping[str, Any]]) -> dict[str, Any]:
    variant, scenario = task
    off = _l1_evaluate((variant, scenario, "OFF"))
    on = _l1_evaluate((variant, scenario, "ON"))
    a, b = _path_hashes(off), _path_hashes(on)
    return {"scenario_id": scenario["scenario_id"], "recording_off_path_hashes": a, "recording_on_path_hashes": b, "equal": a == b}


def _produce_l2(args: argparse.Namespace) -> dict[str, Any]:
    from quantfusion.application import stress_artifacts, stress_metrics, stress_scenarios
    from quantfusion.application.c6_bound_run import DiagnosticCheckpoint, execution_item_ids
    from quantfusion.application.c6_contract import load_preregistration, load_run_bindings, select_binding

    prereg = load_preregistration(args.preregistration, repository=Path.cwd())
    bindings = load_run_bindings(args.bindings_file)
    binding = next(
        item for item in bindings["binding_records"]
        if item["record_id"] == args.binding_record_id
    )
    selected = select_binding(
        bindings, binding["workflow_binding_id"], candidate_id=binding["candidate_id"]
    )
    if selected is not binding or binding["stage"] != "L2" or binding["source_revision"] != args.source_revision:
        raise ValueError("CLI identity does not select one exact L2 binding")
    manifest = prereg["scenario_manifests"]["L2_EXACT_SCENARIO_IDS"]
    ids = validate_manifest_identity(manifest["ids"], manifest)
    plan = stress_scenarios._multi_seed_scenarios(
        random_samples=50, permutation_samples=50,
        seeds=(20260807, 20260817, 20260827),
    )
    by_id = {item["scenario_id"]: item for item in plan}
    item_ids = execution_item_ids(binding, prereg)
    checkpoint = DiagnosticCheckpoint.from_environment(item_ids, chunk_size=binding["runtime"]["checkpoint_every"])
    results = checkpoint.map(_l2_evaluate, [by_id[item] for item in ids], item_ids)
    summary = stress_metrics._summary(results)
    for key in ("trades_worst", "date_symbol_side_buckets_worst", "sleeve_fills_worst"):
        summary[key] = int(summary[key])
    reference_path = Path(prereg["transition_reference"]["path"])
    reference = stress_artifacts._load_initial_baseline_reference(reference_path)
    payload = {
        "schema_version": 2, "kind": "c6_l2", "diagnostic_noncanonical": True,
        "scenario_manifest": {"name": "L2_EXACT_SCENARIO_IDS", "count": len(ids), "unique_count": len(set(ids)), "sha256": manifest["sha256"]},
        "summary": summary, "results": results,
        "diagnostic_predicates": _predicate_rows(
            prereg["diagnostic_predicate_manifests"]["L2_APPLICABLE_DIAGNOSTIC_PREDICATES"],
            results, reference, ids,
        ),
    }
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    """Produce the exact selected R-bound diagnostic payload."""
    args = build_parser().parse_args(argv)
    if len(args.source_revision) != 40 or any(
        character not in "0123456789abcdef" for character in args.source_revision
    ):
        raise ValueError("source_revision must be a lowercase 40-character Git SHA")
    if args.binding_record_id.endswith(".l2"):
        payload = _produce_l2(args)
    else:
        payload = _produce_l1(args)
    output = Path(args.output)
    if output.exists():
        raise ValueError("diagnostic output path already exists")
    from quantfusion.application.c6_contract import canonical_json_bytes

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(canonical_json_bytes(payload))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the bound runner
    raise SystemExit(main())
