"""Experiment C: index-based sector-guard probes vs origin/main baseline."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import time
from pathlib import Path
from typing import Any

from quantfusion.application import engine_api as qf
from quantfusion.config.paths import MARKET_DATA_DIR, PROJECT_ROOT
from quantfusion.config.universe import SYMBOL_NAMES, VALIDATION_UNIVERSES

START = "2025-04-01"
END = "2026-07-20"
CAPITAL = 2_000_000.0
POOLS = ("1_symbol", "3_symbols", "5_symbols", "13_symbols", "17_symbols")
MODES = ("tech_only", "dual_confirm")
OUT = PROJECT_ROOT / "artifacts" / "diagnostics" / "sector-guard-index-compare.json"
MAIN_BASELINE_CANDIDATES = (
    Path("/workspace/trade-guard-pool/artifacts/diagnostics/sector-guard-trade-pool-compare.json"),
)


def _summarize(result: dict[str, Any], *, elapsed_s: float, symbols: list[str]) -> dict[str, Any]:
    events = list(result.get("risk_events") or [])
    guard_on_dates = sorted(
        {event["date"] for event in events if event.get("event") == "sector_guard_on"}
    )
    guard_off_dates = sorted(
        {event["date"] for event in events if event.get("event") == "sector_guard_off"}
    )
    return {
        "total_return": float(result["total_return"]),
        "max_drawdown": float(result["max_drawdown"]),
        "final_assets": float(result["final_assets"]),
        "total_trades": int(result["total_trades"]),
        "sell_trades": int(result.get("sell_trades", 0)),
        "guard_scope_mode": result.get("guard_scope_mode"),
        "sector_guard_index_mode": result.get("sector_guard_index_mode"),
        "guard_on_dates": guard_on_dates,
        "guard_off_dates": guard_off_dates,
        "guard_on_count": len(guard_on_dates),
        "first_guard_on": guard_on_dates[0] if guard_on_dates else None,
        "guard_event_count": sum(
            1 for e in events if str(e.get("event", "")).startswith("sector_")
        ),
        "elapsed_s": round(elapsed_s, 2),
        "symbol_count": len(symbols),
        "symbols": symbols,
    }


def _run_pool(symbols: tuple[str, ...], *, mode: str) -> dict[str, Any]:
    cfg: dict[str, Any] = {"sector_guard_index_mode": mode}
    if mode == "tech_only":
        cfg["sector_guard_min_symbols"] = 1
    elif mode == "dual_confirm":
        cfg["sector_guard_min_symbols"] = 2
    elif mode == "regime_basket":
        cfg["sector_guard_min_symbols"] = max(1, math.ceil(5 * 0.8))
    engine = qf.BacktestEngine(CAPITAL, cfg=cfg)
    t0 = time.perf_counter()
    with contextlib.redirect_stdout(io.StringIO()):
        result = engine.run(
            {code: SYMBOL_NAMES[code] for code in symbols},
            START,
            END,
            data_dir=str(MARKET_DATA_DIR),
            indicator_state="warm",
        )
    return _summarize(result, elapsed_s=time.perf_counter() - t0, symbols=list(symbols))


def _load_main_baseline() -> dict[str, Any] | None:
    for path in MAIN_BASELINE_CANDIDATES:
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        main = payload.get("main")
        if isinstance(main, dict) and all(pool in main for pool in POOLS):
            print(f"Loaded cached main baseline from {path}")
            return main
    return None


def _run_main_baseline() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in POOLS:
        summary = _run_pool(VALIDATION_UNIVERSES[name], mode="regime_basket")
        out[name] = {
            "total_return": summary["total_return"],
            "max_drawdown": summary["max_drawdown"],
            "total_trades": summary["total_trades"],
            "first_guard_on": summary["first_guard_on"],
            "guard_on_count": summary["guard_on_count"],
            "guard_event_count": summary["guard_event_count"],
        }
        print(
            f"main {name} return={summary['total_return']:.4f} "
            f"first_guard={summary['first_guard_on']}"
        )
    return out


def _compare(main_row: dict[str, Any], cand: dict[str, Any]) -> dict[str, Any]:
    return {
        "delta_return": cand["total_return"] - main_row["total_return"],
        "delta_mdd": cand["max_drawdown"] - main_row["max_drawdown"],
        "delta_trades": cand["total_trades"] - main_row["total_trades"],
        "guard_on_changed": (
            cand.get("first_guard_on") != main_row.get("first_guard_on")
            or cand.get("guard_on_count") != main_row.get("guard_on_count")
        ),
        "first_guard_on_main": main_row.get("first_guard_on"),
        "first_guard_on_cand": cand.get("first_guard_on"),
        "guard_on_count_main": main_row.get("guard_on_count"),
        "guard_on_count_cand": cand.get("guard_on_count"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--recompute-main",
        action="store_true",
        help="Ignore cached main baseline and re-run regime_basket",
    )
    args = parser.parse_args()

    main_baseline = None if args.recompute_main else _load_main_baseline()
    if main_baseline is None:
        print("Computing main baseline with regime_basket...")
        main_baseline = _run_main_baseline()

    probes: dict[str, dict[str, Any]] = {}
    comparisons: dict[str, dict[str, Any]] = {}
    for mode in MODES:
        probes[mode] = {}
        comparisons[mode] = {}
        for name in POOLS:
            print(f"Running {mode} / {name}...")
            summary = _run_pool(VALIDATION_UNIVERSES[name], mode=mode)
            probes[mode][name] = summary
            comparisons[mode][name] = _compare(main_baseline[name], summary)
            c = comparisons[mode][name]
            print(
                f"  return={summary['total_return']:.4f} "
                f"(d={c['delta_return']:+.4f}) "
                f"mdd={summary['max_drawdown']:.4f} "
                f"trades={summary['total_trades']} "
                f"guard_on={summary['guard_on_count']} "
                f"first={summary['first_guard_on']}"
            )

    payload = {
        "main": main_baseline,
        "probes": probes,
        "comparison": comparisons,
        "meta": {
            "branch_intent": (
                "Experiment C: sector-guard shock/recovery from fixed indices "
                "000682 (tech_only) or 000300 AND 000682 (dual_confirm); "
                "market-regime thermometer stays on regime_symbols."
            ),
            "window": f"{START}..{END}",
            "capital": CAPITAL,
            "engine": "BacktestEngine",
            "indicator_state": "warm",
            "ab5_unchanged": True,
            "regime_symbols_unused_for_guard": True,
            "index_files": {"broad": "000300", "technology": "000682"},
            "mapping": {
                "tech_only": (
                    "equal_return=000682 daily return; breadth=binary MA "
                    "membership of 000682 (multi-name breadth N/A)."
                ),
                "dual_confirm": (
                    "shock/recovery require BOTH 000300 and 000682 to "
                    "independently meet return+MA conditions (AND)."
                ),
            },
            "recommendation": "discard",
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
