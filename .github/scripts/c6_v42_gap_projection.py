"""Read-only v42 AB5 residual projection; never changes economic evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping


CANDIDATE = "C6-Base+AB5"
FIXED_SCENARIOS = (
    "random-20260817-03-027",
    "random-20260807-12-040",
    "random-20260817-08-023",
    "random-20260807-05-040",
    "leave-one-out-300308",
)
MDD_LIMIT = 0.18
TOLERANCE = 1e-15


def _date(value: Any) -> str | None:
    return value[:10] if isinstance(value, str) and len(value) >= 10 else None


def _first_breach(record: Mapping[str, Any]) -> Mapping[str, Any]:
    for item in record["drawdown_series"]:
        if abs(float(item["drawdown"])) > MDD_LIMIT + TOLERANCE:
            return item
    raise ValueError("selected residual has no reproducible 18% breach")


def _budget_events(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = []
    for event in record["risk_events"]:
        raw = event.get("raw_event")
        if isinstance(raw, Mapping) and raw.get("event") == "account_budget_envelope":
            result.append({"timestamp": event["timestamp"], **dict(raw)})
    return result


def _is_effective(event: Mapping[str, Any]) -> bool:
    return bool(
        event.get("buy_envelope_binding")
        or int(event.get("new_reduction_orders", 0))
        or int(event.get("buy_shares_removed", 0))
    )


def _budget_actions(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item) for item in record["action_lifecycle"]
        if item.get("reason") == "account_budget_trim"
    ]


def _budget_orders(record: Mapping[str, Any], action_ids: set[str]) -> list[dict[str, Any]]:
    return [
        dict(item) for item in record["orders"]
        if item.get("action_id") in action_ids
        or "account_budget" in str(item.get("reason", ""))
    ]


def classify_record(record: Mapping[str, Any]) -> dict[str, Any]:
    breach = _first_breach(record)
    breach_date = _date(breach["timestamp"])
    events = _budget_events(record)
    effective = [item for item in events if _is_effective(item)]
    first_effective = min((_date(item["timestamp"]) for item in effective), default=None)
    actions = _budget_actions(record)
    action_ids = {str(item["action_id"]) for item in actions}
    orders = _budget_orders(record, action_ids)
    prebreach_orders = [
        item for item in orders
        if (_date(item.get("decision_timestamp")) or "9999-99-99") < breach_date
    ]
    order_ids = {int(item["order_ordinal"]) for item in prebreach_orders}
    fills = [
        item for item in record["fills"]
        if int(item["order_ordinal"]) in order_ids
        and (_date(item.get("timestamp")) or "9999-99-99") <= breach_date
    ]
    filled = sum(int(item["shares"]) for item in fills)
    obstructed = any(
        item.get("blocked_reason") is not None
        or item.get("status") in {"blocked", "cancelled", "expired", "pending", "partial"}
        or int(item.get("filled_shares", 0)) < int(item.get("authorized_shares", 0))
        or (_date(item.get("execution_timestamp")) or "9999-99-99") > breach_date
        for item in prebreach_orders
    )
    if first_effective is None or first_effective >= breach_date:
        classification = "SIGNAL_TOO_LATE"
    elif obstructed:
        classification = "EXECUTION_BLOCKED"
    else:
        classification = "ACTION_INSUFFICIENT"
    return {
        "primary_classification": classification,
        "first_breach_date": breach_date,
        "first_effective_budget_date": first_effective,
        "prebreach_budget_order_count": len(prebreach_orders),
        "prebreach_budget_filled_shares": filled,
        "prebreach_execution_obstruction": obstructed,
    }


def select_target_records(evaluations: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    candidates = {
        str(item["scenario_id"]): item for item in evaluations
        if item.get("variant_id") == CANDIDATE
    }
    fixed = []
    for scenario in FIXED_SCENARIOS:
        item = candidates.get(scenario)
        if item is None:
            raise ValueError(f"missing fixed representative: {scenario}")
        if abs(float(item["official_metrics"]["max_drawdown"])) <= MDD_LIMIT + TOLERANCE:
            raise ValueError(f"fixed representative is not an MDD residual: {scenario}")
        fixed.append(item)
    others = [
        item for scenario, item in candidates.items()
        if scenario not in FIXED_SCENARIOS
        and abs(float(item["official_metrics"]["max_drawdown"])) > MDD_LIMIT + TOLERANCE
    ]
    if not others:
        raise ValueError("no near-threshold residual outside the fixed representatives")
    near = min(
        others,
        key=lambda item: (
            abs(float(item["official_metrics"]["max_drawdown"])) - MDD_LIMIT,
            str(item["scenario_id"]),
        ),
    )
    return fixed + [near]


def _window(series: Iterable[Mapping[str, Any]], breach_date: str) -> list[dict[str, Any]]:
    rows = list(series)
    indexes = [index for index, item in enumerate(rows) if _date(item.get("timestamp")) == breach_date]
    if not indexes:
        return []
    lo, hi = max(0, indexes[0] - 1), min(len(rows), indexes[-1] + 2)
    return [dict(item) for item in rows[lo:hi]]


def project_record(record: Mapping[str, Any]) -> dict[str, Any]:
    outcome = classify_record(record)
    breach_date = outcome["first_breach_date"]
    events = _budget_events(record)
    effective = [item for item in events if _is_effective(item)]
    relevant_events = []
    for item in (
        effective[:1]
        + [event for event in effective if (_date(event["timestamp"]) or "") < breach_date][-1:]
        + [event for event in events if _date(event["timestamp"]) == breach_date]
    ):
        if item not in relevant_events:
            relevant_events.append(item)
    actions = _budget_actions(record)
    action_ids = {str(item["action_id"]) for item in actions}
    orders = _budget_orders(record, action_ids)
    order_ids = {int(item["order_ordinal"]) for item in orders}
    dates = {breach_date, outcome["first_effective_budget_date"]}
    dates.discard(None)
    return {
        "scenario_id": record["scenario_id"],
        "official_metrics": record["official_metrics"],
        "classification": outcome,
        "drawdown_window": _window(record["drawdown_series"], breach_date),
        "equity_window": _window(record["equity_series"], breach_date),
        "budget_envelopes": relevant_events,
        "budget_actions": actions,
        "budget_orders": orders,
        "budget_fills": [dict(item) for item in record["fills"] if int(item["order_ordinal"]) in order_ids],
        "exposure_snapshots": [
            dict(item) for item in record["exposure_series"]
            if _date(item.get("timestamp")) in dates
            and item.get("symbol") is None and item.get("cluster") is None
        ],
    }


def project_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if payload.get("kind") != "c6_l1_base":
        raise ValueError("unexpected Base payload kind")
    evaluations = payload.get("evaluations")
    if evaluations is None:
        raise ValueError("Base payload has no evaluations")
    selected = select_target_records(evaluations)
    rows = [project_record(item) for item in selected]
    counts = Counter(item["classification"]["primary_classification"] for item in rows)
    return {
        "schema_version": 1,
        "kind": "c6_v42_ab5_gap_projection_noncanonical",
        "candidate_id": CANDIDATE,
        "mdd_limit": MDD_LIMIT,
        "mdd_tolerance": TOLERANCE,
        "target_selection": "five_preregistered_plus_nearest_other_residual",
        "classification_counts": dict(sorted(counts.items())),
        "records": rows,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if _sha256(args.payload) != args.expected_sha256:
        raise ValueError("Base payload SHA-256 mismatch")
    from quantfusion.io.c6_stream import load_object
    projection = project_payload(load_object(args.payload))
    raw = json.dumps(projection, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(raw)
    receipt = {
        "schema_version": 1,
        "kind": "c6_v42_ab5_gap_projection_receipt",
        "diagnostic_noncanonical": True,
        "economic_dispatches": 0,
        "base_workflow_run_id": "34509818018",
        "base_payload_sha256": args.expected_sha256,
        "projection_sha256": hashlib.sha256(raw).hexdigest(),
        "target_scenarios": [item["scenario_id"] for item in projection["records"]],
        "classification_counts": projection["classification_counts"],
    }
    args.output.with_name("receipt.json").write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
