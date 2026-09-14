"""Fresh strategy evidence, not a past reduction, determines new-risk eligibility."""
from copy import deepcopy
from types import SimpleNamespace

import pandas as pd
import pytest

from quantfusion.config.engine import default_engine_config
from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.domain.models import BarContext, Position, TradeRecord
from quantfusion.engine.ensemble import EnsembleSleeveBacktestEngine
from quantfusion.risk.account_budget import apply_account_risk_budget
from quantfusion.strategy.trend import TurtleBreakoutStrategy


def setup_signal(*, upper=131., adx=40.):
    cfg = default_engine_config()
    strategy = TurtleBreakoutStrategy(cfg)
    strategy.position = Position(
        '300308', strategy.name, 500, 100., '2025-06-06',
        stop_loss=80., highest_since_entry=133., highest_close_since_entry=132.,
        units=1, last_buy_date='2025-06-06', last_add_price=100.,
    )
    dates = pd.bdate_range(end='2026-01-05', periods=40)
    frame = pd.DataFrame({'close': [120.]*39+[132.], 'high': [121.]*39+[133.]}, index=dates)
    indicators = {name: pd.Series([value]*40, index=dates) for name, value in {
        'atr': 10., 'adx': adx, 'ma_short': 115., 'ma_long': 90.,
        'donchian_upper': upper, 'donchian_lower': 100.,
    }.items()}
    ctx = BarContext(39, frame, 1_000_000., indicators, '300308', '2026-01-05')
    return cfg, strategy, ctx


@pytest.mark.parametrize('upper,adx', [(135., 40.), (131., 1.)])
def test_old_fill_anchor_is_not_new_breakout_evidence(upper, adx):
    _, strategy, ctx = setup_signal(upper=upper, adx=adx)
    assert strategy.on_bar(ctx) is None


def test_fresh_breakout_uses_next_unit_sizing_without_restoring_a_past_quantity():
    cfg, strategy, ctx = setup_signal()
    signal = strategy.on_bar(ctx)
    assert signal is not None and signal.direction == 'buy'
    assert signal.target_shares == strategy._calc_shares(
        ctx.current_assets*cfg['strategy_weight'], 132., 10., unit_number=2)
    assert signal.signal_date == ctx.date
    assert strategy.position.shares == 500
    assert strategy.position.units == 1
    assert strategy.position.last_add_price == 100.


def test_genuine_new_signal_survives_budget_after_old_shock_has_ended():
    cfg, strategy, ctx = setup_signal()
    signal = strategy.on_bar(ctx)
    assert signal is not None
    sleeve = EnsembleSleeveBacktestEngine(
        1_000_000., cfg=cfg, policy=PortfolioPolicy(),
        allocation_lookbacks=(3, 5, 10), sleeve_name='base')
    sleeve._reset_run_state({'300308': 'test'})
    sleeve.positions = {'300308': {strategy.name: strategy.position}}
    sleeve.cash = 934_000.
    sleeve.trades = [
        TradeRecord('300308', 'base:turtle_breakout', 'buy', 2000, 100.,
                    '2025-06-06', reason='[two-strategy confirmation] Turtle breakout'),
        TradeRecord('300308', 'base:turtle_breakout', 'sell', 1500, 120.,
                    '2025-09-05', reason='account_budget_trim', signal_date='2025-09-04'),
    ]
    state = SimpleNamespace(sleeve=sleeve, data_map={'300308': ctx.df},
                            indicator_map={'300308': ctx.indicators}, pending=[(signal, strategy)])
    events = [{'date': '2025-09-04', 'event': 'account_budget_envelope',
               'observed_shock_confirmed': True, 'shock_episode_active': True,
               'new_reduction_orders': 1, 'crowded_portfolio': False}]
    history = deepcopy((sleeve.trades, events))
    apply_account_risk_budget([state], pd.Timestamp(ctx.date), 1_000_000., 1_000_000.,
                              cfg, lambda _: 1., events, preserve_strategy_valid_holdings=True)
    assert state.pending == [(signal, strategy)]
    assert events[-1]['shock_episode_active'] is False
    assert events[-1]['buy_scales'] == [1.]
    assert (sleeve.trades, events[:-1]) == history
    assert strategy.position.shares == 500
    assert strategy.position.units == 1


def test_future_prices_cannot_supply_current_growth_evidence():
    _, strategy, ctx = setup_signal(upper=135.)
    future = pd.DataFrame({'close': [1_000_000.], 'high': [1_000_001.]},
                          index=[pd.Timestamp('2026-01-06')])
    ctx.df = pd.concat([ctx.df, future])
    assert strategy.on_bar(ctx) is None


def test_required_exit_still_precedes_any_growth_eligibility():
    _, strategy, ctx = setup_signal()
    strategy.position.stop_loss = 140.
    signal = strategy.on_bar(ctx)
    assert signal is not None and signal.direction == 'sell'
    assert signal.target_shares == 500
