"""Isolated native sell execution from observed books, not a candidate backtest.

Targets are full liquidation of the supplied, close-known inventory. Future
opening/closing prices affect the measured outcome, never the chosen quantities.
This does not establish that a strategy would have selected that action.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any

import pandas as pd

from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.domain.models import Position, Signal
from quantfusion.domain.rules import require_finite, require_int
from quantfusion.engine.ensemble import _EnsembleSleeveBacktestEngine


def probe_next_open(
    close_rows: Sequence[Mapping[str, Any]],
    data_map: Mapping[str, pd.DataFrame],
    policy: PortfolioPolicy,
    cfg: Mapping[str, Any],
    close_date: str,
    execution_date: str,
) -> dict[str, Any]:
    """Execute copied inventory through the unmodified matcher and check conservation.

    This is deliberately a one-open, sell-only diagnostic. Existing buy intents
    are not executed, and no subsequent strategy/risk history is manufactured.
    Every supplied sleeve must be one complete observed closing snapshot.
    """
    close, execution = pd.Timestamp(close_date), pd.Timestamp(execution_date)
    if not close_rows:
        raise ValueError('at least one complete closing snapshot is required')
    names = [str(row['sleeve']) for row in close_rows]
    if len(set(names)) != len(names):
        raise ValueError('duplicate sleeve snapshots')
    held = {symbol for row in close_rows for symbol in row['book_before']}
    for symbol in held:
        frame = data_map.get(symbol)
        if frame is None or close not in frame.index:
            raise ValueError(f'missing observed closing mark: {symbol}')
        require_finite('closing mark', frame.loc[close, 'close'], min_value=0.000001)
    dates = sorted({pd.Timestamp(day) for frame in data_map.values() for day in frame.index})
    future = [day for day in dates if day > close]
    if not future or execution != future[0]:
        raise ValueError('execution must be the next trading session after the close')
    date_to_pos = {day: index for index, day in enumerate(dates)}
    starting_cash = ending_cash = closing_inventory = residual_value = 0.
    residual_shares: dict[str, int] = {}
    fills: list[dict[str, Any]] = []
    unfilled: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    shared_adv: dict[tuple[str, str, str], int] = {}
    before_shares: dict[str, int] = {}
    for row in close_rows:
        cash = require_finite('snapshot cash', row['cash'], min_value=0.)
        engine = _EnsembleSleeveBacktestEngine(
            max(1., cash), cfg=dict(cfg), policy=policy,
            allocation_lookbacks=tuple(policy.single_lookbacks), sleeve_name=str(row['sleeve']))
        engine.cash = cash
        engine._adv_used = shared_adv
        pending = []
        for symbol, books in row['book_before'].items():
            engine.positions[symbol] = {}
            for name, fields in books.items():
                position = Position(**copy.deepcopy(dict(fields)))
                if position.symbol != symbol or position.strategy_name != name:
                    raise ValueError('position key does not match its observed identity')
                shares = require_int('observed shares', position.shares, min_value=0)
                if not shares:
                    continue
                if pd.Timestamp(position.entry_date) > close or (
                    position.last_buy_date and pd.Timestamp(position.last_buy_date) > close
                ):
                    raise ValueError('snapshot contains a future acquisition')
                engine.positions[symbol][name] = position
                before_shares[symbol] = before_shares.get(symbol, 0) + shares
                price = float(data_map[symbol].loc[close, 'close'])
                closing_inventory += shares * price
                pending.append((Signal(symbol, name, 'sell', target_shares=shares,
                                       price=price, reason='isolated_execution_probe',
                                       signal_date=close_date), None))
        retained = engine._execute_pending_signals(
            pending, dict(data_map), execution, date_to_pos, frozenset({'sell'}))
        starting_cash += cash
        ending_cash += engine.cash
        fills.extend({'sleeve': str(row['sleeve']), **asdict(trade)} for trade in engine.trades)
        unfilled.extend({'sleeve': str(row['sleeve']), **asdict(signal)} for signal, _ in retained)
        events.extend({'sleeve': str(row['sleeve']), **event} for event in engine.order_events)
        for symbol, books in engine.positions.items():
            shares = sum(position.shares for position in books.values())
            if not shares:
                continue
            residual_shares[symbol] = residual_shares.get(symbol, 0) + shares
            frame = data_map[symbol]
            if execution not in frame.index:
                raise ValueError('next-close residual valuation unavailable')
            price = require_finite('next-close mark', frame.loc[execution, 'close'], min_value=0.000001)
            residual_value += shares * price
    proceeds = sum(fill['net_cash_flow'] for fill in fills)
    cash_error = ending_cash - starting_cash - proceeds
    if abs(cash_error) > 1e-6:
        raise AssertionError(f'native cash conservation failure: {cash_error}')
    for symbol, shares in before_shares.items():
        sold = sum(fill['shares'] for fill in fills if fill['symbol'] == symbol)
        if shares != sold + residual_shares.get(symbol, 0):
            raise AssertionError('native inventory conservation failure')
    return {'diagnostic_noncanonical': True, 'continuous_candidate_run': False,
            'action': 'full sell of supplied close-known books; no buys',
            'close_date': close_date, 'execution_date': execution_date,
            'starting_cash': starting_cash, 'closing_inventory': closing_inventory,
            'starting_equity': starting_cash + closing_inventory,
            'ending_cash': ending_cash, 'residual_shares': residual_shares,
            'next_close_residual_value': residual_value,
            'next_close_equity': ending_cash + residual_value,
            'cash_conservation_error': cash_error, 'fills': fills, 'unfilled': unfilled,
            'order_events': events, 'shared_adv_used': [list(key) + [value] for key, value in shared_adv.items()]}
