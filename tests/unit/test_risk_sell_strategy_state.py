"""Filled risk exits must leave strategy decisions consistent with the ledger."""
import pandas as pd
import pytest

from quantfusion.domain.models import BarContext, Position, Signal
from quantfusion.engine.core import CoreBacktestEngine
from quantfusion.strategy.trend import ATRChannelStrategy


@pytest.mark.parametrize('external', [False, True])
def test_filled_risk_exit_allows_new_strategy_entry(external):
    engine = CoreBacktestEngine()
    strategy = ATRChannelStrategy(engine.cfg)
    position = Position('300308', strategy.name, 1000, 80., '2025-01-02',
                        stop_loss=70., highest_since_entry=100., highest_close_since_entry=100.)
    strategy.position = position
    engine.positions = {'300308': {strategy.name: position}}
    registry = engine.external_strategy_instances if external else engine.strategy_instances
    registry['300308'] = [strategy]
    signal = Signal('300308', strategy.name, 'sell', 1000, 100.,
                    signal_date='2025-01-03', reason='account_budget_trim')
    assert engine._execute_sell(signal, None, '2025-01-06') == 1000
    assert '300308' not in engine.positions
    assert strategy.position is None
    frame = pd.DataFrame({'close': [120.] * 40, 'high': [121.] * 40})
    indicators = {name: pd.Series([value] * 40) for name, value in
                  {'atr': 2., 'adx': 40., 'ma_short': 100.}.items()}
    ctx = BarContext(39, frame, engine.cash, indicators, '300308', '2025-03-03')
    new = strategy.on_bar(ctx)
    assert new is not None and new.direction == 'buy' and new.target_shares > 0


def test_partial_risk_sale_retains_live_position_and_does_not_touch_other_book():
    engine = CoreBacktestEngine()
    strategy = ATRChannelStrategy(engine.cfg)
    other = ATRChannelStrategy(engine.cfg)
    position = Position('300308', strategy.name, 1000, 80., '2025-01-02')
    other_position = Position('300502', other.name, 500, 50., '2025-01-02')
    strategy.position, other.position = position, other_position
    engine.strategy_instances = {'300308': [strategy], '300502': [other]}
    engine.positions = {'300308': {strategy.name: position}, '300502': {other.name: other_position}}
    signal = Signal('300308', strategy.name, 'sell', 400, 100., signal_date='2025-01-03')
    assert engine._execute_sell(signal, None, '2025-01-06') == 400
    assert strategy.position is engine.positions['300308'][strategy.name]
    assert strategy.position.shares == 600
    assert other.position is other_position and other_position.shares == 500
