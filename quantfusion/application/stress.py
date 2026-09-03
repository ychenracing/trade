"""Production-replay stress tests for universe composition and ordering."""

# Keep the established adjacent-transition formula literal during decomposition.
# ruff: noqa: RUF007

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any

from quantfusion.application import engine_api as qf
from quantfusion.application import regime_api as ra
from quantfusion.application import stress_artifacts, stress_metrics, stress_scenarios
from quantfusion.config.overlay import SYMBOL_SUB_INDUSTRY
from quantfusion.config.paths import MARKET_DATA_DIR, PROJECT_ROOT, REGIME_DATA_DIR
from quantfusion.config.universe import SYMBOL_NAMES as NAMES

DATA_DIR = MARKET_DATA_DIR

_DRAWDOWN_RISK_EVENTS = {
    "portfolio_drawdown_alert_on",
    "confirmed_cycle_drawdown_lock",
    "emergency_drawdown_lock",
    "terminal_drawdown_lock",
    "persistent_portfolio_risk_lock",
}


def _reason_category(trade: qf.TradeRecord) -> str:
    """Map free-text execution reasons into stable audit categories."""
    reason = str(trade.reason).lower().replace("-", "_")
    if "sticky" in reason or "rotation" in reason or "replacement" in reason:
        return "sticky_replacement"
    if "route" in reason or "migration" in reason:
        return "route_migration"
    if trade.direction == "buy":
        if "re_entry" in reason or "reentry" in reason or "probe" in reason:
            return "re_entry"
        if "add" in reason or "pyramid" in reason or "confirm" in reason:
            return "add"
        return "initial_entry"
    if "sector" in reason:
        return "sector_liquidation"
    if any(
        token in reason
        for token in (
            "risk",
            "drawdown",
            "stop",
            "reduction",
            "choppy",
            "transition",
            "catastrophe",
            "trim",
        )
    ):
        return "risk_reduction"
    return "strategy_exit"


def _date_text(value: object) -> str:
    return value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else str(value)


def _held_symbols_at(trades: list[Any], date: str) -> list[str]:
    shares: dict[str, int] = {}
    for trade in trades:
        if str(trade.date) > date:
            continue
        signed = int(trade.shares) if trade.direction == "buy" else -int(trade.shares)
        shares[str(trade.symbol)] = shares.get(str(trade.symbol), 0) + signed
    return sorted(symbol for symbol, quantity in shares.items() if quantity > 0)


