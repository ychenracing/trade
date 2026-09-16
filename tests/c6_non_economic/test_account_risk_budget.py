"""Account risk-budget plans, default enablement, and execution receipts."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pandas as pd
import pytest
from quantfusion.research.c6_runtime import run_c6_diagnostic

from quantfusion.config.engine import default_engine_config, validate_engine_config
from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.domain.models import Position, Signal, TradeRecord
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


def test_budget_intervenes_before_18_percent_without_mutating_account():
    engine, state, dates = fixture()
    before = deepcopy((state.sleeve.positions, state.sleeve.cash, vars(state.sleeve.risk)))
    receipt = apply(engine, [state], dates)
    assert receipt['gross_before'] == 80000.
    assert receipt['gross_cap'] < 80000.
    assert receipt['planned_not_filled'] is True
    assert state.pending and all(s.direction == 'sell' for s, _ in state.pending)
    assert (state.sleeve.positions, state.sleeve.cash, vars(state.sleeve.risk)) == before


def test_budget_bull_silent_and_enabled_by_default():
    engine, state, dates = fixture(cash=20000.)
    apply(engine, [state], dates, equity=100000.)
    assert not state.pending
    assert default_engine_config()['account_risk_budget_enabled'] is True


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


def test_portfolio_rejected_candidate_does_not_consume_account_buy_budget():
    """Only evidence-screened buys participate in close-known risk allocation."""
    engine, state, dates = fixture(shares=2_000, cash=70_000.)
    frame = state.data_map['300308']
    state.data_map.update({'300502': frame.copy(), '603986': frame.copy()})
    accepted = Signal(
        '300502', 'turtle_breakout', 'buy', 5_000, 10.,
        signal_date='2026-01-05', reason='accepted candidate',
    )
    rejected = Signal(
        '603986', 'turtle_breakout', 'buy', 5_000, 10.,
        signal_date='2026-01-05', reason='unconfirmed candidate',
    )
    state.pending = [
        (accepted, SimpleNamespace(name='turtle_breakout')),
        (rejected, SimpleNamespace(name='turtle_breakout')),
    ]
    events = []

    engine._apply_account_risk_budget(
        [state], dates[0], 90_000., 100_000., events,
        portfolio_evidence_buy_symbols={'300502'},
    )

    by_symbol = {signal.symbol: signal for signal, _ in state.pending}
    assert 0 < by_symbol['300502'].target_shares < accepted.target_shares
    assert by_symbol['603986'] == rejected
    assert events[-1]['portfolio_evidence_buy_symbols'] == ['300502']
    assert events[-1]['portfolio_excluded_buy_slots'] == [[0, 1]]


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
        run_c6_diagnostic(
            engine, {}, '2025-04-01', '2026-07-20',
            data_dir='unused', regime_data_dir='unused',
            diagnostic_request={
                'schema_version': 1,
                'intervention_id': 'C6_BASE',
                'recording_mode': 'DEFAULT',
                'scenario_id': 'prefix-05',
                'diagnostic_noncanonical': True,
                'allow_publication': False,
            },
        )


def test_portfolio_alert_uses_current_indicators_to_exit_weak_book():
    """Catch the ensemble adapter dropping alert and holding-quality state."""
    engine, state, dates = fixture(shares=5_000, cash=40_000.)
    position = state.sleeve.positions['300308']['turtle_breakout']
    position.entry_price = 12.
    state.indicator_map = {
        '300308': {'ma_short': pd.Series([11., 11., 11.], index=dates)}
    }
    events = []
    engine._apply_account_risk_budget(
        [state], dates[0], 90_000., 100_000., events,
        shock_floor=86_000., preserve_strategy_valid_holdings=True,
        risk_alert_active=True,
    )
    assert [(signal.symbol, signal.target_shares) for signal, _ in state.pending] == [
        ('300308', 5_000),
    ]
    assert events[-1]['weak_book_ids'] == [(0, '300308', 'turtle_breakout')]


def test_budget_infers_live_alert_state_from_portfolio_manager_events():
    """Catch orchestration recording an alert without changing the decision path."""
    engine, state, dates = fixture(shares=5_000, cash=40_000.)
    position = state.sleeve.positions['300308']['turtle_breakout']
    position.entry_price = 12.
    state.indicator_map = {
        '300308': {'ma_short': pd.Series([11., 11., 11.], index=dates)}
    }
    events = [{
        'date': '2026-01-05',
        'event': 'portfolio_drawdown_alert_on',
        'drawdown': .12,
    }]
    engine._apply_account_risk_budget(
        [state], dates[0], 90_000., 100_000., events,
        shock_floor=86_000., preserve_strategy_valid_holdings=True,
    )
    assert events[-1]['risk_alert_active'] is True
    assert [(signal.symbol, signal.target_shares) for signal, _ in state.pending] == [
        ('300308', 5_000),
    ]


def test_sleeve_alert_cannot_override_latest_portfolio_alert_state():
    """Account coordination must consume only the portfolio manager state."""
    engine, state, dates = fixture(shares=2_000, cash=80_000.)
    events = [
        {'sleeve': 'portfolio', 'date': '2026-01-05',
         'event': 'portfolio_drawdown_alert_off', 'drawdown': .10},
        {'sleeve': 'fast', 'date': '2026-01-05',
         'event': 'portfolio_drawdown_alert_on', 'drawdown': .18},
    ]
    engine._apply_account_risk_budget(
        [state], dates[0], 100_000., 100_000., events,
        preserve_strategy_valid_holdings=True,
    )
    assert events[-1]['risk_alert_active'] is False


def test_independent_confirmed_buy_is_selected_before_ordinary_buy():
    """A confirmed new group keeps its size while ordinary risk stays bounded."""
    engine, state, dates = fixture(shares=2_000, cash=65_000.)
    frame = state.data_map['300308']
    state.data_map.update({'603986': frame.copy(), '300502': frame.copy()})
    confirmed = Signal(
        '603986', 'atr_channel', 'buy', 1_000, 10.,
        signal_date='2026-01-05', fusion_votes=2,
        fusion_label='two_strategy_confirmation',
    )
    ordinary = Signal(
        '300502', 'turtle_breakout', 'buy', 1_000, 10.,
        signal_date='2026-01-05', fusion_votes=1,
    )
    state.pending = [
        (confirmed, SimpleNamespace(name='atr_channel')),
        (ordinary, SimpleNamespace(name='turtle_breakout')),
    ]
    events = []
    engine._apply_account_risk_budget(
        [state], dates[0], 85_000., 100_000., events,
        preserve_strategy_valid_holdings=True,
    )
    by_symbol = {signal.symbol: signal.target_shares for signal, _ in state.pending}
    assert by_symbol['603986'] == 1_000
    assert by_symbol.get('300502', 0) < 1_000
    assert events[-1]['quality_admitted_buy_indexes'] == [0]


def test_budget_derives_dual_ma_handoff_from_same_day_filled_exit():
    """Catch the adapter dropping a causal filled exit before close planning."""
    engine, state, dates = fixture(shares=0, cash=90_000.)
    state.sleeve.positions = {}
    state.sleeve.trades.append(TradeRecord(
        symbol='300308', strategy_name='turtle_breakout', direction='sell',
        shares=300, price=10., date='2026-01-05',
        reason='Donchian exit', signal_date='2026-01-02',
    ))
    buy = Signal(
        '300308', 'dual_ma', 'buy', 10_000, 10.,
        signal_date='2026-01-05', fusion_votes=1,
    )
    state.pending = [(buy, SimpleNamespace(name='dual_ma'))]
    events = []
    engine._apply_account_risk_budget(
        [state], dates[0], 90_000., 100_000., events,
        preserve_strategy_valid_holdings=True,
    )
    assert [(signal.symbol, signal.target_shares) for signal, _ in state.pending] == [
        ('300308', 10_000),
    ]
    assert events[-1]['strategy_handoff_symbols'] == ['300308']
    assert events[-1]['handoff_admitted_buy_indexes'] == [0]


def test_alert_does_not_immediately_reverse_filled_dual_ma_handoff():
    """Catch the generic weak-book rule undoing yesterday's slow-strategy handoff."""
    engine, state, dates = fixture(shares=5_000, cash=50_000.)
    position = state.sleeve.positions['300308'].pop('turtle_breakout')
    position.strategy_name = 'dual_ma'
    position.entry_price = 12.
    position.entry_date = '2026-01-05'
    state.sleeve.positions['300308']['dual_ma'] = position
    state.indicator_map = {
        '300308': {'ma_short': pd.Series([11., 11., 11.], index=dates)}
    }
    events = [{
        'date': '2026-01-04',
        'event': 'account_budget_envelope',
        'strategy_handoff_symbols': ['300308'],
    }]
    engine._apply_account_risk_budget(
        [state], dates[0], 100_000., 100_000., events,
        preserve_strategy_valid_holdings=True,
        risk_alert_active=True,
    )
    assert state.pending == []
    assert events[-1]['weak_book_ids'] == []
    assert events[-1]['protected_handoff_book_ids'] == [
        (0, '300308', 'dual_ma'),
    ]


