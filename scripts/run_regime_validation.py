"""Reproduce causal weak-market and frozen bull-market validation.

The final blind-pool seed is part of the public protocol.  It was opened only
after the strategy constants and tests were frozen; rerunning this program is
reproduction, not parameter selection.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import random
import statistics
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Iterable

from quantfusion.application import engine_api as qf
from scripts.backtest_universes import NAMES, UNIVERSES
from quantfusion.config.paths import (
    BACKTEST_GOLDEN_METRICS,
    MARKET_DATA_DIR,
    PROJECT_ROOT,
    REGIME_DATA_DIR,
    VALIDATION_ARTIFACT_DIR,
)
from quantfusion.engine.replay import RegimeAdaptiveBacktestEngine


MARKET_DATA = MARKET_DATA_DIR
ROOT = PROJECT_ROOT
OUTPUT = VALIDATION_ARTIFACT_DIR / "regime_validation_results.json"
INITIAL_CAPITAL = 2_000_000.0
DEVELOPMENT_SEED = 20260802
FINAL_BLIND_SEED = 20260805
AI_SYMBOLS = (
    "002409", "300054", "300308", "300394", "300502", "300604",
    "300666", "603986", "688008", "688072", "688082", "688120",
    "688256", "688300", "688347", "688361",
)


def _golden_bull(name: str) -> tuple[float, float, int]:
    """Read the canonical 2026-07-20 bull-market metrics for one universe."""
    payload = json.loads(BACKTEST_GOLDEN_METRICS.read_text(encoding="utf-8"))
    item = payload[str(len(UNIVERSES[name]))]
    return (
        float(item["total_return"]),
        float(item["max_drawdown"]),
        int(item["total_trades"]),
    )


def deterministic_pools() -> list[tuple[str, tuple[str, ...]]]:
    pools: list[tuple[str, tuple[str, ...]]] = []
    for size in (1, 3, 5, 8, 12):
        head = AI_SYMBOLS[:size]
        tail = AI_SYMBOLS[-size:]
        if size == 1:
            spread = (AI_SYMBOLS[len(AI_SYMBOLS) // 2],)
        else:
            indexes = [
                int(i * (len(AI_SYMBOLS) - 1) / (size - 1))
                for i in range(size)
            ]
            spread = tuple(AI_SYMBOLS[index] for index in indexes)
        pools.extend(
            [(f"det_{size}_head", head), (f"det_{size}_spread", spread), (f"det_{size}_tail", tail)]
        )
    pools.append(("det_16_all", AI_SYMBOLS))
    return pools


def random_pools(
    *, seed: int, sizes: tuple[int, ...], per_size: int, prefix: str
) -> list[tuple[str, tuple[str, ...]]]:
    rng = random.Random(seed)
    pools: list[tuple[str, tuple[str, ...]]] = []
    for size in sizes:
        seen: set[tuple[str, ...]] = set()
        while len(seen) < per_size:
            codes = tuple(sorted(rng.sample(AI_SYMBOLS, size)))
            if codes in seen:
                continue
            seen.add(codes)
            pools.append((f"{prefix}_{size}_{len(seen) - 1}", codes))
    return pools


def development_pools() -> list[tuple[str, tuple[str, ...]]]:
    return random_pools(
        seed=DEVELOPMENT_SEED,
        sizes=(1, 3, 5, 8, 12),
        per_size=10,
        prefix="development",
    )


def final_blind_pools() -> list[tuple[str, tuple[str, ...]]]:
    pools = random_pools(
        seed=FINAL_BLIND_SEED,
        sizes=(2, 4, 6, 10, 14),
        per_size=5,
        prefix="blind",
    )
    pools.append(("blind_16_control", AI_SYMBOLS))
    return pools


def _run_pool(task: tuple[str, tuple[str, ...], str, str, str]) -> dict[str, Any]:
    name, codes, route, start_date, end_date = task
    symbols = {code: code for code in codes}
    with contextlib.redirect_stdout(io.StringIO()):
        if route == "adaptive":
            result = RegimeAdaptiveBacktestEngine(INITIAL_CAPITAL).run(
                symbols,
                start_date,
                end_date,
                data_dir=str(REGIME_DATA_DIR),
                indicator_state="warm",
            )
        else:
            result = qf.BacktestEngine(INITIAL_CAPITAL).run(
                symbols,
                start_date,
                end_date,
                data_dir=str(REGIME_DATA_DIR),
                indicator_state="warm",
            )
    return {
        "name": name,
        "symbols": list(codes),
        "total_return": float(result["total_return"]),
        "max_drawdown": float(result["max_drawdown"]),
        "total_trades": int(result["total_trades"]),
        "deployment_policy": str(result.get("deployment_policy", "original")),
        "selected_symbols": list(result.get("selected_symbols", codes)),
        "unavailable_symbols": list(result.get("unavailable_symbols", [])),
    }


def run_distribution(
    pools: Iterable[tuple[str, tuple[str, ...]]],
    *,
    route: str,
    start_date: str,
    end_date: str,
    workers: int,
) -> list[dict[str, Any]]:
    tasks = [(name, codes, route, start_date, end_date) for name, codes in pools]
    with ProcessPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(_run_pool, tasks))


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [float(row["total_return"]) for row in rows]
    drawdowns = [abs(float(row["max_drawdown"])) for row in rows]
    trades = [int(row["total_trades"]) for row in rows]
    return {
        "count": len(rows),
        "profitable_ratio": sum(value > 0 for value in returns) / len(rows),
        "nonloss_ratio": sum(value >= 0 for value in returns) / len(rows),
        "median_return": statistics.median(returns),
        "worst_return": min(returns),
        "best_return": max(returns),
        "median_drawdown": statistics.median(drawdowns),
        "worst_drawdown": max(drawdowns),
        "median_trades": statistics.median(trades),
    }


def _run_bull(task: tuple[str, tuple[str, ...]]) -> dict[str, Any]:
    name, codes = task
    with contextlib.redirect_stdout(io.StringIO()):
        result = RegimeAdaptiveBacktestEngine(INITIAL_CAPITAL).run(
            {code: NAMES[code] for code in codes},
            "2025-04-01",
            "2026-07-20",
            data_dir=str(MARKET_DATA),
            regime_data_dir=str(REGIME_DATA_DIR),
            indicator_state="warm",
        )
    expected = _golden_bull(name)
    actual = (
        float(result["total_return"]),
        float(result["max_drawdown"]),
        int(result["total_trades"]),
    )
    frozen_match = (
        math.isclose(actual[0], expected[0], rel_tol=1e-9, abs_tol=1e-12)
        and math.isclose(actual[1], expected[1], rel_tol=1e-9, abs_tol=1e-12)
        and actual[2] == expected[2]
    )
    return {
        "universe": name,
        "symbol_count": len(codes),
        "total_return": actual[0],
        "max_drawdown": actual[1],
        "total_trades": actual[2],
        "deployment_policy": result["deployment_policy"],
        "golden": {
            "total_return": expected[0],
            "max_drawdown": expected[1],
            "total_trades": expected[2],
        },
        "strict_invariant": frozen_match,
    }


def data_integrity() -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for path in sorted(REGIME_DATA_DIR.glob("*.csv")):
        frame = pd_read_csv_dates(path)
        rows.append(
            {
                "file": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "rows": len(frame),
                # A CSV with only a header (or entirely blank) yields no rows;
                # guard the first/last access so integrity checks never crash on
                # an empty file but still surface it for review.
                "first_date": frame[0] if frame else None,
                "last_date": frame[-1] if frame else None,
            }
        )
    return {"file_count": len(rows), "files": rows}


def pd_read_csv_dates(path: Path) -> list[str]:
    # A tiny parser is enough for integrity metadata and avoids a second pandas copy.
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.split(",", 1)[0] for line in lines[1:] if line]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--skip-original",
        action="store_true",
        help="Skip the slower original-engine comparison; full evidence is the default.",
    )
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")

    deterministic = deterministic_pools()
    development = development_pools()
    blind = final_blind_pools()
    adaptive_det = run_distribution(
        deterministic, route="adaptive", start_date="2024-01-02", end_date="2024-12-31", workers=args.workers
    )
    adaptive_dev = run_distribution(
        development, route="adaptive", start_date="2024-01-02", end_date="2024-12-31", workers=args.workers
    )
    adaptive_blind = run_distribution(
        blind, route="adaptive", start_date="2024-01-02", end_date="2024-12-31", workers=args.workers
    )
    years = {
        year: run_distribution(
            deterministic,
            route="adaptive",
            start_date=f"{year}-01-03" if year == "2023" else f"{year}-01-04",
            end_date=f"{year}-12-29" if year == "2023" else f"{year}-12-30",
            workers=args.workers,
        )
        for year in ("2022", "2023")
    }
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        bull = list(executor.map(_run_bull, UNIVERSES.items()))

    original: dict[str, Any] = {"skipped": True}
    if not args.skip_original:
        original_det = run_distribution(
            deterministic, route="original", start_date="2024-01-02", end_date="2024-12-31", workers=args.workers
        )
        original_dev = run_distribution(
            development, route="original", start_date="2024-01-02", end_date="2024-12-31", workers=args.workers
        )
        original = {
            "skipped": False,
            "deterministic": {"summary": summarize(original_det), "results": original_det},
            "development": {"summary": summarize(original_dev), "results": original_dev},
        }

    artifact = {
        "protocol": {
            "adjustment": "qfq",
            "initial_capital": INITIAL_CAPITAL,
            "signal_timing": "close signal, next tradable open execution",
            "development_seed": DEVELOPMENT_SEED,
            "final_blind_seed": FINAL_BLIND_SEED,
            "final_blind_opened_after_freeze": True,
            "final_blind_used_for_tuning": False,
        },
        "data_integrity": data_integrity(),
        "adaptive_2024": {
            "deterministic": {"summary": summarize(adaptive_det), "results": adaptive_det},
            "development": {"summary": summarize(adaptive_dev), "results": adaptive_dev},
            "final_blind": {"summary": summarize(adaptive_blind), "results": adaptive_blind},
        },
        "original_2024": original,
        "adaptive_prior_years": {
            year: {"summary": summarize(rows), "results": rows} for year, rows in years.items()
        },
        "frozen_bull": {
            "strict_invariant": all(row["strict_invariant"] for row in bull),
            "results": bull,
        },
        "acceptance": {
            "deterministic_profitable_at_least_80pct": summarize(adaptive_det)["profitable_ratio"] >= 0.8,
            "deterministic_median_return_at_least_20pct": summarize(adaptive_det)["median_return"] >= 0.2,
            "blind_profitable_at_least_80pct": summarize(adaptive_blind)["profitable_ratio"] >= 0.8,
            "blind_median_return_at_least_20pct": summarize(adaptive_blind)["median_return"] >= 0.2,
            "bull_strict_invariant": all(row["strict_invariant"] for row in bull),
        },
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(OUTPUT), "acceptance": artifact["acceptance"]}, indent=2))
    return 0 if all(artifact["acceptance"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