def _diagnostic_telemetry(result: dict[str, Any]) -> dict[str, Any]:
    """Reduce replay paths to compact causal evidence for diagnostic runs."""
    equity = result["equity_curve"]
    drawdown = result["drawdown_series"]
    trough_index = drawdown.idxmin()
    peak_index = equity.loc[:trough_index, "assets"].idxmax()
    peak_assets = float(equity.at[peak_index, "assets"])
    post_trough = equity.loc[trough_index:]
    recovered = post_trough[post_trough["assets"] >= peak_assets]
    recovery_date = _date_text(recovered.index[0]) if not recovered.empty else None
    trades = list(result.get("trades", []))
    risk_events = [
        event
        for event in result.get("risk_events", [])
        if event.get("event") in _DRAWDOWN_RISK_EVENTS
        or "rearm" in str(event.get("event", ""))
    ]
    first_trigger = next(
        (
            event
            for event in risk_events
            if event.get("event") in _DRAWDOWN_RISK_EVENTS
        ),
        None,
    )
    trigger_date = str(first_trigger["date"]) if first_trigger is not None else None
    reduction = next(
        (
            trade
            for trade in trades
            if trigger_date is not None
            and str(trade.date) >= trigger_date
            and trade.direction == "sell"
            and _reason_category(trade) in {"risk_reduction", "sector_liquidation"}
        ),
        None,
    )
    reduction_date = str(reduction.date) if reduction is not None else None

    symbol_counts = {
        str(item["date"]): int(item["symbol_count"])
        for item in result.get("portfolio_symbol_count_curve", [])
    }

    def snapshot(index: Any) -> dict[str, Any]:
        date = _date_text(index)
        row = equity.loc[index]
        assets = float(row["assets"])
        held = _held_symbols_at(trades, date)
        group_counts: dict[str, int] = {}
        for symbol in held:
            group = str(SYMBOL_SUB_INDUSTRY.get(symbol, "unmapped"))
            group_counts[group] = group_counts.get(group, 0) + 1
        return {
            "date": date,
            "assets": assets,
            "cash_ratio": float(row["cash"]) / assets if assets else 0.0,
            "exposure_ratio": float(row["position_value"]) / assets if assets else 0.0,
            "held_symbol_count": symbol_counts.get(date, len(held)),
            "largest_group_symbol_share": (
                max(group_counts.values()) / len(held) if held else 0.0
            ),
        }

    trigger_snapshot = (
        snapshot(equity.index[equity.index.get_indexer([trigger_date], method="pad")[0]])
        if trigger_date is not None
        else None
    )
    warning_buys = [
        trade
        for trade in trades
        if trigger_date is not None
        and str(trade.date) >= trigger_date
        and (reduction_date is None or str(trade.date) < reduction_date)
        and trade.direction == "buy"
    ]
    max_drawdown = abs(float(drawdown.min()))
    trigger_drawdown = (
        float(first_trigger.get("drawdown", first_trigger.get("threshold", 0.0)))
        if first_trigger is not None
        else None
    )
    return {
        "peak": snapshot(peak_index),
        "first_risk_trigger": (
            {
                key: first_trigger[key]
                for key in ("date", "event", "sleeve", "drawdown", "threshold")
                if key in first_trigger
            }
            if first_trigger is not None
            else None
        ),
        "trigger_snapshot": trigger_snapshot,
        "trough": snapshot(trough_index),
        "recovery_date": recovery_date,
        "first_executable_reduction": (
            {
                "date": str(reduction.date),
                "symbol": str(reduction.symbol),
                "reason_category": _reason_category(reduction),
            }
            if reduction is not None
            else None
        ),
        "execution_overshoot": (
            max_drawdown - trigger_drawdown if trigger_drawdown is not None else None
        ),
        "warning_period_buy_count": len(warning_buys),
        "warning_period_add_count": sum(
            _reason_category(trade) == "add" for trade in warning_buys
        ),
        "risk_milestones": [
            {
                key: event[key]
                for key in ("date", "event", "sleeve", "drawdown", "threshold")
                if key in event
            }
            for event in risk_events
        ],
        "concentration": {
            "max_concurrent_symbols": int(result.get("max_concurrent_symbols", 0)),
            "portfolio_max_positions": int(result.get("portfolio_max_positions", 0)),
        },
        "locks": {
            "cycle_lock_count": int(result.get("cycle_lock_count", 0)),
            "portfolio_cycle_lock_count": int(
                result.get("portfolio_cycle_lock_count", 0)
            ),
            "terminal_risk_lock": bool(result.get("terminal_risk_lock", False)),
            "persistent_risk_lock": bool(result.get("persistent_risk_lock", False)),
            "all_sleeves_locked": bool(result.get("all_sleeves_locked", False)),
            "rearm_event_count": sum(
                "rearm" in str(event.get("event", "")) for event in risk_events
            ),
        },
    }


def _metrics(
    codes: tuple[str, ...],
    *,
    data_dir: str | Path = DATA_DIR,
    regime_data_dir: str | Path = REGIME_DATA_DIR,
    include_diagnostics: bool = False,
) -> dict[str, Any]:
    with contextlib.redirect_stdout(io.StringIO()):
        result = ra.ProductionReplayEngine(stress_metrics.INITIAL_CAPITAL).run(
            {code: NAMES[code] for code in codes},
            stress_metrics.START_DATE,
            stress_metrics.END_DATE,
            data_dir=str(data_dir),
            regime_data_dir=str(regime_data_dir),
            indicator_state="warm",
        )
    attribution = {category: 0 for category in stress_metrics.ATTRIBUTION_CATEGORIES}
    for trade in result["trades"]:
        attribution[_reason_category(trade)] += 1
    metrics = {
        "symbol_count": len(codes),
        "symbols": list(codes),
        "total_return": float(result["total_return"]),
        "max_drawdown": float(result["max_drawdown"]),
        "sharpe": float(result["sharpe"]),
        "calmar": float(result["calmar"]),
        "total_trades": int(result["total_trades"]),
        "sleeve_fill_count": int(result["sleeve_fill_count"]),
        "date_symbol_side_count": int(result["date_symbol_side_count"]),
        "reason_attribution": attribution,
        "max_concurrent_symbols": int(result["max_concurrent_symbols"]),
        "terminal_risk_lock": bool(result["terminal_risk_lock"]),
        "deployment_policy": str(result["deployment_policy"]),
    }
    if include_diagnostics:
        metrics["diagnostic_telemetry"] = _diagnostic_telemetry(result)
    return metrics


