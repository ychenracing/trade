#!/usr/bin/env python3
"""Stress universe composition, order, omission, and incremental additions."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import random
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import quant_fusion as qf
from backtest_universes import NAMES


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "market_data"
START_DATE = "2025-04-01"
END_DATE = "2026-07-20"
INITIAL_CAPITAL = 2_000_000.0
ORDERED_CODES = tuple(NAMES)


def _metrics(codes: tuple[str, ...]) -> dict[str, Any]:
    with contextlib.redirect_stdout(io.StringIO()):
        result = qf.BacktestEngine(INITIAL_CAPITAL).run(
            {code: NAMES[code] for code in codes},
            START_DATE,
            END_DATE,
            data_dir=str(DATA_DIR),
            indicator_state="warm",
        )
    return {
        "symbol_count": len(codes),
        "symbols": list(codes),
        "total_return": float(result["total_return"]),
        "max_drawdown": float(result["max_drawdown"]),
        "sharpe": float(result["sharpe"]),
        "calmar": float(result["calmar"]),
        "total_trades": int(result["total_trades"]),
        "max_concurrent_symbols": int(result["max_concurrent_symbols"]),
        "terminal_risk_lock": bool(result["terminal_risk_lock"]),
    }


def _run(count: int) -> dict[str, Any]:
    """Compatibility helper: run one warm-state ordered prefix."""
    return _metrics(ORDERED_CODES[:count])


def _run_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    codes = tuple(str(code) for code in scenario["symbols"])
    return {**scenario, **_metrics(codes)}


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    location = (len(ordered) - 1) * probability
    lower = int(location)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = location - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [float(item["total_return"]) for item in results]
    drawdowns = [float(item["max_drawdown"]) for item in results]
    trades = [float(item["total_trades"]) for item in results]
    return {
        "scenario_count": len(results),
        "return_median": _quantile(returns, 0.50),
        "return_p10": _quantile(returns, 0.10),
        "return_worst": min(returns) if returns else 0.0,
        "drawdown_median": _quantile(drawdowns, 0.50),
        "drawdown_p10": _quantile(drawdowns, 0.10),
        "drawdown_worst": min(drawdowns) if drawdowns else 0.0,
        "trades_median": _quantile(trades, 0.50),
        "trades_p90": _quantile(trades, 0.90),
        "trades_worst": max(trades) if trades else 0.0,
    }


def _scenarios(
    *, random_samples: int, permutation_samples: int, seed: int
) -> list[dict[str, Any]]:
    if random_samples < 1 or permutation_samples < 1:
        raise ValueError("sample counts must be positive")
    smallest_capacity = min(
        math.comb(len(ORDERED_CODES), size) for size in (3, 5, 8, 12, 16)
    )
    if random_samples > smallest_capacity:
        raise ValueError(
            f"random_samples exceeds unique subset capacity {smallest_capacity}"
        )
    if permutation_samples > math.factorial(len(ORDERED_CODES)):
        raise ValueError("permutation_samples exceeds unique ordering capacity")
    rng = random.Random(seed)
    scenarios: list[dict[str, Any]] = []
    for count in range(1, len(ORDERED_CODES) + 1):
        scenarios.append(
            {
                "scenario_id": f"prefix-{count:02d}",
                "scenario_type": "prefix",
                "symbols": list(ORDERED_CODES[:count]),
            }
        )
    for omitted in ORDERED_CODES:
        scenarios.append(
            {
                "scenario_id": f"leave-one-out-{omitted}",
                "scenario_type": "leave_one_out",
                "omitted_symbol": omitted,
                "symbols": [code for code in ORDERED_CODES if code != omitted],
            }
        )
    for base_size in (5, 9, 13):
        base = ORDERED_CODES[:base_size]
        for added in ORDERED_CODES[base_size:]:
            scenarios.append(
                {
                    "scenario_id": f"add-one-{base_size:02d}-{added}",
                    "scenario_type": "add_one",
                    "base_size": base_size,
                    "added_symbol": added,
                    "symbols": [*base, added],
                }
            )
    for size in (3, 5, 8, 12, 16):
        seen: set[tuple[str, ...]] = set()
        while len(seen) < random_samples:
            subset = tuple(sorted(rng.sample(ORDERED_CODES, size)))
            seen.add(subset)
        for index, subset in enumerate(sorted(seen), start=1):
            scenarios.append(
                {
                    "scenario_id": f"random-{size:02d}-{index:03d}",
                    "scenario_type": "random_subset",
                    "sample_size": size,
                    "symbols": list(subset),
                }
            )
    permutations: set[tuple[str, ...]] = set()
    while len(permutations) < permutation_samples:
        sample = list(ORDERED_CODES)
        rng.shuffle(sample)
        permutations.add(tuple(sample))
    for index, ordering in enumerate(sorted(permutations), start=1):
        scenarios.append(
            {
                "scenario_id": f"permutation-{index:03d}",
                "scenario_type": "permutation",
                "symbols": list(ordering),
            }
        )
    return scenarios


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.stem}-", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary_name).replace(path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--random-samples", type=int, default=50)
    parser.add_argument("--permutation-samples", type=int, default=50)
    parser.add_argument("--seed", type=int, default=20260807)
    return parser


def main() -> int:
    """Run every scenario and atomically publish complete audit artifacts."""
    args = build_argument_parser().parse_args()
    if args.workers < 1 or args.random_samples < 1 or args.permutation_samples < 1:
        raise ValueError("workers and sample counts must be positive")
    scenarios = _scenarios(
        random_samples=args.random_samples,
        permutation_samples=args.permutation_samples,
        seed=args.seed,
    )
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        results = list(executor.map(_run_scenario, scenarios))
    results.sort(key=lambda item: item["scenario_id"])
    prefixes = sorted(
        (item for item in results if item["scenario_type"] == "prefix"),
        key=lambda item: item["symbol_count"],
    )
    wealth = [1.0 + item["total_return"] for item in prefixes]
    adjacent = [
        {
            "from_count": left["symbol_count"],
            "to_count": right["symbol_count"],
            "wealth_change": (
                (1.0 + right["total_return"]) / (1.0 + left["total_return"]) - 1.0
            ),
        }
        for left, right in zip(prefixes, prefixes[1:], strict=False)
    ]
    common = {
        "engine": "Quant Fusion",
        "portfolio_policy": qf.PortfolioPolicy().as_dict(),
        "data_directory": DATA_DIR.name,
        "start_date": START_DATE,
        "end_date": END_DATE,
        "indicator_state": "warm",
        "seed": args.seed,
    }
    prefix_artifact = {
        **common,
        "ordering": list(ORDERED_CODES),
        "minimum_to_maximum_wealth_ratio": min(wealth) / max(wealth),
        "worst_adjacent_transition": min(
            adjacent, key=lambda item: item["wealth_change"]
        ),
        "summary": _summary(prefixes),
        "results": prefixes,
    }
    by_type = {
        scenario_type: _summary(
            [item for item in results if item["scenario_type"] == scenario_type]
        )
        for scenario_type in sorted({item["scenario_type"] for item in results})
    }
    universe_artifact = {
        **common,
        "random_samples_per_size": args.random_samples,
        "permutation_samples": args.permutation_samples,
        "summary": {"all": _summary(results), "by_type": by_type},
        "results": results,
    }
    _atomic_json(ROOT / "prefix_stress.json", prefix_artifact)
    _atomic_json(ROOT / "universe_stress.json", universe_artifact)
    print(json.dumps(universe_artifact["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