def test_alert_does_not_reverse_fresh_proven_dual_transition():
    """The first close preserves a causal re-entry after a durable ATR cycle."""
    engine, state, dates = fixture(shares=5_000, cash=50_000.)
    position = state.sleeve.positions['300308'].pop('turtle_breakout')
    position.strategy_name = 'dual_ma'
    position.entry_price = 12.
    position.entry_date = '2026-01-05'
    state.sleeve.positions['300308']['dual_ma'] = position
    state.indicator_map = {
        '300308': {
            'ma_short': pd.Series([11., 11., 11.], index=dates),
            'rsi': pd.Series([54., 54., 54.], index=dates),
        }
    }
    state.sleeve.trades.extend([
        TradeRecord(
            symbol='300308', strategy_name='atr_channel', direction='buy',
            shares=1_000, price=10., date='2025-06-24',
            reason='[single-strategy probe] ATR channel breakout(ADX=61.6)',
            signal_date='2025-06-23',
        ),
        TradeRecord(
            symbol='300308', strategy_name='atr_channel', direction='sell',
            shares=1_000, price=15., date='2025-12-09',
            reason='ATR trailing stop', signal_date='2025-12-08',
            pnl=5_000., net_cash_flow=15_000., peak_close=15.,
        ),
        TradeRecord(
            symbol='300308', strategy_name='dual_ma', direction='buy',
            shares=5_000, price=12., date='2026-01-05',
            reason='[single-strategy probe] MA golden cross(RSI=54)',
            signal_date='2026-01-05',
        ),
    ])
    events = []

    engine._apply_account_risk_budget(
        [state], dates[0], 100_000., 100_000., events,
        preserve_strategy_valid_holdings=True,
        risk_alert_active=True,
    )

    assert state.pending == []
    assert events[-1]['weak_book_ids'] == []
    assert events[-1]['protected_proven_dual_book_ids'] == [
        (0, '300308', 'dual_ma'),
    ]


