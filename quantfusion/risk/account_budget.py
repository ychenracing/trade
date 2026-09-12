"""Shared close-known AB5 budget; planning stress is not a guaranteed loss bound."""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

import pandas as pd
from typing import Any

from quantfusion.domain.rules import floor_to_lot, limit_pct_for_code, require_finite, require_int
from quantfusion.domain.models import Signal
from quantfusion.config.overlay import RISK_ACTION_PRIORITY
from quantfusion.execution.c6_receipts import reconcile_close_queue
from quantfusion.risk.overlay.adapter import apply_risk_actions
from quantfusion.risk.overlay.models import RiskAction


def account_budget_capacity(
    equity: float, peak: float, cfg: Mapping[str, Any], book_count: int,
) -> dict[str, float]:
    """Use the continuous account HWM, two stress sessions and exit-cost reserve."""
    equity = require_finite('account equity', equity, min_value=0.)
    peak = require_finite('account lifetime peak', peak, min_value=0.01)
    if equity > peak + 1e-8:
        raise ValueError('account lifetime peak must include current equity')
    book_count = require_int('book_count', book_count, min_value=0)
    daily = require_finite('daily_loss_limit', cfg['daily_loss_limit'],
                           min_value=0.000001, max_value=1., inclusive_max=False)
    costs = {key: require_finite(key, cfg[key], min_value=0.)
             for key in ('slippage', 'commission_rate', 'stamp_duty', 'min_commission')}
    maximum = require_finite('max_total_weight', cfg['max_total_weight'],
                              min_value=0., max_value=1.)
    floor = 0.82 * peak
    stress = 1. - (1. - daily)**2
    cost_rate = 2*costs['slippage'] + 2*costs['commission_rate'] + costs['stamp_duty']
    fixed = 2*book_count*costs['min_commission']
    budget = max(0., equity - floor - fixed)
    ordinary_cap = maximum * equity
    return {'equity': equity, 'lifetime_peak': peak, 'floor': floor,
            'stress_fraction': stress, 'cost_rate': cost_rate, 'fixed_cost_reserve': fixed,
            'remaining_loss_budget': budget, 'ordinary_gross_cap': ordinary_cap,
            'gross_cap': min(ordinary_cap, budget/(stress+cost_rate))}


def plan_account_risk_budget(
    equity: float, peak: float, cfg: Mapping[str, Any],
    books: Sequence[tuple[int, str, str, int, float]],
    buys: Sequence[tuple[int, Signal, float]],
    score: Callable[[str], float], *, date_str: str,
) -> tuple[dict[str, Any], list[RiskAction]]:
    """Plan the same AB5 reductions for real snapshot books or replay books.

    No portfolio, cash, order queue or history is created or mutated here.
    Existing/queued sells provide no buying credit. Callers retain their own
    execution adapter and must not treat these close-known plans as fills.
    """
    cfg = dict(cfg)
    book_ids = {(state, symbol, strategy) for state, symbol, strategy, shares, _ in books if shares}
    book_ids.update((state, signal.symbol, signal.strategy_name)
                    for state, signal, _ in buys if signal.target_shares)
    receipt = account_budget_capacity(equity, peak, cfg, len(book_ids))
    gross = sum(shares*price for _, _, _, shares, price in books)
    if gross > equity + 1e-8:
        raise ValueError("account budget cannot certify leveraged/negative-cash books")
    cap = receipt["gross_cap"]
    requested = sum(value for _, _, value in buys)
    binding = cap < receipt["ordinary_gross_cap"] - 1e-8
    gross_scale = min(1., max(0., cap-gross)/requested) if binding and requested else 1.
    current_gap = sum(shares*price*(limit_pct_for_code(symbol, cfg)+receipt["cost_rate"])
                      for _, symbol, _, shares, price in books)
    buy_gap = sum(value*(limit_pct_for_code(signal.symbol, cfg)+receipt["cost_rate"])
                  for _, signal, value in buys)
    gap_scale = min(1., max(0., receipt["remaining_loss_budget"]-current_gap)/buy_gap) if binding and buy_gap else 1.
    actions = []
    relief = max(0., gross-cap)
    for state, symbol, strategy, shares, price in sorted(
        books, key=lambda book: (score(book[1]), book[1], book[0], book[2]),
    ):
        reduction = min(shares, math.ceil(relief/price/100.)*100)
        if not reduction:
            continue
        actions.append(RiskAction(symbol, strategy, reduction, price, date_str,
                                  "account_budget_trim", RISK_ACTION_PRIORITY["account_budget_trim"],
                                  state_index=state))
        relief = max(0., relief-reduction*price)
    return {**receipt, "gross_before": gross, "buy_envelope_binding": binding,
            "buy_gross_scale": gross_scale, "current_gap_debit": current_gap,
            "requested_buy_gap_debit": buy_gap, "buy_gap_scale": gap_scale,
            "buy_scale": min(gross_scale, gap_scale)}, actions


