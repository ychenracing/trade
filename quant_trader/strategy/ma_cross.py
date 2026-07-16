"""
策略1: EMA均线交叉趋势跟踪
- 买入: EMA短线上穿EMA长线 + 价格在EMA趋势线上方 + 放量确认
- 卖出: EMA短线下穿EMA长线
"""
import pandas as pd
from strategy.base import BaseStrategy, Signal, StrategyResult
from utils.helpers import calc_ema, calc_momentum_score
from utils.logger import log


class EMACrossStrategy(BaseStrategy):
    """EMA均线交叉策略"""

    def __init__(self, params: dict, weight: float = 1.0):
        super().__init__("EMA_Cross", params, weight)
        self.min_data_days = max(
            params["ema_short"], params["ema_long"], params["ema_trend"]
        ) + 10

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算EMA指标"""
        df = df.copy()
        p = self.params
        df["ema_short"] = calc_ema(df["close"], p["ema_short"])
        df["ema_long"] = calc_ema(df["close"], p["ema_long"])
        df["ema_trend"] = calc_ema(df["close"], p["ema_trend"])
        # 成交量均线
        df["vol_ma5"] = df["volume"].rolling(window=5).mean()
        # 金叉/死叉标记
        df["cross"] = 0  # 0=无, 1=金叉, -1=死叉
        prev_above = (df["ema_short"].shift(1) > df["ema_long"].shift(1)).astype(bool)
        curr_above = (df["ema_short"] > df["ema_long"]).astype(bool)
        df.loc[~prev_above & curr_above, "cross"] = 1   # 金叉
        df.loc[prev_above & ~curr_above, "cross"] = -1   # 死叉
        return df

    def generate_signal(self, df: pd.DataFrame) -> StrategyResult:
        if not self._check_data_sufficient(df):
            return self._safe_signal(Signal.HOLD, 0, "数据不足", {})

        df = self.calculate_indicators(df)
        latest = df.iloc[-1]
        prev = df.iloc[-2]

        p = self.params
        close = latest["close"]
        ema_s = latest["ema_short"]
        ema_l = latest["ema_long"]
        ema_t = latest["ema_trend"]
        vol = latest["volume"]
        vol_ma5 = latest["vol_ma5"]
        cross = latest["cross"]

        data = {
            "close": close, "ema_short": ema_s, "ema_long": ema_l,
            "ema_trend": ema_t, "volume": vol, "vol_ma5": vol_ma5,
        }

        # ---- 买入信号 ----
        # 条件1: 金叉或短线在长线上方
        is_above = ema_s > ema_l
        # 条件2: 价格在趋势EMA上方（大趋势向上）
        trend_up = close > ema_t
        # 条件3: 均线多头排列 (ema_s > ema_l > ema_t)
        bullish_alignment = ema_s > ema_l > ema_t
        # 条件4: 放量确认
        volume_confirm = vol > vol_ma5 * p["volume_ratio"] if vol_ma5 > 0 else False

        if cross == 1 and trend_up and volume_confirm:
            strength = 0.8 if bullish_alignment else 0.6
            return self._safe_signal(
                Signal.BUY, strength,
                f"EMA金叉, 价格在EMA{p['ema_trend']}上方, 放量确认",
                data
            )

        # ---- 动量评分替代多头排列软买入 ----
        # 旧逻辑: is_above and trend_up and bullish_alignment → BUY 0.4
        # 新逻辑: 均线多头 + 0.1<动量<5 + R²>0.3 → BUY（strength随动量缩放）
        # 过热过滤: 动量>8 = 极端加速冲顶，不给BUY
        if is_above and trend_up and bullish_alignment:
            mom_score, r2 = calc_momentum_score(df["close"], 25)
            if mom_score > 0.1 and r2 > 0.15 and mom_score <= 8.0:
                strength = min(0.5, 0.2 + mom_score * 0.2)
                return self._safe_signal(
                    Signal.BUY, strength,
                    f"EMA多头排列+动量评分={mom_score:.2f}(R²={r2:.2f})",
                    data
                )
            elif mom_score > 8.0:
                return self._safe_signal(
                    Signal.HOLD, 0,
                    f"EMA多头排列但动量过热(评分={mom_score:.2f},可能冲顶)",
                    data
                )
            else:
                return self._safe_signal(
                    Signal.HOLD, 0,
                    f"EMA多头排列但动量不足(评分={mom_score:.2f},R²={r2:.2f})",
                    data
                )

        # ---- 卖出信号 ----
        if cross == -1:
            return self._safe_signal(
                Signal.SELL, 0.8,
                "EMA死叉",
                data
            )

        # 跌破趋势线
        if close < ema_t and prev["close"] > prev["ema_trend"]:
            return self._safe_signal(
                Signal.SELL, 0.7,
                f"价格跌破EMA{p['ema_trend']}趋势线",
                data
            )

        return self._safe_signal(Signal.HOLD, 0, "无明显信号", data)
