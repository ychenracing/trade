"""Temporary Phase-3 execution-efficiency attribution probe.

The probe is research-only.  It runs the frozen production replay unchanged and
uses future prices only for retrospective chain attribution.  No future value
enters any production decision, and no risk action is removed by this probe.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import contextlib
import io
import json
import math
from pathlib import Path
import statistics
from typing import Any

import pandas as pd
import pytest

from quantfusion.config.paths import MARKET_DATA_DIR
from quantfusion.config.universe import SYMBOL_NAMES, VALIDATION_UNIVERSES
from quantfusion.domain.models import date_symbol_side_count
from quantfusion.engine import BacktestEngine

CAPITAL = 2_000_000.0
START = "2025-04-01"
END = "2026-07-20"
SOURCE_MAIN = "85cce57e370b1d8097f2931ffc4ea76c01ae7c86"
EXPECTED_RETURN = 8.610542744515504
EXPECTED_DRAWDOWN = -0.15104978428469945
EXPECTED_FILLS = 645
EXPECTED_BUCKETS = 190
EXPECTED_REBUY_20 = 40
EXPECTED_REBUY_60 = 67


def _load_frames(symbols: list[str]) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        frame = pd.read_csv(
            Path(MARKET_DATA_DIR) / f"{symbol}.csv", parse_dates=["date"]
        )
        frame = frame.set_index("date").sort_index()
        frames[symbol] = frame.loc[
            (frame.index >= START) & (frame.index <= END)
        ].copy()
    return frames


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
    ordered = sorted(
        grouped.items(),
        key=lambda item: (
            item[0][0],
            0 if item[0][2] == "sell" else 1,
            item[0][1],
        ),
    )
    for (day, symbol, direction), fills in ordered:
        shares = sum(int(fill.shares) for fill in fills)
        prior = int(holdings[symbol])
        gross = sum(float(fill.gross_value) for fill in fills)
        weighted_price = gross / shares if shares else 0.0
        costs = sum(
            float(fill.commission) + float(fill.stamp_duty_cost) for fill in fills
        )
        pnl = sum(float(fill.pnl) for fill in fills if direction == "sell")
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
                "reasons": reasons,
                "action": action,
                "prior_symbol_shares": prior,
                "after_symbol_shares": int(holdings[symbol]),
            }
        )
    return buckets


def _bucket_slippage(bucket: dict[str, Any], frame: pd.DataFrame) -> float:
    day = pd.Timestamp(str(bucket["date"]))
    if day not in frame.index:
        return 0.0
    opening = float(frame.loc[day, "open"])
    price = float(bucket["price"])
    shares = int(bucket["shares"])
    if bucket["direction"] == "buy":
        return max(price - opening, 0.0) * shares
    return max(opening - price, 0.0) * shares


def _is_pure_ab5(reasons: list[str]) -> bool:
    return bool(reasons) and all(
        reason.split(":", 1)[0] == "account_budget_trim" for reason in reasons
    )


def _has_hard_risk_or_strategy_reason(reasons: list[str]) -> bool:
    text = " ".join(reasons).lower()
    return any(
        token in text
        for token in (
            "atr trailing",
            "donchian exit",
            "reversal",
            "stop",
            "sector breadth risk liquidation",
            "drawdown",
            "circuit",
            "risk lock",
            "catastrophe",
            "liquidation",
        )
    )


def _chain_rows(
    buckets: list[dict[str, Any]], frames: dict[str, pd.DataFrame]
) -> list[dict[str, Any]]:
    buys_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sells = [bucket for bucket in buckets if bucket["direction"] == "sell"]
    for bucket in buckets:
        if bucket["direction"] == "buy":
            buys_by_symbol[str(bucket["symbol"])].append(bucket)
    rows: list[dict[str, Any]] = []
    for sell in sells:
        symbol = str(sell["symbol"])
        future_buys = [
            buy for buy in buys_by_symbol[symbol]
            if str(buy["date"]) > str(sell["date"])
        ]
        if not future_buys:
            continue
        buy = future_buys[0]
        frame = frames[symbol]
        gap = _session_gap(frame, str(sell["date"]), str(buy["date"]))
        if gap is None or gap <= 0:
            continue
        left = int(frame.index.get_loc(pd.Timestamp(str(sell["date"]))))
        right = int(frame.index.get_loc(pd.Timestamp(str(buy["date"]))))
        post_sell_pre_rebuy = frame.iloc[left:right]
        if post_sell_pre_rebuy.empty:
            continue
        sell_price = float(sell["price"])
        buy_price = float(buy["price"])
        min_close = float(post_sell_pre_rebuy["close"].min())
        max_close = float(post_sell_pre_rebuy["close"].max())
        matched_shares = min(int(sell["shares"]), int(buy["shares"]))
        if matched_shares <= 0 or sell_price <= 0 or buy_price <= 0:
            continue
        sell_fraction = matched_shares / int(sell["shares"])
        buy_fraction = matched_shares / int(buy["shares"])
        explicit_cost = (
            float(sell["costs"]) * sell_fraction
            + float(buy["costs"]) * buy_fraction
        )
        slippage_cost = (
            _bucket_slippage(sell, frame) * sell_fraction
            + _bucket_slippage(buy, frame) * buy_fraction
        )
        direct_cost = explicit_cost + slippage_cost
        avoided_downside_value = max(sell_price - min_close, 0.0) * matched_shares
        reacquisition_price_effect = (buy_price - sell_price) * matched_shares
        net_chain_drag = direct_cost + reacquisition_price_effect - avoided_downside_value
        pure_ab5 = _is_pure_ab5(list(sell["reasons"]))
        short_reentry = gap <= 20
        if pure_ab5 and short_reentry and net_chain_drag > 0.0:
            category = "pure_ab5_low_value_candidate"
        elif pure_ab5 and short_reentry:
            category = "pure_ab5_protective_or_economic"
        elif pure_ab5:
            category = "pure_ab5_later_reentry"
        else:
            category = "preserve_strategy_or_mixed"
        rows.append(
            {
                "sell_date": str(sell["date"]),
                "rebuy_date": str(buy["date"]),
                "symbol": symbol,
                "gap_sessions": gap,
                "sell_action": str(sell["action"]),
                "sell_reasons": list(sell["reasons"]),
                "sell_fill_count": int(sell["fill_count"]),
                "rebuy_fill_count": int(buy["fill_count"]),
                "matched_shares": matched_shares,
                "sell_price": sell_price,
                "rebuy_price": buy_price,
                "matched_sell_notional": sell_price * matched_shares,
                "realized_pnl_sell_bucket": float(sell["pnl"]),
                "min_close_before_rebuy": min_close,
                "max_close_before_rebuy": max_close,
                "interim_min_return_from_sell": min_close / sell_price - 1.0,
                "interim_max_return_from_sell": max_close / sell_price - 1.0,
                "price_never_below_sell": min_close >= sell_price,
                "explicit_cost": explicit_cost,
                "estimated_slippage_cost": slippage_cost,
                "modeled_direct_cost": direct_cost,
                "avoided_downside_value": avoided_downside_value,
                "reacquisition_price_effect": reacquisition_price_effect,
                "net_chain_drag": net_chain_drag,
                "pure_ab5": pure_ab5,
                "hard_risk_or_strategy_reason": _has_hard_risk_or_strategy_reason(
                    list(sell["reasons"])
                ),
                "category": category,
            }
        )
    return rows


def _category_summary(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["category"])].append(row)
    result: dict[str, dict[str, Any]] = {}
    for category, items in sorted(grouped.items()):
        result[category] = {
            "chains": len(items),
            "gap_sessions_median": float(
                statistics.median(int(item["gap_sessions"]) for item in items)
            ),
            "matched_sell_notional": sum(
                float(item["matched_sell_notional"]) for item in items
            ),
            "realized_pnl_sell_buckets": sum(
                float(item["realized_pnl_sell_bucket"]) for item in items
            ),
            "modeled_direct_cost": sum(
                float(item["modeled_direct_cost"]) for item in items
            ),
            "avoided_downside_value": sum(
                float(item["avoided_downside_value"]) for item in items
            ),
            "reacquisition_price_effect": sum(
                float(item["reacquisition_price_effect"]) for item in items
            ),
            "net_chain_drag": sum(float(item["net_chain_drag"]) for item in items),
            "price_never_below_sell_chains": sum(
                bool(item["price_never_below_sell"]) for item in items
            ),
        }
    return result


def _multi_fill_summary(buckets: list[dict[str, Any]]) -> dict[str, Any]:
    multi = [bucket for bucket in buckets if int(bucket["fill_count"]) > 1]
    same_day_sides: dict[tuple[str, str], set[str]] = defaultdict(set)
    for bucket in buckets:
        same_day_sides[(str(bucket["date"]), str(bucket["symbol"]))].add(
            str(bucket["direction"])
        )
    opposite_side_dates = [
        key for key, sides in same_day_sides.items() if sides == {"buy", "sell"}
    ]
    top = sorted(
        multi,
        key=lambda bucket: (
            int(bucket["fill_count"]),
            float(bucket["gross_value"]),
        ),
        reverse=True,
    )[:15]
    return {
        "date_symbol_side_buckets": len(buckets),
        "multi_fill_buckets": len(multi),
        "fills_inside_multi_fill_buckets": sum(
            int(bucket["fill_count"]) for bucket in multi
        ),
        "excess_internal_fills_over_one_per_bucket": sum(
            int(bucket["fill_count"]) - 1 for bucket in multi
        ),
        "explicit_cost_in_multi_fill_buckets": sum(
            float(bucket["costs"]) for bucket in multi
        ),
        "multi_fill_by_side": dict(
            Counter(str(bucket["direction"]) for bucket in multi)
        ),
        "same_day_symbol_with_both_buy_and_sell": len(opposite_side_dates),
        "same_day_opposite_side_examples": [
            {"date": day, "symbol": symbol} for day, symbol in opposite_side_dates[:15]
        ],
        "top_multi_fill_buckets": [
            {
                "date": str(bucket["date"]),
                "symbol": str(bucket["symbol"]),
                "direction": str(bucket["direction"]),
                "fill_count": int(bucket["fill_count"]),
                "shares": int(bucket["shares"]),
                "gross_value": float(bucket["gross_value"]),
                "explicit_cost": float(bucket["costs"]),
                "reasons": list(bucket["reasons"]),
            }
            for bucket in top
        ],
    }


def _partial_reduction_summary(buckets: list[dict[str, Any]]) -> dict[str, Any]:
    partial = [
        bucket for bucket in buckets
        if bucket["direction"] == "sell"
        and bucket["action"] == "reduce_position"
        and int(bucket["prior_symbol_shares"]) > 0
    ]
    rows = [
        {
            "date": str(bucket["date"]),
            "symbol": str(bucket["symbol"]),
            "shares": int(bucket["shares"]),
            "prior_symbol_shares": int(bucket["prior_symbol_shares"]),
            "reduction_fraction": int(bucket["shares"])
            / int(bucket["prior_symbol_shares"]),
            "gross_value": float(bucket["gross_value"]),
            "explicit_cost": float(bucket["costs"]),
            "fill_count": int(bucket["fill_count"]),
            "reasons": list(bucket["reasons"]),
        }
        for bucket in partial
    ]
    fractions = [float(row["reduction_fraction"]) for row in rows]
    return {
        "partial_reduction_buckets": len(rows),
        "reduction_fraction_min": min(fractions) if fractions else None,
        "reduction_fraction_median": (
            float(statistics.median(fractions)) if fractions else None
        ),
        "reduction_fraction_max": max(fractions) if fractions else None,
        "explicit_cost": sum(float(row["explicit_cost"]) for row in rows),
        "smallest_reductions": sorted(
            rows, key=lambda row: (float(row["reduction_fraction"]), row["date"])
        )[:15],
    }


def emit_receipt() -> None:
    symbols = list(
        next(codes for codes in VALIDATION_UNIVERSES.values() if len(codes) == 17)
    )
    names = {symbol: SYMBOL_NAMES[symbol] for symbol in symbols}
    with contextlib.redirect_stdout(io.StringIO()):
        result = BacktestEngine(CAPITAL).run(
            names,
            START,
            END,
            data_dir=str(MARKET_DATA_DIR),
            indicator_state="warm",
        )
    trades = list(result["trades"])
    frames = _load_frames(symbols)
    buckets = _trade_buckets(trades)
    chains = _chain_rows(buckets, frames)
    low_value = [
        row for row in chains if row["category"] == "pure_ab5_low_value_candidate"
    ]
    chain_20 = sum(int(row["gap_sessions"]) <= 20 for row in chains)
    chain_60 = sum(int(row["gap_sessions"]) <= 60 for row in chains)
    receipt = {
        "research_contract": {
            "phase": "execution_efficiency",
            "source_main_revision": SOURCE_MAIN,
            "window": [START, END],
            "initial_capital": CAPITAL,
            "symbols": symbols,
            "production_change": "NONE",
            "future_data_in_production_decisions": False,
            "diagnostic_future_path_use": "Only retrospective sell-rebuy attribution uses prices after a sale.",
            "low_value_definition": "pure account_budget_trim sell bucket, next same-symbol buy within the retained 20-session churn horizon, and modeled direct costs plus reacquisition price effect exceed the interim downside avoided on matched shares",
            "new_production_thresholds": 0,
        },
        "source_identity_check": {
            "total_return_matches": math.isclose(
                float(result["total_return"]), EXPECTED_RETURN, rel_tol=0.0, abs_tol=1e-12
            ),
            "max_drawdown_matches": math.isclose(
                float(result["max_drawdown"]), EXPECTED_DRAWDOWN, rel_tol=0.0, abs_tol=1e-12
            ),
            "fills_match": int(result["total_trades"]) == EXPECTED_FILLS,
            "date_symbol_side_buckets_match": date_symbol_side_count(trades) == EXPECTED_BUCKETS,
            "rebuy_within_20_matches": chain_20 == EXPECTED_REBUY_20,
            "rebuy_within_60_matches": chain_60 == EXPECTED_REBUY_60,
            "observed": {
                "total_return": float(result["total_return"]),
                "max_drawdown": float(result["max_drawdown"]),
                "fills": int(result["total_trades"]),
                "date_symbol_side_buckets": date_symbol_side_count(trades),
                "sell_buckets": date_symbol_side_count(trades, direction="sell"),
                "sell_rebuy_chains": len(chains),
                "rebuy_within_20": chain_20,
                "rebuy_within_60": chain_60,
            },
        },
        "sell_rebuy_chain_attribution": {
            "categories": _category_summary(chains),
            "low_value_candidate_count": len(low_value),
            "low_value_candidate_net_chain_drag": sum(
                float(row["net_chain_drag"]) for row in low_value
            ),
            "low_value_candidate_modeled_direct_cost": sum(
                float(row["modeled_direct_cost"]) for row in low_value
            ),
            "low_value_candidate_avoided_downside_value": sum(
                float(row["avoided_downside_value"]) for row in low_value
            ),
            "top_low_value_candidates": sorted(
                low_value, key=lambda row: float(row["net_chain_drag"]), reverse=True
            )[:20],
        },
        "order_fragmentation": _multi_fill_summary(buckets),
        "partial_adjustments": _partial_reduction_summary(buckets),
        "decision_rules": {
            "preserve": [
                "hard-risk liquidation and non-AB5 strategy exits",
                "mixed sell buckets where a strategy/risk exit shares the same date-symbol-side action",
                "pure AB5 chains whose observed interim downside benefit covers their modeled execution/reacquisition drag"
            ],
            "research_candidates_only": [
                "pure AB5 short-horizon sell-rebuy chains with positive net chain drag",
                "multi-fill date-symbol-side fragmentation at a broker-boundary representation, only if internal sleeve accounting and existing execution semantics remain exact"
            ],
        },
        "limitations": [
            "The low-value classification is ex-post and uses the future rebuy and interim path; it is not a causal production rule and cannot be copied into live decisions.",
            "Avoided downside is a matched-share price-path attribution, not a guarantee of executable savings or a complete tail-risk measure.",
            "Internal sleeve fills are not verified broker orders. Multi-fill bucket consolidation is an operational research observation, not authorization to change the frozen execution semantics.",
            "A same-symbol future buy can belong to a different sleeve/strategy. The chain deliberately measures symbol-level churn because that is the economically relevant reacquisition path, while preserving reason details for interpretation."
        ],
    }
    pytest.fail(
        "EXECUTION_EFFICIENCY_JSON="
        + json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
