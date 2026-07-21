#!/usr/bin/env python3
"""Validate v17 across all requested universe sizes and indicator states."""

from __future__ import annotations

import contextlib
import io
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import codex_quant_fusion_v17 as v17
from backtest_v16_universes import NAMES, UNIVERSES


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "market_data_qfq_22_20260720"
START_DATE = "2025-04-01"
END_DATES = ("2026-06-30", "2026-07-20")
INDICATOR_STATES = ("cold", "warm")
INITIAL_CAPITAL = 2_000_000.0


def _run(task: tuple[str, tuple[str, ...], str, str]) -> dict[str, Any]:
    """Run one scenario and retain metrics needed for cross-universe review."""
    universe, codes, state, end_date = task
    engine = v17.BacktestEngine(INITIAL_CAPITAL)
    with contextlib.redirect_stdout(io.StringIO()):
        result = engine.run(
            {code: NAMES[code] for code in codes},
            START_DATE,
            end_date,
            data_dir=str(DATA_DIR),
            indicator_state=state,
        )
    return {
        "universe": universe,
        "symbol_count": len(codes),
        "symbols": list(codes),
        "indicator_state": state,
        "start_date": START_DATE,
        "end_date": end_date,
        "initial_capital": INITIAL_CAPITAL,
        "final_assets": float(result["final_assets"]),
        "total_return": float(result["total_return"]),
        "annual_return": float(result["annual_return"]),
        "max_drawdown": float(result["max_drawdown"]),
        "sharpe": float(result["sharpe"]),
        "total_trades": int(result["total_trades"]),
        "sector_guard_active": bool(result["sector_guard_active"]),
        "persistent_risk_lock": bool(result["persistent_risk_lock"]),
        "terminal_risk_lock": bool(result["terminal_risk_lock"]),
        "cycle_lock_count": int(result["cycle_lock_count"]),
        "locked_sleeves": list(result.get("locked_sleeves", [])),
        "open_positions": int(result["open_positions"]),
        "allocation_mode": str(result["allocation_mode"]),
        "v17_policy": result["v17_policy"],
        "effective_v17_policy": result["effective_v17_policy"],
        "sleeve_summaries": result["sleeve_summaries"],
    }


def main() -> int:
    """Run scenarios in parallel and write one deterministic result artifact."""
    tasks = [
        (universe, codes, state, end_date)
        for universe, codes in UNIVERSES.items()
        for state in INDICATOR_STATES
        for end_date in END_DATES
    ]
    with ProcessPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(_run, tasks))
    order = {name: index for index, name in enumerate(UNIVERSES)}
    results.sort(
        key=lambda item: (
            order[item["universe"]],
            item["indicator_state"],
            item["end_date"],
        )
    )
    artifact = {
        "engine": "Codex Quant Fusion v17",
        "data_directory": DATA_DIR.name,
        "data_adjustment": "qfq",
        "data_provider": "Eastmoney push2his",
        "initial_capital": INITIAL_CAPITAL,
        "results": results,
    }
    Path("v17_universe_backtest_20260720.json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for item in results:
        print(
            item["universe"],
            item["indicator_state"],
            item["end_date"],
            f"return={item['total_return']:.6%}",
            f"max_drawdown={item['max_drawdown']:.6%}",
            f"cycles={item['cycle_lock_count']}",
            f"terminal={item['terminal_risk_lock']}",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
