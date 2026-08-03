"""Reproducible simple benchmarks for strategy attribution."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import quant_fusion as qf
import regime_adaptive as ra


def _buy_hold_return(frame: pd.DataFrame, start: str, end: str) -> float:
    sample = frame.loc[(frame.index >= pd.Timestamp(start)) & (frame.index <= pd.Timestamp(end))]
    if len(sample) < 2:
        raise ValueError("insufficient buy-and-hold observations")
    return float(sample["close"].iloc[-1] / sample["open"].iloc[0] - 1.0)


def run_benchmarks(
    symbols: dict[str, str],
    *,
    data_dir: str,
    regime_data_dir: str,
    start: str,
    end: str,
) -> dict:
    returns = {
        code: _buy_hold_return(
            qf.DataFetcher.load_stock_data(code, start, end, data_dir=data_dir),
            start,
            end,
        )
        for code in symbols
    }
    equal_weight = sum(returns.values()) / len(returns)
    boundary = str((pd.Timestamp(start) - pd.Timedelta(days=1)).date())
    leaders = ra.select_positive_momentum_leaders(
        tuple(symbols), data_dir=data_dir, as_of=boundary
    )
    top3 = (
        sum(returns[code] for code in leaders.selected_symbols)
        / len(leaders.selected_symbols)
        if leaders.selected_symbols
        else 0.0
    )
    adaptive = ra.RegimeAdaptiveBacktestEngine().run(
        symbols,
        start,
        end,
        data_dir=data_dir,
        regime_data_dir=regime_data_dir,
        leader_data_dir=data_dir,
        indicator_state="warm",
    )
    return {
        "period": {"start": start, "end": end},
        "symbols": sorted(symbols),
        "equal_weight_buy_hold_return": equal_weight,
        "causal_top3_buy_hold_return": top3,
        "adaptive_total_return": float(adaptive["total_return"]),
        "adaptive_max_drawdown": float(adaptive["max_drawdown"]),
        "adaptive_total_trades": int(adaptive["total_trades"]),
        "top3_symbols": list(leaders.selected_symbols),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--regime-data-dir", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", default="benchmark_validation.json")
    args = parser.parse_args()
    codes = [item.strip() for item in args.symbols.split(",") if item.strip()]
    payload = run_benchmarks(
        {code: code for code in codes},
        data_dir=args.data_dir,
        regime_data_dir=args.regime_data_dir,
        start=args.start,
        end=args.end,
    )
    Path(args.output).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
