"""Temporary Phase-1 winner-lifecycle counterfactual probe.

The probe reuses the canonical engine and existing profit-lock controls.  It is
removed after the JSON receipt is recovered and never becomes production code.
"""
from __future__ import annotations

from collections import Counter
import contextlib
import io
import json
import math
from pathlib import Path
import statistics
from typing import Any
from unittest import mock

import pandas as pd
import pytest

from quantfusion.config.paths import MARKET_DATA_DIR
from quantfusion.config.universe import SYMBOL_NAMES, VALIDATION_UNIVERSES
from quantfusion.engine import BacktestEngine
from quantfusion.research.winner_lifecycle import (
    classify_winner_lifecycle,
    should_defer_atr_trailing_exit,
)
from quantfusion.strategy.trend import BaseStrategy

CAPITAL = 2_000_000.0
START = "2025-04-01"
END = "2026-07-20"
EXPECTED_RETURN = 8.610542744542613
EXPECTED_DRAWDOWN = -0.15104978428469945
_ORIGINAL_MAKE_SELL = BaseStrategy._make_sell_signal


def _load_frames(symbols: list[str]) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        frame = pd.read_csv(Path(MARKET_DATA_DIR) / f"{symbol}.csv", parse_dates=["date"])
        frame = frame.set_index("date").sort_index()
        frames[symbol] = frame.loc[frame.index <= pd.Timestamp(END)].copy()
    return frames


def _indicator(ctx: Any, name: str) -> float | None:
    series = ctx.indicators.get(name)
    if series is None:
        return None
    value = series.iloc[ctx.i]
    if pd.isna(value) or not math.isfinite(float(value)):
        return None
    return float(value)


def _hold_sessions(ctx: Any, entry_date: str) -> int | None:
    index = ctx.df.index
    timestamp = pd.Timestamp(entry_date)
    if timestamp not in index:
        return None
    return int(ctx.i) - int(index.get_loc(timestamp))


def _observer(records: list[dict[str, Any]], *, defer_atr: bool):
    def wrapped(self: BaseStrategy, ctx: Any, reason: str):
        position = self.position
        close = float(ctx.df["close"].iloc[ctx.i])
        if position is not None:
            state = classify_winner_lifecycle(position, close=close, cfg=self.cfg)
            ma_short = _indicator(ctx, "ma_short")
            ma_long = _indicator(ctx, "ma_long")
            adx = _indicator(ctx, "adx")
            record = {
                "date": str(ctx.date),
                "symbol": str(ctx.symbol),
                "strategy": str(self.name),
                "reason": str(reason),
                "close": close,
                "entry_price": float(position.entry_price),
                "hold_sessions": _hold_sessions(ctx, str(position.entry_date)),
                "peak_gain": state.peak_gain,
                "current_gain": state.current_gain,
                "giveback_from_peak": state.giveback_from_peak,
                "strategic_winner": state.strategic_winner,
                "profit_lock_active": state.profit_lock_active,
                "profit_lock_intact": state.profit_lock_intact,
                "close_above_ma_short": None if ma_short is None else close > ma_short,
                "close_above_ma_long": None if ma_long is None else close > ma_long,
                "ma_short_above_long": None if ma_short is None or ma_long is None else ma_short > ma_long,
                "adx": adx,
            }
            deferred = defer_atr and should_defer_atr_trailing_exit(
                position, close=close, reason=reason, cfg=self.cfg
            )
            record["deferred"] = bool(deferred)
            records.append(record)
            if deferred:
                return None
        return _ORIGINAL_MAKE_SELL(self, ctx, reason)

    return wrapped


