"""Pure C6 diagnostic and qualification predicates over recorded evidence."""
from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from typing import Any

from quantfusion.io.c6_stream import select_records

from quantfusion.application.c6_contract import canonical_json_bytes as _canonical_bytes, canonical_payload_hash


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


def _metrics_equal(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if set(left) != set(right):
        return False
    return all(abs(float(left[key]) - float(right[key])) <= 1e-12
               if type(left[key]) in {int, float} and type(right[key]) in {int, float}
               else left[key] == right[key] for key in left)


def _predicate_result(spec: Mapping[str, Any], ids: list[str], values: object, references: object, failed: list[str], value: object) -> dict[str, Any]:
    failed = sorted(set(failed))
    detail = {"predicate_id": spec["id"], "input_item_ids": ids, "input_values_sha256": canonical_payload_hash(values), "reference_values_sha256": canonical_payload_hash(references if spec["reference_available"] else [])}
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
        if any(not _metrics_equal(left, right) for i, left in enumerate(fields) for right in fields[i + 1:]):
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


def _l1_predicate_rows(specs: Sequence[Mapping[str, Any]], selected: Sequence[dict[str, Any]], evaluations: Sequence[dict[str, Any]], controls: list[dict[str, Any]], pairs: list[dict[str, Any]], reference: Mapping[str, Any], manifests: Mapping[str, Any], base: bool, common: list[dict[str, Any]], no_effect: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chosen = "C6-Base" if base else "C6-Base+S"
    by_id = {item["scenario_id"]: {key: item[key] for key in ("scenario_definition", "official_metrics")} for item in selected}
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
        if any(not _metrics_equal(left, right) for i, left in enumerate(metrics) for right in metrics[i + 1:]):
            perm_bad.extend(f"{chosen}::{item}" for item in group)
    warm_bad = [item["evaluation_id"] for item in selected if item["warm_boundary"]["indicator_history"] == [] or item["data_identity"]["ordered_symbols"] != list(__import__("quantfusion.application.stress_scenarios", fromlist=["ORDERED_CODES"]).ORDERED_CODES)]
    finite_bad = [item["evaluation_id"] for item in selected if not all(math.isfinite(number) for number in _numbers(item))]
    eval_name, control_name = (("L1_BASE_EVALUATION_MANIFEST", "L1_BASE_SYNTHETIC_CONTROL_IDS") if base else ("L1_S_EVALUATION_MANIFEST", "L1_S_SYNTHETIC_CONTROL_IDS"))
    identity_bad = [] if _manifest_ok([x["evaluation_id"] for x in evaluations], manifests[eval_name]) and _manifest_ok([x["control_id"] for x in controls], manifests[control_name]) and _manifest_ok([x["scenario_id"] for x in pairs], manifests["L1_INSTRUMENTATION_NO_DRIFT_SCENARIO_IDS"]) else [chosen]
    correctness_bad = [x["control_id"] for x in controls if not x["passed"]] + [x["scenario_id"] for x in common if not x["equal"]] + [x["item_id"] for x in no_effect if not x["equal"]]
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
        "l1.correctness.synthetic_controls": ([x["control_id"] for x in controls] + [x["scenario_id"] for x in common] + [x["item_id"] for x in no_effect], controls + common + no_effect, [], correctness_bad, not correctness_bad),
        "l1.architecture.boundaries": (sorted(arch_names), [x for x in controls if x["control_id"] in arch_names], [], architecture_bad, not architecture_bad),
    }
    return [_predicate_result(spec, *facts[spec["id"]]) for spec in specs]


_CRITERIA = (
    (
        "q1_base_breach_exists",
        (
            "base.evaluation.official_metrics.max_drawdown",
            "base.evaluation.causal_matrix.event_timeline.first_official_mdd_breach.timestamp",
        ),
        "Q1_BASE_BREACH_MISSING",
    ),
    (
        "q2_causal_stress_evidence_precedes_breach",
        (
            "base.evaluation.causal_matrix.s_evidence.first_causal_stressed_cluster_close",
            "base.evaluation.causal_matrix.event_timeline.first_official_mdd_breach.timestamp",
        ),
        "Q2_CAUSAL_EVIDENCE_NOT_EARLY",
    ),
    (
        "q3_dominant_cluster_is_stressed_and_over_cap",
        (
            "base.evaluation.causal_matrix.s_evidence.worst_cluster",
            "base.evaluation.causal_matrix.s_evidence.worst_cluster_weight",
            "base.evaluation.causal_matrix.s_evidence.stressed_cluster_set",
        ),
        "Q3_CLUSTER_NOT_STRESSED_OR_OVER_CAP",
    ),
    (
        "q4_complete_independent_evidence",
        (
            "base.evaluation.causal_matrix.s_evidence.coverage",
            "base.evaluation.causal_matrix.s_evidence.leave_held_components_out",
        ),
        "Q4_EVIDENCE_INCOMPLETE",
    ),
    (
        "q5_scheduled_prebreach_nonzero_lot",
        (
            "base.evaluation.causal_matrix.s_evidence.first_early_sell_required_close",
            "base.evaluation.causal_matrix.s_evidence.risk_level",
            "base.evaluation.causal_matrix.s_evidence.portfolio_fast_return",
            "base.evaluation.causal_matrix.s_evidence.existing_concentration_eligible",
            "base.evaluation.causal_matrix.s_evidence.cluster_symbol_count",
            "base.evaluation.causal_matrix.s_evidence.minimum_cluster_size",
            "base.evaluation.causal_matrix.s_evidence.legacy_gate_open",
            "base.evaluation.causal_matrix.s_evidence.early_sell_required",
            "base.evaluation.causal_matrix.s_evidence.scheduled_execution_batch",
            "base.evaluation.causal_matrix.s_evidence.planned_shares",
            "base.evaluation.causal_matrix.s_evidence.executable_lot_shares",
            "base.evaluation.causal_matrix.s_evidence.book_fillability",
            "base.evaluation.causal_matrix.s_evidence.queue_fillability",
            "base.evaluation.causal_matrix.s_evidence.fillability",
            "base.evaluation.causal_matrix.s_evidence.shortfall",
        ),
        "Q5_NO_PREBREACH_EXECUTABLE_ACTION",
    ),
    (
        "q6_action_strictly_precedes_breach_sample",
        (
            "base.evaluation.causal_matrix.s_evidence.scheduled_execution_batch.execution_open",
            "base.evaluation.causal_matrix.event_timeline.first_official_mdd_breach.timestamp",
            "base.evaluation.causal_matrix.s_evidence.lead_batch_count",
        ),
        "Q6_ACTION_NOT_BEFORE_BREACH",
    ),
    (
        "q7_official_sample_gap_not_proven_unavoidable",
        (
            "base.evaluation.causal_matrix.event_timeline.first_official_mdd_breach.timestamp",
            "base.evaluation.causal_matrix.s_evidence.official_sample_relation",
            "base.evaluation.causal_matrix.s_evidence.pre_trade_open_drawdown",
            "base.evaluation.causal_matrix.s_evidence.identical_valuation_instant_proven",
        ),
        "Q7_GAP_PROVEN_UNAVOIDABLE",
    ),
)


def _resolve(evaluation: Mapping[str, Any], path: str) -> Any:
    prefix = "base.evaluation."
    if not path.startswith(prefix):
        raise ValueError(f"qualification path has invalid root: {path}")
    value: Any = evaluation
    for segment in path[len(prefix):].split("."):
        if value is None:
            return None
        if not isinstance(value, Mapping) or segment not in value:
            raise ValueError(f"qualification path does not resolve: {path}")
        value = value[segment]
    return value


def _ordered_unique(values: object) -> bool:
    return (
        isinstance(values, list)
        and all(isinstance(value, str) and value for value in values)
        and values == sorted(values)
        and len(values) == len(set(values))
    )


def _coverage_valid(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    try:
        freshness = (
            value["latest_source_timestamp"] == value["decision_timestamp"]
        )
        coverage = (
            value["observed_count"] >= 4
            and value["observed_industries"] >= 3
        )
        unmapped = value["unmapped_weight"] < 0.05
        return (
            value["minimum_observed"] == 4
            and value["minimum_observed_industries"] == 3
            and value["freshness_max_sessions"] == 0
            and value["unmapped_limit"] == 0.05
            and value["freshness_passed"] is freshness
            and value["coverage_passed"] is coverage
            and value["unmapped_passed"] is unmapped
            and freshness and coverage and unmapped
        )
    except (KeyError, TypeError):
        return False


def _leave_valid(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    try:
        removed = value["removed_components"]
        remaining = value["remaining_components"]
        stressed = value["recomputed_stressed_cluster_set"]
        if not all(_ordered_unique(item) for item in (removed, remaining, stressed)):
            return False
        coverage = (
            value["observed_count"] >= 4
            and value["observed_industries"] >= 3
        )
        same = value["target_cluster"] in stressed
        common = (
            value["minimum_observed"] == 4
            and value["minimum_observed_industries"] == 3
            and value["fast_return_threshold"] == -0.06
            and value["breadth_threshold"] == 0.60
            and value["coverage_passed"] is coverage
            and value["same_evidence_preserved"] is same
        )
        if value["mode"] == "disjoint_pass":
            passed = not removed and same
        elif value["mode"] == "recomputed":
            passed = (
                bool(removed)
                and value["freshness_passed"] is True
                and coverage
                and value["recomputed_fast_return"] <= -0.06
                and value["recomputed_declining_ratio"] >= 0.60
                and same
            )
        else:
            return False
        return common and value["passed"] is passed and passed
    except (KeyError, TypeError):
        return False


def _queue_valid(evidence: Mapping[str, Any]) -> bool:
    """Reconcile the ordered share-only feasibility receipts independently."""
    queue = evidence["queue_fillability"]
    if not isinstance(queue, list):
        return False
    books = {(row["state_index"], row["symbol"], row["strategy_name"]): row for row in evidence["book_fillability"]}
    if len(books) != len(evidence["book_fillability"]):
        return False
    remaining, inventory, observed = {}, {}, set()
    previous_order = (-1, -1)
    ranks = {"turtle_breakout": 0, "dual_ma": 1, "atr_channel": 2}
    for row in queue:
        for key in ("state_index", "requested_shares", "inventory_before_shares", "raw_adv_capacity_shares", "capacity_shares", "executable_shares"):
            if type(row[key]) is not int or row[key] < 0:
                return False
        state_symbol = (row["state_index"], row["symbol"])
        book = (*state_symbol, row["strategy_name"])
        order = (row["state_index"], ranks.get(row["strategy_name"], 99))
        if order < previous_order:
            return False
        previous_order = order
        before, capacity, filled = row["inventory_before_shares"], row["capacity_shares"], row["executable_shares"]
        if state_symbol in remaining and row["raw_adv_capacity_shares"] != remaining[state_symbol]:
            return False
        if book in inventory and before != inventory[book]:
            return False
        if book not in inventory and book in books and before != books[book]["current_shares"]:
            return False
        available = min(before, row["requested_shares"], row["raw_adv_capacity_shares"])
        expected_capacity = available if available == before else available // 100 * 100
        permitted = all(row[key] is True for key in ("t_plus_one_passed", "open_available", "not_suspended", "not_limit_blocked"))
        if capacity != expected_capacity or filled != (capacity if permitted else 0):
            return False
        remaining[state_symbol] = row["raw_adv_capacity_shares"] - filled
        inventory[book] = before - filled
        if type(row["is_s_proposal"]) is not bool:
            return False
        if row["is_s_proposal"]:
            if book not in books or book in observed:
                return False
            observed.add(book)
            target = books[book]
            if target["suppression_winner_reason"] is not None or row["requested_shares"] != target["planned_shares"]:
                return False
            for key in ("inventory_before_shares", "raw_adv_capacity_shares", "capacity_shares", "executable_shares",
                        "t_plus_one_passed", "open_available", "not_suspended", "not_limit_blocked"):
                if row[key] != target[key]:
                    return False
    return all(book in observed or (isinstance(row["suppression_winner_reason"], str)
                                   and row["capacity_shares"] == row["executable_shares"] == 0)
               for book, row in books.items())


def _fill_valid(evidence: Mapping[str, Any]) -> bool:
    fill = evidence.get("fillability")
    shortfall = evidence.get("shortfall")
    if not isinstance(fill, Mapping) or not isinstance(shortfall, Mapping):
        return False
    try:
        planned = evidence["planned_shares"]
        executable = evidence["executable_lot_shares"]
        books = evidence.get("book_fillability")
        if books is not None:
            if not isinstance(books, list) or not books:
                return False
            queue = evidence.get("queue_fillability")
            if queue is not None and not _queue_valid(evidence):
                return False
            shared = {}
            for row in books:
                integers = ("state_index", "current_shares", "planned_shares", "raw_adv_capacity_shares", "capacity_shares", "executable_shares")
                if any(type(row[key]) is not int or row[key] < 0 for key in integers):
                    return False
                key = (row["state_index"], row["symbol"])
                if queue is None and key in shared and row["raw_adv_capacity_shares"] > shared[key]:
                    return False
                shared[key] = row["raw_adv_capacity_shares"] - row["capacity_shares"]
                if not 0 <= row["executable_shares"] <= row["capacity_shares"] <= min(row["planned_shares"], row.get("inventory_before_shares", row["current_shares"]), row["raw_adv_capacity_shares"]):
                    return False
                if row["executable_shares"] % 100 and row["executable_shares"] != row.get("inventory_before_shares", row["current_shares"]):
                    return False
                permitted = all(row[key] is True for key in ("t_plus_one_passed", "open_available", "not_suspended", "not_limit_blocked"))
                if row["executable_shares"] != (row["capacity_shares"] if permitted else 0):
                    return False
            if (sum(row["planned_shares"] for row in books) != planned
                    or sum(row["executable_shares"] for row in books) != executable
                    or sum(row["capacity_shares"] for row in books) != fill["adv_capacity_shares"]):
                return False
        minimum = 1 if books is not None else fill["lot_size"]
        nonzero = (
            fill["t_plus_one_passed"] is True
            and fill["open_available"] is True
            and fill["not_suspended"] is True
            and fill["not_limit_blocked"] is True
            and fill["adv_capacity_shares"] >= minimum
            and executable >= minimum
            and executable <= planned
        )
        shares = max(planned - executable, 0)
        checks = (
            (fill["t_plus_one_passed"], "T_PLUS_ONE"),
            (fill["open_available"], "MISSING_OPEN"),
            (fill["not_suspended"], "SUSPENDED"),
            (fill["not_limit_blocked"], "LIMIT_BLOCKED"),
            (fill["adv_capacity_shares"] > 0, "ADV_ZERO"),
            (fill["adv_capacity_shares"] >= planned, "CAPACITY"),
            (executable >= minimum, "SUB_LOT"),
        )
        reason = next((name for passed, name in checks if not passed), "NONE")
        return (
            fill["lot_size"] == 100
            and fill["nonzero_executable_lot"] is nonzero
            and shortfall == {"shares": shares, "reason": reason}
            and nonzero
        )
    except (KeyError, TypeError):
        return False


def _criterion_passes(
    criterion_id: str, evaluation: Mapping[str, Any]
) -> bool:
    evidence = evaluation["causal_matrix"]["s_evidence"]
    breach = evaluation["causal_matrix"]["event_timeline"][
        "first_official_mdd_breach"
    ]["timestamp"]
    if criterion_id == "q1_base_breach_exists":
        return (
            abs(float(evaluation["official_metrics"]["max_drawdown"]))
            > 0.18 + 1e-15
            and isinstance(breach, str) and bool(breach)
        )
    if criterion_id == "q2_causal_stress_evidence_precedes_breach":
        first = evidence["first_causal_stressed_cluster_close"]
        return isinstance(first, str) and isinstance(breach, str) and first < breach
    if criterion_id == "q3_dominant_cluster_is_stressed_and_over_cap":
        weight = evidence["worst_cluster_weight"]
        return (
            isinstance(evidence["worst_cluster"], str)
            and evidence["worst_cluster"] in evidence["stressed_cluster_set"]
            and isinstance(weight, (int, float))
            and not isinstance(weight, bool)
            and weight > 0.8
        )
    if criterion_id == "q4_complete_independent_evidence":
        return _coverage_valid(evidence["coverage"]) and _leave_valid(
            evidence["leave_held_components_out"]
        )
    if criterion_id == "q5_scheduled_prebreach_nonzero_lot":
        return (
            isinstance(evidence["first_early_sell_required_close"], str)
            and evidence["risk_level"] >= 1
            and evidence["portfolio_fast_return"] < 0
            and evidence["existing_concentration_eligible"] is True
            and evidence["cluster_symbol_count"] >= 2
            and evidence["minimum_cluster_size"] == 2
            and evidence["legacy_gate_open"] is False
            and evidence["early_sell_required"] is True
            and isinstance(evidence["scheduled_execution_batch"], Mapping)
            and evidence["planned_shares"] > 0
            and evidence["executable_lot_shares"] > 0
            and ("queue_fillability" not in evidence or isinstance(evidence["queue_fillability"], list))
            and _fill_valid(evidence)
        )
    if criterion_id == "q6_action_strictly_precedes_breach_sample":
        schedule = evidence["scheduled_execution_batch"]
        return (
            isinstance(schedule, Mapping)
            and isinstance(breach, str)
            and schedule["execution_open"].split("T", 1)[0]
            <= breach.split("T", 1)[0]
            and evidence["lead_batch_count"] >= 1
        )
    if criterion_id == "q7_official_sample_gap_not_proven_unavoidable":
        return (
            isinstance(breach, str)
            and evidence["official_sample_relation"]
            != "UNAVOIDABLE_AT_OFFICIAL_SAMPLE"
            and not (
                evidence["official_sample_relation"]
                == "OPEN_MARK_GAP_NOT_OFFICIAL_SAMPLE"
                and evidence["identical_valuation_instant_proven"] is True
            )
        )
    raise ValueError(f"unknown qualification criterion: {criterion_id}")


def _qualify(evaluation: Mapping[str, Any]) -> dict[str, Any]:
    evidence = evaluation["causal_matrix"]["s_evidence"]
    breach = evaluation["causal_matrix"]["event_timeline"][
        "first_official_mdd_breach"
    ]["timestamp"]
    criteria = []
    for criterion_id, paths, failure in _CRITERIA:
        observed = {
            path.rsplit(".", 1)[-1]: _resolve(evaluation, path) for path in paths
        }
        passed = _criterion_passes(criterion_id, evaluation)
        criteria.append(
            {
                "criterion_id": criterion_id,
                "passed": passed,
                "input_paths": list(paths),
                "observed_values": observed,
                "failure_reason": None if passed else failure,
            }
        )
    failures = [
        item["failure_reason"] for item in criteria if not item["passed"]
    ]
    keys = (
        "first_causal_stressed_cluster_close", "worst_cluster",
        "worst_cluster_weight", "stressed_cluster_set", "coverage",
        "leave_held_components_out", "first_early_sell_required_close",
        "risk_level", "portfolio_fast_return",
        "existing_concentration_eligible", "cluster_symbol_count",
        "minimum_cluster_size", "legacy_gate_open", "early_sell_required",
        "scheduled_execution_batch", "lead_batch_count",
        "pre_trade_open_drawdown", "official_sample_relation",
        "identical_valuation_instant_proven", "planned_shares",
        "executable_lot_shares", "fillability", "shortfall",
    )
    return {
        "scenario_id": evaluation["scenario_id"],
        "criteria": criteria,
        "first_official_mdd_breach": breach,
        **{key: evidence[key] for key in keys},
        "passed": not failures,
        "failure_reasons": failures,
    }




def validate_predicate_results(payload: Mapping[str, Any], prereg: Mapping[str, Any], reference: Mapping[str, Any]) -> None:
    """Recompute complete result rows from their authenticated primitive inputs."""
    from quantfusion.io.c6_stream import select_records
    kind = payload['kind']
    manifests = prereg['scenario_manifests']
    if kind == 'c6_l2':
        for row in payload['results']:
            validate_l2_telemetry(row)
        expected = _predicate_rows(prereg['diagnostic_predicate_manifests']['L2_APPLICABLE_DIAGNOSTIC_PREDICATES'],
                                   payload['results'], reference, manifests['L2_EXACT_SCENARIO_IDS']['ids'])
    elif kind in {'c6_l1_base', 'c6_l1_base_plus_s'}:
        base = kind == 'c6_l1_base'
        if base:
            validate_intervention_results(payload)
        candidate = 'C6-Base' if base else 'C6-Base+S'
        selected = select_records(payload['evaluations'], lambda row: row['variant_id'] == candidate)
        expected = _l1_predicate_rows(prereg['diagnostic_predicate_manifests']['L1_APPLICABLE_DIAGNOSTIC_PREDICATES'],
            selected, payload['evaluations'], payload['synthetic_controls'], payload['no_drift_pairs'], reference,
            manifests, base, payload.get('common_prefix_comparisons', []), payload.get('no_effect_comparisons', []))
    else:
        raise ValueError('unknown diagnostic predicate payload')
    if canonical_payload_hash(payload['diagnostic_predicates']) != canonical_payload_hash(expected):
        raise ValueError('diagnostic predicates differ from recomputed factual inputs')


def validate_qualification_results(qualification: Mapping[str, Any], base: Mapping[str, Any]) -> None:
    """Bind every qualification value to the actual corresponding Base row."""
    selected = [{'scenario_id': row['scenario_id'], 'official_metrics': row['official_metrics'],
                 'causal_matrix': {key: row['causal_matrix'][key] for key in ('event_timeline','s_evidence')}}
                for row in base['evaluations'] if row['variant_id'] == 'C6-Base']
    if len(selected) != 765 or len({row['scenario_id'] for row in selected}) != 765:
        raise ValueError('qualification requires exact Base scenario coverage')
    residual = sorted((row for row in selected if abs(row['official_metrics']['max_drawdown']) > .18 + 1e-15),
                      key=lambda row: row['scenario_id'])
    ids = [row['scenario_id'] for row in residual]
    if not ids or qualification['residual_ids'] != ids or len(qualification['results']) != len(ids):
        raise ValueError('qualification residual coverage differs from Base')
    passed = True
    for row, observed in zip(residual, qualification['results'], strict=True):
        expected = _qualify(row)
        if canonical_payload_hash(observed) != canonical_payload_hash(expected):
            raise ValueError('qualification differs from recomputed Base evidence')
        passed = passed and expected['passed']
    if qualification['all_passed'] is not passed:
        raise ValueError('qualification all_passed differs from recomputed criteria')


def validate_intervention_results(payload: Mapping[str, Any]) -> None:
    rows = [dict(row, _c6_score_trace=row['intervention_601869']['score_trace'])
            for row in payload['evaluations'] if str(row['variant_id']).startswith('W')]
    observed = [row['intervention_601869'] for row in rows]
    _attach_interventions(rows)
    if canonical_payload_hash(observed) != canonical_payload_hash([row['intervention_601869'] for row in rows]):
        raise ValueError('W intervention evidence differs from recorded paths')
    if canonical_payload_hash(payload['attribution_sensitivity']) != canonical_payload_hash(_attribution(rows)):
        raise ValueError('W attribution differs from recorded wealth')


def validate_s_comparison_results(payload: Mapping[str, Any], base: Mapping[str, Any], scenario_ids: Sequence[str]) -> None:
    common, no_effect = compare_s_paths(base['evaluations'], payload['evaluations'], scenario_ids)
    if (canonical_payload_hash(payload['common_prefix_comparisons']) != canonical_payload_hash(common)
            or canonical_payload_hash(payload['no_effect_comparisons']) != canonical_payload_hash(no_effect)):
        raise ValueError('S comparisons differ from authenticated recorded paths')


def validate_l2_telemetry(row: Mapping[str, Any]) -> None:
    from quantfusion.config.overlay import SYMBOL_SUB_INDUSTRY
    receipts = row['execution_receipts']
    expected = risk_execution_telemetry({'_c6_orders': receipts['orders'], '_c6_fills': receipts['fills']}, SYMBOL_SUB_INDUSTRY)
    samples = [item for item in receipts['exposure_series'] if item['phase'] == 'official_sample']
    expected.update(
        cash_days=sum(item['gross_notional'] == 0 for item in samples if item['symbol'] is None and item['cluster'] is None),
        max_gross_ratio=max((item['gross_ratio'] for item in samples), default=0.),
        max_cluster_weight=max((item['cluster_weight'] for item in samples if item['cluster'] is not None), default=0.),
        mdd_slack=.18 - abs(row['max_drawdown']),
        near_18pct=abs(.18 - abs(row['max_drawdown'])) <= 1e-12,
        terminal_lock_count=int(row['terminal_risk_lock']),
    )
    if not _metrics_equal({key: row['diagnostic_telemetry'][key] for key in expected}, expected):
        raise ValueError('L2 telemetry differs from recorded execution evidence')
    count = len(receipts['fills'])
    if (row['total_trades'] != count or row['sleeve_fill_count'] != count
            or row['date_symbol_side_count'] != len({(fill['timestamp'], fill['symbol'], fill['side']) for fill in receipts['fills']})
            or sum(row['reason_attribution'].values()) != count):
        raise ValueError('L2 trade counts differ from recorded fills')


def base_counterpart_id(s_evaluation_id: str) -> str:
    """Derive the sole authorized Base record for a Base+S evaluation."""
    prefix = "C6-Base+S::"
    if not isinstance(s_evaluation_id, str) or not s_evaluation_id.startswith(prefix):
        raise ValueError("S evaluation_id must start with exact C6-Base+S:: prefix")
    scenario_id = s_evaluation_id[len(prefix) :]
    if not scenario_id:
        raise ValueError("S evaluation_id must contain a scenario ID")
    return f"C6-Base::{scenario_id}"



def risk_execution_telemetry(result: Mapping[str, Any], groups: Mapping[str, str]) -> dict[str, Any]:
    """Summarize actual defensive instruction attempts and same-batch buys."""
    orders, fills = result["_c6_orders"], result["_c6_fills"]
    risk = [row for row in orders if row["side"] == "SELL" and row["defensive"]]
    retained = [row for row in risk if row["status"] not in {"suppressed", "cancelled"}]
    def book(row: Mapping[str, Any]) -> tuple[Any, ...]:
        return row["state_index"], row["strategy_name"], row["symbol"]
    by_batch: dict[str, list[Mapping[str, Any]]] = {}
    for row in retained:
        if row["execution_timestamp"] is not None:
            by_batch.setdefault(row["execution_timestamp"], []).append(row)
    offsets = [fill for fill in fills if fill["side"] == "BUY"
               and any(book(fill) == book(sell) for sell in by_batch.get(fill["timestamp"], []))]
    conflicts = [row for row in retained if row["carried_from_order_ordinal"] is not None
                 and row["execution_timestamp"] is not None
                 and any(buy["side"] == "BUY" and buy["execution_timestamp"] == row["execution_timestamp"]
                         and book(buy) == book(row) for buy in orders)]
    substitutions = {fill["order_ordinal"] for fill in fills if fill["side"] == "BUY"
                     and any(sell["symbol"] != fill["symbol"] and groups.get(fill["symbol"]) is not None
                             and groups.get(sell["symbol"]) == groups[fill["symbol"]]
                             for sell in by_batch.get(fill["timestamp"], []))}
    risk_ids = {row["order_ordinal"] for row in risk}
    return {"planned_risk_sell_shares": sum(row["requested_shares"] for row in risk),
            "retained_risk_sell_shares": sum(row["requested_shares"] for row in retained),
            "suppressed_risk_sell_shares": sum(row["requested_shares"] for row in risk if row["status"] == "suppressed"),
            "filled_risk_sell_shares": sum(fill["shares"] for fill in fills if fill["order_ordinal"] in risk_ids),
            "first_executable_open": min((row["execution_timestamp"] for row in risk
                                           if row["execution_timestamp"] is not None and row["authorized_shares"] > 0), default=None),
            "same_open_offset_shares": sum(fill["shares"] for fill in offsets),
            "carried_conflict_count": len(conflicts), "cluster_substitution_count": len(substitutions)}



def _path_hashes(record: Mapping[str, Any]) -> dict[str, str]:
    return {name: hashlib.sha256(_canonical_bytes(record[key])).hexdigest() for name, key in (("orders_sha256", "orders"), ("fills_sha256", "fills"), ("cash_sha256", "cash_series"), ("positions_sha256", "position_series"), ("equity_sha256", "equity_series"))}



def _prefix_hash(record: Mapping[str, Any], boundary: str | None) -> str:
    paths = []
    for key, timestamp in (("orders", "execution_timestamp"), ("fills", "timestamp"), ("cash_series", "timestamp"), ("position_series", "timestamp"), ("equity_series", "timestamp")):
        selected = []
        for item in record[key]:
            if key == "orders" and "queued_state" in item:
                if boundary is not None and item["queued_timestamp"] >= boundary:
                    continue
                last = max([item["queued_timestamp"], item["execution_timestamp"] or ""]
                           + [event["timestamp"] for event in item["events"]])
                if boundary is not None and last >= boundary:
                    selected.append(item["queued_state"])
                else:
                    selected.append({k: v for k, v in item.items() if k not in {"queued_state", "queued_timestamp"}})
            elif boundary is None or (item[timestamp] or item.get("decision_timestamp")) < boundary:
                selected.append(item)
        paths.append(selected)
    return hashlib.sha256(_canonical_bytes(paths)).hexdigest()



def compare_s_paths(base_evaluations: Sequence[Mapping[str, Any]], s_evaluations: Sequence[Mapping[str, Any]], scenario_ids: Sequence[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Require exact counterpart coverage before comparing any producer path."""
    base_selected = select_records(base_evaluations, lambda item: item["variant_id"] == "C6-Base")
    for records, variant in ((base_selected, "C6-Base"), (s_evaluations, "C6-Base+S")):
        expected = [f"{variant}::{item}" for item in scenario_ids]
        if (len(set(scenario_ids)) != len(scenario_ids)
                or [item["evaluation_id"] for item in records] != expected
                or [item["scenario_id"] for item in records] != list(scenario_ids)
                or any(item["variant_id"] != variant for item in records)):
            raise ValueError("S comparison requires exact ordered Base and S coverage")
    common, no_effect = [], []
    for counterpart, row in zip(base_selected, s_evaluations):
        base_id = base_counterpart_id(row["evaluation_id"])
        timestamp = row["causal_matrix"]["s_evidence"]["first_early_sell_required_close"]
        left, right = _prefix_hash(counterpart, timestamp), _prefix_hash(row, timestamp)
        common.append({"scenario_id": row["evaluation_id"], "base_evaluation_id": base_id, "first_s_effective_timestamp": timestamp, "base_prefix_sha256": left, "s_prefix_sha256": right, "equal": left == right})
        if timestamp is None:
            a, b = _path_hashes(counterpart), _path_hashes(row)
            no_effect.append({"item_kind": "evaluation", "item_id": row["evaluation_id"], "base_evaluation_id": base_id, "s_effective_count": 0, **{f"base_{key}": value for key, value in a.items()}, **{f"s_{key}": value for key, value in b.items()}, "equal": a == b})
    return common, no_effect



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
    books: dict[tuple[int, str], tuple[int, float]] = {}
    realized = 0.0
    for fill in record["fills"]:
        if fill["symbol"] != symbol:
            continue
        key = (fill["state_index"], fill["strategy_name"])
        shares, cost = books.get(key, (0, 0.))
        if fill["side"] == "BUY":
            shares += fill["shares"]
            cost += fill["notional"] + fill["fee"]
        elif shares:
            basis = cost / shares * fill["shares"]
            shares -= fill["shares"]
            cost -= basis
            realized += fill["notional"] - fill["fee"] - basis
        books[key] = (shares, cost)
    marks = [item for item in record["position_series"] if item["symbol"] == symbol]
    final_timestamp = record["equity_series"][-1]["timestamp"]
    market_value = sum(item["market_value"] for item in marks if item["timestamp"] == final_timestamp)
    return realized, market_value - sum(cost for _, cost in books.values())



def _first_path_divergence(before: Mapping[str, Any], after: Mapping[str, Any],
                           before_name: str, after_name: str,
                           before_scores: Sequence[Mapping[str, Any]] = (),
                           after_scores: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any] | None:
    """Locate the first different observed phase, retaining the actual date."""
    def batches(record: Mapping[str, Any], scores: Sequence[Mapping[str, Any]]) -> dict[tuple[str, int, str], list[Any]]:
        result: dict[tuple[str, int, str], list[Any]] = {}
        for key, phase, reason in (("orders", 0, "ORDER"), ("fills", 0, "FILL"),
                                   ("cash_series", 3, "VALUATION"), ("position_series", 3, "VALUATION"),
                                   ("equity_series", 3, "VALUATION")):
            for item in record[key]:
                if key == "orders":
                    queued = item.get("queued_state")
                    if queued is not None:
                        result.setdefault((item["queued_timestamp"], 1, "ORDER"), []).append(("queued", queued))
                    timestamp = item["execution_timestamp"]
                    item_phase = phase
                    if timestamp is None:
                        timestamp = max((event["timestamp"] for event in item.get("events", [])), default=None)
                        item_phase = 1
                    if timestamp is None:
                        continue
                    value = {k: v for k, v in item.items() if k not in {"queued_state", "queued_timestamp"}}
                else:
                    timestamp, item_phase, value = item["timestamp"], phase, item
                result.setdefault((timestamp, item_phase, reason), []).append((key, value))
        for row in scores:
            result.setdefault((row["decision_timestamp"], 0, "SCORE"), []).append(("score", row))
        for name, row in record.get("causal_matrix", {}).get("event_timeline", {}).items():
            if name != "first_official_mdd_breach" and row.get("timestamp"):
                result.setdefault((row["timestamp"], 1, "LOCK_STATE"), []).append((name, row))
        return result
    a, b = batches(before, before_scores), batches(after, after_scores)
    def observed_hash(path, boundary):
        return hashlib.sha256(_canonical_bytes([[list(key), [list(item) for item in path[key]]] for key in sorted(path) if key <= boundary])).hexdigest()
    for key in sorted(set(a) | set(b)):
        if a.get(key, []) == b.get(key, []):
            continue
        timestamp, phase, reason = key
        different = next((value for _, value in b.get(key, []) + a.get(key, []) if isinstance(value, Mapping)), {})
        return {"compared_from_intervention_id": before_name, "compared_to_intervention_id": after_name,
                "timestamp": timestamp, "phase": {0: "EXECUTION_OPEN", 1: "DECISION_CLOSE", 3: "VALUATION_CLOSE"}[phase],
                "reason": reason, "state_index": different.get("state_index"), "sleeve_name": different.get("sleeve_name"),
                "symbol": different.get("symbol"), "before_path_sha256": observed_hash(a, key),
                "after_path_sha256": observed_hash(b, key)}
    return None



def _score_comparison(before: Sequence[Mapping[str, Any]], after: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    def expand(trace: Sequence[Mapping[str, Any]]) -> dict[tuple[str, int, str], dict[str, Any]]:
        result = {}
        for row in trace:
            ranks = {symbol: rank for rank, symbol in enumerate(sorted(row["pool_relative_scores"], key=lambda symbol: (-row["pool_relative_scores"][symbol], symbol)), 1)}
            ref = hashlib.sha256(_canonical_bytes(row["reference_inputs"])).hexdigest()
            pool = hashlib.sha256("".join(symbol + "\n" for symbol in row["pool_members"]).encode()).hexdigest()
            for symbol in sorted(set(row["fixed_reference_scores"]) | set(ranks)):
                if symbol == "601869":
                    continue
                key = (row["decision_timestamp"], row["state_index"], symbol)
                if key in result:
                    raise ValueError("duplicate coordinator score observation")
                result[key] = {"decision_timestamp": key[0], "state_index": key[1], "sleeve_name": row["sleeve_name"],
                               "symbol": symbol, "score": row["fixed_reference_scores"].get(symbol), "rank": ranks.get(symbol),
                               "reference_fingerprint": ref, "pool_members_sha256": pool}
        return result
    a, b = expand(before), expand(after)
    scores, ranks = [], []
    for key in sorted(set(a) | set(b)):
        old, new = a.get(key), b.get(key)
        witness = new if new is not None else old
        assert witness is not None
        identity = {k: witness[k] for k in ("decision_timestamp", "state_index", "sleeve_name", "symbol")}
        for field, rows, fingerprint in (("score", scores, "reference_fingerprint"), ("rank", ranks, "pool_members_sha256")):
            left, right = old.get(field) if old else None, new.get(field) if new else None
            if left == right:
                continue
            rows.append({**identity, f"{field}_before": left, f"{field}_after": right,
                         f"{field}_delta": right - left if left is not None and right is not None else None,
                         f"{fingerprint}_before": old.get(fingerprint) if old else None,
                         f"{fingerprint}_after": new.get(fingerprint) if new else None})
    score_path = [{k: row[k] for k in ("decision_timestamp", "state_index", "sleeve_name", "symbol", "score", "reference_fingerprint")} for row in b.values()]
    rank_path = [{k: row[k] for k in ("decision_timestamp", "state_index", "sleeve_name", "symbol", "rank", "pool_members_sha256")} for row in b.values()]
    # A changed admitted set is a batch-level observation. Do not invent a
    # unique admitted/displaced pairing when multiple slots changed together.
    old_batches = {(row["decision_timestamp"], row["state_index"]): row for row in before}
    slots = []
    for row in after:
        old = old_batches.get((row["decision_timestamp"], row["state_index"]))
        if old is None:
            continue
        admitted = set(row["allowed_symbols"]) & set(row["candidate_symbols"])
        prior = set(old["allowed_symbols"]) & set(old["candidate_symbols"])
        if admitted - prior and prior - admitted:
            slots.append({"execution_timestamp": row["decision_timestamp"], "state_index": row["state_index"],
                          "sleeve_name": row["sleeve_name"], "admitted_symbols": sorted(admitted - prior),
                          "displaced_symbols": sorted(prior - admitted), "capacity_before": old["candidate_capacity"],
                          "capacity_after": row["candidate_capacity"], "unique_causal_pairing_claimed": False})
    return {"old_symbol_fixed_reference_score_hash": hashlib.sha256(_canonical_bytes(score_path)).hexdigest(),
            "old_symbol_fixed_reference_score_changes": scores,
            "pool_relative_rank_hash": hashlib.sha256(_canonical_bytes(rank_path)).hexdigest(),
            "coordinator_pool_relative_rank_changes": ranks, "displaced_slots": slots}



def _post_lock_effect(locked: Mapping[str, Any], unlocked: Mapping[str, Any]) -> dict[str, Any]:
    from collections import Counter
    timeline = locked["causal_matrix"]["event_timeline"]
    first = min((row["timestamp"] for name, row in timeline.items() if "lock" in name and row["timestamp"]), default=None)
    def unmatched(key: str) -> int:
        if first is None:
            return 0
        def counts(record: Mapping[str, Any]) -> Counter:
            rows = []
            for item in record[key]:
                timestamp = item.get("execution_timestamp") if key == "orders" else item["timestamp"]
                if timestamp is None or timestamp <= first or (key == "orders" and item["authorized_shares"] <= 0):
                    continue
                rows.append(_canonical_bytes({k: v for k, v in item.items() if k not in {"order_ordinal", "fill_ordinal", "carried_from_order_ordinal", "suppression_winner_order_ordinal", "queued_state", "queued_timestamp"}}))
            return Counter(rows)
        return sum((counts(unlocked) - counts(locked)).values())
    a, b = locked["official_metrics"]["terminal_wealth"], unlocked["official_metrics"]["terminal_wealth"]
    return {"first_lock_timestamp": first, "missed_order_count": unmatched("orders"), "missed_trade_count": unmatched("fills"),
            "locked_terminal_wealth": a, "no_lock_terminal_wealth": b, "delta_log_wealth": math.log(b) - math.log(a), "compounding_ratio": b / a}



def _attach_interventions(evaluations: list[dict[str, Any]]) -> None:
    rows = [item for item in evaluations if str(item["variant_id"]).startswith("W")]
    names = ["W0_no_601869", "W1_data_map_only", "W2_pool_denominator_only", "W3_real_intents_fixed_reference_U", "W4_full_base_production_pool_relative", "W5_full_base_production_pool_relative_no_lock"]
    if len(rows) != 6 or [row["variant_id"].split("-", 1)[0] for row in rows] != [f"W{i}" for i in range(6)]:
        raise ValueError("intervention attribution requires exact ordered W0..W5 coverage")
    breach = rows[4]["causal_matrix"]["event_timeline"]["first_official_mdd_breach"]["timestamp"]
    prior = [item["timestamp"] for item in rows[4]["equity_series"] if breach is None or item["timestamp"] < breach]
    anchor = prior[-1] if prior else None
    traces = [row.pop("_c6_score_trace") for row in rows]
    wealth = []
    for row in rows:
        sample = next((item for item in row["equity_series"] if item["timestamp"] == anchor), None)
        if anchor is not None and sample is None:
            raise ValueError("intervention lacks the common pre-breach valuation")
        wealth.append(float(sample["equity"]) if sample is not None else None)
    for index, (row, name) in enumerate(zip(rows, names)):
        gaps = []
        comparisons = [] if index == 0 else [("VS_W0", 0)]
        if index > 1:
            comparisons.append(("VS_PREVIOUS_FORWARD", index - 1))
        for label, origin in comparisons:
            if anchor is None:
                continue
            gaps.append({"comparison": label, "from_intervention_id": names[origin], "to_intervention_id": name, "anchor_timestamp": anchor, "from_wealth": wealth[origin], "to_wealth": wealth[index], "wealth_gap": wealth[index] - wealth[origin], "log_wealth_gap": math.log(wealth[index]) - math.log(wealth[origin])})
        previous = rows[index - 1] if index else None
        divergence = _first_path_divergence(previous, row, names[index - 1], name, traces[index - 1], traces[index]) if previous is not None else None
        post = _post_lock_effect(rows[4], row) if index == 5 else None
        realized, unrealized = _symbol_pnl(row, "601869")
        score_facts = _score_comparison(traces[index - 1] if index else traces[index], traces[index])
        row["intervention_601869"] = {"intervention_id": name, "scenario_id": row["scenario_id"], "terminal_wealth": row["official_metrics"]["terminal_wealth"], "total_return": row["official_metrics"]["total_return"], "max_drawdown": row["official_metrics"]["max_drawdown"], "total_trades": row["official_metrics"]["total_trades"], "pre_breach_anchor_timestamp": anchor, "pre_breach_wealth": wealth[index], "pre_breach_wealth_gaps": gaps, "own_601869_realized_pnl": realized, "own_601869_unrealized_pnl": unrealized, **score_facts, "score_trace": traces[index], "first_path_divergence": divergence, "post_lock_effect": post, "no_lock_terminal_wealth_ratio": None if index != 5 else row["official_metrics"]["terminal_wealth"] / rows[4]["official_metrics"]["terminal_wealth"]}
