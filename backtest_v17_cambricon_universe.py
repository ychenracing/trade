#!/usr/bin/env python3
"""Reproduce the mapped nine-symbol Cambricon regression for version 17."""

from __future__ import annotations

import contextlib
import io
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import codex_quant_fusion_v17 as v17


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "market_data_qfq_9_cambricon_20260720"
START_DATE = "2025-04-01"
END_DATES = ("2026-06-30", "2026-07-20")
INDICATOR_STATES = ("cold", "warm")
INITIAL_CAPITAL = 2_000_000.0
SYMBOLS = {
    "300308": "中际旭创",
    "688256": "寒武纪",
    "300502": "新易盛",
    "300394": "天孚通信",
    "603986": "兆易创新",
    "688008": "澜起科技",
    "688347": "华虹宏力",
    "300054": "鼎龙股份",
    "688300": "联瑞新材",
}


def _run(task: tuple[str, str]) -> dict[str, Any]:
    """Run one indicator-state and end-date combination."""
    indicator_state, end_date = task
    with contextlib.redirect_stdout(io.StringIO()):
        result = v17.BacktestEngine(INITIAL_CAPITAL).run(
            SYMBOLS,
            START_DATE,
            end_date,
            data_dir=str(DATA_DIR),
            indicator_state=indicator_state,
        )
    guard_on_dates = sorted(
        {
            event["date"]
            for event in result["risk_events"]
            if event.get("event") == "sector_guard_on"
        }
    )
    return {
        "indicator_state": indicator_state,
        "start_date": START_DATE,
        "end_date": end_date,
        "final_assets": float(result["final_assets"]),
        "total_return": float(result["total_return"]),
        "annual_return": float(result["annual_return"]),
        "max_drawdown": float(result["max_drawdown"]),
        "sharpe": float(result["sharpe"]),
        "total_trades": int(result["total_trades"]),
        "max_concurrent_symbols": int(result["max_concurrent_symbols"]),
        "terminal_risk_lock": bool(result["terminal_risk_lock"]),
        "cycle_lock_count": int(result["cycle_lock_count"]),
        "guard_on_dates": guard_on_dates,
        "cambricon_parameter_route": result["parameter_routes"]["688256"],
    }


def main() -> int:
    """Run all four scenarios and write a deterministic JSON artifact."""
    tasks = [
        (indicator_state, end_date)
        for indicator_state in INDICATOR_STATES
        for end_date in END_DATES
    ]
    with ProcessPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(_run, tasks))
    results.sort(key=lambda item: (item["indicator_state"], item["end_date"]))
    artifact = {
        "engine": "Codex Quant Fusion v17",
        "data_directory": DATA_DIR.name,
        "data_adjustment": "qfq",
        "data_provider": "Eastmoney push2his",
        "initial_capital": INITIAL_CAPITAL,
        "symbols": SYMBOLS,
        "cambricon_mapping": {
            "classification": v17.BacktestEngine._KNOWN_CLASSIFICATION["688256"],
            "risk_group": v17.BacktestEngine._SYMBOL_GROUP["688256"],
            "parameter_profile": v17.BacktestEngine._SYMBOL_PROFILE["688256"],
        },
        "results": results,
    }
    output = ROOT / "v17_cambricon_universe_backtest_20260720.json"
    output.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for item in results:
        print(
            item["indicator_state"],
            item["end_date"],
            f"return={item['total_return']:.6%}",
            f"max_drawdown={item['max_drawdown']:.6%}",
            f"route={item['cambricon_parameter_route']}",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
