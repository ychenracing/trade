"""策略基类与订单结构。

设计要点（事件驱动、杜绝前视偏差、符合 A 股 T+1）：
- 策略在每根 bar 收盘后读取当根及历史数据，生成「下一根开盘执行」的订单。
- 所有订单均在下一根 bar 的开盘价撮合，因此「当日买入、当日卖出」在结构上不可能发生，
  天然满足 T+1。
- 止损单携带 limit_price（止损价），下根开盘若跳空低于止损价则按开盘价成交（更保守）。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

import pandas as pd


@dataclass
class Order:
    symbol: str
    action: str          # "BUY" / "SELL"
    shares: int          # 目标股数（正整手）
    strategy: str        # 来源策略 id
    reason: str = ""     # 下单理由（用于回测复盘）
    limit_price: Optional[float] = None  # 止损价；None 表示按下一根开盘价市价成交


class Strategy(ABC):
    """所有策略的基类，负责维护自身在单只标的上的持仓状态。"""

    id: str = "base"

    def __init__(self, symbol: str, capital: float, cfg):
        self.symbol = symbol
        self.capital = float(capital)   # 分配给本策略的资本（元）
        self.cfg = cfg
        # ---- 持仓状态 ----
        self.in_position: bool = False
        self.units: int = 0             # 当前持有的「海龟单位」数
        self.entry_prices: List[float] = []   # 每个单位的建仓价
        self.last_add_price: float = None     # 上次加仓参考价
        self.unit_shares: int = 0       # 每单位对应股数（基于建仓时 ATR 固定）
        self.stop_price: float = None   # 当前止损价
        self.highest_close: float = None  # 持仓期最高收盘价（用于 ATR 吊灯止损 ratchet）
        self.trades: List[dict] = []    # 本策略成交记录（仅供策略内部参考）

    # ---------- 工具方法 ----------
    def _total_shares(self) -> int:
        return self.units * self.unit_shares

    def _reset_position(self) -> None:
        self.in_position = False
        self.units = 0
        self.entry_prices = []
        self.last_add_price = None
        self.unit_shares = 0
        self.stop_price = None
        self.highest_close = None

    def _compute_unit_shares(self, atr: float, price: float = None) -> int:
        """根据 ATR 与分配给本策略的资本，计算 1 个单位对应的整手股数。

        单位股数 = floor(资本 × 单笔风险比例 / (ATR × 每手)) × 每手。
        price 参数保留以兼容调用点，波动率目标化不参与「单位股数」计算
        （满仓入场/加仓，仅在持仓期高波动深跌段才按波动缩放减仓，详见子类 on_bar）。
        """
        lot = self.cfg.min_lot
        if atr <= 0:
            return 0
        risk_cash = self.capital * self.cfg.risk_per_trade
        raw_units = risk_cash / (atr * lot)
        shares = int(raw_units) * lot
        return max(shares, 0)

    def _vol_scale(self, atr: float, price: float) -> float:
        """波动率缩放系数：高波动(ATR%高)→<1 缩仓；低波动→=cap 维持。

        无前视偏差：使用本根 bar 自身的 ATR 与收盘价计算，仅用于下一根开盘的撮合决策。
        """
        tgt = getattr(self.cfg, "target_atr_pct", 0)
        if not tgt or tgt <= 0 or price <= 0 or atr <= 0:
            return 1.0
        atr_pct = atr / price
        s = min(self.cfg.vol_target_cap, tgt / atr_pct)
        s = max(self.cfg.vol_target_floor, s)
        return s

    def _update_trailing_stop(self, close: float, atr: float) -> None:
        """ATR 吊灯止损（chandelier exit）：止损价随持仓期最高价上移，仅升不降。

        stop = max(原止损价, highest_close − trail_multiple×ATR)

        - trail_multiple<=0（默认）时不启用，止损价保持建仓时的固定硬止损（向后兼容）。
        - 启用后：价格创新高 → 止损价上移锁定利润；价格自高位回落 trail×ATR → 触发离场，
          从而在趋势反转时于更高位置离场，压低回撤、保留大部分趋势收益。

        注意：highest_close（持仓期最高收盘价）**始终**随创新高更新，与是否启用吊灯止损无关——
        它同时被波动率目标化的「方向性减仓闸门」复用（需真实的持仓期峰值）。
        调用约定：持仓状态下、在硬止损/加仓/离场判断**之前**每根 bar 调用一次。
        """
        if self.highest_close is None or close > self.highest_close:
            self.highest_close = close
        if self.cfg.trail_multiple and self.cfg.trail_multiple > 0 and atr > 0:
            trail = self.highest_close - self.cfg.trail_multiple * atr
            if self.stop_price is None or trail > self.stop_price:
                self.stop_price = trail

    def sync_position(self, actual_shares: int) -> None:
        """用组合实际持仓回填策略状态（修复：成交被现金截断或账户熔断清算后，
        策略内部状态与实际持仓脱节的问题）。「组合是唯一真相源」。

        - 实际持仓为 0：清空策略状态（含清算后卡死的 in_position）。
        - 实际持仓 > 0：标记为持仓中，但**不**按「实际股数 / 单位股数」回算 units。

        说明：信号期按足额成交预估的 units 只会 ≥ 实际（仅当现金不足被截断时才可能偏小）。
        保留信号期 units 是更保守的选择——即便发生截断，units 不会偏低，加仓闸门
        `units < max_units` 不会失效，从而杜绝「越权超加」。实际股数不足由组合层
        SELL 的 `min(order.shares, 实际持仓)` 兜底，不会产生孤儿仓。
        """
        if actual_shares <= 0:
            if self.in_position:
                self._reset_position()
            return
        self.in_position = True

    @abstractmethod
    def on_bar(self, bar: pd.Series) -> List[Order]:
        """输入当根 bar（含指标），返回下一根开盘执行的订单列表。

        约定：bar 的列至少包含 open/high/low/close/atr 及所需通道/均线列。
        """
        raise NotImplementedError
