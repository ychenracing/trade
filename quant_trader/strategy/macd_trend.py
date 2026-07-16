"""
策略3: MACD趋势确认
- 买入: MACD金叉 + 柱状图转正 + 价格在MA60上方
- 卖出: MACD死叉 或 柱状图连续缩短
"""
import pandas as pd
from strategy.base import BaseStrategy, Signal, StrategyResult
from utils.helpers import calc_macd, calc_sma, calc_momentum_score
from utils.logger import log


class MACDTrendStrategy(BaseStrategy):
    """MACD趋势跟踪策略"""

    def __init__(self, params: dict, weight: float = 1.0):
        super().__init__("MACD_Trend", params, weight)
        self.min_data_days = max(params["slow"], params["ma_filter"]) + 10

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算MACD指标"""
        df = df.copy()
        p = self.params
        dif, dea, hist = calc_macd(df["close"], p["fast"], p["slow"], p["signal"])
        df["dif"] = dif
        df["dea"] = dea
        df["hist"] = hist
        df["ma_filter"] = calc_sma(df["close"], p["ma_filter"])
        # MACD金叉/死叉
        df["macd_cross"] = 0
        prev_above = (df["dif"].shift(1) > df["dea"].shift(1)).astype(bool)
        curr_above = (df["dif"] > df["dea"]).astype(bool)
        df.loc[~prev_above & curr_above, "macd_cross"] = 1
        df.loc[prev_above & ~curr_above, "macd_cross"] = -1
        # 柱状图变化趋势
        df["hist_change"] = df["hist"].diff()
        return df

    def generate_signal(self, df: pd.DataFrame) -> StrategyResult:
        if not self._check_data_sufficient(df):
            return self._safe_signal(Signal.HOLD, 0, "数据不足", {})

        df = self.calculate_indicators(df)
        latest = df.iloc[-1]

        p = self.params
        close = latest["close"]
        dif = latest["dif"]
        dea = latest["dea"]
        hist = latest["hist"]
        ma = latest["ma_filter"]
        macd_cross = latest["macd_cross"]
        hist_change = latest["hist_change"]

        data = {
            "close": close, "dif": dif, "dea": dea,
            "hist": hist, "ma_filter": ma,
        }

        # 趋势过滤：价格在MA上方
        trend_up = close > ma if not pd.isna(ma) else False

        # ---- 买入信号 ----
        if macd_cross == 1 and trend_up and hist > p["hist_threshold"]:
            strength = 0.85 if dif > 0 else 0.6
            return self._safe_signal(
                Signal.BUY, strength,
                f"MACD金叉, DIF={dif:.3f}, DEA={dea:.3f}, 价格在MA{p['ma_filter']}上方",
                data
            )

        # ---- 动量评分替代零轴上方软买入 ----
        # 旧逻辑: dif > 0 and dea > 0 and dif > dea and trend_up → BUY 0.5
        # 新逻辑: MACD强势区 + 0.1<动量<8 + R²>0.3 → BUY（strength随动量缩放）
        # 过热过滤: 动量>8 不给BUY
        if dif > 0 and dea > 0 and dif > dea and trend_up:
            mom_score, r2 = calc_momentum_score(df["close"], 25)
            if mom_score > 0.1 and r2 > 0.15 and mom_score <= 8.0:
                strength = min(0.55, 0.25 + mom_score * 0.2)
                return self._safe_signal(
                    Signal.BUY, strength,
                    f"MACD零轴上方+动量评分={mom_score:.2f}(R²={r2:.2f})",
                    data
                )
            elif mom_score > 8.0:
                return self._safe_signal(
                    Signal.HOLD, 0,
                    f"MACD强势区但动量过热(评分={mom_score:.2f})",
                    data
                )
            else:
                return self._safe_signal(
                    Signal.HOLD, 0,
                    f"MACD强势区但动量不足(评分={mom_score:.2f},R²={r2:.2f})",
                    data
                )

        # ---- 卖出信号 ----
        if macd_cross == -1:
            return self._safe_signal(
                Signal.SELL, 0.8,
                f"MACD死叉, DIF={dif:.3f}, DEA={dea:.3f}",
                data
            )

        # 柱状图连续3天缩短（动能衰减）
        if len(df) >= 4:
            hist_shrinking = all(df["hist_change"].iloc[-3:] < 0)
            if hist_shrinking and hist > 0:
                return self._safe_signal(
                    Signal.SELL, 0.6,
                    "MACD柱状图连续缩短, 动能衰减",
                    data
                )

        # 跌破MA过滤线
        prev_close = df.iloc[-2]["close"]
        prev_ma = df.iloc[-2]["ma_filter"]
        if close < ma and prev_close > prev_ma:
            return self._safe_signal(
                Signal.SELL, 0.5,
                f"价格跌破MA{p['ma_filter']}",
                data
            )

        return self._safe_signal(Signal.HOLD, 0, "MACD无明显信号", data)
