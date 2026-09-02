"""Reproduce the mapped nine-symbol Cambricon regression."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from concurrent.futures import ProcessPoolExecutor
from typing import Any

from quantfusion.application import engine_api as qf
from quantfusion.config.paths import (
    MARKET_DATA_DIR,
    PROJECT_ROOT,
    VALIDATION_ARTIFACT_DIR,
)
from quantfusion.config.profiles import get_symbol_classification


DATA_DIR = MARKET_DATA_DIR
ROOT = PROJECT_ROOT
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
        result = qf.BacktestEngine(INITIAL_CAPITAL).run(
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
        "calmar": float(result["calmar"]),
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
        "engine": "Quant Fusion",
        "data_directory": "data/market",
        "data_adjustment": "qfq",
        "data_provider": "Eastmoney push2his",
        "initial_capital": INITIAL_CAPITAL,
        "symbols": SYMBOLS,
        "cambricon_mapping": {
            "classification": get_symbol_classification("688256", "unknown"),
            "risk_group": qf.get_symbol_group("688256", "unknown"),
            "parameter_profile": qf.get_symbol_profile("688256", "unknown"),
        },
        "results": results,
    }
    output = VALIDATION_ARTIFACT_DIR / "cambricon_universe_backtest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
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
    argparse.ArgumentParser(description=__doc__).parse_args()
    raise SystemExit(main())