def test_budget_derives_repeated_reentry_from_two_confirmed_atr_cycles():
    """Catch the adapter dropping repeated completed-cycle quality."""
    engine, state, dates = fixture(shares=0, cash=90_000.)
    state.sleeve.positions = {}
    state.data_map = {'603986': state.data_map.pop('300308')}
    state.sleeve.trades.extend([
        TradeRecord(
            symbol='603986', strategy_name='atr_channel', direction='buy',
            shares=1_000, price=10., date='2025-09-15',
            reason='[two-strategy confirmation] ATR range breakout(ADX=35.9)',
            signal_date='2025-09-12',
        ),
        TradeRecord(
            symbol='603986', strategy_name='atr_channel', direction='sell',
            shares=1_000, price=9., date='2025-11-25',
            reason='account_budget_trim', signal_date='2025-11-24',
            pnl=-1_000., net_cash_flow=9_000., peak_close=12.2,
        ),
        TradeRecord(
            symbol='603986', strategy_name='atr_channel', direction='buy',
            shares=1_000, price=10., date='2025-12-01',
            reason='[two-strategy confirmation] ATR range breakout(ADX=24.0)',
            signal_date='2025-11-28',
        ),
        TradeRecord(
            symbol='603986', strategy_name='atr_channel', direction='sell',
            shares=1_000, price=9., date='2025-12-22',
            reason='account_budget_trim', signal_date='2025-12-19',
            pnl=-1_000., net_cash_flow=9_000., peak_close=12.2,
        ),
    ])
    atr = Signal(
        '603986', 'atr_channel', 'buy', 1_000, 100.,
        signal_date='2026-01-05', fusion_votes=2,
        fusion_label='two_strategy_confirmation',
    )
    turtle = Signal(
        '603986', 'turtle_breakout', 'buy', 1_000, 100.,
        signal_date='2026-01-05', fusion_votes=2,
        fusion_label='two_strategy_confirmation',
    )
    state.pending = [
        (atr, SimpleNamespace(name='atr_channel')),
        (turtle, SimpleNamespace(name='turtle_breakout')),
    ]
    events = []
    engine._apply_account_risk_budget(
        [state], dates[0], 90_000., 100_000., events,
        preserve_strategy_valid_holdings=True,
    )
    assert [signal.target_shares for signal, _ in state.pending] == [1_000, 1_000]
    assert events[-1]['repeated_proven_reentry_symbols'] == ['603986']
    assert events[-1]['repeated_reentry_admitted_buy_indexes'] == [1]


