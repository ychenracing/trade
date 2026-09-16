"""Temporary read-only alpha-attribution probe.

This file exists only on the isolated research branch.  It does not change
production code or strategy parameters.  The Python 3.12 CI run intentionally
emits one JSON research receipt via a controlled test failure; the probe is
removed after the receipt is recovered.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import contextlib
import io
import json
import math
from pathlib import Path
import statistics
import sys
from typing import Any

import numpy as np
import pandas as pd
import pytest

from quantfusion.config.engine import default_engine_config
from quantfusion.config.paths import MARKET_DATA_DIR
from quantfusion.config.universe import SYMBOL_NAMES, VALIDATION_UNIVERSES
from quantfusion.domain.rules import floor_to_lot
from quantfusion.engine import BacktestEngine
from quantfusion.research.fingerprints import canonical_sequence_sha

CAPITAL = 2_000_000.0
START = "2025-04-01"
END = "2026-07-20"
HORIZONS = (10, 20, 40, 60, 120)


def _stats(values: list[float]) -> dict[str, Any]:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    if not finite:
        return {"count": 0}
    array = np.asarray(finite, dtype=float)
    return {
        "count": len(finite),
        "mean": float(array.mean()),
        "median": float(np.median(array)),
        "p25": float(np.quantile(array, 0.25)),
        "p75": float(np.quantile(array, 0.75)),
        "min": float(array.min()),
        "max": float(array.max()),
        "positive_rate": float((array > 0).mean()),
        "gt_10pct_rate": float((array > 0.10).mean()),
        "gt_20pct_rate": float((array > 0.20).mean()),
        "gt_50pct_rate": float((array > 0.50).mean()),
        "lt_minus_10pct_rate": float((array < -0.10).mean()),
    }


def _load_frames(symbols: list[str]) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        frame = pd.read_csv(Path(MARKET_DATA_DIR) / f"{symbol}.csv", parse_dates=["date"])
        frame = frame.set_index("date").sort_index()
        frames[symbol] = frame.loc[(frame.index >= START) & (frame.index <= END)].copy()
    return frames


def _forward_return(frame: pd.DataFrame, day: str, price: float, horizon: int) -> float | None:
    timestamp = pd.Timestamp(day)
    if timestamp not in frame.index or price <= 0:
        return None
    location = int(frame.index.get_loc(timestamp))
    target = location + horizon
    if target >= len(frame):
        return None
    close = float(frame.iloc[target]["close"])
    return close / price - 1.0


def _session_gap(frame: pd.DataFrame, left: str, right: str) -> int | None:
    left_ts, right_ts = pd.Timestamp(left), pd.Timestamp(right)
    if left_ts not in frame.index or right_ts not in frame.index:
        return None
    return int(frame.index.get_loc(right_ts)) - int(frame.index.get_loc(left_ts))


def _trade_buckets(trades: list[Any]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
    for trade in trades:
        grouped[(trade.date, trade.symbol, trade.direction)].append(trade)
    buckets: list[dict[str, Any]] = []
    holdings: Counter[str] = Counter()
    ordered = sorted(grouped.items(), key=lambda item: (item[0][0], 0 if item[0][2] == "sell" else 1, item[0][1]))
    for (day, symbol, direction), fills in ordered:
        shares = sum(int(fill.shares) for fill in fills)
        prior = int(holdings[symbol])
        gross = sum(float(fill.gross_value) for fill in fills)
        weighted_price = gross / shares if shares else 0.0
        costs = sum(float(fill.commission) + float(fill.stamp_duty_cost) for fill in fills)
        pnl = sum(float(fill.pnl) for fill in fills if direction == "sell")
        pnl_weight = sum(abs(float(fill.gross_value)) for fill in fills)
        weighted_pnl_pct = (
            sum(float(fill.pnl_pct) * abs(float(fill.gross_value)) for fill in fills) / pnl_weight
            if pnl_weight
            else 0.0
        )
        reasons = sorted({str(fill.reason) for fill in fills})
        if direction == "buy":
            action = "new_position" if prior <= 0 else "add_position"
            holdings[symbol] += shares
        else:
            action = "full_exit" if shares >= prior else "reduce_position"
            holdings[symbol] = max(prior - shares, 0)
        buckets.append(
            {
                "date": day,
                "symbol": symbol,
                "direction": direction,
                "fill_count": len(fills),
                "shares": shares,
                "price": weighted_price,
                "gross_value": gross,
                "costs": costs,
                "pnl": pnl,
                "pnl_pct": weighted_pnl_pct,
                "reasons": reasons,
                "action": action,
                "prior_symbol_shares": prior,
                "after_symbol_shares": int(holdings[symbol]),
            }
        )
    return buckets


def _reason_group(reasons: list[str]) -> str:
    text = " ".join(reasons).lower()
    if any(token in text for token in ("account_budget", "circuit", "drawdown", "risk lock", "risk_")):
        return "risk_adjustment"
    if any(token in text for token in ("regime", "route", "choppy")):
        return "route_or_regime"
    if "stop" in text:
        return "stop_or_trailing"
    if "reversal" in text:
        return "strategy_reversal"
    return "strategy_or_other"


def _bucket_attribution(
    buckets: list[dict[str, Any]], frames: dict[str, pd.DataFrame]
) -> dict[str, Any]:
    buys = [bucket for bucket in buckets if bucket["direction"] == "buy"]
    sells = [bucket for bucket in buckets if bucket["direction"] == "sell"]
    buy_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sell_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for bucket in buys:
        buy_by_symbol[bucket["symbol"]].append(bucket)
    for bucket in sells:
        sell_by_symbol[bucket["symbol"]].append(bucket)

    for bucket in buys + sells:
        frame = frames[bucket["symbol"]]
        bucket["forward_returns"] = {
            str(horizon): _forward_return(frame, bucket["date"], float(bucket["price"]), horizon)
            for horizon in HORIZONS
        }

    for sell in sells:
        future_buys = [buy for buy in buy_by_symbol[sell["symbol"]] if buy["date"] > sell["date"]]
        next_buy = future_buys[0] if future_buys else None
        gap = _session_gap(frames[sell["symbol"]], sell["date"], next_buy["date"]) if next_buy else None
        sell["reentry_sessions"] = gap
        r60 = sell["forward_returns"]["60"]
        r120 = sell["forward_returns"]["120"]
        if r60 is not None and r120 is not None and r60 <= 0 and r120 <= 0:
            sell["exit_class"] = "correct_decline"
        elif (r60 is not None and r60 >= 0.20) or (r120 is not None and r120 >= 0.30):
            sell["exit_class"] = "premature_major_winner"
        else:
            sell["exit_class"] = "mixed_or_censored"
        sell["reason_group"] = _reason_group(sell["reasons"])

    for buy in buys:
        future_sells = [sell for sell in sell_by_symbol[buy["symbol"]] if sell["date"] > buy["date"]]
        next_sell = future_sells[0] if future_sells else None
        buy["next_sell_sessions"] = (
            _session_gap(frames[buy["symbol"]], buy["date"], next_sell["date"])
            if next_sell
            else None
        )

    sell_horizons = {
        str(horizon): _stats([
            value
            for sell in sells
            if (value := sell["forward_returns"][str(horizon)]) is not None
        ])
        for horizon in HORIZONS
    }
    buy_horizons = {
        str(horizon): _stats([
            value
            for buy in buys
            if (value := buy["forward_returns"][str(horizon)]) is not None
        ])
        for horizon in HORIZONS
    }
    reason_stats: dict[str, dict[str, Any]] = {}
    for group in sorted({sell["reason_group"] for sell in sells}):
        rows = [sell for sell in sells if sell["reason_group"] == group]
        reason_stats[group] = {
            "sell_buckets": len(rows),
            "fills": sum(int(row["fill_count"]) for row in rows),
            "realized_pnl": sum(float(row["pnl"]) for row in rows),
            "modeled_costs": sum(float(row["costs"]) for row in rows),
            "median_pnl_pct": statistics.median(float(row["pnl_pct"]) for row in rows) if rows else None,
            "forward_60d": _stats([
                value for row in rows if (value := row["forward_returns"]["60"]) is not None
            ]),
            "forward_120d": _stats([
                value for row in rows if (value := row["forward_returns"]["120"]) is not None
            ]),
        }

    top_premature = sorted(
        [sell for sell in sells if sell["exit_class"] == "premature_major_winner"],
        key=lambda row: max(
            row["forward_returns"]["60"] if row["forward_returns"]["60"] is not None else -999.0,
            row["forward_returns"]["120"] if row["forward_returns"]["120"] is not None else -999.0,
        ),
        reverse=True,
    )[:15]
    top_premature_view = [
        {
            key: row[key]
            for key in ("date", "symbol", "shares", "price", "pnl", "pnl_pct", "reasons", "reason_group", "reentry_sessions", "forward_returns")
        }
        for row in top_premature
    ]

    return {
        "buy_buckets": len(buys),
        "sell_buckets": len(sells),
        "buy_actions": dict(Counter(row["action"] for row in buys)),
        "sell_actions": dict(Counter(row["action"] for row in sells)),
        "buy_forward_returns": buy_horizons,
        "sell_forward_returns": sell_horizons,
        "exit_classes": dict(Counter(row["exit_class"] for row in sells)),
        "reentry_within_20_sessions": sum(
            row["reentry_sessions"] is not None and row["reentry_sessions"] <= 20 for row in sells
        ),
        "reentry_within_60_sessions": sum(
            row["reentry_sessions"] is not None and row["reentry_sessions"] <= 60 for row in sells
        ),
        "buy_exited_within_10_sessions": sum(
            row["next_sell_sessions"] is not None and row["next_sell_sessions"] <= 10 for row in buys
        ),
        "buy_exited_within_20_sessions": sum(
            row["next_sell_sessions"] is not None and row["next_sell_sessions"] <= 20 for row in buys
        ),
        "low_abs_pnl_sell_buckets_2pct": sum(abs(float(row["pnl_pct"])) < 0.02 for row in sells),
        "reason_groups": reason_stats,
        "top_premature_exits": top_premature_view,
    }


def _pnl_contribution(trades: list[Any]) -> dict[str, Any]:
    sells = [trade for trade in trades if trade.direction == "sell"]
    positives = sorted((trade for trade in sells if trade.pnl > 0), key=lambda trade: trade.pnl, reverse=True)
    negatives = sorted((trade for trade in sells if trade.pnl < 0), key=lambda trade: trade.pnl)
    positive_pnl = sum(float(trade.pnl) for trade in positives)
    negative_pnl = sum(float(trade.pnl) for trade in negatives)
    top_count = max(1, math.ceil(len(positives) * 0.10)) if positives else 0
    by_symbol: dict[str, float] = defaultdict(float)
    for trade in sells:
        by_symbol[trade.symbol] += float(trade.pnl)

    def view(trade: Any) -> dict[str, Any]:
        return {
            "date": trade.date,
            "symbol": trade.symbol,
            "strategy": trade.strategy_name,
            "pnl": float(trade.pnl),
            "pnl_pct": float(trade.pnl_pct),
            "reason": str(trade.reason),
            "shares": int(trade.shares),
        }

    return {
        "winning_sell_fills": len(positives),
        "losing_sell_fills": len(negatives),
        "zero_sell_fills": len(sells) - len(positives) - len(negatives),
        "gross_positive_realized_pnl": positive_pnl,
        "gross_negative_realized_pnl": negative_pnl,
        "top_10pct_positive_fill_share_of_positive_pnl": (
            sum(float(trade.pnl) for trade in positives[:top_count]) / positive_pnl
            if positive_pnl > 0
            else None
        ),
        "top_winners": [view(trade) for trade in positives[:12]],
        "largest_losses": [view(trade) for trade in negatives[:12]],
        "realized_pnl_by_symbol": dict(sorted(by_symbol.items(), key=lambda item: item[1], reverse=True)),
    }


def _cash_and_exposure(
    result: dict[str, Any], trades: list[Any], symbols: list[str]
) -> dict[str, Any]:
    equity = result["equity_curve"].copy().sort_index()
    flows: dict[str, float] = defaultdict(float)
    buckets: dict[str, list[Any]] = defaultdict(list)
    for trade in trades:
        flows[trade.date] += float(trade.net_cash_flow)
        buckets[trade.date].append(trade)
    cash = CAPITAL
    shares: Counter[str] = Counter()
    cash_fractions: list[float] = []
    exposed_days: Counter[str] = Counter()
    for timestamp, row in equity.iterrows():
        day = pd.Timestamp(timestamp).strftime("%Y-%m-%d")
        cash += flows.get(day, 0.0)
        for trade in buckets.get(day, []):
            shares[trade.symbol] += int(trade.shares) * (1 if trade.direction == "buy" else -1)
        assets = float(row["assets"])
        if assets > 0:
            cash_fractions.append(cash / assets)
        for symbol in symbols:
            if shares[symbol] > 0:
                exposed_days[symbol] += 1
    days = len(equity)
    return {
        "cash_fraction": _stats(cash_fractions),
        "cash_fraction_gt_50pct": (
            sum(value > 0.5 for value in cash_fractions) / len(cash_fractions)
            if cash_fractions
            else None
        ),
        "symbol_exposure_fraction": {
            symbol: exposed_days[symbol] / days if days else 0.0 for symbol in symbols
        },
    }


def _modeled_costs(trades: list[Any], frames: dict[str, pd.DataFrame]) -> dict[str, float]:
    explicit = sum(float(trade.commission) + float(trade.stamp_duty_cost) for trade in trades)
    slippage = 0.0
    for trade in trades:
        frame = frames[trade.symbol]
        day = pd.Timestamp(trade.date)
        if day not in frame.index:
            continue
        opening = float(frame.loc[day, "open"])
        if trade.direction == "buy":
            slippage += max(float(trade.price) - opening, 0.0) * int(trade.shares)
        else:
            slippage += max(opening - float(trade.price), 0.0) * int(trade.shares)
    return {
        "commission_plus_stamp_duty": explicit,
        "estimated_slippage_from_open": slippage,
        "total_modeled_cost": explicit + slippage,
        "cost_over_initial_capital": (explicit + slippage) / CAPITAL,
    }


def _buy_hold(
    symbols: list[str], frames: dict[str, pd.DataFrame], equity_dates: pd.DatetimeIndex
) -> dict[str, Any]:
    cfg = default_engine_config()
    commission_rate = float(cfg.get("commission_rate", 0.00025))
    min_commission = float(cfg.get("min_commission", 0.0))
    slippage = float(cfg.get("slippage", 0.001))
    per_symbol_budget = CAPITAL / len(symbols)
    cash = CAPITAL
    positions: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        frame = frames[symbol]
        eligible = frame.loc[frame.index >= pd.Timestamp(START)]
        if eligible.empty:
            continue
        entry_day = eligible.index[0]
        opening = float(eligible.iloc[0]["open"])
        execution_price = opening * (1.0 + slippage)
        shares = floor_to_lot(per_symbol_budget / execution_price)
        while shares > 0:
            gross = shares * execution_price
            commission = max(gross * commission_rate, min_commission)
            if gross + commission <= per_symbol_budget:
                break
            shares -= 100
        if shares <= 0:
            continue
        gross = shares * execution_price
        commission = max(gross * commission_rate, min_commission)
        cash -= gross + commission
        final_close = float(frame.iloc[-1]["close"])
        positions[symbol] = {
            "entry_date": entry_day.strftime("%Y-%m-%d"),
            "entry_price": execution_price,
            "shares": shares,
            "entry_commission": commission,
            "raw_return_to_final_close": final_close / execution_price - 1.0,
        }
    assets: list[float] = []
    for day in equity_dates:
        value = cash
        for symbol, position in positions.items():
            if day < pd.Timestamp(position["entry_date"]):
                continue
            frame = frames[symbol]
            history = frame.loc[frame.index <= day, "close"]
            if not history.empty:
                value += int(position["shares"]) * float(history.iloc[-1])
        assets.append(value)
    series = pd.Series(assets, index=equity_dates, dtype=float)
    drawdown = series / series.cummax() - 1.0
    final_assets = float(series.iloc[-1])
    return {
        "method": "equal capital across current 17; first available opening buy with native buy slippage/commission; terminal mark-to-market, no forced terminal sale",
        "final_assets": final_assets,
        "total_return": final_assets / CAPITAL - 1.0,
        "max_drawdown": float(drawdown.min()),
        "entry_trades": len(positions),
        "entry_commission": sum(float(item["entry_commission"]) for item in positions.values()),
        "per_symbol": positions,
    }


def _summary_metrics(result: dict[str, Any]) -> dict[str, Any]:
    trades = result["trades"]
    return {
        key: float(result[key]) if key not in {"total_trades", "sell_trades", "date_symbol_side_count", "date_symbol_sell_side_count"} else int(result[key])
        for key in ("final_assets", "total_return", "annual_return", "max_drawdown", "sharpe", "calmar", "total_trades", "sell_trades", "date_symbol_side_count", "date_symbol_sell_side_count")
    } | {
        "explicit_fees": sum(float(trade.commission) + float(trade.stamp_duty_cost) for trade in trades),
        "account_risk_budget": result.get("account_risk_budget"),
        "order_event_count": len(result.get("order_events", [])),
        "risk_event_count": len(result.get("risk_events", [])),
    }


def test_emit_alpha_attribution_research_receipt() -> None:
    if sys.version_info[:2] != (3, 12):
        pytest.skip("single-source research receipt emitted only on Python 3.12")
    symbols = list(next(codes for codes in VALIDATION_UNIVERSES.values() if len(codes) == 17))
    names = {symbol: SYMBOL_NAMES[symbol] for symbol in symbols}
    with contextlib.redirect_stdout(io.StringIO()):
        default_result = BacktestEngine(CAPITAL).run(
            names, START, END, data_dir=str(MARKET_DATA_DIR), indicator_state="warm"
        )
        no_budget_result = BacktestEngine(
            CAPITAL, cfg={"account_risk_budget_enabled": False}
        ).run(names, START, END, data_dir=str(MARKET_DATA_DIR), indicator_state="warm")
    trades = list(default_result["trades"])
    frames = _load_frames(symbols)
    buckets = _trade_buckets(trades)
    bucket_report = _bucket_attribution(buckets, frames)
    equity_dates = pd.DatetimeIndex(default_result["equity_curve"].index)
    buy_hold = _buy_hold(symbols, frames, equity_dates)
    first_buys = {
        symbol: min((bucket["date"] for bucket in buckets if bucket["direction"] == "buy" and bucket["symbol"] == symbol), default=None)
        for symbol in symbols
    }
    cash_exposure = _cash_and_exposure(default_result, trades, symbols)
    bh_winners = sorted(
        (
            {
                "symbol": symbol,
                "buy_hold_return": float(item["raw_return_to_final_close"]),
                "trade_first_buy": first_buys[symbol],
                "trade_exposure_fraction": cash_exposure["symbol_exposure_fraction"][symbol],
                "trade_sell_buckets": sum(bucket["direction"] == "sell" and bucket["symbol"] == symbol for bucket in buckets),
            }
            for symbol, item in buy_hold["per_symbol"].items()
        ),
        key=lambda item: item["buy_hold_return"],
        reverse=True,
    )
    order_events = default_result.get("order_events", [])
    risk_events = default_result.get("risk_events", [])
    report = {
        "research_contract": {
            "production_change": "NONE",
            "canonical": False,
            "purpose": "read-only alpha attribution; no parameter optimization or strategy promotion",
            "window": [START, END],
            "initial_capital": CAPITAL,
            "symbols": symbols,
            "config_sha256": canonical_sequence_sha(default_engine_config()),
        },
        "trade": _summary_metrics(default_result),
        "trade_no_account_budget": _summary_metrics(no_budget_result),
        "account_budget_counterfactual": {
            "return_delta_on_minus_off": float(default_result["total_return"] - no_budget_result["total_return"]),
            "max_drawdown_delta_on_minus_off": float(default_result["max_drawdown"] - no_budget_result["max_drawdown"]),
            "trade_fill_delta_on_minus_off": int(default_result["total_trades"] - no_budget_result["total_trades"]),
            "explicit_fee_delta_on_minus_off": sum(float(trade.commission) + float(trade.stamp_duty_cost) for trade in default_result["trades"]) - sum(float(trade.commission) + float(trade.stamp_duty_cost) for trade in no_budget_result["trades"]),
            "scope_note": "isolates existing account-risk-budget toggle only; not all risk logic",
        },
        "buy_hold": buy_hold,
        "trade_vs_buy_hold": {
            "return_delta_trade_minus_buy_hold": float(default_result["total_return"] - buy_hold["total_return"]),
            "max_drawdown_delta_trade_minus_buy_hold": float(default_result["max_drawdown"] - buy_hold["max_drawdown"]),
            "note": "capability-level comparison; not an additive causal decomposition because entry/exit/exposure differ",
        },
        "pnl_contribution": _pnl_contribution(trades),
        "buy_sell_order_efficiency": bucket_report,
        "modeled_costs": _modeled_costs(trades, frames),
        "cash_and_exposure": cash_exposure,
        "top_buy_hold_winners_vs_trade_exposure": bh_winners[:12],
        "order_event_types": dict(Counter(str(event.get("event", "unknown")) for event in order_events)),
        "risk_event_types": dict(Counter(str(event.get("event", "unknown")) for event in risk_events)),
        "limitations": [
            "Forward returns after exits are descriptive opportunity diagnostics and are not summed as portfolio alpha because capital is reused and exits overlap.",
            "TradeRecord fills are internal sleeve fills; date-symbol-side buckets are reported separately and are closer to broker-nettable actions but still not broker orders.",
            "Buy-and-hold is equal-capital current-17 with native entry costs and terminal mark-to-market; it does not share Trade's dynamic allocation path.",
            "Account-budget on/off is the only direct risk counterfactual here; other risk rules remain active in both paths.",
        ],
    }
    pytest.fail("ALPHA_ATTRIBUTION_JSON=" + json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