def _run_scenario(
    scenario: dict[str, Any],
    *,
    data_dir: str | Path = DATA_DIR,
    regime_data_dir: str | Path = REGIME_DATA_DIR,
    include_diagnostics: bool = False,
) -> dict[str, Any]:
    codes = tuple(str(code) for code in scenario["symbols"])
    return {
        **scenario,
        **_metrics(
            codes,
            data_dir=data_dir,
            regime_data_dir=regime_data_dir,
            include_diagnostics=include_diagnostics,
        ),
    }


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument(
        "--random-samples", type=int, default=stress_scenarios.DEFAULT_RANDOM_SAMPLES
    )
    parser.add_argument(
        "--permutation-samples",
        type=int,
        default=stress_scenarios.DEFAULT_PERMUTATION_SAMPLES,
    )
    parser.add_argument(
        "--seeds",
        default=",".join(str(seed) for seed in stress_scenarios.DEFAULT_SEEDS),
        help="Comma-separated deterministic seeds",
    )
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    parser.add_argument("--regime-data-dir", default=str(REGIME_DATA_DIR))
    parser.add_argument(
        "--checkpoint",
        default=str(PROJECT_ROOT / "artifacts" / "checkpoints" / "stress.json"),
    )
    parser.add_argument("--scenario-id", help="Run one exact scenario as a diagnostic")
    parser.add_argument(
        "--scenario-ids-file",
        help="Run the listed exact scenarios as one non-canonical diagnostic batch",
    )
    parser.add_argument(
        "--scenario-type", help="Run one scenario family as a diagnostic"
    )
    parser.add_argument("--shard-index", type=int, help="Zero-based diagnostic shard")
    parser.add_argument("--shard-count", type=int, help="Diagnostic shard count")
    parser.add_argument(
        "--diagnostic-checkpoint",
        default=str(
            PROJECT_ROOT / "artifacts" / "checkpoints" / "stress-diagnostic.json"
        ),
    )
    parser.add_argument(
        "--diagnostic-output",
        help="Optional non-canonical diagnostic JSON output path",
    )
    parser.add_argument(
        "--establish-initial-baseline",
        action="store_true",
        help="Explicitly establish the first accepted artifact for this contract",
    )
    parser.add_argument(
        "--initial-baseline-reference",
        help="Retained current-semantic artifact used for transition protections",
    )
    parser.add_argument("--checkpoint-every", type=int, default=10)
    parser.add_argument(
        "--source-revision",
        required=True,
        help="Verified 40-character Git SHA containing the final Python source",
    )
    return parser


