"""Synthetic controls for the separately preregistered AB2 risk envelope."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pandas as pd
import pytest

from quantfusion.config.engine import default_engine_config, validate_engine_config
from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.domain.models import Position, Signal
from quantfusion.domain.rules import limit_pct_for_code
from quantfusion.engine.ensemble import EnsembleSleeveBacktestEngine
from quantfusion.engine.universe import BacktestEngine


def fixture(shares=8000, cash=10000.0):
    cfg = default_engine_config()
    cfg['account_risk_budget_enabled'] = True
    sleeve = EnsembleSleeveBacktestEngine(100000., cfg=cfg, policy=PortfolioPolicy(),
                                          allocation_lookbacks=(3, 5, 10), sleeve_name='fast')
    sleeve._reset_run_state({'300308': 'test'})
    sleeve.positions = {'300308': {'turtle_breakout': Position(
        symbol='300308', strategy_name='turtle_breakout', shares=shares,
        entry_price=10., entry_date='2026-01-01', stop_loss=7.,
        highest_since_entry=10., highest_close_since_entry=10., last_buy_date='2026-01-01')}}
    sleeve.cash = cash
    dates = pd.to_datetime(['2026-01-05', '2026-01-06', '2026-01-07'])
    frame = pd.DataFrame({'open': [10., 10., 10.], 'close': [10., 10., 10.],
                          'high': [10., 10., 10.], 'low': [10., 10., 10.],
                          'volume': [1e8]*3}, index=dates)
    state = SimpleNamespace(sleeve=sleeve, data_map={'300308': frame}, pending=[],
                            all_dates=list(dates), date_to_pos={d:i for i,d in enumerate(dates)})
    engine = BacktestEngine(cfg={"account_risk_budget_enabled": True})
    return engine, state, dates


def apply(engine, states, dates, equity=90000., peak=100000.):
    events = []
    engine._apply_account_risk_budget(states, dates[0], equity, peak, events)
    return events[-1]


def apply_ab6(engine, states, dates, equity=90000., peak=100000.):
    engine._c6_diagnostic_request = {"intervention_id": "C6_BASE_AB6"}
    return apply(engine, states, dates, equity=equity, peak=peak)


def apply_ab7(engine, states, dates, equity=90000., peak=100000.):
    engine._c6_diagnostic_request = {"intervention_id": "C6_BASE_AB7"}
    return apply(engine, states, dates, equity=equity, peak=peak)


def apply_ab8(engine, states, dates, equity=90000., peak=100000.):
    engine._c6_diagnostic_request = {"intervention_id": "C6_BASE_AB8"}
    return apply(engine, states, dates, equity=equity, peak=peak)


def apply_ab9(engine, states, dates, equity=90000., peak=100000.):
    engine._c6_diagnostic_request = {"intervention_id": "C6_BASE_AB9"}
    return apply(engine, states, dates, equity=equity, peak=peak)


def test_budget_intervenes_before_18_percent_without_mutating_account():
    engine, state, dates = fixture()
    before = deepcopy((state.sleeve.positions, state.sleeve.cash, vars(state.sleeve.risk)))
    receipt = apply(engine, [state], dates)
    assert receipt['gross_before'] == 80000.
    assert receipt['gross_cap'] < 80000.
    assert receipt['planned_not_filled'] is True
    assert state.pending and all(s.direction == 'sell' for s, _ in state.pending)
    assert (state.sleeve.positions, state.sleeve.cash, vars(state.sleeve.risk)) == before


def test_budget_bull_silent_and_disabled_by_default():
    engine, state, dates = fixture(cash=20000.)
    apply(engine, [state], dates, equity=100000.)
    assert not state.pending
    assert default_engine_config().get('account_risk_budget_enabled', False) is False


def test_budget_formula_has_fixed_two_session_and_fee_reserve():
    engine, state, dates = fixture()
    r = apply(engine, [state], dates)
    cfg = engine.cfg
    stress = 1-(1-cfg['daily_loss_limit'])**2
    costs = 2*cfg['slippage']+2*cfg['commission_rate']+cfg['stamp_duty']
    assert r['floor'] == pytest.approx(82000.)
    assert r['stress_fraction'] == pytest.approx(stress)
    assert r['gross_cap'] == pytest.approx(min(cfg['max_total_weight']*90000.,
                       (90000.-82000.-2*cfg['min_commission'])/(stress+costs)))


def test_nonbinding_budget_leaves_existing_buy_batch_unchanged():
    engine, state, dates = fixture(shares=2000, cash=80000.)
    buy = Signal('300308', 'turtle_breakout', 'buy', target_shares=10000,
                 price=10., signal_date='2026-01-05', reason='initial entry')
    state.pending = [(buy, SimpleNamespace(name='turtle_breakout'))]

    r = apply(engine, [state], dates, equity=100000., peak=100000.)

    assert r['gross_cap'] == r['ordinary_gross_cap']
    assert r['buy_envelope_binding'] is False
    assert state.pending == [(buy, state.pending[0][1])]
    assert r['buy_shares_removed'] == 0


def test_binding_budget_retains_bounded_buy_batch_without_sell_credit():
    engine, state, dates = fixture(shares=2000, cash=70000.)
    buy = Signal('300308', 'turtle_breakout', 'buy', target_shares=10000,
                 price=10., signal_date='2026-01-05', reason='initial entry')
    state.pending = [(buy, SimpleNamespace(name='turtle_breakout')),
                     (Signal('300308', 'turtle_breakout', 'sell', target_shares=2000,
                             price=10., signal_date='2026-01-05', reason='portfolio-level drawdown liquidation'), None)]
    r = apply(engine, [state], dates)
    buys = [s for s, _ in state.pending if s.direction == 'buy']
    assert r['gross_cap'] < r['ordinary_gross_cap']
    assert r['buy_envelope_binding'] is True
    assert 0 < sum(s.target_shares*s.price for s in buys) <= r['gross_cap']-20000.
    assert all(s.target_shares % 100 == 0 for s in buys)
    assert r['buy_shares_removed'] > 0
    assert state.sleeve.positions['300308']['turtle_breakout'].shares == 2000


def test_binding_buy_batch_debits_existing_board_limit_gap_risk():
    engine, state, dates = fixture(shares=2000, cash=70000.)
    buy = Signal('300308', 'turtle_breakout', 'buy', target_shares=10000,
                 price=10., signal_date='2026-01-05', reason='initial entry')
    state.pending = [(buy, SimpleNamespace(name='turtle_breakout'))]

    r = apply(engine, [state], dates)

    factor = limit_pct_for_code('300308', engine.cfg) + r['cost_rate']
    current_debit = 20000. * factor
    retained_debit = sum(
        signal.target_shares * signal.price * factor
        for signal, _ in state.pending if signal.direction == 'buy'
    )
    assert r['buy_envelope_binding'] is True
    assert r['current_gap_debit'] == pytest.approx(current_debit)
    assert 0 < retained_debit <= r['remaining_loss_budget'] - current_debit
    assert r['buy_gap_scale'] < r['buy_gross_scale']


def test_stock_gap_debit_forces_minimum_additional_sell_relief():
    engine, state, dates = fixture(shares=8000, cash=10000.)

    r = apply_ab6(engine, [state], dates)

    factor = limit_pct_for_code('300308', engine.cfg) + r['cost_rate']
    planned = sum(
        signal.target_shares
        for signal, _ in state.pending
        if signal.direction == 'sell' and signal.reason == 'account_budget_trim'
    )
    remaining_gap_debit = (8000 - planned) * 10. * factor
    assert r['current_gap_debit'] > r['remaining_loss_budget']
    assert r['stock_gap_constraint_binding'] is True
    assert r['post_plan_current_gap_debit'] == pytest.approx(remaining_gap_debit)
    assert r['post_plan_current_gap_debit'] <= r['remaining_loss_budget']
    assert (8000 - planned + 100) * 10. * factor > r['remaining_loss_budget']


def test_stock_gap_planner_uses_each_books_board_limit_debit():
    engine, weak, dates = fixture(shares=3000, cash=30000.)
    strong = deepcopy(weak)
    position = strong.sleeve.positions.pop('300308').pop('turtle_breakout')
    position.symbol = '601869'
    strong.sleeve.positions = {'601869': {'turtle_breakout': position}}
    strong.data_map = {'601869': strong.data_map.pop('300308')}
    scores = {'300308': 0.1, '601869': 0.9}
    weak.sleeve._allocation_scores = lambda *_: scores
    strong.sleeve._allocation_scores = lambda *_: scores

    r = apply_ab6(engine, [weak, strong], dates)

    sold = [(signal.symbol, signal.target_shares)
            for state in (weak, strong) for signal, _ in state.pending
            if signal.reason == 'account_budget_trim']
    factor_300 = limit_pct_for_code('300308', engine.cfg) + r['cost_rate']
    factor_600 = limit_pct_for_code('601869', engine.cfg) + r['cost_rate']
    post_gap = 3000 * 10. * (factor_300 + factor_600)
    for symbol, shares in sold:
        factor = factor_300 if symbol == '300308' else factor_600
        post_gap -= shares * 10. * factor
    assert sold and sold[0][0] == '300308'
    assert post_gap == pytest.approx(r['post_plan_current_gap_debit'])
    assert post_gap <= r['remaining_loss_budget']


def test_ab7_does_not_create_a_stock_only_sell_date():
    engine, state, dates = fixture(shares=4000, cash=50000.)

    r = apply_ab7(engine, [state], dates)

    assert r['gross_before'] <= r['gross_cap']
    assert r['current_gap_debit'] > r['remaining_loss_budget']
    assert r['stock_gap_constraint_binding'] is False
    assert r['mechanism'] == 'AB7'
    assert state.pending == []


def test_ab7_strengthens_an_existing_ab5_sell_only_to_gap_budget():
    engine, state, dates = fixture(shares=8000, cash=10000.)

    r = apply_ab7(engine, [state], dates)

    assert r['gross_before'] > r['gross_cap']
    assert r['stock_gap_constraint_binding'] is True
    assert r['post_plan_current_gap_debit'] <= r['remaining_loss_budget']


def test_ab8_bounds_gap_reinforcement_to_one_existing_ab5_tranche():
    engine, state, dates = fixture(shares=8000, cash=10000.)

    r = apply_ab8(engine, [state], dates)

    planned_notional = sum(
        signal.target_shares * signal.price
        for signal, _ in state.pending
        if signal.reason == 'account_budget_trim'
    )
    base_relief = r['gross_before'] - r['gross_cap']
    assert r['stock_gap_constraint_binding'] is True
    assert planned_notional >= 2 * base_relief
    assert planned_notional - 1000. < 2 * base_relief
    assert r['post_plan_current_gap_debit'] > r['remaining_loss_budget']


def test_ab8_keeps_ab7_no_new_sell_date_boundary():
    engine, state, dates = fixture(shares=4000, cash=50000.)

    r = apply_ab8(engine, [state], dates)

    assert r['gross_before'] <= r['gross_cap']
    assert r['current_gap_debit'] > r['remaining_loss_budget']
    assert r['stock_gap_constraint_binding'] is False
    assert state.pending == []


def test_ab9_extra_tranche_retains_the_final_lot_of_each_surviving_book():
    engine, first, dates = fixture(shares=2000, cash=10000.)
    states = [first]
    for index in range(1, 4):
        sibling = deepcopy(first)
        sibling.sleeve.sleeve_name = f'sleeve-{index}'
        states.append(sibling)

    r = apply_ab9(engine, states, dates)

    sold = [sum(signal.target_shares for signal, _ in state.pending
                if signal.reason == 'account_budget_trim') for state in states]
    planned_notional = sum(sold) * 10.
    base_relief = r['gross_before'] - r['gross_cap']
    assert sold[0] == 1900
    assert all(2000 - quantity >= 100 for quantity in sold)
    assert planned_notional >= 2 * base_relief
    assert planned_notional - 2000. < 2 * base_relief
    assert r['extra_gap_relief_unfilled'] >= 0


def test_binding_budget_zero_headroom_vetoes_buys_without_sell_credit():
    engine, state, dates = fixture(shares=8000, cash=2000.)
    buy = Signal('300308', 'turtle_breakout', 'buy', target_shares=10000,
                 price=10., signal_date='2026-01-05', reason='initial entry')
    sell = Signal('300308', 'turtle_breakout', 'sell', target_shares=8000,
                  price=10., signal_date='2026-01-05', reason='pending exit')
    state.pending = [(buy, SimpleNamespace(name='turtle_breakout')), (sell, None)]

    r = apply(engine, [state], dates, equity=82000., peak=100000.)

    assert r['gross_cap'] == 0
    assert r['buy_envelope_binding'] is True
    assert [s for s, _ in state.pending if s.direction == 'buy'] == []


def test_unlocked_empty_account_with_positive_budget_can_reenter():
    engine, state, dates = fixture(shares=0, cash=90000.)
    buy = Signal('300308', 'turtle_breakout', 'buy', target_shares=10000,
                 price=10., signal_date='2026-01-05', reason='initial entry')
    state.pending = [(buy, SimpleNamespace(name='turtle_breakout'))]

    r = apply(engine, [state], dates, equity=90000., peak=100000.)

    buys = [s for s, _ in state.pending if s.direction == 'buy']
    assert r['buy_envelope_binding'] is True
    assert r['gross_cap'] > 0
    assert 0 < sum(s.target_shares*s.price for s in buys) <= r['gross_cap']


def test_stronger_lock_liquidation_is_not_replaced():
    engine, state, dates = fixture()
    signal = Signal('300308', 'turtle_breakout', 'sell', target_shares=8000,
                    price=10., signal_date='2026-01-05', reason='portfolio-level drawdown liquidation')
    state.pending = [(signal, None)]
    apply(engine, [state], dates)
    assert state.pending == [(signal, None)]


def test_tied_sibling_books_use_stable_order_and_one_partial_final_book():
    engine, state, dates = fixture(shares=4050, cash=4500.)
    sibling = deepcopy(state)
    sibling.sleeve.sleeve_name = 'slow'
    r = apply(engine, [state, sibling], dates)
    sold = [(index, s.target_shares) for index, st in enumerate((state, sibling))
            for s, _ in st.pending]
    required = 81000. - r['gross_cap']
    assert sold == [(0, min(4050, int(-(-required//1000))*100))]
    assert (8100-sum(quantity for _, quantity in sold))*10 <= r['gross_cap']


def test_weakest_book_absorbs_minimum_reduction_and_preserves_winner():
    engine, weak, dates = fixture(shares=5000, cash=10000.)
    strong = deepcopy(weak)
    position = strong.sleeve.positions.pop('300308').pop('turtle_breakout')
    position.symbol = '300502'
    position.shares = 3000
    strong.sleeve.positions = {'300502': {'turtle_breakout': position}}
    strong.data_map = {'300502': strong.data_map.pop('300308')}
    scores = {'300308': 0.1, '300502': 0.9}
    weak.sleeve._allocation_scores = lambda *_: scores
    strong.sleeve._allocation_scores = lambda *_: scores

    r = apply(engine, [weak, strong], dates)

    required = 80000. - r['gross_cap']
    expected = min(5000, int(-(-required//1000))*100)
    assert [(s.symbol, s.target_shares) for s, _ in weak.pending] == [
        ('300308', expected)
    ]
    assert strong.pending == []
    assert r['new_reduction_orders'] == 1


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1., True])
def test_invalid_equity_fails_closed_before_queue_mutation(value):
    engine, state, dates = fixture()
    with pytest.raises(ValueError):
        apply(engine, [state], dates, equity=value)
    assert not state.pending


def test_missing_held_mark_fails_closed():
    engine, state, dates = fixture()
    state.data_map = {}
    with pytest.raises(ValueError):
        apply(engine, [state], dates)
    assert not state.pending


def test_future_prices_cannot_change_close_decision():
    engine, state, dates = fixture()
    other = deepcopy(state)
    other.data_map['300308'].loc[dates[1]:, ['open', 'close', 'volume']] = 0.
    apply(engine, [state], dates)
    apply(engine, [other], dates)
    assert state.pending == other.pending


def test_generated_reduction_uses_canonical_next_open_fill_and_limit_block():
    engine, state, dates = fixture()
    apply(engine, [state], dates)
    before = state.sleeve.positions['300308']['turtle_breakout'].shares
    pending = state.sleeve._execute_pending_signals(state.pending, state.data_map,
                                                   dates[0], state.date_to_pos, frozenset({'sell'}))
    assert state.sleeve.positions['300308']['turtle_breakout'].shares == before
    assert not state.sleeve.trades
    blocked = deepcopy(state)
    blocked.data_map['300308'].loc[dates[1], 'open'] = 7.9
    blocked.sleeve._execute_pending_signals(pending, blocked.data_map, dates[1],
                                          blocked.date_to_pos, frozenset({'sell'}))
    assert not blocked.sleeve.trades
    state.sleeve._execute_pending_signals(pending, state.data_map, dates[1],
                                         state.date_to_pos, frozenset({'sell'}))
    assert state.sleeve.trades and state.sleeve.trades[0].date == '2026-01-06'
    assert state.sleeve.trades[0].commission > 0


def test_budget_flag_is_not_truthy_string():
    cfg = default_engine_config()
    cfg['account_risk_budget_enabled'] = 'false'
    with pytest.raises(ValueError):
        validate_engine_config(cfg)


def test_budget_cannot_masquerade_as_frozen_base_or_s():
    from quantfusion.engine.replay import ProductionReplayEngine
    engine = ProductionReplayEngine(cfg={'account_risk_budget_enabled': True})
    with pytest.raises(ValueError, match="own evidence identity"):
        engine.run_c6_diagnostic({}, '2025-04-01', '2026-07-20',
            data_dir='unused', regime_data_dir='unused', diagnostic_request={})
