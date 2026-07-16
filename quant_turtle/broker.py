"""券商接口适配层。

本系统默认以「模拟（paper）」模式运行，绝不自动连接真实资金账户。
如需实盘自动交易，请在 LiveBroker 中接入你的券商 API（如 XTP、easytrader 等），
并自行承担风险与合规责任。实盘模式默认关闭，需显式启用且配置凭据。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .costs import trade_value
from .strategies.base import Order


@dataclass
class Bar:
    symbol: str
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


class Broker(ABC):
    @abstractmethod
    def get_cash(self) -> float: ...

    @abstractmethod
    def get_position(self, symbol: str, strategy: Optional[str] = None) -> int: ...

    @abstractmethod
    def get_last_bar(self, symbol: str) -> Optional[Bar]: ...

    @abstractmethod
    def submit_order(self, order: Order) -> Optional[float]:
        """提交订单，返回实际成交价；无法成交返回 None。"""
        ...

    @abstractmethod
    def get_all_positions(self) -> List[Tuple[str, str, int]]: ...


class PaperBroker(Broker):
    """模拟券商：用内存账本撮合，仅供策略验证与「模拟托管」演示。

    持仓按 (标的, 策略) 维度记账，避免多策略在同一标的上互相越权买卖（H4 修复）。
    """

    def __init__(self, initial_cash: float, commission: float = 0.0003,
                 slippage: float = 0.001, stamp_duty: float = 0.0005, min_lot: int = 100):
        self.cash = initial_cash
        self.positions: Dict[tuple, int] = {}   # (symbol, strategy) -> 股数
        self.commission = commission
        self.slippage = slippage
        self.stamp_duty = stamp_duty
        self.min_lot = min_lot
        self.last_bars: Dict[str, Bar] = {}
        self.fills = []
        self.halted = False   # 账户级风控熔断后置 True，拒绝一切新成交（与 Portfolio 一致）

    def update_bar(self, bar: Bar) -> None:
        self.last_bars[bar.symbol] = bar

    def get_cash(self) -> float:
        return self.cash

    def get_position(self, symbol: str, strategy: Optional[str] = None) -> int:
        if strategy is not None:
            return self.positions.get((symbol, strategy), 0)
        return sum(sh for (sym, _), sh in self.positions.items() if sym == symbol)

    def get_all_positions(self) -> List[Tuple[str, str, int]]:
        return [(sym, st, sh) for (sym, st), sh in self.positions.items() if sh > 0]

    def get_last_bar(self, symbol: str) -> Optional[Bar]:
        return self.last_bars.get(symbol)

    def submit_order(self, order: Order) -> Optional[float]:
        if self.halted:
            return None  # 熔断后拒绝一切新成交，避免停机后重新入场（BUG-2 修复）
        lot = self.min_lot
        bar = self.last_bars.get(order.symbol)
        if bar is None:
            return None
        key = (order.symbol, order.strategy)

        if order.action == "BUY":
            if order.shares <= 0:
                return None
            # 无杠杆：买入金额不得超过可用现金（按「每股成本」计算可买整手数）
            _, cash_delta = trade_value(
                "BUY", order.shares, bar.open, self.slippage,
                self.commission, self.stamp_duty,
            )
            per_share = -cash_delta / order.shares
            max_affordable = int(self.cash / per_share // lot) * lot
            shares = min(order.shares, max_affordable)
            if shares <= 0:
                return None
            eff, real_delta = trade_value(
                "BUY", shares, bar.open, self.slippage,
                self.commission, self.stamp_duty,
            )
            self.cash += real_delta
            self.positions[key] = self.positions.get(key, 0) + shares
            self.fills.append((bar.date, order.symbol, order.strategy, "BUY", shares, eff))
            return eff

        # SELL：止损单尊重 limit_price（与回测一致，H9）
        if order.limit_price is not None:
            fill_px = bar.open if bar.open <= order.limit_price else order.limit_price
        else:
            fill_px = bar.open
        sh = self.positions.get(key, 0)
        sell = min(order.shares, sh)
        if sell <= 0:
            return None
        eff, cash_delta = trade_value(
            "SELL", sell, fill_px, self.slippage,
            self.commission, self.stamp_duty,
        )
        self.cash += cash_delta
        self.positions[key] = sh - sell
        self.fills.append((bar.date, order.symbol, order.strategy, "SELL", sell, eff))
        return eff

    def liquidate_all(self) -> None:
        """账户级风控触发时清仓（按各标的最近一根收盘价撮合，含成本）。"""
        for (symbol, strategy), sh in list(self.positions.items()):
            if sh > 0:
                bar = self.last_bars.get(symbol)
                if bar is None:
                    continue
                eff, cash_delta = trade_value(
                    "SELL", sh, bar.close, self.slippage,
                    self.commission, self.stamp_duty,
                )
                self.cash += cash_delta
                self.positions[(symbol, strategy)] = 0
                self.fills.append((bar.date, symbol, strategy, "SELL", sh, eff))


class LiveBroker(Broker):
    """实盘券商接口占位实现。

    默认不启用。接入真实账户前，请在此实现你券商 SDK 的登录、下单、撤单、
    持仓/资金查询，并严格遵守所在市场的交易规则与监管合规要求。
    """

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "LiveBroker 尚未接入真实券商。请在 broker.py 中实现你的券商 API，"
            "并将 Config.mode 显式设为 'live' 后方可实盘运行。实盘有本金亏损风险，请谨慎。"
        )

    def get_cash(self) -> float:
        raise NotImplementedError

    def get_position(self, symbol: str, strategy: Optional[str] = None) -> int:
        raise NotImplementedError

    def get_all_positions(self) -> List[Tuple[str, str, int]]:
        raise NotImplementedError

    def get_last_bar(self, symbol: str) -> Optional[Bar]:
        raise NotImplementedError

    def submit_order(self, order: Order) -> Optional[float]:
        raise NotImplementedError
