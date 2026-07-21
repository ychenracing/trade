#!/usr/bin/env python3
"""Measure v17 stability while adding tradable symbols one at a time."""

from __future__ import annotations

import contextlib
import io
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import codex_quant_fusion_v17 as v17
from backtest_v16_universes import NAMES


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "market_data_qfq_22_20260720"
START_DATE = "2025-04-01"
END_DATE = "2026-07-20"
INITIAL_CAPITAL = 2_000_000.0
ORDERED_CODES = tuple(NAMES)


def _run(count: int) -> dict[str, Any]:
    """Run one warm-state prefix of the user-supplied symbol order."""
    codes = ORDERED_CODES[:count]
    with contextlib.redirect_stdout(io.StringIO()):
        result = v17.BacktestEngine(INITIAL_CAPITAL).run(
            {code: NAMES[code] for code in codes},
            START_DATE,
            END_DATE,
            data_dir=str(DATA_DIR),
            indicator_state="warm",
        )
    return {
        "symbol_count": count,
        "symbols": list(codes),
        "total_return": float(result["total_return"]),
        "max_drawdown": float(result["max_drawdown"]),
        "sharpe": float(result["sharpe"]),
        "total_trades": int(result["total_trades"]),
        "terminal_risk_lock": bool(result["terminal_risk_lock"]),
    }


def main() -> int:
    """Run all prefixes and persist a deterministic audit artifact."""
    with ProcessPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(_run, range(1, len(ORDERED_CODES) + 1)))
    results.sort(key=lambda item: item["symbol_count"])
    wealth = [1.0 + item["total_return"] for item in results]
    adjacent_drops = [
        {
            "from_count": left["symbol_count"],
            "to_count": right["symbol_count"],
            "wealth_change": (
                (1.0 + right["total_return"]) / (1.0 + left["total_return"]) - 1.0
            ),
        }
        for left, right in zip(results, results[1:])
    ]
    worst_transition = min(adjacent_drops, key=lambda item: item["wealth_change"])
    artifact = {
        "engine": "Codex Quant Fusion v17",
        "v17_policy": v17.V17Policy().as_dict(),
        "data_directory": DATA_DIR.name,
        "start_date": START_DATE,
        "end_date": END_DATE,
        "indicator_state": "warm",
        "ordering": list(ORDERED_CODES),
        "minimum_to_maximum_wealth_ratio": min(wealth) / max(wealth),
        "worst_adjacent_transition": worst_transition,
        "results": results,
    }
    output = ROOT / "v17_prefix_stress_20260720.json"
    output.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for item in results:
        print(
            f"count={item['symbol_count']:2d}",
            f"return={item['total_return']:.6%}",
            f"max_drawdown={item['max_drawdown']:.6%}",
        )
    print(
        "minimum_to_maximum_wealth_ratio=",
        f"{artifact['minimum_to_maximum_wealth_ratio']:.6%}",
        sep="",
    )
    print(
        "worst_adjacent_transition=",
        f"{worst_transition['from_count']}->{worst_transition['to_count']} ",
        f"{worst_transition['wealth_change']:.6%}",
        sep="",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
