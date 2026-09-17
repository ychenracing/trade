"""Apply the frozen root-cause path-selection rule to exactly twenty replays."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def read(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def indexed(rows: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {str(row["date"]): row for row in rows}


def common_dates(left: Mapping[str, Any], right: Mapping[str, Any]) -> list[str]:
    return sorted(set(left) & set(right))


def flatten_books(snapshot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        book
        for sleeve in snapshot.get("sleeves", [])
        for book in sleeve.get("positions", [])
    ]


def trades_by_date(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    result: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        result[str(row["date"])].append(row)
    return result


def first_trade_difference(
    incumbent: Mapping[str, Any], candidate: Mapping[str, Any]
) -> str | None:
    left, right = trades_by_date(incumbent["trades"]), trades_by_date(candidate["trades"])
    for date in sorted(set(left) | set(right)):
        if canonical(left.get(date, [])) != canonical(right.get(date, [])):
            return date
    return None


def ordinary_trim_fills(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(row) for row in result["trades"]
        if row.get("direction") == "sell"
        and row.get("reason") == "account_budget_trim"
    ]


def clipped_buys(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    output = []
    route = {
        str(row.get("date")): str(row.get("route", "UNKNOWN"))
        for row in result.get("route_sequence", [])
    }
    for ledger in result["budget_ledger"]:
        envelope = ledger.get("envelope") or {}
        for row in ledger.get("buy_authorization", []):
            requested = int(row.get("requested_shares", 0) or 0)
            ratio = float(row.get("authorization_ratio", 1.0))
            if requested and ratio < 0.50:
                output.append({
                    **row,
                    "date": ledger["date"],
                    "gross_before": envelope.get("gross_before"),
                    "gross_cap": envelope.get("gross_cap"),
                    "buy_scale": envelope.get("buy_scale"),
                    "route": route.get(str(ledger["date"])),
                })
    return output


def capacity_chains(
    incumbent: Mapping[str, Any], candidate: Mapping[str, Any]
) -> list[dict[str, Any]]:
    inc_budget, can_budget = indexed(incumbent["budget_ledger"]), indexed(
        candidate["budget_ledger"]
    )
    inc_trims = ordinary_trim_fills(incumbent)
    inc_buys = [row for row in incumbent["trades"] if row.get("direction") == "buy"]
    can_buys = [row for row in candidate["trades"] if row.get("direction") == "buy"]
    dates = sorted(can_budget)
    positions = {date: index for index, date in enumerate(dates)}
    output = []
    for blocked in clipped_buys(candidate):
        date, index = blocked["date"], positions[blocked["date"]]
        previous = set(dates[max(0, index - 5):index + 1])
        future = set(dates[index:min(len(dates), index + 21)])
        fill_deadline = dates[min(len(dates) - 1, index + 5)]
        trim_fills = [
            row for row in inc_trims
            if str(row.get("signal_date")) in previous
            and str(row.get("date")) <= fill_deadline
        ]
        trim_plans = [
            day for day in previous
            if any(
                row.get("reason") == "account_budget_trim"
                for row in inc_budget.get(day, {}).get("new_budget_sell_plans", [])
            )
        ]
        symbol, strategy = blocked.get("symbol"), blocked.get("strategy")

        def matching(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
            return [
                row for row in rows
                if row.get("symbol") == symbol
                and row.get("strategy_name") == strategy
                and (
                    str(row.get("signal_date")) in future
                    or str(row.get("date")) in future
                )
            ]

        inc_matches, can_matches = matching(inc_buys), matching(can_buys)
        inc_auth = next((
            row for row in inc_budget.get(date, {}).get("buy_authorization", [])
            if row.get("symbol") == symbol and row.get("strategy") == strategy
        ), None)
        inc_ratio = float((inc_auth or {}).get("authorization_ratio", 0.0))
        can_ratio = float(blocked.get("authorization_ratio", 0.0))
        inc_shares = sum(int(row.get("shares", 0) or 0) for row in inc_matches)
        can_shares = sum(int(row.get("shares", 0) or 0) for row in can_matches)
        cap, gross = blocked.get("gross_cap"), blocked.get("gross_before")
        near_capacity = bool(
            cap is not None and gross is not None
            and (float(gross) >= 0.95 * float(cap) or can_ratio < 0.50)
        )
        output.append({
            "blocked": blocked,
            "near_capacity": near_capacity,
            "incumbent_planned_trim_dates": sorted(trim_plans),
            "incumbent_actual_trim_fills": trim_fills,
            "incumbent_authorization_ratio": inc_ratio,
            "candidate_authorization_ratio": can_ratio,
            "authorization_advantage": inc_ratio - can_ratio,
            "incumbent_matching_buys_next_20_sessions": inc_matches,
            "candidate_matching_buys_next_20_sessions": can_matches,
            "incumbent_matching_buy_shares": inc_shares,
            "candidate_matching_buy_shares": can_shares,
            "incumbent_had_higher_capacity": bool(
                inc_ratio - can_ratio >= 0.25 or inc_shares >= can_shares + 100
            ),
        })
    return output


def holdings(result: Mapping[str, Any]) -> dict[str, dict[str, float]]:
    output: dict[str, dict[str, float]] = {}
    for ledger in result["budget_ledger"]:
        values: dict[str, float] = defaultdict(float)
        for book in ledger["books"]:
            if book.get("market_value") is not None:
                values[str(book["symbol"])] += float(book["market_value"])
        output[str(ledger["date"])] = dict(values)
    return output


def linked_gap_share(
    incumbent: Mapping[str, Any], candidate: Mapping[str, Any],
    chains: Sequence[Mapping[str, Any]],
) -> float:
    symbols = {str(chain["blocked"].get("symbol")) for chain in chains}
    if not symbols:
        return 0.0
    inc = {str(row["date"]): float(row["assets"]) for row in incumbent["equity_curve"]}
    can = {str(row["date"]): float(row["assets"]) for row in candidate["equity_curve"]}
    inc_hold, can_hold = holdings(incumbent), holdings(candidate)
    dates = common_dates(inc, can)
    if len(dates) < 2:
        return 0.0
    positive = linked = 0.0
    previous = inc[dates[0]] - can[dates[0]]
    for date in dates[1:]:
        gap = inc[date] - can[date]
        increase = max(0.0, gap - previous)
        positive += increase
        if increase and any(
            inc_hold.get(date, {}).get(symbol, 0.0)
            > can_hold.get(date, {}).get(symbol, 0.0) + 1e-6
            for symbol in symbols
        ):
            linked += increase
        previous = gap
    return linked / positive if positive else 0.0


def pre_state_equal(
    incumbent: Mapping[str, Any], candidate: Mapping[str, Any], first_trim: str | None
) -> bool:
    if first_trim is None:
        return False
    inc, can = indexed(incumbent["budget_ledger"]), indexed(candidate["budget_ledger"])
    earlier = [date for date in common_dates(inc, can) if date < first_trim]
    if not earlier:
        return True
    date = earlier[-1]

    def material(row: Mapping[str, Any]) -> dict[str, Any]:
        before = row.get("before", {})
        held = sorted((
            int(book.get("state_index", 0)), str(book.get("symbol")),
            str(book.get("strategy")), int(book.get("shares", 0)),
            round(float(book.get("mark") or 0.0), 8),
        ) for book in flatten_books(before))
        pending = sorted((
            int(sleeve.get("state_index", 0)), str(item.get("direction")),
            str(item.get("symbol")), str(item.get("strategy_name")),
            int(item.get("target_shares", 0) or 0), str(item.get("signal_date")),
            str(item.get("reason")),
        ) for sleeve in before.get("sleeves", []) for item in sleeve.get("pending", []))
        return {
            "cash": round(float(before.get("cash", 0.0)), 6),
            "gross": round(float(before.get("gross_exposure", 0.0)), 6),
            "held": held,
            "pending": pending,
        }
    return material(inc[date]) == material(can[date])


def gap_by_2024(incumbent: Mapping[str, Any], candidate: Mapping[str, Any]) -> float:
    inc = {str(row["date"]): float(row["assets"]) for row in incumbent["equity_curve"]}
    can = {str(row["date"]): float(row["assets"]) for row in candidate["equity_curve"]}
    dates = common_dates(inc, can)
    cutoff = [date for date in dates if date <= "2024-12-31"]
    if not cutoff:
        return 0.0
    final_gap = inc[dates[-1]] - can[dates[-1]]
    cutoff_gap = inc[cutoff[-1]] - can[cutoff[-1]]
    return max(0.0, cutoff_gap) / final_gap if final_gap > 0 else 0.0


def non_trend_gap_share(
    incumbent: Mapping[str, Any], candidate: Mapping[str, Any]
) -> float:
    inc = {str(row["date"]): float(row["assets"]) for row in incumbent["equity_curve"]}
    can = {str(row["date"]): float(row["assets"]) for row in candidate["equity_curve"]}
    routes = {
        str(row.get("date")): str(row.get("route", "UNKNOWN"))
        for row in incumbent.get("route_sequence", [])
    }
    dates = common_dates(inc, can)
    if len(dates) < 2:
        return 0.0
    positive = non_trend = 0.0
    previous = inc[dates[0]] - can[dates[0]]
    for date in dates[1:]:
        gap = inc[date] - can[date]
        increase = max(0.0, gap - previous)
        positive += increase
        if increase and routes.get(date, "UNKNOWN") != "TREND":
            non_trend += increase
        previous = gap
    return non_trend / positive if positive else 0.0


def assess_pair(
    incumbent: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    chains = capacity_chains(incumbent, candidate)
    qualifying = [
        chain for chain in chains
        if chain["near_capacity"]
        and chain["incumbent_planned_trim_dates"]
        and chain["incumbent_actual_trim_fills"]
        and chain["incumbent_had_higher_capacity"]
    ]
    gap_share = linked_gap_share(incumbent, candidate, qualifying)
    first_difference = first_trade_difference(incumbent, candidate)
    first_trim = min(
        (str(row.get("signal_date")) for row in ordinary_trim_fills(incumbent)),
        default=None,
    )
    prior_equal = pre_state_equal(incumbent, candidate, first_trim)
    major = bool(
        qualifying and first_difference and first_trim
        and first_difference >= first_trim and prior_equal and gap_share >= 0.35
    )
    left, right = incumbent["metrics"], candidate["metrics"]
    correct = bool(
        left["finite"] and right["finite"]
        and left["minimum_cash"] >= -1e-6 and right["minimum_cash"] >= -1e-6
        and left["maximum_conservation_error"] <= 1e-6
        and right["maximum_conservation_error"] <= 1e-6
    )
    return {
        "wealth_ratio": right["wealth_multiple"] / left["wealth_multiple"],
        "first_trade_difference": first_difference,
        "first_incumbent_ordinary_trim_signal_date": first_trim,
        "pre_divergence_state_equal": prior_equal,
        "candidate_clipped_buy_count": len(clipped_buys(candidate)),
        "capacity_chain_count": len(chains),
        "qualifying_capacity_chain_count": len(qualifying),
        "linked_positive_gap_share": gap_share,
        "capacity_chain_major_source": major,
        "gap_formed_by_2024_share": gap_by_2024(incumbent, candidate),
        "non_trend_positive_gap_growth_share": non_trend_gap_share(
            incumbent, candidate
        ),
        "correctness": correct,
        "qualifying_capacity_chains": qualifying[:20],
    }


def verify_previous(
    results: Mapping[str, Mapping[str, Any]], previous: Path
) -> dict[str, Any]:
    checks = {}
    for pool in ("pool_b", "pool_d", "pool_g", "pool_f"):
        for mode in ("incumbent", "candidate"):
            old = json.loads(
                (previous / "results" / f"{mode}-{pool}.json").read_text()
            )
            current = results[f"long:{pool}:{mode}"]["metrics"]
            expected = {
                key: old[key] for key in (
                    "wealth_multiple", "max_drawdown",
                    "date_symbol_side_buckets", "active_fill_days", "sleeve_fills",
                )
            }
            row = {}
            for key, value in expected.items():
                actual = current[key]
                row[key] = actual == value if isinstance(value, int) else math.isclose(
                    float(actual), float(value), rel_tol=1e-11, abs_tol=1e-8
                )
            checks[f"{mode}:{pool}"] = row
    return {
        "checks": checks,
        "all_reproduced": all(all(row.values()) for row in checks.values()),
    }


def run(args: argparse.Namespace) -> None:
    root, previous = Path(args.results_dir), Path(args.previous_evidence_dir)
    expected = [
        f"{window}:{pool}:{mode}"
        for window in ("long", "common")
        for pool in ("core17", "pool_b", "pool_d", "pool_g", "pool_f")
        for mode in ("incumbent", "candidate")
    ]
    results = {}
    for key in expected:
        window, pool, mode = key.split(":")
        path = root / f"{window}-{pool}-{mode}.json.gz"
        if not path.exists():
            raise FileNotFoundError(path)
        results[key] = read(path)
    source_trees = {
        mode: sorted({
            result["identity"]["source_root_tree"]
            for key, result in results.items() if key.endswith(f":{mode}")
        }) for mode in ("incumbent", "candidate")
    }
    identity_consistent = bool(
        all(len(trees) == 1 for trees in source_trees.values())
        and len({r["identity"]["market_manifest_sha256"] for r in results.values()}) == 1
        and len({r["identity"]["input_identity_sha256"] for r in results.values()}) == 1
    )
    pairs = {
        f"{window}:{pool}": assess_pair(
            results[f"{window}:{pool}:incumbent"],
            results[f"{window}:{pool}:candidate"],
        )
        for window in ("long", "common")
        for pool in ("core17", "pool_b", "pool_d", "pool_g", "pool_f")
    }
    targets = ("pool_b", "pool_d", "pool_g")
    failed = [p for p in targets if pairs[f"common:{p}"]["wealth_ratio"] < 0.90]
    major = [p for p in failed if pairs[f"common:{p}"]["capacity_chain_major_source"]]
    acceptable = [p for p in targets if pairs[f"common:{p}"]["wealth_ratio"] >= 0.90]
    early = [p for p in targets if pairs[f"long:{p}"]["gap_formed_by_2024_share"] >= 0.70]
    non_trend = [
        p for p in targets
        if pairs[f"long:{p}"]["non_trend_positive_gap_growth_share"] >= 0.70
    ]
    all_correct = bool(identity_consistent and all(row["correctness"] for row in pairs.values()))
    path_a = bool(len(failed) >= 2 and len(major) >= 2 and all_correct)
    path_b = bool(
        not path_a and len(acceptable) >= 2 and len(early) >= 2
        and len(non_trend) >= 2 and not major and all_correct
    )
    selected = "A" if path_a else "B" if path_b else "STOP"
    reuse = verify_previous(results, previous)
    if not reuse["all_reproduced"]:
        raise RuntimeError("prior long-window evidence did not reproduce")
    payload = {
        "schema_version": 1,
        "kind": "account_risk_capacity_root_cause_assessment",
        "status": "PASS" if selected in {"A", "B"} else "STOP",
        "selected_path": selected,
        "decision": {
            "path_a_supported": path_a,
            "path_b_supported": path_b,
            "failed_common_pools": failed,
            "capacity_chain_major_source_pools": major,
            "common_acceptable_pools": acceptable,
            "long_2023_2024_gap_share_at_least_70pct_pools": early,
            "long_non_trend_positive_gap_growth_at_least_70pct_pools": non_trend,
            "identity_consistent": identity_consistent,
            "all_correct": all_correct,
        },
        "source_trees": source_trees,
        "reused_long_evidence_reproduction": reuse,
        "pairs": pairs,
        "contract_sha256": sha(Path(args.contract)),
        "result_file_sha256": {
            path.name: sha(path) for path in sorted(root.glob("*.json.gz"))
        },
    }
    Path(args.output).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(payload["decision"], ensure_ascii=False, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--previous-evidence-dir", required=True)
    parser.add_argument("--contract", required=True)
    parser.add_argument("--output", required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
