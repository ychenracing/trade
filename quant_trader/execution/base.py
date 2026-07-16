"""
交易执行基类
定义统一接口：下单、撤单、查询持仓
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional
import pandas as pd


class OrderStatus(Enum):
    """订单状态"""
    PENDING = "pending"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class OrderSide(Enum):
    """买卖方向"""
    BUY = "buy"
    SELL = "sell"


@dataclass
class Order:
    """订单"""
    code: str
    name: str
    side: OrderSide
    price: float
    shares: int
    status: OrderStatus = OrderStatus.PENDING
    filled_price: float = 0.0
    filled_shares: int = 0
    realized_pnl: float = 0.0  # 卖出时的已实现盈亏（含手续费），由executor填充
    error_msg: str = ""
    order_id: str = ""


@dataclass
class AccountInfo:
    """账户信息"""
    cash: float                  # 可用资金
    total_value: float           # 总市值（现金+持仓）
    positions: dict              # 持仓 {code: PositionInfo}
    today_trades: list           # 今日成交


class BaseExecutor(ABC):
    """交易执行基类"""

    @abstractmethod
    def buy(self, code: str, name: str, price: float, shares: int) -> Order:
        """买入"""
        pass

    @abstractmethod
    def sell(self, code: str, name: str, price: float, shares: int) -> Order:
        """卖出"""
        pass

    @abstractmethod
    def get_account(self) -> AccountInfo:
        """获取账户信息"""
        pass

    @abstractmethod
    def get_positions(self) -> dict:
        """获取持仓"""
        pass
