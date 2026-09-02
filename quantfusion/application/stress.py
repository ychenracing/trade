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
from quantfusion.config.paths import MARKET_DATA_DIR, PROJECT_ROOT, REGIME_DATA_DIR
from quantfusion.config.universe import SYMBOL_NAMES as NAMES

DATA_DIR = MARKET_DATA_DIR


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


def _metrics(
    codes: tuple[str, ...],
    *,
    data_dir: str | Path = DATA_DIR,
    regime_data_dir: str | Path = REGIME_DATA_DIR,
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
    return {
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


def _run_scenario(
    scenario: dict[str, Any],
    *,
    data_dir: str | Path = DATA_DIR,
    regime_data_dir: str | Path = REGIME_DATA_DIR,
) -> dict[str, Any]:
    codes = tuple(str(code) for code in scenario["symbols"])
    return {
        **scenario,
        **_metrics(codes, data_dir=data_dir, regime_data_dir=regime_data_dir),
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
    full_scenarios = stress_scenarios._multi_seed_scenarios(
        random_samples=args.random_samples,
        permutation_samples=args.permutation_samples,
        seeds=seeds,
    )
    full_scenario_ids = [str(item["scenario_id"]) for item in full_scenarios]
    if len(set(full_scenario_ids)) != len(full_scenario_ids):
        raise ValueError("Stress scenario plan contains duplicate scenario_id values")
    scenarios, formal_plan_complete = stress_scenarios.select_scenarios(
        full_scenarios,
        scenario_id=args.scenario_id,
        scenario_type=args.scenario_type,
        shard_index=args.shard_index,
        shard_count=args.shard_count,
    )
    selection = {
        "scenario_id": args.scenario_id,
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
        formal_checkpoint = Path(args.checkpoint).resolve()
        validation_namespace = stress_artifacts.VALIDATION_ARTIFACT_DIR.resolve()
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
    worker = partial(_run_scenario, data_dir=data_dir, regime_data_dir=regime_data_dir)
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
    gates = stress_metrics._hard_gates(results)
    # 2026-08-16 报告 P0-4: 在覆盖正式工件之前加载既有基线，评估强制晋级门。
    incumbent_path = stress_artifacts.VALIDATION_ARTIFACT_DIR / "universe_stress.json"
    incumbent = stress_artifacts._load_incumbent(incumbent_path)
    promotion = stress_metrics._promotion_gates(results, incumbent)
    universe_artifact = {
        **common,
        "scenario_count": len(results),
        "random_samples_per_size_per_seed": args.random_samples,
        "permutation_samples_per_seed": args.permutation_samples,
        "hard_gates": gates,
        "promotion_gates": promotion,
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
    )
    print(
        json.dumps(
            {
                "hard_gates": gates,
                "promotion_gates": promotion,
                "summary": universe_artifact["summary"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if published else 2


if __name__ == "__main__":
    raise SystemExit(main())
