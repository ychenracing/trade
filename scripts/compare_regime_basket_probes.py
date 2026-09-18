"""Experiment A: alternate regime referee baskets vs origin/main (validation pools).

Runs BacktestEngine warm 2025-04-01→2026-07-20 capital 2e6 on pools 1/3/5/13/17.
Does NOT retie sector guard to the trading pool. Sector recovery thresholds unchanged.
Formal 958 on main does NOT cover these candidates.
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
from quantfusion.config.paths import MARKET_DATA_DIR, PROJECT_ROOT
from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.config.universe import SYMBOL_NAMES, VALIDATION_UNIVERSES

START = "2025-04-01"
END = "2026-07-20"
CAPITAL = 2_000_000.0
POOLS = ("1_symbol", "3_symbols", "5_symbols", "13_symbols", "17_symbols")

PROBES: dict[str, tuple[str, ...]] = {
    "main": ("300308", "300502", "300394", "688008", "603986"),
    "drop_688008": ("300308", "300502", "300394", "603986"),
    "swap_002384": ("300308", "300502", "300394", "002384", "603986"),
    "optical_only_3": ("300308", "300502", "300394"),
    # optional second swap if primary crashes (run with --probes)
    "swap_300408": ("300308", "300502", "300394", "300408", "603986"),
    "swap_601869": ("300308", "300502", "300394", "601869", "603986"),
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
    summary["guard_scope_mode"] = result.get("guard_scope_mode")
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
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probes",
        nargs="+",
        default=["main", "drop_688008", "swap_002384"],
        choices=sorted(PROBES),
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--out",
        type=Path,
        default=PROJECT_ROOT
        / "artifacts"
        / "diagnostics"
        / "regime-alt-referee-pool-compare.json",
    )
    args = parser.parse_args()

    tasks = [
        (probe, pool, PROBES[probe])
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

    main_res = results.get("main")
    comparisons: dict[str, Any] = {}
    if main_res:
        for probe in args.probes:
            if probe == "main":
                continue
            comparisons[probe] = {
                pool: _delta(main_res[pool], results[probe][pool]) for pool in POOLS
            }

    artifact = {
        "meta": {
            "experiment": "A_alternate_referee_members",
            "window": f"{START}..{END}",
            "capital": CAPITAL,
            "engine": "BacktestEngine",
            "indicator_state": "warm",
            "sector_recovery_unchanged": True,
            "guard_not_retied_to_trading_pool": True,
            "formal_958_does_not_cover_candidates": True,
            "probes": {name: list(PROBES[name]) for name in args.probes},
            "notes": [
                "drop_688008: 4-symbol referee; sector_guard_min_symbols=ceil(0.8*4)=4.",
                "swap_002384: replace out-of-universe 688008 with in-universe 东山精密 "
                "(optical_component chain thermometer; production rank #13).",
                "Prior 688256 sole-swap discarded (see regime-basket-688256-pool-compare.json).",
            ],
        },
        "results": results,
        "comparison_vs_main": comparisons,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
