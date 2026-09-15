"""A fresh range breakout can re-admit a mature trend without channel extension."""
import pandas as pd

from quantfusion.config.engine import default_engine_config
from quantfusion.domain.models import BarContext, Position
from quantfusion.strategy.trend import ATRChannelStrategy, TurtleBreakoutStrategy


def test_mature_trend_breakout_can_enter_before_extended_atr_channel():
    cfg = default_engine_config()
    strategy = ATRChannelStrategy(cfg)
    frame = pd.DataFrame({'close': [110.] * 39 + [115.], 'high': [111.] * 39 + [116.]})
    indicators = {name: pd.Series([value] * 40) for name, value in
                  {'atr': 8., 'adx': 40., 'ma_short': 100., 'ma_long': 80., 'donchian_upper': 112.}.items()}
    indicators['ma_short'].iloc[-2] = 99.
    ctx = BarContext(39, frame, 2e6, indicators, '300308', '2025-03-03')
    signal = strategy.on_bar(ctx)
    assert signal is not None and signal.direction == 'buy'
    assert signal.signal_date == ctx.date
    indicators['ma_long'].iloc[-1] = 105.
    assert strategy.on_bar(ctx) is None


def test_turtle_does_not_queue_an_add_after_a_limit_sized_close_jump():
    cfg = default_engine_config()
    strategy = TurtleBreakoutStrategy(cfg)
    strategy.position = Position(
        '688300', strategy.name, 1200, 100., '2026-03-10',
        stop_loss=80., highest_since_entry=121., highest_close_since_entry=100.,
        units=1, last_buy_date='2026-03-10', last_add_price=100.,
    )
    dates = pd.bdate_range('2026-01-15', periods=40)
    frame = pd.DataFrame({
        'close': [100.] * 39 + [120.5],
        'high': [101.] * 39 + [121.],
    }, index=dates)
    indicators = {name: pd.Series([value] * 40) for name, value in {
        'atr': 8., 'adx': 40., 'ma_short': 100., 'ma_long': 80.,
        'donchian_upper': 112., 'donchian_lower': 90.,
    }.items()}
    ctx = BarContext(39, frame, 2e6, indicators, '688300', '2026-03-10')
    assert strategy.on_bar(ctx) is None


def test_turtle_does_not_chase_a_missed_add_on_a_retracement():
    cfg = default_engine_config()
    strategy = TurtleBreakoutStrategy(cfg)
    strategy.position = Position(
        '688300', strategy.name, 1200, 100., '2026-03-10',
        stop_loss=80., highest_since_entry=121., highest_close_since_entry=120.5,
        units=1, last_buy_date='2026-03-10', last_add_price=100.,
    )
    dates = pd.bdate_range(end='2026-03-11', periods=40)
    frame = pd.DataFrame({
        'close': [100.] * 38 + [120.5, 119.],
        'high': [101.] * 38 + [121., 120.],
    }, index=dates)
    indicators = {name: pd.Series([value] * 40) for name, value in {
        'atr': 8., 'adx': 40., 'ma_short': 100., 'ma_long': 80.,
        'donchian_upper': 112., 'donchian_lower': 90.,
    }.items()}
    ctx = BarContext(39, frame, 2e6, indicators, '688300', '2026-03-11')
    assert strategy.on_bar(ctx) is None


def test_turtle_keeps_ordinary_add_semantics_without_a_missed_limit_advance():
    cfg = default_engine_config()
    strategy = TurtleBreakoutStrategy(cfg)
    strategy.position = Position(
        '688300', strategy.name, 1200, 100., '2026-03-10',
        stop_loss=80., highest_since_entry=121., highest_close_since_entry=120.5,
        units=1, last_buy_date='2026-03-10', last_add_price=100.,
    )
    dates = pd.bdate_range(end='2026-03-11', periods=40)
    frame = pd.DataFrame({
        'close': [100.] * 37 + [115., 120.5, 119.],
        'high': [101.] * 37 + [116., 121., 120.],
    }, index=dates)
    indicators = {name: pd.Series([value] * 40, index=dates) for name, value in {
        'atr': 8., 'adx': 40., 'ma_short': 100., 'ma_long': 80.,
        'donchian_upper': 112., 'donchian_lower': 90.,
    }.items()}
    ctx = BarContext(39, frame, 2e6, indicators, '688300', '2026-03-11')
    signal = strategy.on_bar(ctx)
    assert signal is not None and 'pyramid add' in signal.reason

