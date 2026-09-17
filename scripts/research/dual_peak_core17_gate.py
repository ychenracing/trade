from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np

EXPECTED_INCUMBENT: dict[str, int | float] = {
    "wealth_multiple": 9.610542744515504,
    "max_drawdown": 0.15104978428469945,
    "date_symbol_side_buckets": 190,
    "active_fill_days": 92,
    "sleeve_fills": 645,
    "turnover_over_average_equity": 16.00753130920448,
    "commission_plus_stamp": 74192.392969,
}

SOURCE_PATHS = (
    "quantfusion/risk/account_budget.py",
    "quantfusion/risk/account_budget_legacy.py",
    "quantfusion/risk/account_risk_capacity.py",
    "quantfusion/risk/account_risk_coverage.py",
    "quantfusion/risk/account_risk_epoch.py",
    "quantfusion/risk/account_risk_reallocation.py",
    "quantfusion/risk/managers.py",
    "quantfusion/risk/managers_legacy.py",
)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _run_one(root: Path, evidence: Path, label: str) -> None:
    root = root.resolve()
    evidence = evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    os.chdir(root)
    sys.path.insert(0, str(root))

    from quantfusion.config.universe import SYMBOL_NAMES
    from quantfusion.engine.replay import ProductionReplayEngine

    result = ProductionReplayEngine(2_000_000.0).run(
        SYMBOL_NAMES,
        "2025-04-01",
        "2026-07-20",
        data_dir="data/market",
        regime_data_dir="data/regime",
        indicator_state="warm",
    )
    trades = list(result["trades"])
    curve = result["equity_curve"].copy()
    gross_traded_value = sum(
        abs(float(trade.gross_value or trade.shares * trade.price))
        for trade in trades
    )
    commission = sum(float(trade.commission) for trade in trades)
    stamp_duty = sum(float(trade.stamp_duty_cost) for trade in trades)
    mean_nav = float(curve["assets"].mean())
    conservation_error = float(
        (curve["assets"] - curve["cash"] - curve["position_value"]).abs().max()
    )
    risk_status = result["account_risk_budget"]
    existing_source_paths = [Path(path) for path in SOURCE_PATHS if Path(path).is_file()]
    payload = {
        "label": label,
        "window": ["2025-04-01", "2026-07-20"],
        "initial_capital": 2_000_000.0,
        "symbol_count": len(SYMBOL_NAMES),
        "symbol_order": list(SYMBOL_NAMES),
        "wealth_multiple": 1.0 + float(result["total_return"]),
        "max_drawdown": abs(float(result["max_drawdown"])),
        "date_symbol_side_buckets": int(result["date_symbol_side_count"]),
        "active_fill_days": len({str(trade.date) for trade in trades}),
        "sleeve_fills": int(result["sleeve_fill_count"]),
        "trade_records": int(result["total_trades"]),
        "gross_traded_value": gross_traded_value,
        "mean_nav": mean_nav,
        "turnover_over_average_equity": gross_traded_value / mean_nav,
        "commission": commission,
        "stamp_duty": stamp_duty,
        "commission_plus_stamp": commission + stamp_duty,
        "minimum_cash": float(curve["cash"].min()),
        "maximum_conservation_error": conservation_error,
        "equity_rows": int(len(curve)),
        "finite": bool(
            np.isfinite(curve[["assets", "cash", "position_value"]].to_numpy()).all()
            and all(
                math.isfinite(float(value))
                for value in (
                    result["total_return"],
                    result["max_drawdown"],
                    gross_traded_value,
                    mean_nav,
                    commission,
                    stamp_duty,
                )
            )
        ),
        "account_risk_budget_enabled": bool(risk_status["enabled"]),
        "account_risk_budget_status": str(risk_status["status"]),
        "account_risk_budget_mechanism": str(risk_status.get("mechanism", "")),
        "account_risk_production_mechanism": str(
            (risk_status.get("latest") or {}).get("production_mechanism", "")
        ),
        "source_files": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in existing_source_paths
        },
    }
    _write_json(evidence / f"{label}-core17.json", payload)
    curve.to_csv(evidence / f"{label}-equity-curve.csv", index=False)
    with (evidence / f"{label}-trades.jsonl").open("w", encoding="utf-8") as handle:
        for trade in trades:
            if dataclasses.is_dataclass(trade):
                record = dataclasses.asdict(trade)
            else:
                record = {
                    name: getattr(trade, name, None)
                    for name in (
                        "date",
                        "symbol",
                        "strategy_name",
                        "direction",
                        "shares",
                        "price",
                        "gross_value",
                        "commission",
                        "stamp_duty_cost",
                        "reason",
                    )
                }
            handle.write(
                json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
                + "\n"
            )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _close_enough(actual: int | float, expected: int | float) -> bool:
    if isinstance(expected, int):
        return actual == expected
    return math.isclose(
        float(actual), float(expected), rel_tol=1e-11, abs_tol=1e-8
    )


