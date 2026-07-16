"""组合与风险管理层。

负责：
- 现金与持仓记账（按 (标的, 策略) 维度区分各策略子仓位）。
- 撮合成交并扣除佣金、滑点、印花税。
- 强制无杠杆：买入金额不得超过可用现金，否则按整手缩减。
- 账户级风控：权益跌破「初始资金 ×(1-最大可容忍亏损)」时清仓并停机。
"""
from dataclasses import dataclass, field
from typing import Dict, List

from .costs import effective_price, trade_value
from .strategies.base import Order


@dataclass
class Fill:
    date: str
    symbol: str
    strategy: str
    side: str
    shares: int
    price: float
    reason: str = ""


class Portfolio:
    def __init__(self, cfg):
        self.cfg = cfg
        self.initial = float(cfg.initial_capital)
        self.cash = float(cfg.initial_capital)
        self.positions: Dict[tuple, int] = {}      # (symbol, strategy) -> 股数
        self.avg_cost: Dict[tuple, float] = {}      # (symbol, strategy) -> 持仓均价
        self.halted = False
        self.peak_equity = float(cfg.initial_capital)  # 用于「峰值最大回撤」口径
        self.fills: List[Fill] = []
        self.equity_curve: List[dict] = []

    # ---------- 估值 ----------
    def market_value(self, prices: Dict[str, float]) -> float:
        mv = 0.0
        for (symbol, _), sh in self.positions.items():
            if sh > 0 and symbol in prices and prices[symbol] is not None:
                mv += sh * prices[symbol]
        return mv

    def equity(self, prices: Dict[str, float]) -> float:
        return self.cash + self.market_value(prices)

    # ---------- 撮合 ----------
    def execute(self, order: Order, fill_price: float, bar_date) -> None:
        if self.halted:
            return
        key = (order.symbol, order.strategy)
        lot = self.cfg.min_lot

        if order.action == "BUY":
            if order.shares <= 0:
                return
            # 无杠杆：买入金额不得超过可用现金（按「每股成本」计算可买整手数）
            eff = effective_price("BUY", fill_price, self.cfg.slippage)
            _, cash_delta = trade_value(
                "BUY", order.shares, fill_price, self.cfg.slippage,
                self.cfg.commission, self.cfg.stamp_duty,
            )
            per_share = -cash_delta / order.shares
            max_affordable = int(self.cash / per_share // lot) * lot
            shares = min(order.shares, max_affordable)
            if shares <= 0:
                return
            _, real_delta = trade_value(
                "BUY", shares, fill_price, self.cfg.slippage,
                self.cfg.commission, self.cfg.stamp_duty,
            )
            prev = self.positions.get(key, 0)
            prev_cost = self.avg_cost.get(key, eff)
            new = prev + shares
            self.avg_cost[key] = (prev * prev_cost + shares * eff) / new
            self.positions[key] = new
            self.cash += real_delta
            self.fills.append(Fill(bar_date, order.symbol, order.strategy, "BUY", shares, eff, order.reason))

        else:  # SELL
            sh = self.positions.get(key, 0)
            sell = min(order.shares, sh)
            if sell <= 0:
                return
            eff, cash_delta = trade_value(
                "SELL", sell, fill_price, self.cfg.slippage,
                self.cfg.commission, self.cfg.stamp_duty,
            )
            self.cash += cash_delta
            remaining = sh - sell
            self.positions[key] = remaining
            if remaining == 0:
                self.avg_cost.pop(key, None)
            self.fills.append(Fill(bar_date, order.symbol, order.strategy, "SELL", sell, eff, order.reason))

    # ---------- 账户级风控 ----------
    def check_drawdown_halt(self, prices: Dict[str, float]) -> bool:
        """权益回撤超过阈值则清仓并停机，返回是否触发。

        回撤口径由 config.max_drawdown_basis 决定：
          - "peak"   ：相对权益历史峰值（标准「最大回撤」定义，更保护收益）
          - "initial"：相对初始资金（即累计亏损达 max_total_risk 即停）
        """
        if self.halted:
            return True
        eq = self.equity(prices)
        self.peak_equity = max(self.peak_equity, eq)
        if self.cfg.max_drawdown_basis == "peak":
            threshold = self.peak_equity * (1.0 - self.cfg.max_total_risk)
        else:
            threshold = self.initial * (1.0 - self.cfg.max_total_risk)
        if eq < threshold:
            self.halted = True
            for (symbol, strategy), sh in list(self.positions.items()):
                if sh > 0 and symbol in prices and prices[symbol] is not None:
                    px = prices[symbol]
                    _, cash_delta = trade_value(
                        "SELL", sh, px, self.cfg.slippage,
                        self.cfg.commission, self.cfg.stamp_duty,
                    )
                    self.cash += cash_delta
                    self.fills.append(Fill("HALT", symbol, strategy, "SELL", sh, px, "max_drawdown_halt"))
                    self.positions[symbol, strategy] = 0
            self.avg_cost.clear()
            return True
        return False