def test_budget_derives_live_shock_reduction_and_blocks_same_cycle_pyramid():
    """Catch a filled shock trim being repurchased before a native cycle reset."""
    engine, state, dates = fixture(shares=5_000, cash=50_000.)
    state.sleeve.trades.extend([
        TradeRecord(
            symbol='300308', strategy_name='turtle_breakout', direction='buy',
            shares=10_000, price=10., date='2025-09-01',
            reason='[two-strategy confirmation] Turtle breakout(ADX=30.0)',
            signal_date='2025-08-29',
        ),
        TradeRecord(
            symbol='300308', strategy_name='turtle_breakout', direction='sell',
            shares=5_000, price=10., date='2025-09-05',
            reason='account_budget_trim', signal_date='2025-09-04',
        ),
    ])
    pyramid = Signal(
        '300308', 'turtle_breakout', 'buy', 1_000, 10.,
        signal_date='2026-01-05', reason='Turtle pyramid add (unit 2)',
    )
    state.pending = [(pyramid, SimpleNamespace(name='turtle_breakout'))]
    events = [{
        'date': '2025-09-04',
        'event': 'account_budget_envelope',
        'observed_shock_confirmed': True,
        'new_reduction_orders': 1,
    }]

    engine._apply_account_risk_budget(
        [state], dates[0], 100_000., 100_000., events,
        preserve_strategy_valid_holdings=True,
    )

    assert state.pending == []
    assert events[-1]['shock_reduced_book_ids'] == [
        (0, '300308', 'turtle_breakout'),
    ]
    assert events[-1]['confirmed_shock_reduced_book_ids'] == [
        (0, '300308', 'turtle_breakout'),
    ]
    assert events[-1]['crowded_shock_reduced_book_ids'] == []
    assert events[-1]['shock_reduced_pyramid_buy_indexes'] == [0]
    assert events[-1]['shock_reduced_pyramid_book_ids'] == [
        (0, '300308', 'turtle_breakout'),
    ]


