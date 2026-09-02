"""Validate strategy across all requested universe sizes and indicator states."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from concurrent.futures import ProcessPoolExecutor
from typing import Any

from quantfusion.application import engine_api as qf
from quantfusion.config.universe import (
    SYMBOL_NAMES as NAMES,
    VALIDATION_UNIVERSES as UNIVERSES,
)
from quantfusion.config.paths import (
    MARKET_DATA_DIR,
    PROJECT_ROOT,
    VALIDATION_ARTIFACT_DIR,
)


DATA_DIR = MARKET_DATA_DIR
ROOT = PROJECT_ROOT
START_DATE = "2025-04-01"
END_DATES = ("2026-06-30", "2026-07-20")
INDICATOR_STATES = ("cold", "warm")
INITIAL_CAPITAL = 2_000_000.0

def _run(task: tuple[str, tuple[str, ...], str, str]) -> dict[str, Any]:
    """Run one scenario and retain metrics needed for cross-universe review."""
    universe, codes, state, end_date = task
    engine = qf.BacktestEngine(INITIAL_CAPITAL)
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
        "calmar": float(result["calmar"]),
        "total_trades": int(result["total_trades"]),
        "sell_trades": int(result["sell_trades"]),
        "sleeve_fill_count": int(result["sleeve_fill_count"]),
        "sleeve_sell_fill_count": int(result["sleeve_sell_fill_count"]),
        "date_symbol_side_count": int(result["date_symbol_side_count"]),
        "date_symbol_sell_side_count": int(
            result["date_symbol_sell_side_count"]
        ),
        "sector_guard_active": bool(result["sector_guard_active"]),
        "persistent_risk_lock": bool(result["persistent_risk_lock"]),
        "terminal_risk_lock": bool(result["terminal_risk_lock"]),
        "cycle_lock_count": int(result["cycle_lock_count"]),
        "locked_sleeves": list(result.get("locked_sleeves", [])),
        "open_positions": int(result["open_positions"]),
        "max_concurrent_symbols": int(result["max_concurrent_symbols"]),
        "portfolio_max_positions": int(result["portfolio_max_positions"]),
        "portfolio_cash_model": str(result["portfolio_cash_model"]),
        "allocation_mode": str(result["allocation_mode"]),
        "portfolio_policy": result["portfolio_policy"],
        "effective_portfolio_policy": result["effective_portfolio_policy"],
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
        "engine": "Quant Fusion",
        "data_directory": "data/market",
        "data_adjustment": "qfq",
        "data_provider": "Eastmoney push2his",
        "initial_capital": INITIAL_CAPITAL,
        "results": results,
    }
    output = VALIDATION_ARTIFACT_DIR / "universe_backtest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
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
    argparse.ArgumentParser(description=__doc__).parse_args()
    raise SystemExit(main())
