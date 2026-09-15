"""Economic hypothesis: bound a lone joiner, not independent fresh confirmation."""
from types import SimpleNamespace

import pytest

from quantfusion.domain.models import Position, Signal
from quantfusion.engine.causal import CausalBacktestEngine


def engine_with(holdings):
    engine = CausalBacktestEngine(initial_capital=100000.)
    engine.positions = {'AAA': {name: Position('AAA', name, shares, 10., '2026-01-05') for name, shares in holdings.items()}}
    return engine


def buy(name='dual_ma', shares=5000):
    return Signal('AAA', name, 'buy', target_shares=shares, price=10., signal_date='2026-01-06'), SimpleNamespace(name=name)


def test_lone_joiner_cannot_dominate_current_symbol_inventory():
    engine = engine_with({'atr_channel': 200, 'turtle_breakout': 100})
    original, strategy = buy()
    out = engine._fuse_daily_signals([(original, strategy)], '2026-01-06')
    assert out[0][0].target_shares == 300
    assert original.target_shares == 5000
    assert engine.positions['AAA']['atr_channel'].shares == 200
    assert engine.order_events[-1]['event'] == 'scaled_late_strategy_join'


@pytest.mark.parametrize('holdings,name', [({}, 'dual_ma'), ({'dual_ma': 100}, 'dual_ma')])
def test_initial_entry_and_own_pyramid_keep_native_sizing(holdings, name):
    engine = engine_with(holdings)
    out = engine._fuse_daily_signals([buy(name)], '2026-01-06')
    assert out[0][0].target_shares == 4500


def test_two_independent_fresh_confirmations_keep_native_sizing():
    engine = engine_with({'turtle_breakout': 100})
    out = engine._fuse_daily_signals([buy('dual_ma'), buy('atr_channel')], '2026-01-06')
    assert [signal.target_shares for signal, _ in out] == [5000, 5000]


def test_mandatory_sell_keeps_priority_and_is_not_size_capped():
    engine = engine_with({'atr_channel': 300})
    sell = Signal('AAA', 'atr_channel', 'sell', target_shares=300, price=10., reason='portfolio-level drawdown liquidation', signal_date='2026-01-06')
    out = engine._fuse_daily_signals([buy(), (sell, SimpleNamespace(name='atr_channel'))], '2026-01-06')
    assert len(out) == 1
    assert out[0][0].direction == 'sell'
    assert out[0][0].target_shares == 300
