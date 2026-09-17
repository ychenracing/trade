"""Read-only account risk-budget observation for the current production policy.

AB5 remains an account-level risk measurement and evidence mechanism. This
module inspects qualified close state and reuses the canonical budget planner,
but it has no execution adapter and never changes pending orders, cash,
holdings, or risk-manager state.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pandas as pd

from quantfusion.config.engine import EARLY_DUAL_TRANSITION_RSI_MAX
from quantfusion.domain.rules import limit_pct_for_code, require_finite, require_int
from quantfusion.risk.account_budget import (
    observed_direct_losses,
    observed_shock_stress,
    plan_account_risk_budget,
)

POLICY_MODE = "OBSERVE_ONLY"
HEALTH_EVALUATED = "EVALUATED"


def _latest_risk_alert(events: Sequence[Mapping[str, Any]]) -> bool:
    latest = next(
        (
            event
            for event in reversed(events)
            if event.get("event")
            in {"portfolio_drawdown_alert_on", "portfolio_drawdown_alert_off"}
            and event.get("sleeve") in {None, "portfolio"}
        ),
        None,
    )
    return bool(latest and latest.get("event") == "portfolio_drawdown_alert_on")


def observe_account_risk_budget(
    states: Sequence[Any],
    date: pd.Timestamp,
    assets: float,
    peak: float,
    cfg: Mapping[str, Any],
    score: Callable[[str], float],
    events: list[dict[str, Any]],
    *,
    shock_floor: float = 0.0,
    preserve_strategy_valid_holdings: bool = False,
    risk_alert_active: bool | None = None,
    portfolio_evidence_buy_symbols: set[str] | None = None,
) -> dict[str, Any]:
    """Evaluate canonical AB5 evidence without changing execution state.

    Observation reconstructs the same close-known evidence that the former
    execution adapter supplied to the pure planner. Historical filled AB5
    actions remain facts when they are already present in a supplied account
    history, while counterfactual current plans are never applied to a queue.
    """
    if risk_alert_active is None:
        risk_alert_active = _latest_risk_alert(events)

    date_str = date.strftime("%Y-%m-%d")
    evidence_symbols = (
        None
        if portfolio_evidence_buy_symbols is None
        else set(portfolio_evidence_buy_symbols)
    )
    if evidence_symbols is not None and any(
        not isinstance(symbol, str) or not symbol for symbol in evidence_symbols
    ):
        raise ValueError("portfolio evidence buy symbols must be non-empty strings")

    previous_handoff_symbols = set(
        next(
            (
                event.get("strategy_handoff_symbols", ())
                for event in reversed(events)
                if event.get("event") == "account_budget_envelope"
            ),
            (),
        )
    )
    observed_shock_reduction_dates = {
        str(event["date"])
        for event in events
        if event.get("event") == "account_budget_envelope"
        and event.get("observed_shock_confirmed") is True
        and event.get("new_reduction_orders", 0)
        and event.get("date")
    }
    crowded_shock_reduction_dates = {
        str(event["date"])
        for event in events
        if str(event.get("date", "")) in observed_shock_reduction_dates
        and event.get("crowded_portfolio") is True
    }
    strategy_handoff_symbols = {
        trade.symbol
        for state in states
        for trade in state.sleeve.trades
        if trade.date == date_str
        and trade.direction == "sell"
        and trade.strategy_name.split(":")[-1] == "turtle_breakout"
        and trade.reason.startswith("Donchian exit")
    }

    books: list[tuple[int, str, str, int, float]] = []
    buys: list[tuple[int, Any, float]] = []
    excluded_buy_slots: list[list[int]] = []
    weak_book_ids: set[tuple[int, str, str]] = set()
    protected_handoff_book_ids: set[tuple[int, str, str]] = set()
    protected_proven_dual_book_ids: set[tuple[int, str, str]] = set()
    repeated_proven_reentry_symbols: set[str] = set()
    proven_early_dual_book_ids: set[tuple[int, str, str]] = set()
    shock_reduced_book_ids: set[tuple[int, str, str]] = set()
    confirmed_shock_reduced_book_ids: set[tuple[int, str, str]] = set()
    crowded_shock_reduced_book_ids: set[tuple[int, str, str]] = set()
    costs = dict(cfg)

    for state_index, state in enumerate(states):
        sleeve = state.sleeve
        for key in ("slippage", "commission_rate", "stamp_duty", "min_commission"):
            costs[key] = max(float(costs[key]), float(sleeve.cfg[key]))

        cycle_shares: dict[tuple[str, str], int] = {}
        confirmed_live_cycles: set[tuple[str, str]] = set()
        shock_reduced_live_cycles: set[tuple[str, str]] = set()
        confirmed_shock_reduced_live_cycles: set[tuple[str, str]] = set()
        crowded_shock_reduced_live_cycles: set[tuple[str, str]] = set()
        for trade in sleeve.trades:
            if trade.date > date_str:
                continue
            strategy_name = trade.strategy_name.split(":")[-1]
            cycle_id = (trade.symbol, strategy_name)
            quantity = require_int("historical cycle shares", trade.shares, min_value=1)
            before = cycle_shares.get(cycle_id, 0)
            if trade.direction == "buy":
                if before == 0:
                    shock_reduced_live_cycles.discard(cycle_id)
                    confirmed_shock_reduced_live_cycles.discard(cycle_id)
                    crowded_shock_reduced_live_cycles.discard(cycle_id)
                    if trade.reason.startswith(
                        ("[two-strategy confirmation]", "[three-strategy confirmation]")
                    ):
                        confirmed_live_cycles.add(cycle_id)
                    else:
                        confirmed_live_cycles.discard(cycle_id)
                cycle_shares[cycle_id] = before + quantity
                continue
            remaining = max(0, before - quantity)
            cycle_shares[cycle_id] = remaining
            if remaining == 0:
                shock_reduced_live_cycles.discard(cycle_id)
                confirmed_shock_reduced_live_cycles.discard(cycle_id)
                crowded_shock_reduced_live_cycles.discard(cycle_id)
                confirmed_live_cycles.discard(cycle_id)
            elif (
                trade.reason == "account_budget_trim"
                and trade.signal_date in observed_shock_reduction_dates
            ):
                shock_reduced_live_cycles.add(cycle_id)
                if cycle_id in confirmed_live_cycles:
                    confirmed_shock_reduced_live_cycles.add(cycle_id)
                if trade.signal_date in crowded_shock_reduction_dates:
                    crowded_shock_reduced_live_cycles.add(cycle_id)

        latest_atr_buy: dict[str, Any] = {}
        counted_atr_entries: set[tuple[str, str]] = set()
        proven_cycle_counts: dict[str, int] = {}
        completed_proven_atr_symbols: set[str] = set()
        for trade in sleeve.trades:
            if trade.date > date_str:
                continue
            if trade.strategy_name.split(":")[-1] != "atr_channel":
                continue
            if trade.direction == "buy":
                latest_atr_buy[trade.symbol] = trade
                continue
            if trade.direction != "sell" or trade.symbol not in latest_atr_buy:
                continue
            entry_trade = latest_atr_buy[trade.symbol]
            shares = require_int("historical sell shares", trade.shares, min_value=1)
            entry_basis = (
                require_finite("historical net proceeds", trade.net_cash_flow)
                - require_finite("historical realized pnl", trade.pnl)
            ) / shares
            peak_close = require_finite(
                "historical peak close", trade.peak_close, min_value=0.000001
            )
            cycle_id = (trade.symbol, entry_trade.date)
            if (
                cycle_id not in counted_atr_entries
                and entry_trade.reason.startswith("[two-strategy confirmation]")
                and entry_basis > 0.0
                and peak_close
                >= entry_basis * (1.0 + limit_pct_for_code(trade.symbol, costs)) ** 2
            ):
                counted_atr_entries.add(cycle_id)
                proven_cycle_counts[trade.symbol] = (
                    proven_cycle_counts.get(trade.symbol, 0) + 1
                )
            if (
                require_finite("historical realized pnl", trade.pnl) > 0.0
                and entry_basis > 0.0
                and peak_close
                >= entry_basis * (1.0 + limit_pct_for_code(trade.symbol, costs)) ** 2
            ):
                completed_proven_atr_symbols.add(trade.symbol)
        repeated_proven_reentry_symbols.update(
            symbol for symbol, count in proven_cycle_counts.items() if count >= 2
        )

        live_strategies = {
            symbol: {
                strategy
                for strategy, position in positions.items()
                if require_int("held shares", position.shares, min_value=0)
            }
            for symbol, positions in sleeve.positions.items()
        }
        shock_reduced_book_ids.update(
            (state_index, symbol, strategy)
            for symbol, strategies in live_strategies.items()
            for strategy in strategies
            if (symbol, strategy) in shock_reduced_live_cycles
        )
        confirmed_shock_reduced_book_ids.update(
            (state_index, symbol, strategy)
            for symbol, strategies in live_strategies.items()
            for strategy in strategies
            if (symbol, strategy) in confirmed_shock_reduced_live_cycles
        )
        crowded_shock_reduced_book_ids.update(
            (state_index, symbol, strategy)
            for symbol, strategies in live_strategies.items()
            for strategy in strategies
            if (symbol, strategy) in crowded_shock_reduced_live_cycles
        )

        def indicator_at_signal(symbol: str, name: str, signal_date: str) -> float | None:
            series = getattr(state, "indicator_map", {}).get(symbol, {}).get(name)
            if series is None:
                return None
            available = series.loc[series.index <= pd.Timestamp(signal_date)].dropna()
            if available.empty:
                return None
            return require_finite(f"{name} at signal", available.iloc[-1])

        for symbol, positions in sorted(sleeve.positions.items()):
            for strategy, position in sorted(positions.items()):
                shares = require_int("held shares", position.shares, min_value=0)
                if not shares:
                    continue
                frame = state.data_map.get(symbol)
                if frame is None:
                    raise ValueError("account budget requires every held mark")
                price = require_finite(
                    "held close",
                    sleeve._latest_close_on_or_before(frame, date),
                    min_value=0.000001,
                )
                books.append((state_index, symbol, strategy, shares, price))
                if not risk_alert_active:
                    continue
                indicators = getattr(state, "indicator_map", {}).get(symbol, {})
                short_ma = indicators.get("ma_short")
                available = (
                    short_ma.loc[short_ma.index <= date].dropna()
                    if short_ma is not None
                    else pd.Series(dtype=float)
                )
                if available.empty:
                    raise ValueError(
                        "account risk alert requires a close-known short moving average"
                    )
                ma_value = require_finite(
                    "held short moving average", available.iloc[-1], min_value=0.000001
                )
                entry = require_finite(
                    "held entry price", position.entry_price, min_value=0.000001
                )
                fresh_handoff = (
                    strategy == "dual_ma"
                    and position.entry_date == date_str
                    and symbol in previous_handoff_symbols
                )
                fresh_dual_trade = next(
                    (
                        trade
                        for trade in reversed(sleeve.trades)
                        if trade.date == date_str
                        and trade.direction == "buy"
                        and trade.symbol == symbol
                        and trade.strategy_name.split(":")[-1] == "dual_ma"
                    ),
                    None,
                )
                fresh_dual_rsi = (
                    indicator_at_signal(symbol, "rsi", fresh_dual_trade.signal_date)
                    if fresh_dual_trade is not None
                    and fresh_dual_trade.signal_date is not None
                    else None
                )
                fresh_proven_dual = (
                    strategy == "dual_ma"
                    and position.entry_date == date_str
                    and symbol in completed_proven_atr_symbols
                    and fresh_dual_rsi is not None
                    and fresh_dual_rsi <= EARLY_DUAL_TRANSITION_RSI_MAX
                )
                if fresh_handoff:
                    protected_handoff_book_ids.add((state_index, symbol, strategy))
                elif fresh_proven_dual:
                    protected_proven_dual_book_ids.add(
                        (state_index, symbol, strategy)
                    )
                elif price < entry and price < ma_value:
                    weak_book_ids.add((state_index, symbol, strategy))

        for pending_index, (signal, _strategy) in enumerate(state.pending):
            if signal.direction != "buy":
                continue
            if evidence_symbols is not None and signal.symbol not in evidence_symbols:
                excluded_buy_slots.append([state_index, pending_index])
                continue
            shares = require_int("pending buy shares", signal.target_shares, min_value=0)
            price = require_finite("pending buy price", signal.price, min_value=0.000001)
            if signal.signal_date is None or pd.Timestamp(signal.signal_date) > date:
                raise ValueError("account budget requires close-known buy intents")
            book_id = (state_index, signal.symbol, signal.strategy_name)
            rsi = indicator_at_signal(signal.symbol, "rsi", signal.signal_date)
            held_for_symbol = live_strategies.get(signal.symbol, set())
            if (
                signal.strategy_name == "dual_ma"
                and rsi is not None
                and rsi <= EARLY_DUAL_TRANSITION_RSI_MAX
                and signal.symbol in completed_proven_atr_symbols
                and "atr_channel" not in held_for_symbol
            ):
                proven_early_dual_book_ids.add(book_id)
            buys.append((state_index, signal, shares * price))

    shock_frames = {
        symbol: frame for state in states for symbol, frame in state.data_map.items()
    }
    previous_episode = bool(
        next(
            (
                event.get("shock_episode_active", False)
                for event in reversed(events)
                if event.get("event") == "account_budget_envelope"
            ),
            False,
        )
    )
    receipt, counterfactual_actions = plan_account_risk_budget(
        assets,
        peak,
        costs,
        books,
        buys,
        score,
        date_str=date_str,
        stress_by_symbol=observed_shock_stress(shock_frames, date, costs),
        direct_loss_by_symbol=observed_direct_losses(shock_frames, date, costs),
        shock_episode_active=previous_episode,
        shock_floor=shock_floor,
        preserve_strategy_valid_holdings=preserve_strategy_valid_holdings,
        risk_alert_active=risk_alert_active,
        weak_book_ids=weak_book_ids,
        strategy_handoff_symbols=strategy_handoff_symbols,
        repeated_proven_reentry_symbols=repeated_proven_reentry_symbols,
        proven_early_dual_book_ids=proven_early_dual_book_ids,
        shock_reduced_book_ids=shock_reduced_book_ids,
        confirmed_shock_reduced_book_ids=confirmed_shock_reduced_book_ids,
        crowded_shock_reduced_book_ids=crowded_shock_reduced_book_ids,
    )
    receipt["protected_handoff_book_ids"] = sorted(protected_handoff_book_ids)
    receipt["protected_proven_dual_book_ids"] = sorted(protected_proven_dual_book_ids)

    observed_buy_scales = list(receipt.pop("buy_scales", []))
    observed_buy_scale = float(receipt.pop("buy_scale", 1.0))
    if len(observed_buy_scales) != len(buys):
        raise RuntimeError("account budget observation lost buy alignment")

    observation = {
        "date": date_str,
        "event": "account_budget_envelope",
        "mechanism": "AB5",
        "policy_mode": POLICY_MODE,
        "health_status": HEALTH_EVALUATED,
        "trade_intervention_allowed": False,
        **receipt,
        "portfolio_evidence_buy_symbols": (
            None if evidence_symbols is None else sorted(evidence_symbols)
        ),
        "portfolio_excluded_buy_slots": excluded_buy_slots,
        "observed_counterfactual_buy_scales": observed_buy_scales,
        "observed_counterfactual_buy_scale": observed_buy_scale,
        "observed_counterfactual_reduction_count": len(counterfactual_actions),
        "buy_shares_removed": 0,
        "new_reduction_orders": 0,
    }
    events.append(observation)
    return observation


def account_budget_observer_status(
    events: Sequence[Mapping[str, Any]], enabled: bool
) -> dict[str, Any]:
    """Expose policy and evaluation health as separate fail-closed facts."""
    last = next(
        (
            dict(event)
            for event in reversed(events)
            if event.get("event") == "account_budget_envelope"
            and event.get("mechanism") == "AB5"
        ),
        None,
    )
    if not enabled:
        return {
            "enabled": False,
            "mechanism": "AB5",
            "policy_mode": "DISABLED_DIAGNOSTIC",
            "health_status": "NOT_EVALUATED",
            "status": "DISABLED_DIAGNOSTIC",
            "latest": last,
        }
    if last is None:
        return {
            "enabled": True,
            "mechanism": "AB5",
            "policy_mode": POLICY_MODE,
            "health_status": "NOT_EVALUATED",
            "status": "NOT_EVALUATED",
            "latest": None,
        }

    policy_mode = last.get("policy_mode")
    health_status = last.get("health_status")
    observed = policy_mode == POLICY_MODE and health_status == HEALTH_EVALUATED
    return {
        "enabled": True,
        "mechanism": "AB5",
        "policy_mode": policy_mode,
        "health_status": health_status,
        "status": "OBSERVED" if observed else "INVALID_OBSERVATION",
        "latest": last,
    }
