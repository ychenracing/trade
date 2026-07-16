"""
单元测试 - 策略信号生成
"""
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from strategy.ma_cross import EMACrossStrategy
from strategy.donchian import DonchianBreakoutStrategy
from strategy.macd_trend import MACDTrendStrategy
from strategy.combo import ComboStrategy
from strategy.base import Signal
from config.strategy_config import EMA_CROSS_CONFIG, DONCHIAN_CONFIG, MACD_TREND_CONFIG
from utils.helpers import calc_ema, calc_sma, calc_atr, calc_macd, round_lot, calc_price_limit


def generate_test_data(days: int = 120, trend: str = "up") -> pd.DataFrame:
    """生成测试用K线数据"""
    dates = pd.date_range(end=datetime.now(), periods=days, freq="B")

    if trend == "up":
        # 上升趋势
        close = np.cumsum(np.random.randn(days) * 0.5 + 0.3) + 10
    elif trend == "down":
        # 下降趋势
        close = np.cumsum(np.random.randn(days) * 0.5 - 0.3) + 30
    elif trend == "flat":
        # 横盘
        close = np.cumsum(np.random.randn(days) * 0.5) + 20
    else:
        close = np.random.randn(days) * 1 + 20

    close = np.maximum(close, 1)  # 确保正价格
    high = close + np.abs(np.random.randn(days)) * 0.3
    low = close - np.abs(np.random.randn(days)) * 0.3
    open_ = (high + low) / 2
    volume = np.random.randint(1000000, 5000000, days).astype(float)

    df = pd.DataFrame({
        "date": dates,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "amount": volume * close,
    })
    return df


class TestEMACrossStrategy:
    """EMA均线交叉策略测试"""

    def test_upward_trend_generates_buy(self):
        """上升趋势应产生买入信号"""
        df = generate_test_data(120, "up")
        strategy = EMACrossStrategy(EMA_CROSS_CONFIG["params"])
        result = strategy.generate_signal(df)
        # 上升趋势中不应出现卖出信号
        assert result.signal != Signal.SELL

    def test_downward_trend_generates_sell_or_hold(self):
        """下降趋势应产生卖出或持有信号"""
        df = generate_test_data(120, "down")
        strategy = EMACrossStrategy(EMA_CROSS_CONFIG["params"])
        result = strategy.generate_signal(df)
        assert result.signal in [Signal.SELL, Signal.HOLD]

    def test_insufficient_data_returns_hold(self):
        """数据不足应返回持有"""
        df = generate_test_data(10, "up")
        strategy = EMACrossStrategy(EMA_CROSS_CONFIG["params"])
        result = strategy.generate_signal(df)
        assert result.signal == Signal.HOLD
        assert "数据不足" in result.reason


class TestDonchianStrategy:
    """唐奇安通道突破策略测试"""

    def test_upward_trend_generates_buy(self):
        """上升趋势应产生买入信号"""
        df = generate_test_data(60, "up")
        strategy = DonchianBreakoutStrategy(DONCHIAN_CONFIG["params"])
        result = strategy.generate_signal(df)
        assert result.signal != Signal.SELL

    def test_insufficient_data(self):
        """数据不足"""
        df = generate_test_data(5, "up")
        strategy = DonchianBreakoutStrategy(DONCHIAN_CONFIG["params"])
        result = strategy.generate_signal(df)
        assert result.signal == Signal.HOLD


class TestMACDStrategy:
    """MACD策略测试"""

    def test_upward_trend(self):
        """上升趋势"""
        df = generate_test_data(80, "up")
        strategy = MACDTrendStrategy(MACD_TREND_CONFIG["params"])
        result = strategy.generate_signal(df)
        assert result.signal != Signal.SELL

    def test_insufficient_data(self):
        """数据不足"""
        df = generate_test_data(5, "up")
        strategy = MACDTrendStrategy(MACD_TREND_CONFIG["params"])
        result = strategy.generate_signal(df)
        assert result.signal == Signal.HOLD


class TestComboStrategy:
    """组合策略测试"""

    def test_combo_initialization(self):
        """组合策略初始化"""
        combo = ComboStrategy()
        assert len(combo.strategies) >= 2
        assert "EMA_Cross" in combo.get_strategy_names()

    def test_combo_signal_generation(self):
        """组合策略信号生成"""
        df = generate_test_data(120, "up")
        combo = ComboStrategy()
        result = combo.generate_signal(df)
        assert result.signal in [Signal.BUY, Signal.SELL, Signal.HOLD]
        assert len(result.reason) > 0


class TestHelpers:
    """工具函数测试"""

    def test_round_lot(self):
        assert round_lot(150) == 100
        assert round_lot(99) == 0
        assert round_lot(250) == 200
        assert round_lot(1000) == 1000

    def test_calc_price_limit(self):
        up, down = calc_price_limit(10.0, "main")
        assert up == 11.0
        assert down == 9.0

        up, down = calc_price_limit(10.0, "star")
        assert up == 12.0
        assert down == 8.0

    def test_calc_ema(self):
        series = pd.Series([1, 2, 3, 4, 5])
        ema = calc_ema(series, 3)
        assert len(ema) == 5
        assert ema.iloc[-1] > ema.iloc[0]  # 上升

    def test_calc_macd(self):
        close = pd.Series(np.random.randn(50).cumsum() + 10)
        dif, dea, hist = calc_macd(close)
        assert len(dif) == len(close)
        assert len(dea) == len(close)
        assert len(hist) == len(close)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