@pytest.mark.parametrize(('crowded', 'blocked'), [(False, False), (True, True)])
def test_single_strategy_shock_cycle_consumes_originating_portfolio_capacity(
    crowded, blocked,
):
    """Single-strategy pyramids yield only when the originating book was crowded."""
    engine, state, dates = fixture(shares=5_000, cash=50_000.)
    state.sleeve.trades.extend([
        TradeRecord(
            symbol='300308', strategy_name='turtle_breakout', direction='buy',
            shares=10_000, price=10., date='2025-10-22',
            reason='[single-strategy probe] Turtle breakout(ADX=30.0)',
            signal_date='2025-10-21',
        ),
        TradeRecord(
            symbol='300308', strategy_name='turtle_breakout', direction='sell',
            shares=5_000, price=10., date='2025-11-03',
            reason='account_budget_trim', signal_date='2025-10-31',
        ),
    ])
    pyramid = Signal(
        '300308', 'turtle_breakout', 'buy', 1_000, 10.,
        signal_date='2026-01-05', reason='Turtle pyramid add (unit 2)',
    )
    state.pending = [(pyramid, SimpleNamespace(name='turtle_breakout'))]
    events = [{
        'date': '2025-10-31',
        'event': 'account_budget_envelope',
        'observed_shock_confirmed': True,
        'new_reduction_orders': 1,
        'crowded_portfolio': crowded,
    }]

    engine._apply_account_risk_budget(
        [state], dates[0], 100_000., 100_000., events,
        preserve_strategy_valid_holdings=True,
    )

    assert bool(state.pending) is not blocked
    assert events[-1]['shock_reduced_book_ids'] == [
        (0, '300308', 'turtle_breakout'),
    ]
    assert events[-1]['confirmed_shock_reduced_book_ids'] == []
    assert events[-1]['crowded_shock_reduced_book_ids'] == (
        [(0, '300308', 'turtle_breakout')] if crowded else []
    )
    assert events[-1]['shock_reduced_pyramid_buy_indexes'] == (
        [0] if blocked else []
    )


def test_budget_keeps_first_proven_reentry_under_ordinary_turtle_budget():
    """One successful prior cycle is not repeated evidence."""
    engine, state, dates = fixture(shares=0, cash=90_000.)
    state.sleeve.positions = {}
    state.data_map = {'603986': state.data_map.pop('300308')}
    state.sleeve.trades.extend([
        TradeRecord(
            symbol='603986', strategy_name='atr_channel', direction='buy',
            shares=1_000, price=10., date='2025-09-15',
            reason='[two-strategy confirmation] ATR range breakout(ADX=35.9)',
            signal_date='2025-09-12',
        ),
        TradeRecord(
            symbol='603986', strategy_name='atr_channel', direction='sell',
            shares=1_000, price=9., date='2025-11-25',
            reason='account_budget_trim', signal_date='2025-11-24',
            pnl=-1_000., net_cash_flow=9_000., peak_close=12.2,
        ),
    ])
    turtle = Signal(
        '603986', 'turtle_breakout', 'buy', 1_000, 100.,
        signal_date='2026-01-05', fusion_votes=2,
        fusion_label='two_strategy_confirmation',
    )
    state.pending = [(turtle, SimpleNamespace(name='turtle_breakout'))]
    events = []
    engine._apply_account_risk_budget(
        [state], dates[0], 90_000., 100_000., events,
        preserve_strategy_valid_holdings=True,
    )
    assert state.pending[0][0].target_shares < 1_000
    assert events[-1]['repeated_proven_reentry_symbols'] == []


