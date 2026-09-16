"""Research-only AB5 candidate comparison on the frozen 17-symbol window."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd

from quantfusion.application import engine_api as qf
from quantfusion.config.paths import MARKET_DATA_DIR
from quantfusion.config.universe import SYMBOL_NAMES, VALIDATION_UNIVERSES

START_DATE = "2025-04-01"
END_DATE = "2026-07-20"
INITIAL_CAPITAL = 2_000_000.0


def _session_map(equity_curve: pd.DataFrame) -> dict[str, int]:
    return {
        pd.Timestamp(date).strftime("%Y-%m-%d"): index
        for index, date in enumerate(equity_curve.index)
    }


def _sell_rebuy_metrics(
    trades: list[Any], equity_curve: pd.DataFrame
) -> dict[str, int]:
    sessions = _session_map(equity_curve)
    bucket_reasons: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for trade in trades:
        bucket_reasons[(trade.date, trade.symbol, trade.direction)].add(
            str(trade.reason).split(":", 1)[0]
        )
    buys_by_symbol: dict[str, list[str]] = defaultdict(list)
    for date, symbol, direction in bucket_reasons:
        if direction == "buy":
            buys_by_symbol[symbol].append(date)
    for dates in buys_by_symbol.values():
        dates.sort(key=lambda value: sessions[value])

    chains = within_20 = within_60 = pure_ab5_within_20 = 0
    for (sell_date, symbol, direction), reasons in bucket_reasons.items():
        if direction != "sell":
            continue
        sell_pos = sessions[sell_date]
        rebuy = next(
            (date for date in buys_by_symbol.get(symbol, ()) if sessions[date] > sell_pos),
            None,
        )
        if rebuy is None:
            continue
        gap = sessions[rebuy] - sell_pos
        chains += 1
        if gap <= 20:
            within_20 += 1
            if reasons == {"account_budget_trim"}:
                pure_ab5_within_20 += 1
        if gap <= 60:
            within_60 += 1
    return {
        "sell_rebuy_chains": chains,
        "rebuy_within_20_sessions": within_20,
        "rebuy_within_60_sessions": within_60,
        "pure_ab5_rebuy_within_20_sessions": pure_ab5_within_20,
    }


def run() -> dict[str, Any]:
    codes = VALIDATION_UNIVERSES["17_symbols"]
    engine = qf.BacktestEngine(INITIAL_CAPITAL)
    with contextlib.redirect_stdout(io.StringIO()):
        result = engine.run(
            {code: SYMBOL_NAMES[code] for code in codes},
            START_DATE,
            END_DATE,
            data_dir=str(MARKET_DATA_DIR),
            indicator_state="cold",
        )
    trades = list(result["trades"])
    equity_curve = result["equity_curve"]
    if not isinstance(equity_curve, pd.DataFrame) or "assets" not in equity_curve:
        raise TypeError("expected result equity_curve DataFrame with assets column")
    mean_assets = float(equity_curve["assets"].astype(float).mean())
    turnover_notional = sum(
        abs(float(trade.gross_value or trade.shares * trade.price)) for trade in trades
    )
    explicit_fees = sum(
        float(trade.commission) + float(trade.stamp_duty_cost) for trade in trades
    )
    ab5_sells = [
        trade
        for trade in trades
        if trade.direction == "sell"
        and str(trade.reason).split(":", 1)[0] == "account_budget_trim"
    ]
    envelopes = [
        event
        for event in result.get("risk_events", [])
        if event.get("event") == "account_budget_envelope"
    ]
    recovery_days = sum("ab5_recovery_state" in event for event in envelopes)
    recovery_states: dict[str, int] = defaultdict(int)
    for event in envelopes:
        if "ab5_recovery_state" in event:
            recovery_states[str(event["ab5_recovery_state"])] += 1

    metrics = {
        "window": [START_DATE, END_DATE],
        "symbols": list(codes),
        "initial_capital": INITIAL_CAPITAL,
        "final_assets": float(result["final_assets"]),
        "total_return": float(result["total_return"]),
        "annual_return": float(result["annual_return"]),
        "sharpe": float(result["sharpe"]),
        "calmar": float(result["calmar"]),
        "max_drawdown": float(result["max_drawdown"]),
        "fills": int(result["sleeve_fill_count"]),
        "sell_fills": int(result["sleeve_sell_fill_count"]),
        "date_symbol_side_buckets": int(result["date_symbol_side_count"]),
        "sell_buckets": int(result["date_symbol_sell_side_count"]),
        "turnover_over_mean_assets": turnover_notional / mean_assets,
        "explicit_fees": explicit_fees,
        "ab5_sell_fills": len(ab5_sells),
        "ab5_sell_notional": sum(abs(float(trade.gross_value)) for trade in ab5_sells),
        "recovery_active_days": recovery_days,
        "recovery_state_days": dict(sorted(recovery_states.items())),
    }
    metrics.update(_sell_rebuy_metrics(trades, equity_curve))
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    metrics = run()
    Path(args.output).write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(metrics, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