def _assess(evidence: Path) -> None:
    evidence = evidence.resolve()
    incumbent = json.loads((evidence / "incumbent-core17.json").read_text())
    candidate = json.loads((evidence / "candidate-core17.json").read_text())
    identity = {
        key: _close_enough(incumbent[key], expected)
        for key, expected in EXPECTED_INCUMBENT.items()
    }
    wealth_ratio = candidate["wealth_multiple"] / incumbent["wealth_multiple"]
    mdd_delta = candidate["max_drawdown"] - incumbent["max_drawdown"]
    bucket_reduction = 1.0 - (
        candidate["date_symbol_side_buckets"]
        / incumbent["date_symbol_side_buckets"]
    )
    active_day_reduction = 1.0 - (
        candidate["active_fill_days"] / incumbent["active_fill_days"]
    )
    turnover_ratio = (
        candidate["turnover_over_average_equity"]
        / incumbent["turnover_over_average_equity"]
    )
    fee_ratio = (
        candidate["commission_plus_stamp"]
        / incumbent["commission_plus_stamp"]
    )
    correctness = bool(
        incumbent["finite"]
        and candidate["finite"]
        and incumbent["minimum_cash"] >= -1e-6
        and candidate["minimum_cash"] >= -1e-6
        and incumbent["maximum_conservation_error"] <= 1e-6
        and candidate["maximum_conservation_error"] <= 1e-6
        and incumbent["account_risk_budget_enabled"]
        and candidate["account_risk_budget_enabled"]
        and candidate["account_risk_budget_status"] == "APPLIED"
        and candidate["account_risk_production_mechanism"]
        == "account_risk_coverage"
    )
    gates = {
        "incumbent_identity_reproduced": all(identity.values()),
        "wealth_ratio_at_least_1_05": wealth_ratio >= 1.05,
        "mdd_degradation_at_most_1pp": mdd_delta <= 0.01,
        "candidate_mdd_at_most_19pct": candidate["max_drawdown"] <= 0.19,
        "bucket_reduction_at_least_15pct": bucket_reduction >= 0.15,
        "active_day_reduction_at_least_10pct": active_day_reduction >= 0.10,
        "turnover_not_above_incumbent": turnover_ratio <= 1.0 + 1e-12,
        "sleeve_fills_decrease": candidate["sleeve_fills"]
        < incumbent["sleeve_fills"],
        "fees_no_significant_increase": fee_ratio <= 1.02,
        "correctness": correctness,
    }
    decision = "PASS" if all(gates.values()) else "FAIL"
    payload = {
        "contract": "dual_peak_account_risk_epoch_and_opportunity_driven_minimal_capacity_reallocation",
        "layer": "core17",
        "decision": decision,
        "identity_checks": identity,
        "gates": gates,
        "comparisons": {
            "wealth_ratio": wealth_ratio,
            "mdd_delta": mdd_delta,
            "bucket_reduction": bucket_reduction,
            "active_fill_day_reduction": active_day_reduction,
            "turnover_ratio": turnover_ratio,
            "fee_ratio": fee_ratio,
        },
        "incumbent": incumbent,
        "candidate": candidate,
        "next_layer_allowed": decision == "PASS",
    }
    _write_json(evidence / "core17-gate.json", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    if decision != "PASS":
        raise SystemExit(1)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_one = subparsers.add_parser("run-one")
    run_one.add_argument("--root", type=Path, required=True)
    run_one.add_argument("--evidence", type=Path, required=True)
    run_one.add_argument("--label", choices=("incumbent", "candidate"), required=True)
    assess = subparsers.add_parser("assess")
    assess.add_argument("--evidence", type=Path, required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "run-one":
        _run_one(args.root, args.evidence, args.label)
    else:
        _assess(args.evidence)


if __name__ == "__main__":
    main()
