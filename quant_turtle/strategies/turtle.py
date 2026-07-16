"""海龟法则 A 股版（趋势跟踪）。

经典海龟交易法则的 A 股适配：
- 仅做多（A 股融券受限、T+1，不适合做空趋势）。
- 入场：收盘价突破 N 日高点（唐奇安通道上轨）。
- 加仓：每上涨 0.5×ATR 加 1 个单位，最多 max_units 个单位。
- 离场：收盘价跌破 M 日低点（唐奇安通道下轨）。
- 止损：首单位建仓价下方 2×ATR 处硬止损。
- 仓位：以 ATR 度量波动，单位规模 = 单笔风险预算 / (ATR × 每手)。

提供两套参数以形成组合：
- turtle20：短周期（20 日突破 / 10 日离场），捕捉中短期趋势。
- turtle55：长周期（55 日突破 / 20 日离场），捕捉中长期趋势。
"""
from typing import List

import pandas as pd

from .base import Order, Strategy


class TurtleStrategy(Strategy):
    def __init__(self, symbol: str, capital: float, cfg, entry_period: int = 20, exit_period: int = 10):
        super().__init__(symbol, capital, cfg)
        self.entry_period = entry_period
        self.exit_period = exit_period
        self.id = f"turtle{entry_period}"

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
        upper = bar.get(f"donchian_upper_{self.entry_period}")
        lower = bar.get(f"donchian_lower_{self.exit_period}")

        if pd.isna(atr) or pd.isna(upper) or pd.isna(lower):
            return orders

        if self.in_position:
            # 0) 更新 ATR 吊灯止损（随最高价上移；trail_multiple=0 时退化为固定硬止损）
            self._update_trailing_stop(close, atr)

            # 1) 硬止损（盘中触及止损价即于下根开盘平仓）
            if self.stop_price is not None and low <= self.stop_price:
                orders.append(
                    Order(self.symbol, "SELL", self._total_shares(), self.id,
                          f"stop_loss@{self.stop_price:.2f}", limit_price=self.stop_price)
                )
                self._reset_position()
                return orders  # 本 bar 触发止损后不再操作，避免同日回转

            # 2) 持仓期单位数管理（波动率目标化 / 经典金字塔 二选一）
            if getattr(self.cfg, "target_atr_pct", 0) > 0:
                # 波动率目标化（非对称）：满仓吃主升浪；仅「自峰值回撤超阈值(且高波动)」才减仓压回撤。
                # 回撤带区分「常态 V 型回调」与「灾难性持续下跌」：带过窄会扇耳光(损收益)，过宽则减仓太晚(回撤压不低)，需折中。
                peak = self.highest_close if self.highest_close is not None else close
                in_drawdown = close < peak * (1.0 - self.cfg.vt_trim_band)
                if in_drawdown and atr > 0:
                    # 高波动下跌段：按 target/当前ATR% 缩放将持仓减至 desired 单位（下限 1）
                    scale = self._vol_scale(atr, close)
                    desired = max(1, min(self.cfg.max_units, int(round(self.cfg.max_units * scale))))
                    if desired < self.units:
                        sell = (self.units - desired) * self.unit_shares
                        if sell >= self.cfg.min_lot:
                            orders.append(Order(self.symbol, "SELL", sell, self.id, f"vol_trim@{close:.2f}"))
                            self.units = desired
                            self.last_add_price = close  # 回补需从当前价重新爬步，避免立即反手加回
                            return orders  # 波动减仓当根不再检查离场，避免下根开盘同日买卖违反 T+1
                elif self.units < self.cfg.max_units and not in_drawdown and self.last_add_price is not None \
                        and atr > 0 and close >= self.last_add_price + self.cfg.pyramid_step * atr:
                    # 非深跌段：维持/回补至满仓（每次 1 单位，步长闸门防追高）
                    add_shares = self.unit_shares
                    if add_shares > 0:
                        orders.append(Order(self.symbol, "BUY", add_shares, self.id, f"vol_add@{close:.2f}"))
                        self.units += 1
                        self.entry_prices.append(close)
                        self.last_add_price = close
                        return orders  # 本 bar 已加仓，不再检查离场，避免下根开盘同日买卖违反 T+1
            else:
                # 经典海龟金字塔加仓（未启用波动率目标化、或指标未就绪时）
                if (
                    self.units < self.cfg.max_units
                    and self.last_add_price is not None
                    and atr > 0
                    and close >= self.last_add_price + self.cfg.pyramid_step * atr
                ):
                    add_shares = self.unit_shares
                    if add_shares > 0:
                        orders.append(Order(self.symbol, "BUY", add_shares, self.id, f"add@{close:.2f}"))
                        self.units += 1
                        self.entry_prices.append(close)
                        self.last_add_price = close
                        # 加仓当根不再检查离场：否则下根开盘会同时成交「加仓买 + 离场卖」，
                        # 等同当日买入并卖出刚加的单位，违反 A 股 T+1。
                        return orders

            # 3) 趋势反转离场
            if close < lower:
                orders.append(
                    Order(self.symbol, "SELL", self._total_shares(), self.id, f"exit_breakdown@{close:.2f}")
                )
                self._reset_position()
            return orders

        # 空仓：突破入场
        if close > upper and atr > 0:
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