def test_budget_derives_proven_early_dual_transition_from_completed_atr_cycle():
    """Catch a completed durable trend being forgotten at an early MA transition."""
    engine, state, dates = fixture(shares=0, cash=90_000.)
    state.sleeve.positions = {}
    state.indicator_map = {
        '300308': {'rsi': pd.Series([54., 54., 54.], index=dates)}
    }
    state.sleeve.trades.extend([
        TradeRecord(
            symbol='300308', strategy_name='atr_channel', direction='buy',
            shares=1_000, price=10., date='2025-06-24',
            reason='[single-strategy probe] ATR channel breakout(ADX=61.6)',
            signal_date='2025-06-23',
        ),
        TradeRecord(
            symbol='300308', strategy_name='atr_channel', direction='sell',
            shares=1_000, price=15., date='2025-12-09',
            reason='ATR trailing stop', signal_date='2025-12-08',
            pnl=5_000., net_cash_flow=15_000., peak_close=15.,
        ),
    ])
    dual = Signal(
        '300308', 'dual_ma', 'buy', 10_000, 10.,
        signal_date='2026-01-05', fusion_votes=1,
    )
    state.pending = [(dual, SimpleNamespace(name='dual_ma'))]
    events = []
    engine._apply_account_risk_budget(
        [state], dates[0], 90_000., 100_000., events,
        preserve_strategy_valid_holdings=True,
    )
    assert state.pending[0][0].target_shares == 10_000
    assert events[-1]['proven_early_dual_book_ids'] == [
        (0, '300308', 'dual_ma'),
    ]
    assert events[-1]['proven_dual_admitted_buy_indexes'] == [0]


def test_confirmed_multigroup_shock_funds_relief_from_direct_hit_books_first():
    """Group confirmation must not pro-rata sell a held non-hit winner."""
    engine, state, _ = fixture(shares=10_700, cash=20_000.)
    dates = pd.to_datetime(['2025-08-29', '2025-09-01', '2025-09-02'])
    losses = {
        '300308': 0.05460835186557933,
        '300502': 0.0782181437190651,
        '300394': 0.10403598808844972,
        '688008': 0.0711175616835994,
        '603986': 0.0711175616835994,
    }
    closes = {
        '300308': 382.6,
        '300502': 255.14,
        '300394': 141.41,
        '688008': 100.,
        '603986': 100.,
    }
    frames = {}
    for symbol, loss in losses.items():
        close = closes[symbol]
        previous = close / (1. - loss)
        frames[symbol] = pd.DataFrame(
            {
                'open': [previous, previous, close],
                'close': [previous, previous, close],
                'high': [previous, previous, close],
                'low': [previous, previous, close],
                'volume': [1e8, 1e8, 1e8],
            },
            index=dates,
        )

    base = state.sleeve.positions['300308']['turtle_breakout']
    base.shares = 10_700
    base.entry_price = 100.
    base.entry_date = '2025-01-01'
    base.last_buy_date = '2025-01-01'
    second = deepcopy(base)
    second.symbol = '300502'
    second.shares = 17_600
    third = deepcopy(base)
    third.symbol = '300394'
    third.shares = 300
    state.sleeve.positions = {
        '300308': {'turtle_breakout': base},
        '300502': {'turtle_breakout': second},
        '300394': {'turtle_breakout': third},
    }
    state.data_map = frames
    state.all_dates = list(dates)
    state.date_to_pos = {date: i for i, date in enumerate(dates)}
    state.pending = []
    events = []

    engine._apply_account_risk_budget(
        [state], dates[-1], 8_638_569.13255525, 9_261_005.13255525, events,
    )

    receipt = events[-1]
    reductions = {
        signal.symbol: signal.target_shares
        for signal, _ in state.pending
        if signal.direction == 'sell' and signal.reason == 'account_budget_trim'
    }
    assert receipt['observed_shock_confirmed'] is True
    assert receipt['shocked_group_count'] == 2
    assert reductions.get('300502', 0) * closes['300502'] + reductions.get('300394', 0) * closes['300394'] >= receipt['gross_before'] - receipt['gross_cap']
    assert '300308' not in reductions


