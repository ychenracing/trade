"""均线趋势策略（多策略组合中的「慢趋势」成员）。

与海龟（突破型）互补，采用均线多头排列确认趋势：
- 入场：收盘价站上快线，且快线位于慢线之上（多头趋势确认），建 1 个单位。
- 加仓：价格较建仓价再涨 1×ATR 时加 1 个单位，最多 2 个单位。
- 离场：收盘价跌破快线（趋势转弱），或盘中触及 2×ATR 硬止损。

特点：信号更平滑、交易频率低于海龟，用于在组合中降低整体回撤与换手。
"""
from typing import List

import pandas as pd

from .base import Order, Strategy


class MATrendStrategy(Strategy):
    def __init__(self, symbol: str, capital: float, cfg, max_units: int = 2):
        super().__init__(symbol, capital, cfg)
        self.id = "ma_trend"
        self.max_units = max_units

    def on_bar(self, bar: pd.Series) -> List[Order]:
        orders: List[Order] = []
        close = bar["close"]
        low = bar["low"]
        atr = bar["atr"]
        if pd.isna(close) or pd.isna(low) or pd.isna(atr):
            return orders
        close = float(close)
        low = float(low)
        atr = float(atr)
        ma_fast = bar.get("ma_fast")
        ma_slow = bar.get("ma_slow")

        if pd.isna(atr) or pd.isna(ma_fast) or pd.isna(ma_slow):
            return orders

        if self.in_position:
            # 0) 更新 ATR 吊灯止损（随最高价上移；trail_multiple=0 时退化为固定硬止损）
            self._update_trailing_stop(close, atr)

            if self.stop_price is not None and low <= self.stop_price:
                orders.append(
                    Order(self.symbol, "SELL", self._total_shares(), self.id,
                          f"stop_loss@{self.stop_price:.2f}", limit_price=self.stop_price)
                )
                self._reset_position()
                return orders

            if getattr(self.cfg, "target_atr_pct", 0) > 0:
                # 波动率目标化（非对称）：满仓吃主升浪；仅「自峰值回撤超阈值(且高波动)」才减仓压回撤
                peak = self.highest_close if self.highest_close is not None else close
                in_drawdown = close < peak * (1.0 - self.cfg.vt_trim_band)
                if in_drawdown and atr > 0:
                    scale = self._vol_scale(atr, close)
                    desired = max(1, min(self.max_units, int(round(self.max_units * scale))))
                    if desired < self.units:
                        sell = (self.units - desired) * self.unit_shares
                        if sell >= self.cfg.min_lot:
                            orders.append(Order(self.symbol, "SELL", sell, self.id, f"vol_trim@{close:.2f}"))
                            self.units = desired
                            self.last_add_price = close
                            return orders  # 波动减仓当根不再检查离场，避免下根开盘同日买卖违反 T+1
                elif self.units < self.max_units and not in_drawdown and self.last_add_price is not None \
                        and atr > 0 and close >= self.last_add_price + self.cfg.ma_pyramid_step * atr:
                    add_shares = self.unit_shares
                    if add_shares > 0:
                        orders.append(Order(self.symbol, "BUY", add_shares, self.id, f"vol_add@{close:.2f}"))
                        self.units += 1
                        self.entry_prices.append(close)
                        self.last_add_price = close
                        return orders
            else:
                if (
                    self.units < self.max_units
                    and self.last_add_price is not None
                    and atr > 0
                    and close >= self.last_add_price + self.cfg.ma_pyramid_step * atr
                ):
                    add_shares = self.unit_shares
                    if add_shares > 0:
                        orders.append(Order(self.symbol, "BUY", add_shares, self.id, f"add@{close:.2f}"))
                        self.units += 1
                        self.entry_prices.append(close)
                        self.last_add_price = close
                        # 加仓当根不再检查离场，避免下根开盘同日买卖违反 T+1
                        return orders

            if close < ma_fast:
                orders.append(
                    Order(self.symbol, "SELL", self._total_shares(), self.id, f"exit_ma@{close:.2f}")
                )
                self._reset_position()
            return orders

        # 空仓：多头排列确认后入场
        if close > ma_fast and ma_fast > ma_slow and atr > 0:
            unit_shares = self._compute_unit_shares(atr, close)
            if unit_shares > 0:
                orders.append(Order(self.symbol, "BUY", unit_shares, self.id, f"entry@{close:.2f}"))
                self.in_position = True
                self.units = 1
                self.entry_prices = [close]
                self.last_add_price = close
                self.unit_shares = unit_shares
                self.stop_price = close - self.cfg.stop_multiple * atr
                self.highest_close = close
        return orders
