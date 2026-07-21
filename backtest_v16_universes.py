#!/usr/bin/env python3
"""Run the five requested v16 universes under identical assumptions."""

from __future__ import annotations

import contextlib
import io
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import codex_quant_fusion_v16 as v16


DATA_DIR = Path(__file__).resolve().parent / "market_data_qfq_22_20260720"
START_DATE = "2025-04-01"
END_DATES = ("2026-06-30", "2026-07-20")
INDICATOR_STATES = ("cold", "warm")
INITIAL_CAPITAL = 2_000_000.0

NAMES = {
    "300308": "中际旭创",
    "300502": "新易盛",
    "300394": "天孚通信",
    "688008": "澜起科技",
    "603986": "兆易创新",
    "002409": "雅克科技",
    "688072": "拓荆科技",
    "688300": "联瑞新材",
    "300054": "鼎龙股份",
    "688205": "德科立",
    "920045": "蘅东光",
    "300776": "帝尔激光",
    "688535": "华海诚科",
    "688249": "晶合集成",
    "688347": "华虹宏力",
    "300666": "江丰电子",
    "600206": "有研新材",
    "688409": "富创精密",
    "688361": "中科飞测",
    "300604": "长川科技",
    "688120": "华海清科",
    "688082": "盛美上海",
}

UNIVERSES = {
    "1_symbol": ("300308",),
    "3_symbols": ("300308", "300502", "300394"),
    "5_symbols": ("300308", "300502", "300394", "688008", "603986"),
    "13_symbols": (
        "300308",
        "300502",
        "300394",
        "688008",
        "603986",
        "002409",
        "688072",
        "688300",
        "300054",
        "688205",
        "920045",
        "300776",
        "688535",
    ),
    "22_symbols": tuple(NAMES),
}


def _run(task: tuple[str, tuple[str, ...], str, str]) -> dict[str, Any]:
    """Run one isolated scenario and return compact auditable metrics."""
    universe, codes, state, end_date = task
    symbols = {code: NAMES[code] for code in codes}
    engine = v16.BacktestEngine(INITIAL_CAPITAL)
    with contextlib.redirect_stdout(io.StringIO()):
        result = engine.run(
            symbols,
            START_DATE,
            end_date,
            data_dir=str(DATA_DIR),
            indicator_state=state,
        )
    lock_events = [
        event
        for event in result.get("risk_events", [])
        if "lock" in str(event.get("event", ""))
    ]
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
        "locked_sleeves": list(result.get("locked_sleeves", [])),
        "lock_events": lock_events,
        "open_positions": int(result["open_positions"]),
        "allocation_mode": str(result["allocation_mode"]),
        "v16_policy": result["v16_policy"],
    }


def main() -> int:
    """Run scenarios in parallel and write a stable JSON result artifact."""
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
        "engine": "Codex Quant Fusion v16",
        "data_directory": DATA_DIR.name,
        "data_adjustment": "qfq",
        "data_provider": "Eastmoney push2his",
        "initial_capital": INITIAL_CAPITAL,
        "results": results,
    }
    output = Path("v16_universe_backtest_20260720.json")
    output.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for item in results:
        print(
            item["universe"],
            item["indicator_state"],
            item["end_date"],
            f"return={item['total_return']:.6%}",
            f"max_drawdown={item['max_drawdown']:.6%}",
            f"final_assets={item['final_assets']:.2f}",
            f"guard={item['sector_guard_active']}",
            f"lock={item['persistent_risk_lock']}",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
