"""Experiments D/E: sector-guard robust-trim and enlarged RISK_BASKET referee vs main.

Pools 1/3/5/13/17, BacktestEngine warm 2025-04-01→2026-07-20 capital 2e6.
Does NOT retie guard to the trading pool. Does NOT swap 688008→688256.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from quantfusion.application import engine_api as qf
from quantfusion.config.overlay import RISK_BASKET
from quantfusion.config.paths import MARKET_DATA_DIR, PROJECT_ROOT
from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.config.universe import ORDERED_SYMBOLS, SYMBOL_NAMES, VALIDATION_UNIVERSES

START = "2025-04-01"
END = "2026-07-20"
CAPITAL = 2_000_000.0
POOLS = ("1_symbol", "3_symbols", "5_symbols", "13_symbols", "17_symbols")

# Experiment D keeps the production 5-name referee; robust trim lives in code.
MAIN_REGIME = ("300308", "300502", "300394", "688008", "603986")

# Experiment E: fixed enlargement = current 5 ∪ (RISK_BASKET ∩ ORDERED_SYMBOLS).
# Order: keep main-5 first, then RISK_BASKET order for extras (no --symbol auto-pick).
_EXTRAS = tuple(
    s for s in RISK_BASKET if s in ORDERED_SYMBOLS and s not in MAIN_REGIME
)
LARGE_REGIME = MAIN_REGIME + _EXTRAS

PROBES: dict[str, dict[str, Any]] = {
    "main_baseline": {
        "label": "origin/main baseline (5-name, plain mean)",
        "regime_symbols": MAIN_REGIME,
        "experiment": "baseline",
        "note": "Re-run on this worktree only if code matches main; prefer "
        "artifact from an origin/main checkout when comparing D.",
    },
    "D_robust_trim": {
        "label": "Experiment D: drop-worst-1 equal-weight / breadth on main-5",
        "regime_symbols": MAIN_REGIME,
        "experiment": "D",
        "note": "regime_symbols unchanged; _build_sector_observation trims "
        "worst 1 daily return before mean (breadth + recovery series too).",
    },
    "E_large_basket": {
        "label": "Experiment E: fixed RISK_BASKET∩universe enlargement",
        "regime_symbols": LARGE_REGIME,
        "experiment": "E",
        "note": (
            f"n={len(LARGE_REGIME)} fixed referee; "
            f"sector_guard_min_symbols=ceil(0.8*{len(LARGE_REGIME)})="
            f"{max(1, math.ceil(len(LARGE_REGIME) * 0.8))}. "
            "No robust trim in E-only branch; on this combined script E inherits "
            "whatever observation code the checkout has."
        ),
    },
}


def _summarize(result: dict[str, Any], elapsed_s: float) -> dict[str, Any]:
    on_dates = sorted(
        {
            event["date"]
            for event in result["risk_events"]
            if event.get("event") == "sector_guard_on"
        }
    )
    off_dates = sorted(
        {
            event["date"]
            for event in result["risk_events"]
            if event.get("event") == "sector_guard_off"
        }
    )
    guard_event_count = sum(
        1 for event in result["risk_events"] if event.get("event") == "sector_guard_on"
    )
    return {
        "total_return": float(result["total_return"]),
        "max_drawdown": float(result["max_drawdown"]),
        "final_assets": float(result["final_assets"]),
        "total_trades": int(result["total_trades"]),
        "sell_trades": int(result["sell_trades"]),
        "sleeve_fill_count": int(result["sleeve_fill_count"]),
        "guard_on_dates": on_dates,
        "guard_off_dates": off_dates,
        "guard_event_count": guard_event_count,
        "first_guard_on": on_dates[0] if on_dates else None,
        "elapsed_s": round(elapsed_s, 2),
    }


def _run_one(task: tuple[str, str, tuple[str, ...]]) -> tuple[str, str, dict[str, Any]]:
    probe_name, pool_name, regime_symbols = task
    policy = PortfolioPolicy(regime_symbols=regime_symbols)
    engine = qf.BacktestEngine(CAPITAL, policy=policy)
    codes = VALIDATION_UNIVERSES[pool_name]
    t0 = time.time()
    with contextlib.redirect_stdout(io.StringIO()):
        result = engine.run(
            {code: SYMBOL_NAMES[code] for code in codes},
            START,
            END,
            data_dir=str(MARKET_DATA_DIR),
            indicator_state="warm",
        )
    summary = _summarize(result, time.time() - t0)
    summary["regime_symbols"] = list(regime_symbols)
    summary["sector_guard_min_symbols"] = max(1, math.ceil(len(regime_symbols) * 0.8))
    return probe_name, pool_name, summary


def _delta(main: dict[str, Any], cand: dict[str, Any]) -> dict[str, Any]:
    return {
        "delta_return": cand["total_return"] - main["total_return"],
        "delta_mdd": cand["max_drawdown"] - main["max_drawdown"],
        "delta_trades": cand["total_trades"] - main["total_trades"],
        "delta_fills": cand["sleeve_fill_count"] - main["sleeve_fill_count"],
        "guard_on_changed": cand["guard_on_dates"] != main["guard_on_dates"],
        "first_guard_on_main": main["first_guard_on"],
        "first_guard_on_cand": cand["first_guard_on"],
        "guard_event_count_main": main["guard_event_count"],
        "guard_event_count_cand": cand["guard_event_count"],
        "guard_on_count_main": len(main["guard_on_dates"]),
        "guard_on_count_cand": len(cand["guard_on_dates"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probes",
        nargs="+",
        default=["D_robust_trim"],
        choices=sorted(PROBES),
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--baseline-json",
        type=Path,
        default=None,
        help="Optional prior main results JSON (regime-alt style) for vs-main deltas",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=PROJECT_ROOT
        / "artifacts"
        / "diagnostics"
        / "sector-guard-robust-trim-compare.json",
    )
    args = parser.parse_args()

    tasks = [
        (probe, pool, tuple(PROBES[probe]["regime_symbols"]))
        for probe in args.probes
        for pool in POOLS
    ]
    results: dict[str, dict[str, Any]] = {p: {} for p in args.probes}
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(_run_one, t) for t in tasks]
        for fut in as_completed(futs):
            probe, pool, summary = fut.result()
            results[probe][pool] = summary
            print(
                f"{probe:16s} {pool:12s} ret={summary['total_return']:.4f} "
                f"mdd={summary['max_drawdown']:.4f} trades={summary['total_trades']} "
                f"guard_on={len(summary['guard_on_dates'])} "
                f"events={summary['guard_event_count']} "
                f"first={summary['first_guard_on']} ({summary['elapsed_s']}s)",
                flush=True,
            )

    main_res: dict[str, Any] | None = None
    if args.baseline_json and args.baseline_json.exists():
        baseline = json.loads(args.baseline_json.read_text(encoding="utf-8"))
        if "results" in baseline and "main" in baseline["results"]:
            main_res = baseline["results"]["main"]
        elif "results" in baseline and "main_baseline" in baseline["results"]:
            main_res = baseline["results"]["main_baseline"]
    if main_res is None and "main_baseline" in results:
        main_res = results["main_baseline"]

    comparisons: dict[str, Any] = {}
    if main_res:
        for probe in args.probes:
            if probe in ("main_baseline", "main"):
                continue
            comparisons[probe] = {
                pool: _delta(main_res[pool], results[probe][pool]) for pool in POOLS
            }

    artifact = {
        "meta": {
            "window": f"{START}..{END}",
            "capital": CAPITAL,
            "engine": "BacktestEngine",
            "indicator_state": "warm",
            "guard_not_retied_to_trading_pool": True,
            "no_688256_sole_swap": True,
            "probes": {
                name: {
                    "label": PROBES[name]["label"],
                    "experiment": PROBES[name]["experiment"],
                    "regime_symbols": list(PROBES[name]["regime_symbols"]),
                    "note": PROBES[name]["note"],
                    "sector_guard_min_symbols": max(
                        1, math.ceil(len(PROBES[name]["regime_symbols"]) * 0.8)
                    ),
                }
                for name in args.probes
            },
            "robust_trim_rule": (
                "Drop the single worst daily return name before averaging "
                "equal_return / shock_breadth / recovery_breadth; normalized_series "
                "uses the same keep-set so recovery sector-MA matches. Quorum "
                "symbol_count unchanged. Confirmations/thresholds unchanged."
            ),
            "large_basket_rule": (
                "MAIN_REGIME ∪ (RISK_BASKET ∩ ORDERED_SYMBOLS) in RISK_BASKET order; "
                f"LARGE_REGIME={list(LARGE_REGIME)}"
            ),
            "baseline_json": str(args.baseline_json) if args.baseline_json else None,
        },
        "results": results,
        "comparison_vs_main": comparisons,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
