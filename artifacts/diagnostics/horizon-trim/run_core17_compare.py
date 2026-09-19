"""Same-condition Core17 compare for B0 and fixed two-horizon structures E1–E3."""

from __future__ import annotations

import json
import math
import time
from dataclasses import replace
from pathlib import Path

from quantfusion.config.paths import MARKET_DATA_DIR, REGIME_DATA_DIR
from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.config.universe import ORDERED_SYMBOLS, SYMBOL_NAMES
from quantfusion.engine.replay import ProductionReplayEngine

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
START = "2025-04-01"
END = "2026-07-20"
CAPITAL = 2_000_000.0

STRUCTURES = {
    "B0": {
        "keep": ("fast", "base", "slow"),
        "horizons": ((3, 5, 10), (5, 10, 20), (5, 20, 60)),
        "candidates": ((10, 20, 40), (10, 20, 40), (10, 40, 80)),
    },
    "E1": {
        "keep": ("base", "slow"),
        "horizons": ((5, 10, 20), (5, 20, 60)),
        "candidates": ((10, 20, 40), (10, 40, 80)),
    },
    "E2": {
        "keep": ("fast", "slow"),
        "horizons": ((3, 5, 10), (5, 20, 60)),
        "candidates": ((10, 20, 40), (10, 40, 80)),
    },
    "E3": {
        "keep": ("fast", "base"),
        "horizons": ((3, 5, 10), (5, 10, 20)),
        "candidates": ((10, 20, 40), (10, 20, 40)),
    },
}


def _metrics(result: dict) -> dict:
    trades = list(result.get("trades") or [])
    fees = sum(float(t.commission) + float(t.stamp_duty_cost) for t in trades)
    gross = sum(abs(float(t.gross_value)) for t in trades)
    equity = result["equity_curve"]
    avg_assets = float(equity["assets"].astype(float).mean())
    active_days = len({t.date for t in trades})
    final_assets = float(result["final_assets"])
    mdd = float(result["max_drawdown"])
    return {
        "final_assets": final_assets,
        "wealth_multiple": final_assets / CAPITAL,
        "total_return": float(result["total_return"]),
        "max_drawdown": mdd,
        "mdd_abs": abs(mdd),
        "date_symbol_side_buckets": int(result["date_symbol_side_count"]),
        "active_operation_days": active_days,
        "sleeve_fills": int(result.get("sleeve_fill_count", len(trades))),
        "trade_records": len(trades),
        "commission_plus_stamp_duty": fees,
        "turnover_over_average_equity": (gross / avg_assets) if avg_assets > 0 else 0.0,
        "sleeve_names": [s.get("sleeve_name") for s in result.get("sleeve_summaries", [])],
        "allocation_horizons": result.get("effective_portfolio_policy", {}).get(
            "allocation_horizons"
        ),
        "persistent_risk_lock": bool(result.get("persistent_risk_lock")),
        "terminal_risk_lock": bool(result.get("terminal_risk_lock")),
        "cycle_lock_count": int(result.get("cycle_lock_count", 0)),
    }


def _gates(cand: dict, base: dict) -> dict:
    w_ratio = cand["wealth_multiple"] / base["wealth_multiple"]
    b_c = cand["date_symbol_side_buckets"]
    b_0 = base["date_symbol_side_buckets"]
    d_c = cand["active_operation_days"]
    d_0 = base["active_operation_days"]
    m_ok = cand["mdd_abs"] <= base["mdd_abs"] + 0.005 + 1e-15
    wealth_route = (
        w_ratio >= 1.03 - 1e-15
        and b_c <= b_0
        and d_c <= d_0
        and m_ok
    )
    burden_route = (
        w_ratio >= 0.99 - 1e-15
        and b_c <= math.floor(0.90 * b_0)
        and d_c <= d_0
        and m_ok
    )
    return {
        "W_ratio": w_ratio,
        "M_delta": cand["mdd_abs"] - base["mdd_abs"],
        "B_ratio": (b_c / b_0) if b_0 else float("nan"),
        "B_c": b_c,
        "B_0": b_0,
        "D_c": d_c,
        "D_0": d_0,
        "floor_0_90_B0": math.floor(0.90 * b_0),
        "mdd_ok": m_ok,
        "wealth_route": wealth_route,
        "burden_route": burden_route,
        "any_route": wealth_route or burden_route,
    }


def run_one(label: str, spec: dict) -> dict:
    policy = replace(
        PortfolioPolicy(),
        allocation_horizons=spec["horizons"],
        candidate_horizons=spec["candidates"],
    )
    engine = ProductionReplayEngine(CAPITAL, policy=policy)
    symbols = {code: SYMBOL_NAMES[code] for code in ORDERED_SYMBOLS}
    t0 = time.time()
    result = engine.run(
        symbols,
        START,
        END,
        data_dir=str(MARKET_DATA_DIR),
        regime_data_dir=str(REGIME_DATA_DIR),
        indicator_state="warm",
    )
    elapsed = time.time() - t0
    metrics = _metrics(result)
    metrics["label"] = label
    metrics["keep"] = list(spec["keep"])
    metrics["elapsed_sec"] = elapsed
    # Persist a slim trade digest for audit (not full originals unless needed).
    digest = {
        **metrics,
        "source": {
            "start": START,
            "end": END,
            "capital": CAPITAL,
            "engine": "ProductionReplayEngine",
            "indicator_state": "warm",
            "symbols": list(ORDERED_SYMBOLS),
        },
    }
    (OUT / f"{label.lower()}-core17.json").write_text(
        json.dumps(digest, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(
        label,
        f"W={metrics['wealth_multiple']:.6f}",
        f"M={metrics['mdd_abs']:.6f}",
        f"B={metrics['date_symbol_side_buckets']}",
        f"D={metrics['active_operation_days']}",
        f"fills={metrics['sleeve_fills']}",
        f"sleeves={metrics['sleeve_names']}",
        f"t={elapsed:.1f}s",
        flush=True,
    )
    return metrics


def main() -> int:
    rows = {}
    for label in ("B0", "E1", "E2", "E3"):
        rows[label] = run_one(label, STRUCTURES[label])
    base = rows["B0"]
    compare = {"B0": base, "candidates": {}}
    for label in ("E1", "E2", "E3"):
        gates = _gates(rows[label], base)
        compare["candidates"][label] = {**rows[label], "gates": gates}
        print(
            f"{label} gates:",
            f"W_ratio={gates['W_ratio']:.4f}",
            f"M_delta={gates['M_delta']:.6f}",
            f"B={gates['B_c']}/{gates['B_0']}",
            f"D={gates['D_c']}/{gates['D_0']}",
            f"wealth={gates['wealth_route']}",
            f"burden={gates['burden_route']}",
            flush=True,
        )
    (OUT / "core17-compare.json").write_text(
        json.dumps(compare, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
