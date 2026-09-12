"""AB5 close-known loss envelope; planning stress is not a guaranteed loss bound."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
import math

import pandas as pd
from typing import Any

from quantfusion.domain.rules import floor_to_lot, limit_pct_for_code, require_finite, require_int
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



def account_budget_plan(
    equity: float, peak: float, cfg: dict[str, Any], book_count: int,
    holdings: Sequence[tuple[str, int, float]],
    buys: Sequence[tuple[str, int, float]], *, sell_order: Sequence[int],
) -> tuple[dict[str, Any], list[int]]:
    """Plan the same AB5 envelope for real books or a declared account snapshot.

    Quantities are close-known plans, not fills. Neither pending nor blocked
    sells provide buying credit. The caller supplies its existing weak-first
    order; the planner does not select alpha or invent account history.
    """
    # Preserve the native producer's Python scalars before summation. NumPy
    # scalar sums can differ at the last bit, including downstream buy scales.
    holdings, buys = (
        [(symbol, require_int("account budget shares", shares, min_value=0),
          require_finite("account budget price", price, min_value=0.000001))
         for symbol, shares, price in rows]
        for rows in (holdings, buys)
    )
    if sorted(sell_order) != list(range(len(holdings))):
        raise ValueError("account budget sell order must cover every book once")
    receipt = account_budget_capacity(equity, peak, cfg, book_count)
    gross = sum(shares * price for _, shares, price in holdings)
    if gross > equity + 1e-8:
        raise ValueError("account budget cannot certify leveraged/negative-cash books")
    cap = receipt["gross_cap"]
    requested = sum(shares * price for _, shares, price in buys)
    binding = cap < receipt["ordinary_gross_cap"] - 1e-8
    gross_scale = min(1., max(0., cap - gross) / requested) if binding and requested else 1.
    gap_cost = receipt["cost_rate"]
    current_debit = sum(shares * price * (limit_pct_for_code(symbol, cfg) + gap_cost)
                        for symbol, shares, price in holdings)
    buy_debit = sum(shares * price * (limit_pct_for_code(symbol, cfg) + gap_cost)
                    for symbol, shares, price in buys)
    gap_scale = (min(1., max(0., receipt["remaining_loss_budget"] - current_debit) / buy_debit)
                 if binding and buy_debit else 1.)
    reductions = [0] * len(holdings)
    relief = max(0., gross - cap)
    for index in sell_order:
        _, shares, price = holdings[index]
        reduction = min(shares, math.ceil(relief / price / 100.) * 100)
        reductions[index] = reduction
        relief = max(0., relief - reduction * price)
    return {**receipt, "gross_before": gross, "buy_envelope_binding": binding,
            "buy_gross_scale": gross_scale, "current_gap_debit": current_debit,
            "requested_buy_gap_debit": buy_debit, "buy_gap_scale": gap_scale,
            "buy_scale": min(gross_scale, gap_scale)}, reductions


def apply_account_risk_budget(
    states: Sequence[Any], date: pd.Timestamp, assets: float, peak: float,
    events: list[dict[str, Any]], *, cfg: Mapping[str, Any],
    score: Callable[[str], float],
) -> None:
    """Adapt a shared AB5 plan to the existing book-local RiskAction queues."""
    date_str = date.strftime("%Y-%m-%d")
    books, buys = [], []
    book_ids = set()
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
                book_ids.add((state_index, symbol, strategy))
        for signal, strategy in state.pending:
            if signal.direction == "buy":
                shares = require_int("pending buy shares", signal.target_shares, min_value=0)
                price = require_finite("pending buy price", signal.price, min_value=0.000001)
                if signal.signal_date is None or pd.Timestamp(signal.signal_date) > date:
                    raise ValueError("account budget requires close-known buy intents")
                buys.append((state_index, signal, shares*price))
                if shares:
                    book_ids.add((state_index, signal.symbol, signal.strategy_name))
    sell_order = sorted(range(len(books)), key=lambda i: (
        score(books[i][1]), books[i][1], books[i][0], books[i][2]))
    receipt, reductions = account_budget_plan(
        assets, peak, costs, len(book_ids),
        [(symbol, shares, price) for _, symbol, _, shares, price in books],
        [(signal.symbol, signal.target_shares, signal.price) for _, signal, _ in buys],
        sell_order=sell_order,
    )
    cap, buy_scale = receipt["gross_cap"], receipt["buy_scale"]
    actions = []
    for index in sell_order:
        state_index, symbol, strategy, shares, price = books[index]
        reduction = reductions[index]
        if not reduction:
            continue
        covered = any(signal.direction == "sell" and signal.symbol == symbol
                      and signal.strategy_name == strategy and signal.target_shares >= reduction
                      for signal, _ in states[state_index].pending)
        if not covered:
            actions.append(RiskAction(symbol, strategy, reduction, price, date_str,
                                      "account_budget_trim", RISK_ACTION_PRIORITY["account_budget_trim"],
                                      state_index=state_index))
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


def account_budget_status(
    enabled: bool, events: Sequence[Mapping[str, Any]], *, hwm_source: str,
) -> dict[str, Any]:
    """Report executed evaluations, not merely the requested configuration flag."""
    evaluations = [event for event in events if event.get("event") == "account_budget_envelope"]
    return {"enabled": enabled, "mechanism": "AB5", "hwm_source": hwm_source,
            "status": "APPLIED" if enabled and evaluations else
                      "NOT_EVALUATED" if enabled else "DISABLED_EXPLICIT",
            "evaluation_count": len(evaluations),
            "last_evaluation": dict(evaluations[-1]) if evaluations else None}