def main() -> int:
    """Run, checkpoint, gate, and atomically publish the formal audit."""
    args = build_argument_parser().parse_args()
    seeds = tuple(
        int(value.strip()) for value in args.seeds.split(",") if value.strip()
    )
    if (
        args.workers < 1
        or args.random_samples < 1
        or args.permutation_samples < 1
        or args.checkpoint_every < 1
        or not seeds
    ):
        raise ValueError(
            "workers, sample counts, checkpoint interval and seeds must be positive"
        )
    selector_requested = any(
        value is not None
        for value in (
            args.scenario_id,
            args.scenario_ids_file,
            args.scenario_type,
            args.shard_index,
            args.shard_count,
        )
    )
    formal_plan_requested = (
        not selector_requested
        and args.random_samples == stress_scenarios.DEFAULT_RANDOM_SAMPLES
        and args.permutation_samples == stress_scenarios.DEFAULT_PERMUTATION_SAMPLES
        and seeds == stress_scenarios.DEFAULT_SEEDS
    )
    if selector_requested and (
        args.establish_initial_baseline or args.initial_baseline_reference is not None
    ):
        raise ValueError("Initial baseline establishment requires the formal plan")
    if args.establish_initial_baseline != (
        args.initial_baseline_reference is not None
    ):
        raise ValueError(
            "--establish-initial-baseline and --initial-baseline-reference are required together"
        )
    formal_checkpoint = Path(args.checkpoint).expanduser().resolve()
    validation_namespace = stress_artifacts.VALIDATION_ARTIFACT_DIR.resolve()
    if formal_plan_requested and (
        formal_checkpoint == validation_namespace
        or formal_checkpoint.is_relative_to(validation_namespace)
    ):
        raise ValueError(
            "Formal checkpoint must be separate from the validation namespace"
        )
    full_scenarios = stress_scenarios._multi_seed_scenarios(
        random_samples=args.random_samples,
        permutation_samples=args.permutation_samples,
        seeds=seeds,
    )
    full_scenario_ids = [str(item["scenario_id"]) for item in full_scenarios]
    if len(set(full_scenario_ids)) != len(full_scenario_ids):
        raise ValueError("Stress scenario plan contains duplicate scenario_id values")
    selected_scenario_ids = (
        stress_scenarios._scenario_ids_from_file(
            Path(args.scenario_ids_file).expanduser().resolve()
        )
        if args.scenario_ids_file is not None
        else None
    )
    scenarios, formal_plan_complete = stress_scenarios.select_scenarios(
        full_scenarios,
        scenario_id=args.scenario_id,
        scenario_type=args.scenario_type,
        shard_index=args.shard_index,
        shard_count=args.shard_count,
        scenario_ids=selected_scenario_ids,
    )
    selection = {
        "scenario_id": args.scenario_id,
        "scenario_ids_file": args.scenario_ids_file,
        "scenario_ids": selected_scenario_ids,
        "scenario_type": args.scenario_type,
        "shard_index": args.shard_index,
        "shard_count": args.shard_count,
    }
    if formal_plan_complete and args.diagnostic_output is not None:
        raise ValueError("--diagnostic-output requires a scenario selector")
    if not formal_plan_complete:
        diagnostic_paths = [Path(args.diagnostic_checkpoint).expanduser().resolve()]
        if args.diagnostic_output is not None:
            diagnostic_paths.append(Path(args.diagnostic_output).expanduser().resolve())
        if len(set(diagnostic_paths)) != len(diagnostic_paths) or any(
            path == formal_checkpoint
            or path == validation_namespace
            or path.is_relative_to(validation_namespace)
            for path in diagnostic_paths
        ):
            raise ValueError(
                "Diagnostic paths must be separate from the formal checkpoint "
                "and validation namespace"
            )
    data_dir = Path(args.data_dir).expanduser().resolve()
    regime_data_dir = Path(args.regime_data_dir).expanduser().resolve()
    missing_stock = [
        code
        for code in stress_scenarios.ORDERED_CODES
        if not (data_dir / f"{code}.csv").is_file()
    ]
    missing_regime = [
        code
        for code in ra.REGIME_INDEX_FILES.values()
        if not (regime_data_dir / f"{code}.csv").is_file()
    ]
    if missing_stock or missing_regime:
        raise ValueError(
            "Stress data snapshot is incomplete "
            f"(stocks={missing_stock}, regime_indices={missing_regime})"
        )
    scenario_ids = [str(item["scenario_id"]) for item in scenarios]
    provenance = stress_artifacts._build_provenance(
        scenarios,
        data_dir,
        regime_data_dir,
        source_revision=args.source_revision,
    )
    signature = str(provenance["run_signature"])
    checkpoint = (
        Path(args.checkpoint)
        if formal_plan_complete
        else Path(args.diagnostic_checkpoint).expanduser()
    )
    completed: dict[str, dict[str, Any]] = {}
    if checkpoint.is_file():
        payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        completed = stress_artifacts._validated_checkpoint(
            payload,
            scenarios,
            signature=signature,
            provenance=provenance,
            diagnostic_selection=None if formal_plan_complete else selection,
        )
    pending = [
        scenario
        for scenario in scenarios
        if str(scenario["scenario_id"]) not in completed
    ]
    worker = partial(
        _run_scenario,
        data_dir=data_dir,
        regime_data_dir=regime_data_dir,
        include_diagnostics=not formal_plan_complete,
    )
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        for start in range(0, len(pending), args.checkpoint_every):
            chunk = pending[start : start + args.checkpoint_every]
            for result in executor.map(worker, chunk):
                completed[str(result["scenario_id"])] = result
            checkpoint_payload = {
                "signature": signature,
                "provenance": provenance,
                "completed": len(completed),
                "scenario_count": len(scenarios),
                "results": (
                    sorted(completed.values(), key=lambda item: item["scenario_id"])
                    if formal_plan_complete
                    else [
                        completed[str(item["scenario_id"])]
                        for item in scenarios
                        if str(item["scenario_id"]) in completed
                    ]
                ),
            }
            if not formal_plan_complete:
                checkpoint_payload.update(
                    {
                        "artifact_status": "diagnostic_checkpoint",
                        "formal_plan_complete": False,
                        "full_scenario_count": len(full_scenarios),
                        "selection": selection,
                    }
                )
            stress_artifacts._atomic_json(checkpoint, checkpoint_payload)
            print(f"checkpoint {len(completed)}/{len(scenarios)}", flush=True)
    results = (
        sorted(completed.values(), key=lambda item: item["scenario_id"])
        if formal_plan_complete
        else [completed[str(item["scenario_id"])] for item in scenarios]
    )
    if set(completed) != set(scenario_ids):
        raise ValueError("Stress run did not complete the exact scenario plan")
    common = {
        **provenance,
        "artifact_status": "current",
        "trade_count_semantics": stress_metrics.TRADE_COUNT_SEMANTICS,
        "portfolio_policy": qf.PortfolioPolicy().as_dict(),
        "data_directory": stress_artifacts._artifact_path(data_dir),
        "regime_data_directory": stress_artifacts._artifact_path(regime_data_dir),
        "indicator_state": "warm",
        "seeds": list(seeds),
    }
    by_type = {
        scenario_type: stress_metrics._summary(
            [item for item in results if item["scenario_type"] == scenario_type]
        )
        for scenario_type in sorted({item["scenario_type"] for item in results})
    }
    summary = {"all": stress_metrics._summary(results), "by_type": by_type}
    if not formal_plan_complete:
        diagnostic_artifact = {
            **common,
            "artifact_status": "diagnostic",
            "formal_plan_complete": False,
            "canonical": False,
            "full_scenario_count": len(full_scenarios),
            "selection": selection,
            "scenario_count": len(results),
            "summary": summary,
            "results": results,
        }
        if args.diagnostic_output is not None:
            stress_artifacts._atomic_json(
                Path(args.diagnostic_output).expanduser(), diagnostic_artifact
            )
        print(json.dumps(diagnostic_artifact, ensure_ascii=False, indent=2))
        return 0

    prefixes = sorted(
        (item for item in results if item["scenario_type"] == "prefix"),
        key=lambda item: item["symbol_count"],
    )
    wealth = [1.0 + item["total_return"] for item in prefixes]
    adjacent = [
        {
            "from_count": left["symbol_count"],
            "to_count": right["symbol_count"],
            "wealth_change": stress_metrics._wealth_change(right, left),
        }
        for left, right in zip(prefixes, prefixes[1:], strict=False)
    ]
    prefix_artifact = {
        **common,
        "ordering": list(stress_scenarios.ORDERED_CODES),
        "minimum_to_maximum_wealth_ratio": min(wealth) / max(wealth),
        "worst_adjacent_transition": min(
            adjacent, key=lambda item: item["wealth_change"]
        ),
        "summary": stress_metrics._summary(prefixes),
        "results": prefixes,
    }
    gates = stress_metrics._absolute_hard_gates(results)
    robustness = stress_metrics._robustness_diagnostics(results)
    # 2026-08-16 报告 P0-4: 在覆盖正式工件之前加载既有基线，评估强制晋级门。
    incumbent_path = stress_artifacts.VALIDATION_ARTIFACT_DIR / "universe_stress.json"
    incumbent = stress_artifacts._load_incumbent(incumbent_path)
    initial_baseline_reference = (
        stress_artifacts._load_initial_baseline_reference(
            Path(args.initial_baseline_reference).expanduser().resolve()
        )
        if args.initial_baseline_reference is not None
        else None
    )
    promotion = stress_metrics._promotion_gates(results, incumbent)
    initial_baseline_gates = stress_metrics._initial_baseline_gates(
        results, initial_baseline_reference
    )
    universe_artifact = {
        **common,
        "scenario_count": len(results),
        "random_samples_per_size_per_seed": args.random_samples,
        "permutation_samples_per_seed": args.permutation_samples,
        "absolute_hard_gates": gates,
        "robustness_diagnostics": robustness,
        "promotion_gates": promotion,
        "initial_baseline_gates": initial_baseline_gates,
        "summary": summary,
        "results": results,
    }
    published = stress_artifacts._publish_formal_artifacts(
        prefix_artifact,
        universe_artifact,
        scenarios=scenarios,
        provenance=provenance,
        incumbent=incumbent,
        formal_plan_complete=formal_plan_complete,
        establish_initial_baseline=args.establish_initial_baseline,
        initial_baseline_reference=initial_baseline_reference,
    )
    print(
        json.dumps(
            {
                "absolute_hard_gates": gates,
                "robustness_diagnostics": robustness,
                "promotion_gates": promotion,
                "initial_baseline_gates": initial_baseline_gates,
                "summary": universe_artifact["summary"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if published else 2


if __name__ == "__main__":
    raise SystemExit(main())
