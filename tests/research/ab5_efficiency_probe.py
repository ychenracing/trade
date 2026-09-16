"""Temporary Phase-2 AB5 efficiency attribution and strength-curve probe.

This probe never changes AB5 trigger thresholds.  The 0.5 point scales only
already-planned AB5 intervention magnitude in the test process; it is a
research sensitivity point, not a production parameter proposal.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import contextlib
from dataclasses import replace
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
from quantfusion.domain.models import date_symbol_side_count
from quantfusion.engine import BacktestEngine
import quantfusion.engine.ensemble_allocation as ensemble_allocation
import quantfusion.engine.replay_loop as replay_loop
import quantfusion.risk.account_budget as account_budget
from quantfusion.research.winner_lifecycle import classify_winner_lifecycle

CAPITAL = 2_000_000.0
START = "2025-04-01"
END = "2026-07-20"
SOURCE_MAIN = "85cce57e370b1d8097f2931ffc4ea76c01ae7c86"
CURRENT_RETURN = 8.610542744515504
CURRENT_DRAWDOWN = -0.15104978428469945
AB5_OFF_RETURN = 12.651110582435127
AB5_OFF_DRAWDOWN = -0.16496270588646905
AB5_OFF_FILLS = 289
_ORIGINAL_PLAN = account_budget.plan_account_risk_budget
_ORIGINAL_APPLY = account_budget.apply_account_risk_budget


def _signal_key(signal: Any) -> tuple[Any, ...]:
    return (
        signal.direction,
        signal.symbol,
        signal.strategy_name,
        int(signal.target_shares),
        str(signal.signal_date),
        str(signal.reason),
    )


def _latest(series: Any, date: pd.Timestamp) -> float | None:
    if series is None:
        return None
    values = series.loc[series.index <= date].dropna()
    if values.empty:
        return None
    value = float(values.iloc[-1])
    return value if math.isfinite(value) else None


def _trailing_return(frame: pd.DataFrame, date: pd.Timestamp, sessions: int = 60) -> float | None:
    closes = frame.loc[frame.index <= date, "close"]
    if len(closes) <= sessions:
        return None
    first, last = float(closes.iloc[-sessions - 1]), float(closes.iloc[-1])
    if first <= 0 or not math.isfinite(first) or not math.isfinite(last):
        return None
    return last / first - 1.0


def _relative_strength(symbol: str, date: pd.Timestamp, frames: dict[str, pd.DataFrame]) -> float | None:
    target = _trailing_return(frames[symbol], date)
    peers = [value for frame in frames.values() if (value := _trailing_return(frame, date)) is not None]
    if target is None or not peers:
        return None
    return target - float(statistics.median(peers))


def _hold_sessions(frame: pd.DataFrame, date: pd.Timestamp, entry_date: str) -> int | None:
    entry = pd.Timestamp(entry_date)
    available = frame.index[frame.index <= date]
    if entry not in frame.index or len(available) == 0:
        return None
    return int(frame.index.get_loc(available[-1])) - int(frame.index.get_loc(entry))


def _market_state(event: dict[str, Any]) -> str:
    if event.get("observed_shock_confirmed") is True:
        return "observed_shock_confirmed"
    if event.get("risk_alert_active") is True:
        return "risk_alert_active"
    if event.get("shock_episode_active") is True:
        return "shock_episode_active"
    return "ordinary_capacity"


def _position_snapshot(state: Any, signal: Any, date: pd.Timestamp) -> dict[str, Any] | None:
    positions = state.sleeve.positions.get(signal.symbol, {})
    position = positions.get(signal.strategy_name)
    if position is None or int(position.shares) <= 0:
        return None
    frame = state.data_map.get(signal.symbol)
    if frame is None:
        return None
    close = float(state.sleeve._latest_close_on_or_before(frame, date))
    symbol_cfg = getattr(state.sleeve, "symbol_configs", {}).get(signal.symbol, state.sleeve.cfg)
    lifecycle = classify_winner_lifecycle(position, close=close, cfg=symbol_cfg)
    indicators = getattr(state, "indicator_map", {}).get(signal.symbol, {})
    ma_short = _latest(indicators.get("ma_short"), date)
    ma_long = _latest(indicators.get("ma_long"), date)
    adx = _latest(indicators.get("adx"), date)
    rs60 = _relative_strength(signal.symbol, date, state.data_map)
    trend_intact = bool(
        ma_short is not None
        and ma_long is not None
        and close > ma_short
        and ma_short > ma_long
    )
    relative_strength_leading = bool(rs60 is not None and rs60 > 0.0)
    super_winner = bool(
        lifecycle.strategic_winner and trend_intact and relative_strength_leading
    )
    return {
        "close": close,
        "entry_price": float(position.entry_price),
        "current_gain": lifecycle.current_gain,
        "peak_gain": lifecycle.peak_gain,
        "profit_lock_active": lifecycle.profit_lock_active,
        "profit_lock_intact": lifecycle.profit_lock_intact,
        "hold_sessions": _hold_sessions(frame, date, str(position.entry_date)),
        "ma_short": ma_short,
        "ma_long": ma_long,
        "adx": adx,
        "trend_intact": trend_intact,
        "relative_strength_60": rs60,
        "relative_strength_leading": relative_strength_leading,
        "strategic_winner": lifecycle.strategic_winner,
        "super_winner": super_winner,
        "pnl_stage": "profitable" if lifecycle.current_gain > 0.0 else "flat_or_losing",
        "lifecycle_class": "super_winner" if super_winner else "ordinary_or_deteriorating",
    }


def _scaled_plan(intensity: float):
    if not 0.0 <= intensity <= 1.0:
        raise ValueError("AB5 research intensity must be within [0, 1]")

    def wrapped(*args: Any, **kwargs: Any):
        receipt, actions = _ORIGINAL_PLAN(*args, **kwargs)
        if intensity == 1.0:
            return receipt, actions
        scaled = dict(receipt)
        original_scales = [float(value) for value in receipt.get("buy_scales", [])]
        scaled_scales = [1.0 - intensity * (1.0 - value) for value in original_scales]
        scaled["buy_scales"] = scaled_scales
        scaled["buy_scale"] = min(scaled_scales, default=1.0)
        scaled_actions = []
        for action in actions:
            shares = int(math.floor(int(action.shares) * intensity / 100.0) * 100)
            if shares > 0:
                scaled_actions.append(replace(action, shares=shares))
        return scaled, scaled_actions

    return wrapped


def _observer(records: dict[str, list[dict[str, Any]]]):
    def wrapped(
        states: Any,
        date: pd.Timestamp,
        assets: float,
        peak: float,
        cfg: Any,
        score: Any,
        events: list[dict[str, Any]],
        **kwargs: Any,
    ) -> None:
        before = [
            {_signal_key(signal) for signal, _ in state.pending}
            for state in states
        ]
        _ORIGINAL_APPLY(states, date, assets, peak, cfg, score, events, **kwargs)
        event = next(
            (
                item for item in reversed(events)
                if item.get("event") == "account_budget_envelope"
                and item.get("mechanism") == "AB5"
                and item.get("date") == date.strftime("%Y-%m-%d")
            ),
            None,
        )
        if event is None:
            return
        records["events"].append({
            "date": str(event.get("date")),
            "market_state": _market_state(event),
            "risk_alert_active": bool(event.get("risk_alert_active")),
            "observed_shock_confirmed": bool(event.get("observed_shock_confirmed")),
            "shock_episode_active": bool(event.get("shock_episode_active")),
            "gross_before": event.get("gross_before"),
            "gross_cap": event.get("gross_cap"),
            "buy_shares_removed": int(event.get("buy_shares_removed", 0) or 0),
            "new_reduction_orders": int(event.get("new_reduction_orders", 0) or 0),
            "remaining_loss_budget": event.get("remaining_loss_budget"),
            "held_stress_debit": event.get("held_stress_debit"),
        })
        for state_index, state in enumerate(states):
            for signal, _ in state.pending:
                if (
                    signal.direction != "sell"
                    or signal.reason != "account_budget_trim"
                    or _signal_key(signal) in before[state_index]
                ):
                    continue
                snapshot = _position_snapshot(state, signal, date)
                if snapshot is None:
                    continue
                records["actions"].append({
                    "date": date.strftime("%Y-%m-%d"),
                    "month": date.strftime("%Y-%m"),
                    "symbol": signal.symbol,
                    "strategy": signal.strategy_name,
                    "shares": int(signal.target_shares),
                    "action_value": int(signal.target_shares) * float(snapshot["close"]),
                    "market_state": _market_state(event),
                    **snapshot,
                })

    return wrapped


def _metrics(result: dict[str, Any]) -> dict[str, Any]:
    trades = list(result["trades"])
    equity = result["equity_curve"].copy().sort_index()
    mean_assets = float(equity["assets"].mean())
    gross = sum(abs(float(trade.gross_value)) for trade in trades)
    flows: dict[str, float] = defaultdict(float)
    for trade in trades:
        flows[str(trade.date)] += float(trade.net_cash_flow)
    cash = CAPITAL
    cash_fractions: list[float] = []
    for timestamp, row in equity.iterrows():
        cash += flows.get(pd.Timestamp(timestamp).strftime("%Y-%m-%d"), 0.0)
        assets = float(row["assets"])
        if assets > 0:
            cash_fractions.append(cash / assets)
    mean_cash = float(statistics.mean(cash_fractions)) if cash_fractions else None
    return {
        "final_assets": float(result["final_assets"]),
        "total_return": float(result["total_return"]),
        "max_drawdown": float(result["max_drawdown"]),
        "sharpe": float(result["sharpe"]),
        "calmar": float(result["calmar"]),
        "fills": int(result["total_trades"]),
        "date_symbol_side_buckets": date_symbol_side_count(trades),
        "sell_buckets": date_symbol_side_count(trades, direction="sell"),
        "gross_traded_value": gross,
        "turnover_over_mean_assets": gross / mean_assets if mean_assets > 0 else None,
        "explicit_fees": sum(float(t.commission) + float(t.stamp_duty_cost) for t in trades),
        "mean_cash_fraction": mean_cash,
        "mean_invested_fraction": None if mean_cash is None else 1.0 - mean_cash,
        "cash_fraction_gt_50pct": (
            sum(value > 0.5 for value in cash_fractions) / len(cash_fractions)
            if cash_fractions else None
        ),
    }


def _run(names: dict[str, str], *, intensity: float, observe: bool) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    records: dict[str, list[dict[str, Any]]] = {"events": [], "actions": []}
    patches = [
        mock.patch.object(account_budget, "plan_account_risk_budget", new=_scaled_plan(intensity)),
    ]
    if observe:
        observer = _observer(records)
        patches.extend([
            mock.patch.object(ensemble_allocation, "apply_account_risk_budget", new=observer),
            mock.patch.object(replay_loop, "apply_account_risk_budget", new=observer),
        ])
    with contextlib.ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        with contextlib.redirect_stdout(io.StringIO()):
            result = BacktestEngine(CAPITAL).run(
                names, START, END, data_dir=str(MARKET_DATA_DIR), indicator_state="warm"
            )
    return result, records


def _sum_by(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row[key])].append(row)
    return {
        label: {
            "actions": len(items),
            "shares": sum(int(item["shares"]) for item in items),
            "action_value": sum(float(item["action_value"]) for item in items),
            "median_current_gain": float(statistics.median(float(item["current_gain"]) for item in items)),
        }
        for label, items in sorted(grouped.items())
    }


def _attribution(records: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    actions = records["actions"]
    events = records["events"]
    super_rows = [row for row in actions if row["super_winner"]]
    return {
        "envelope_events": len(events),
        "events_with_buy_clipping": sum(int(row["buy_shares_removed"]) > 0 for row in events),
        "events_with_new_reduction_orders": sum(int(row["new_reduction_orders"]) > 0 for row in events),
        "total_buy_shares_removed": sum(int(row["buy_shares_removed"]) for row in events),
        "new_trim_actions_observed": len(actions),
        "unique_date_symbol_strategy_actions": len({(r["date"], r["symbol"], r["strategy"]) for r in actions}),
        "unique_date_symbol_actions": len({(r["date"], r["symbol"]) for r in actions}),
        "lifecycle_counts": dict(Counter(str(row["lifecycle_class"]) for row in actions)),
        "pnl_stage_counts": dict(Counter(str(row["pnl_stage"]) for row in actions)),
        "market_state_counts": dict(Counter(str(row["market_state"]) for row in actions)),
        "by_symbol": _sum_by(actions, "symbol"),
        "by_month": _sum_by(actions, "month"),
        "by_market_state": _sum_by(actions, "market_state"),
        "by_lifecycle": _sum_by(actions, "lifecycle_class"),
        "super_winner_action_value_share": (
            sum(float(row["action_value"]) for row in super_rows)
            / sum(float(row["action_value"]) for row in actions)
            if actions else None
        ),
        "top_super_winner_interventions": sorted(
            super_rows, key=lambda row: float(row["action_value"]), reverse=True
        )[:15],
        "top_all_interventions": sorted(
            actions, key=lambda row: float(row["action_value"]), reverse=True
        )[:15],
    }


def emit_receipt() -> None:
    symbols = list(next(codes for codes in VALIDATION_UNIVERSES.values() if len(codes) == 17))
    names = {symbol: SYMBOL_NAMES[symbol] for symbol in symbols}
    current, records = _run(names, intensity=1.0, observe=True)
    half, _ = _run(names, intensity=0.5, observe=False)
    current_metrics = _metrics(current)
    half_metrics = _metrics(half)
    receipt = {
        "research_contract": {
            "phase": "ab5_efficiency",
            "source_main_revision": SOURCE_MAIN,
            "window": [START, END],
            "initial_capital": CAPITAL,
            "symbols": symbols,
            "production_change": "NONE",
            "trigger_threshold_changes": 0,
            "strength_definition": "scale only magnitude of already-planned AB5 buy clipping and trim shares; triggers and source risk thresholds unchanged",
            "tested_intermediate_intensity": 0.5,
            "production_parameter_proposal": False,
        },
        "source_identity_check": {
            "current_total_return_matches": math.isclose(current_metrics["total_return"], CURRENT_RETURN, rel_tol=0.0, abs_tol=1e-10),
            "current_max_drawdown_matches": math.isclose(current_metrics["max_drawdown"], CURRENT_DRAWDOWN, rel_tol=0.0, abs_tol=1e-12),
            "observed_current_total_return": current_metrics["total_return"],
            "observed_current_max_drawdown": current_metrics["max_drawdown"],
        },
        "strength_curve": {
            "0.0_prior_direct_ab5_disabled_reference": {
                "total_return": AB5_OFF_RETURN,
                "max_drawdown": AB5_OFF_DRAWDOWN,
                "fills": AB5_OFF_FILLS,
                "note": "retained prior direct counterfactual; AB5 disabled, other risk logic active",
            },
            "0.5_magnitude_sensitivity": half_metrics,
            "1.0_current_ab5": current_metrics,
        },
        "half_minus_current": {
            key: half_metrics[key] - current_metrics[key]
            for key in (
                "total_return", "max_drawdown", "sharpe", "calmar", "fills",
                "date_symbol_side_buckets", "sell_buckets", "gross_traded_value",
                "turnover_over_mean_assets", "explicit_fees", "mean_cash_fraction",
                "mean_invested_fraction", "cash_fraction_gt_50pct",
            )
            if half_metrics[key] is not None and current_metrics[key] is not None
        },
        "ab5_lifecycle_attribution": _attribution(records),
        "lifecycle_definition": {
            "strategic_winner": "existing profit-lock active and current gain positive",
            "super_winner": "strategic winner AND close above short MA AND short MA above long MA AND close-known 60-session return exceeds fixed-universe median",
            "new_numeric_thresholds": 0,
        },
        "limitations": [
            "The 0.5 point is a sensitivity study, not a production control proposal and not a search over a parameter grid.",
            "The 0.0 endpoint is the retained direct AB5-disabled counterfactual rather than the transformed-plan wrapper at intensity zero; it is labeled separately to avoid claiming implementation identity.",
            "AB5 attribution observes newly queued account_budget_trim instructions. TradeRecord/fill counts remain internal sleeve fills and are not claimed to be broker orders.",
        ],
    }
    pytest.fail("AB5_EFFICIENCY_JSON=" + json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