def _run(names: dict[str, str], *, defer_atr: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    with mock.patch.object(BaseStrategy, "_make_sell_signal", new=_observer(records, defer_atr=defer_atr)):
        with contextlib.redirect_stdout(io.StringIO()):
            result = BacktestEngine(CAPITAL).run(
                names, START, END, data_dir=str(MARKET_DATA_DIR), indicator_state="warm"
            )
    return result, records


def _metrics(result: dict[str, Any]) -> dict[str, Any]:
    trades = list(result["trades"])
    equity = result["equity_curve"]
    mean_assets = float(equity["assets"].mean())
    gross = sum(abs(float(trade.gross_value)) for trade in trades)
    return {
        "final_assets": float(result["final_assets"]),
        "total_return": float(result["total_return"]),
        "max_drawdown": float(result["max_drawdown"]),
        "sharpe": float(result["sharpe"]),
        "calmar": float(result["calmar"]),
        "fills": int(result["total_trades"]),
        "sell_fills": int(result["sell_trades"]),
        "date_symbol_side_buckets": int(result["date_symbol_side_count"]),
        "sell_buckets": int(result["date_symbol_sell_side_count"]),
        "gross_traded_value": gross,
        "turnover_over_mean_assets": gross / mean_assets if mean_assets > 0 else None,
        "explicit_fees": sum(float(t.commission) + float(t.stamp_duty_cost) for t in trades),
    }


def _trailing_return(frame: pd.DataFrame, day: str, sessions: int = 60) -> float | None:
    history = frame.loc[frame.index <= pd.Timestamp(day), "close"]
    if len(history) <= sessions:
        return None
    first, last = float(history.iloc[-sessions - 1]), float(history.iloc[-1])
    if first <= 0:
        return None
    return last / first - 1.0


def _relative_strength(symbol: str, day: str, frames: dict[str, pd.DataFrame]) -> float | None:
    target = _trailing_return(frames[symbol], day)
    peers = [value for value in (_trailing_return(frame, day) for frame in frames.values()) if value is not None]
    if target is None or not peers:
        return None
    return target - float(statistics.median(peers))


def _median(values: list[float]) -> float | None:
    finite = [float(v) for v in values if math.isfinite(float(v))]
    return float(statistics.median(finite)) if finite else None


def _lifecycle_summary(records: list[dict[str, Any]], frames: dict[str, pd.DataFrame]) -> dict[str, Any]:
    enriched: list[dict[str, Any]] = []
    for record in records:
        item = dict(record)
        item["relative_strength_60"] = _relative_strength(item["symbol"], item["date"], frames)
        enriched.append(item)
    strategic = [item for item in enriched if item["strategic_winner"]]
    ordinary = [item for item in enriched if not item["strategic_winner"]]
    deferred = [item for item in enriched if item["deferred"]]

    def group(items: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "signals": len(items),
            "hold_sessions_median": _median([x["hold_sessions"] for x in items if x["hold_sessions"] is not None]),
            "current_gain_median": _median([x["current_gain"] for x in items]),
            "peak_gain_median": _median([x["peak_gain"] for x in items]),
            "relative_strength_60_median": _median([x["relative_strength_60"] for x in items if x["relative_strength_60"] is not None]),
            "close_above_ma_short_rate": (sum(x["close_above_ma_short"] is True for x in items) / len(items)) if items else None,
            "ma_short_above_long_rate": (sum(x["ma_short_above_long"] is True for x in items) / len(items)) if items else None,
        }

    return {
        "state_definition": "strategic_winner = existing profit-lock active and current gain positive; otherwise ordinary_or_unvalidated",
        "ordinary_or_unvalidated": group(ordinary),
        "strategic_winner": group(strategic),
        "deferred_atr": group(deferred),
        "reason_counts": dict(Counter(item["reason"].split("@")[0] for item in enriched)),
        "deferred_unique_date_symbol_strategy": len({(x["date"], x["symbol"], x["strategy"]) for x in deferred}),
        "top_strategic_examples": sorted(strategic, key=lambda x: x["peak_gain"], reverse=True)[:10],
    }


def _rebuy_summary(result: dict[str, Any], frames: dict[str, pd.DataFrame]) -> dict[str, Any]:
    buckets = sorted({(t.date, t.symbol, t.direction) for t in result["trades"]})
    sells = [item for item in buckets if item[2] == "sell"]
    gaps: list[int] = []
    for sell_day, symbol, _ in sells:
        next_buy = next((day for day, code, side in buckets if code == symbol and side == "buy" and day > sell_day), None)
        if next_buy is None:
            continue
        frame = frames[symbol]
        left, right = pd.Timestamp(sell_day), pd.Timestamp(next_buy)
        if left in frame.index and right in frame.index:
            gaps.append(int(frame.index.get_loc(right)) - int(frame.index.get_loc(left)))
    return {
        "sell_buckets": len(sells),
        "with_later_rebuy": len(gaps),
        "rebuy_within_20_sessions": sum(gap <= 20 for gap in gaps),
        "rebuy_within_60_sessions": sum(gap <= 60 for gap in gaps),
        "rebuy_gap_median": _median([float(gap) for gap in gaps]),
    }


def emit_receipt() -> None:
    symbols = list(next(codes for codes in VALIDATION_UNIVERSES.values() if len(codes) == 17))
    names = {symbol: SYMBOL_NAMES[symbol] for symbol in symbols}
    frames = _load_frames(symbols)
    current, current_records = _run(names, defer_atr=False)
    candidate, candidate_records = _run(names, defer_atr=True)
    current_metrics, candidate_metrics = _metrics(current), _metrics(candidate)
    receipt = {
        "research_contract": {
            "phase": "winner_lifecycle",
            "source_main_revision": "85cce57e370b1d8097f2931ffc4ea76c01ae7c86",
            "window": [START, END],
            "initial_capital": CAPITAL,
            "symbols": symbols,
            "production_change": "NONE",
            "candidate_rule": "defer ATR trailing sell only while the existing per-strategy profit-lock is active and intact",
            "new_thresholds": 0,
            "future_data_in_candidate": False,
            "hard_and_structural_risk_actions_preserved": True,
        },
        "source_identity_check": {
            "matches_prior_total_return": math.isclose(current_metrics["total_return"], EXPECTED_RETURN, rel_tol=0.0, abs_tol=1e-12),
            "matches_prior_max_drawdown": math.isclose(current_metrics["max_drawdown"], EXPECTED_DRAWDOWN, rel_tol=0.0, abs_tol=1e-12),
        },
        "current": current_metrics,
        "candidate": candidate_metrics,
        "candidate_minus_current": {
            key: candidate_metrics[key] - current_metrics[key]
            for key in ("total_return", "max_drawdown", "sharpe", "calmar", "fills", "sell_fills", "date_symbol_side_buckets", "sell_buckets", "gross_traded_value", "turnover_over_mean_assets", "explicit_fees")
        },
        "current_lifecycle": _lifecycle_summary(current_records, frames),
        "candidate_lifecycle": _lifecycle_summary(candidate_records, frames),
        "current_sell_rebuy": _rebuy_summary(current, frames),
        "candidate_sell_rebuy": _rebuy_summary(candidate, frames),
        "exit_attribution_reference": "artifacts/diagnostics/alpha-attribution/02-sell-analysis.json",
        "limitations": [
            "Lifecycle state uses close-known position/indicator state only; 60-session relative strength uses only prices on or before the signal close.",
            "Existing future-return exit attribution remains diagnostic-only in the referenced prior artifact and is not used by this candidate.",
            "TradeRecord fills are internal sleeve fills; date-symbol-side buckets are reported separately and are not claimed to be broker orders.",
        ],
    }
    pytest.fail("WINNER_LIFECYCLE_JSON=" + json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
