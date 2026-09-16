"""Temporary Phase-2 lifecycle-aware AB5 trim-source reallocation probe.

The candidate does not change AB5 triggers, caps, stress math, buy scales,
required relief, shock handling or alert handling.  It only layers the existing
ordinary trim-source score so symbols containing a close-known super-winner
book are sourced after other symbols.  Winners remain reducible when required.
"""
from __future__ import annotations

from collections import Counter
import contextlib
import io
import json
from pathlib import Path
import runpy
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pandas as pd
import pytest

from quantfusion.config.paths import MARKET_DATA_DIR
from quantfusion.config.universe import SYMBOL_NAMES, VALIDATION_UNIVERSES
from quantfusion.engine import BacktestEngine
import quantfusion.engine.ensemble_allocation as ensemble_allocation
import quantfusion.engine.replay_loop as replay_loop
import quantfusion.risk.account_budget as account_budget

_HELPERS = runpy.run_path(str(Path(__file__).with_name("ab5_efficiency_probe.py")))
CAPITAL = _HELPERS["CAPITAL"]
START = _HELPERS["START"]
END = _HELPERS["END"]
SOURCE_MAIN = _HELPERS["SOURCE_MAIN"]
_metrics = _HELPERS["_metrics"]
_market_state = _HELPERS["_market_state"]
_position_snapshot = _HELPERS["_position_snapshot"]
_signal_key = _HELPERS["_signal_key"]
_ORIGINAL_APPLY = account_budget.apply_account_risk_budget

CURRENT = {
    "final_assets": 19221085.48903101,
    "total_return": 8.610542744515504,
    "max_drawdown": -0.15104978428469945,
    "sharpe": 3.7258627517994465,
    "calmar": 33.61339205407966,
    "fills": 645,
    "date_symbol_side_buckets": 190,
    "sell_buckets": 109,
    "gross_traded_value": 139414683.29,
    "turnover_over_mean_assets": 16.00753130920448,
    "explicit_fees": 74192.392969,
    "mean_cash_fraction": 0.2987438578983292,
    "mean_invested_fraction": 0.7012561421016708,
    "cash_fraction_gt_50pct": 0.3069620253164557,
}


def _winner_symbols(states: Any, date: pd.Timestamp) -> set[str]:
    winners: set[str] = set()
    for state in states:
        for symbol, positions in state.sleeve.positions.items():
            for strategy, position in positions.items():
                if int(position.shares) <= 0:
                    continue
                signal = SimpleNamespace(symbol=symbol, strategy_name=strategy)
                snapshot = _position_snapshot(state, signal, date)
                if snapshot is not None and snapshot["super_winner"]:
                    winners.add(symbol)
                    break
    return winners


def _candidate_apply(records: list[dict[str, Any]]):
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
        winners = _winner_symbols(states, date)
        held_symbols = sorted({
            symbol
            for state in states
            for symbol, positions in state.sleeve.positions.items()
            if any(int(position.shares) > 0 for position in positions.values())
        })
        base_scores = {symbol: float(score(symbol)) for symbol in held_symbols}
        ordered = sorted(
            held_symbols,
            key=lambda symbol: (symbol in winners, base_scores[symbol], symbol),
        )
        rank = {symbol: index for index, symbol in enumerate(ordered)}

        def lifecycle_score(symbol: str) -> float:
            return float(rank.get(symbol, len(rank)))

        before = [
            {_signal_key(signal) for signal, _ in state.pending}
            for state in states
        ]
        _ORIGINAL_APPLY(
            states, date, assets, peak, cfg, lifecycle_score, events, **kwargs
        )
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
        market_state = _market_state(event)
        for state_index, state in enumerate(states):
            for signal, _ in state.pending:
                if (
                    signal.direction != "sell"
                    or signal.reason != "account_budget_trim"
                    or _signal_key(signal) in before[state_index]
                ):
                    continue
                snapshot = _position_snapshot(state, signal, date)
                records.append({
                    "date": date.strftime("%Y-%m-%d"),
                    "symbol": signal.symbol,
                    "strategy": signal.strategy_name,
                    "shares": int(signal.target_shares),
                    "winner_symbol_at_decision": signal.symbol in winners,
                    "market_state": market_state,
                    "action_value": (
                        None
                        if snapshot is None
                        else int(signal.target_shares) * float(snapshot["close"])
                    ),
                })

    return wrapped