def apply_account_risk_budget(
    states: Sequence[Any], date: pd.Timestamp, assets: float, peak: float,
    cfg: Mapping[str, Any], score: Callable[[str], float], events: list[dict[str, Any]],
) -> None:
    """Adapt the shared plan to the existing replay books and order queues."""
    date_str = date.strftime("%Y-%m-%d")
    books, buys = [], []
    costs = dict(cfg)
    for state_index, state in enumerate(states):
        for key in ("slippage", "commission_rate", "stamp_duty", "min_commission"):
            costs[key] = max(costs[key], state.sleeve.cfg[key])
        for symbol, positions in sorted(state.sleeve.positions.items()):
            for strategy, position in sorted(positions.items()):
                shares = require_int("held shares", position.shares, min_value=0)
                if not shares:
                    continue
                frame = state.data_map.get(symbol)
                if frame is None:
                    raise ValueError("account budget requires every held mark")
                price = require_finite("held close", state.sleeve._latest_close_on_or_before(frame, date), min_value=0.000001)
                books.append((state_index, symbol, strategy, shares, price))
        for signal, strategy in state.pending:
            if signal.direction == "buy":
                shares = require_int("pending buy shares", signal.target_shares, min_value=0)
                price = require_finite("pending buy price", signal.price, min_value=0.000001)
                if signal.signal_date is None or pd.Timestamp(signal.signal_date) > date:
                    raise ValueError("account budget requires close-known buy intents")
                buys.append((state_index, signal, shares*price))
    receipt, planned_actions = plan_account_risk_budget(
        assets, peak, costs, books, buys, score, date_str=date_str,
    )
    cap = receipt["gross_cap"]
    buy_scale = receipt["buy_scale"]
    actions = [action for action in planned_actions if not any(
        signal.direction == "sell" and signal.symbol == action.symbol
        and signal.strategy_name == action.strategy_name
        and signal.target_shares >= action.shares
        for signal, _ in states[action.state_index].pending
    )]
    # Validate and plan the entire batch before changing any pending queue.
    previous = [list(state.pending) for state in states]
    clipped = 0
    for state in states:
        retained = []
        for signal, strategy in state.pending:
            if signal.direction == "buy" and buy_scale < 1.:
                quantity = floor_to_lot(signal.target_shares*buy_scale)
                clipped += signal.target_shares - quantity
                state.sleeve._record_order_event(
                    date=date_str, signal=signal, event="account_budget_buy_reduced",
                    authorized_shares=quantity, close_gross_cap=cap,
                )
                if not quantity:
                    continue
                signal = replace(signal, target_shares=quantity)
            retained.append((signal, strategy))
        state.pending = retained
    apply_risk_actions(actions, states, date_str=date_str, events=events,
                       state_local_books=True)
    for state, before in zip(states, previous):
        reconcile_close_queue(state.sleeve, before, state.pending, date_str, "account_budget_envelope")
    events.append({"date": date_str, "event": "account_budget_envelope",
                   "mechanism": "AB5", "planned_not_filled": True, **receipt,
                   "buy_shares_removed": clipped, "new_reduction_orders": len(actions)})


def account_budget_status(events: Sequence[Mapping[str, Any]], enabled: bool) -> dict[str, Any]:
    """Report actual evaluation rather than claiming success from a config flag."""
    last = next((dict(e) for e in reversed(events) if e.get("event") == "account_budget_envelope"), None)
    return {"enabled": enabled, "mechanism": "AB5",
            "status": "APPLIED" if enabled and last is not None else
                      "NOT_EVALUATED" if enabled else "DISABLED_DIAGNOSTIC",
            "latest": last}
