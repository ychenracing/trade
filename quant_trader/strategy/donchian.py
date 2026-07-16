"""
策略2: 唐奇安通道突破（海龟交易法核心）
- 买入: 价格突破N日最高价 + 突破幅度大于ATR×阈值
- 卖出: 价格跌破M日最低价
"""
import pandas as pd
from strategy.base import BaseStrategy, Signal, StrategyResult
from utils.helpers import calc_donchian, calc_atr, calc_momentum_score
from utils.logger import log


class DonchianBreakoutStrategy(BaseStrategy):
    """唐奇安通道突破策略"""

    def __init__(self, params: dict, weight: float = 1.0):
        super().__init__("Donchian_Breakout", params, weight)
        self.min_data_days = max(params["entry_period"], params["exit_period"]) + 10

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算唐奇安通道和ATR"""
        df = df.copy()
        p = self.params
        upper, lower, middle = calc_donchian(df["high"], df["low"], p["entry_period"])
        df["dc_upper"] = upper
        df["dc_lower"] = lower
        df["dc_middle"] = middle
        exit_upper, exit_lower, _ = calc_donchian(df["high"], df["low"], p["exit_period"])
        df["dc_exit_lower"] = exit_lower
        df["atr"] = calc_atr(df["high"], df["low"], df["close"], p["atr_period"])
        return df

    def generate_signal(self, df: pd.DataFrame) -> StrategyResult:
        if not self._check_data_sufficient(df):
            return self._safe_signal(Signal.HOLD, 0, "数据不足", {})

        df = self.calculate_indicators(df)
        latest = df.iloc[-1]
        prev = df.iloc[-2]

        p = self.params
        close = latest["close"]
        dc_upper = latest["dc_upper"]
        dc_lower = latest["dc_lower"]
        dc_exit_lower = latest["dc_exit_lower"]
        atr = latest["atr"]

        data = {
            "close": close, "dc_upper": dc_upper,
            "dc_lower": dc_lower, "dc_exit_lower": dc_exit_lower,
            "atr": atr, "dc_middle": latest["dc_middle"],
        }

        # ---- 买入信号: 突破N日高点 ----
        if not pd.isna(dc_upper) and close > dc_upper:
            # 突破幅度过滤
            breakout_pct = (close - dc_upper) / close if close > 0 else 0
            atr_pct = (atr * p["atr_filter_multiple"]) / close if close > 0 else 0

            if atr_pct == 0 or breakout_pct >= atr_pct:
                # 确认是否为向上突破（非跳空高开回踩）
                strength = 0.9 if close > prev["close"] else 0.6
                return self._safe_signal(
                    Signal.BUY, strength,
                    f"突破{p['entry_period']}日新高({dc_upper:.2f}), ATR={atr:.2f}",
                    data
                )

        # ---- 卖出信号: 跌破M日低点 ----
        if not pd.isna(dc_exit_lower) and close < dc_exit_lower:
            return self._safe_signal(
                Signal.SELL, 0.8,
                f"跌破{p['exit_period']}日新低({dc_exit_lower:.2f})",
                data
            )

        # ---- 动量评分+通道位置替代通道上部软买入 ----
        # 旧逻辑: pos > 0.7 → BUY 0.3
        # 新逻辑: 通道上部(pos>0.7) + 0.1<动量<8 + R²>0.3 → BUY
        # 过热过滤: 动量>8 不给BUY
        if not pd.isna(dc_upper) and not pd.isna(dc_lower) and dc_upper > dc_lower:
            pos = (close - dc_lower) / (dc_upper - dc_lower)
            if pos > 0.7:
                mom_score, r2 = calc_momentum_score(df["close"], 25)
                if mom_score > 0.1 and r2 > 0.15 and mom_score <= 8.0:
                    strength = min(0.4, 0.15 + mom_score * 0.2)
                    return self._safe_signal(
                        Signal.BUY, strength,
                        f"通道上部({pos:.0%})+动量评分={mom_score:.2f}(R²={r2:.2f})",
                        data
                    )
                elif mom_score > 8.0:
                    return self._safe_signal(
                        Signal.HOLD, 0,
                        f"通道上部但动量过热(评分={mom_score:.2f})",
                        data
                    )

        return self._safe_signal(Signal.HOLD, 0, "价格在通道中部", data)