def _run_candidate(names: dict[str, str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    candidate = _candidate_apply(records)
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            mock.patch.object(ensemble_allocation, "apply_account_risk_budget", new=candidate)
        )
        stack.enter_context(
            mock.patch.object(replay_loop, "apply_account_risk_budget", new=candidate)
        )
        with contextlib.redirect_stdout(io.StringIO()):
            result = BacktestEngine(CAPITAL).run(
                names, START, END, data_dir=str(MARKET_DATA_DIR), indicator_state="warm"
            )
    return result, records


def emit_receipt() -> None:
    symbols = list(next(codes for codes in VALIDATION_UNIVERSES.values() if len(codes) == 17))
    names = {symbol: SYMBOL_NAMES[symbol] for symbol in symbols}
    result, records = _run_candidate(names)
    candidate = _metrics(result)
    numeric_keys = (
        "final_assets", "total_return", "max_drawdown", "sharpe", "calmar",
        "fills", "date_symbol_side_buckets", "sell_buckets",
        "gross_traded_value", "turnover_over_mean_assets", "explicit_fees",
        "mean_cash_fraction", "mean_invested_fraction", "cash_fraction_gt_50pct",
    )
    winner_actions = [row for row in records if row["winner_symbol_at_decision"]]
    ordinary_actions = [row for row in records if not row["winner_symbol_at_decision"]]
    receipt = {
        "research_contract": {
            "phase": "ab5_lifecycle_reallocation",
            "source_main_revision": SOURCE_MAIN,
            "window": [START, END],
            "initial_capital": CAPITAL,
            "symbols": symbols,
            "production_change": "NONE",
            "new_thresholds": 0,
            "ab5_trigger_changes": 0,
            "ab5_cap_or_stress_changes": 0,
            "ab5_required_relief_changes": 0,
            "shock_or_alert_semantics_changes": 0,
            "candidate": "ordinary AB5 trim sourcing preserves original score order within lifecycle class and sources symbols containing a close-known super-winner after other symbols",
            "winner_fallback_reducible": True,
        },
        "current_retained_reference": CURRENT,
        "candidate": candidate,
        "candidate_minus_current": {
            key: candidate[key] - CURRENT[key]
            for key in numeric_keys
            if candidate[key] is not None and CURRENT[key] is not None
        },
        "trim_attribution": {
            "new_trim_actions": len(records),
            "winner_symbol_trim_actions": len(winner_actions),
            "ordinary_symbol_trim_actions": len(ordinary_actions),
            "winner_symbol_trim_action_value": sum(
                float(row["action_value"]) for row in winner_actions
                if row["action_value"] is not None
            ),
            "ordinary_symbol_trim_action_value": sum(
                float(row["action_value"]) for row in ordinary_actions
                if row["action_value"] is not None
            ),
            "market_state_counts": dict(Counter(str(row["market_state"]) for row in records)),
            "winner_symbol_market_state_counts": dict(
                Counter(str(row["market_state"]) for row in winner_actions)
            ),
        },
        "causality": {
            "winner_state": "close-known existing profit-lock + current trend + close-known 60-session relative strength",
            "future_data_in_candidate": False,
            "score_scope_verified_in_source": "plan_account_risk_budget score callback is consumed only in the ordinary non-alert/non-shock trim-source ordering branch",
        },
        "limitations": [
            "Winner classification is symbol-level for ordering because the existing AB5 score callback is symbol-level; a symbol is winner-last when at least one live strategy book is a close-known super-winner.",
            "The candidate preserves the planner's required relief mechanics; later portfolio state can still differ causally because different books are trimmed first.",
            "Fill counts are internal sleeve fills and are not claimed to be broker orders.",
        ],
    }
    pytest.fail(
        "AB5_LIFECYCLE_REALLOCATION_JSON="
        + json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