def test_snapshot_account_budget_uses_same_close_known_shock_evidence(monkeypatch):
    """Manual account advice must not silently omit replay shock evidence."""
    from quantfusion.account.models import AccountPosition, AccountSnapshot
    from quantfusion.application import account_scan

    dates = pd.to_datetime(['2025-08-29', '2025-09-01', '2025-09-02'])
    losses = {
        '300308': 0.05460835186557933,
        '300502': 0.0782181437190651,
        '300394': 0.10403598808844972,
        '688008': 0.0711175616835994,
        '603986': 0.0711175616835994,
    }
    closes = {
        '300308': 382.6,
        '300502': 255.14,
        '300394': 141.41,
        '688008': 100.,
        '603986': 100.,
    }
    cfg = default_engine_config()
    prepared = {}
    for symbol, loss in losses.items():
        close = closes[symbol]
        previous = close / (1. - loss)
        frame = pd.DataFrame(
            {
                'open': [previous, previous, close],
                'close': [previous, previous, close],
                'high': [previous, previous, close],
                'low': [previous, previous, close],
                'volume': [1e8, 1e8, 1e8],
            },
            index=dates,
        )
        prepared[symbol] = (frame, '2025-09-02', cfg, {})
    positions = (
        AccountPosition('300308', 10_700, 10_700, 100., '2025-01-01'),
        AccountPosition('300502', 17_600, 17_600, 100., '2025-01-01'),
        AccountPosition('300394', 300, 300, 100., '2025-01-01'),
    )
    snapshot = AccountSnapshot(
        3, 'main', '2025-09-02', 20_000., 9_261_005.13255525, positions,
    )
    actions = [
        {
            'symbol': position.symbol,
            'shares': position.shares,
            'sellable_shares': position.sellable_shares,
            'close': closes[position.symbol],
            'action': 'HOLD',
            'recommended_shares': 0,
            'blocked_shares': 0,
            'execution_status': 'NO_ACTION',
            'reason': 'hold',
        }
        for position in positions
    ]
    monkeypatch.setattr(
        account_scan.SleeveBacktestEngine,
        '_allocation_scores',
        lambda self, frames, date: {},
    )

    receipt = account_scan.AccountSignalEngine._apply_account_budget(
        snapshot,
        prepared,
        actions,
        equity=8_638_569.13255525,
        as_of='2025-09-02',
    )

    assert receipt['observed_shock_confirmed'] is True
    assert receipt['shocked_group_count'] == 2
    assert receipt['planned_reductions']
    by_symbol = {row['symbol']: row for row in actions}
    assert by_symbol['300308']['action'] == 'HOLD'
    assert by_symbol['300502']['action'] == 'REDUCE_REVIEW'
    assert by_symbol['300394']['action'] == 'REDUCE_REVIEW'


def test_close_budget_does_not_depend_on_future_candidate_bar():
    engine, state, dates = fixture(shares=2_000, cash=70_000.)
    engine._runtime_tradable_count = 3
    engine._new_candidate_intent_streak = {}
    state.sleeve._tradable_symbol_codes = {'300308', '300502', '603986'}
    state.sleeve._fixed_reference_scores = lambda date, candidates: {symbol: 1. for symbol in candidates}
    for symbol in ('300502', '603986'):
        state.data_map[symbol] = state.data_map['300308'].copy()
        state.pending.append((Signal(symbol, 'turtle_breakout', 'buy', 5_000, 10.,
                                     signal_date='2026-01-05', reason='entry'),
                              SimpleNamespace(name='turtle_breakout')))
    decisions = []
    for missing in (False, True):
        variant = deepcopy(state)
        if missing:
            variant.data_map['603986'] = variant.data_map['603986'].drop(dates[1])
        _, eligible = engine._authorize_portfolio_buys(
            [variant], dates[1], carried_symbols=engine._held_portfolio_symbols([variant]),
            preview_only=True)
        engine._apply_account_risk_budget(
            [variant], dates[0], 90_000., 100_000., [],
            portfolio_evidence_buy_symbols=eligible, preserve_strategy_valid_holdings=True)
        decisions.append({signal.symbol: signal.target_shares for signal, _ in variant.pending})
    assert decisions[0] == decisions[1]
