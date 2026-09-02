"""CLI and public API for leakage-resistant parameter research."""

from __future__ import annotations

# ruff: noqa: F401

import argparse
import hashlib
import json
from pathlib import Path

from quantfusion.application.backtest_cli import parse_symbols
from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.config.regime import REGIME_INDEX_FILES
from quantfusion.research.artifacts import (
    atomic_text,
    cache_signature,
    load_resume_evaluations,
    render_markdown_summary,
)
from quantfusion.research.candidates import (
    Candidate,
    DEFAULT_DRAWDOWN_LIMIT as DEFAULT_DRAWDOWN_LIMIT,
    DateWindow,
    INTEGER_SYMBOL_PARAMETERS as INTEGER_SYMBOL_PARAMETERS,
    MAX_POSITIONS as MAX_POSITIONS,
    MAX_SYMBOL_WEIGHT as MAX_SYMBOL_WEIGHT,
    MAX_TOTAL_WEIGHT as MAX_TOTAL_WEIGHT,
    ParameterSpace,
    POLICY_RISK_KEYS as POLICY_RISK_KEYS,
    POLICY_RISK_PROFILES as POLICY_RISK_PROFILES,
    SLEEVE_WEIGHT_PROFILES as SLEEVE_WEIGHT_PROFILES,
    WalkForwardFold,
)
from quantfusion.research.catalog import LocalDataCatalog, build_walk_forward_folds
from quantfusion.research.evaluation import (
    CandidateEvaluation,
    CandidateRunner,
    CandidateRunnerProtocol,
    WindowMetrics,
    pareto_frontier,
)
from quantfusion.research.fingerprints import (
    engine_source_sha,
    optimizer_source_sha,
    replay_source_sha,
)
from quantfusion.research.search import WalkForwardOptimizer
from quantfusion.research import replay_api as ra

_atomic_text = atomic_text
_cache_signature = cache_signature
_load_resume_evaluations = load_resume_evaluations

def build_argument_parser() -> argparse.ArgumentParser:
    """Build the deterministic local-data optimizer command line."""
    parser = argparse.ArgumentParser(
        description="Walk-forward parameter optimizer for Quant Fusion"
    )
    parser.add_argument("--symbol", "-s", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument(
        "--regime-data-dir",
        required=True,
        help="Frozen directory containing 000300.csv and 000682.csv",
    )
    parser.add_argument("--start", default="2024-01-02")
    parser.add_argument("--test-start", default="2026-01-05")
    parser.add_argument("--end", default="2026-07-20")
    parser.add_argument("--train-months", type=int, default=12)
    parser.add_argument("--validation-months", type=int, default=6)
    parser.add_argument("--step-months", type=int, default=6)
    parser.add_argument("--minimum-folds", type=int, default=2)
    parser.add_argument("--candidates", type=int, default=40)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--drawdown-limit", type=float, default=0.20)
    parser.add_argument("--capital", type=float, default=2_000_000)
    parser.add_argument("--search-space", default="")
    parser.add_argument(
        "--stage",
        choices=("risk", "turnover", "return", "all"),
        default="risk",
        help="Optimize one parameter family at a time; start with risk",
    )
    parser.add_argument(
        "--resume-report",
        default="",
        help="Reuse matching pre-test candidate evaluations from a prior report",
    )
    parser.add_argument("--output-dir", default="optimizer_output")
    return parser


def main() -> int:
    """Run automatic selection and save an auditable report plus loadable config."""
    args = build_argument_parser().parse_args()
    symbols = parse_symbols(args.symbol)
    catalog = LocalDataCatalog(
        args.data_dir,
        symbols,
        PortfolioPolicy().regime_symbols,
    )
    regime_data_dir = Path(args.regime_data_dir).expanduser()
    regime_fingerprint = hashlib.sha256()
    for code in sorted(REGIME_INDEX_FILES.values()):
        path = regime_data_dir / f"{code}.csv"
        if not path.is_file():
            raise ValueError(f"Missing regime index data for {code}: {path}")
        regime_fingerprint.update(code.encode("ascii"))
        regime_fingerprint.update(hashlib.sha256(path.read_bytes()).digest())
    regime_data_fingerprint = regime_fingerprint.hexdigest()
    folds, holdout = build_walk_forward_folds(
        catalog.calendar,
        start=args.start,
        test_start=args.test_start,
        end=args.end,
        train_months=args.train_months,
        validation_months=args.validation_months,
        step_months=args.step_months,
        minimum_folds=args.minimum_folds,
    )
    space = (
        ParameterSpace.from_json(args.search_space)
        if args.search_space
        else ParameterSpace.for_stage(args.stage)
    )
    candidates = space.candidates(args.candidates, args.seed)
    runner = CandidateRunner(
        symbols,
        catalog,
        regime_data_dir=regime_data_dir,
        initial_capital=args.capital,
    )
    optimizer = WalkForwardOptimizer(
        runner,
        folds,
        holdout,
        drawdown_limit=args.drawdown_limit,
        progress=True,
    )
    cached = (
        load_resume_evaluations(
            args.resume_report,
            symbols=symbols,
            catalog=catalog,
            folds=folds,
            drawdown_limit=args.drawdown_limit,
            regime_data_fingerprint=regime_data_fingerprint,
        )
        if args.resume_report
        else {}
    )
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    signature = cache_signature(
        symbols=symbols,
        catalog=catalog,
        folds=folds,
        drawdown_limit=args.drawdown_limit,
        initial_capital=args.capital,
        regime_data_fingerprint=regime_data_fingerprint,
        indicator_state=runner.indicator_state,
        warmup_calendar_days=runner.warmup_calendar_days,
    )
    report = optimizer.optimize(
        candidates,
        cached_evaluations=cached,
        cache_dir=output / "candidate_cache" / signature,
    )
    report["run_metadata"] = {
        "symbols": symbols,
        "data_directory": str(Path(args.data_dir).expanduser()),
        "regime_data_directory": str(regime_data_dir),
        "data_coverage": catalog.coverage(),
        "candidate_count": len(candidates),
        "seed": args.seed,
        "optimization_stage": args.stage,
        "engine_sha256": engine_source_sha(),
        "production_replay_sha256": replay_source_sha(),
        "optimizer_sha256": optimizer_source_sha(),
        "data_fingerprint": catalog.fingerprint,
        "regime_data_fingerprint": regime_data_fingerprint,
        "cache_signature": signature,
    }
    atomic_text(
        output / "optimization_report.json",
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
    )
    atomic_text(
        output / "optimization_summary.md",
        render_markdown_summary(report),
    )
    if report.get("recommended_candidate") is not None:
        recommended = dict(report["recommended_candidate"])
        recommended["materialized_per_symbol_config"] = Candidate(
            recommended["engine_overrides"],
            recommended["symbol_multipliers"],
            recommended["policy_overrides"],
        ).per_symbol_config(symbols)
        atomic_text(
            output / "recommended_config.json",
            json.dumps(recommended, ensure_ascii=False, indent=2, allow_nan=False)
            + "\n",
        )
    print(render_markdown_summary(report))
    return 0 if report["status"] != "no_feasible_candidate" else 2


if __name__ == "__main__":
    raise SystemExit(main())
