"""
策略基类 - 所有策略继承此类
统一接口：生成信号 -> 返回 buy/sell/hold
"""
from enum import Enum
from dataclasses import dataclass
from typing import Optional
import pandas as pd

from utils.logger import log


class Signal(Enum):
    """交易信号"""
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass
class StrategyResult:
    """策略输出结果"""
    signal: Signal
    strength: float       # 信号强度 0~1
    reason: str            # 信号原因
    indicator_data: dict   # 指标数据快照


class BaseStrategy:
    """策略基类"""

    def __init__(self, name: str, params: dict, weight: float = 1.0):
        self.name = name
        self.params = params
        self.weight = weight
        self.min_data_days = 60   # 最少需要的数据天数

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算策略所需技术指标（子类实现）"""
        raise NotImplementedError

    def generate_signal(self, df: pd.DataFrame) -> StrategyResult:
        """
        根据最新数据生成交易信号
        df: 日线数据，按日期升序排列
        """
        raise NotImplementedError

    def _check_data_sufficient(self, df: pd.DataFrame) -> bool:
        """检查数据是否充足"""
        if df is None or len(df) < self.min_data_days:
            return False
        return True

    def _get_latest(self, df: pd.DataFrame, col: str, offset: int = 0) -> float:
        """获取最新值"""
        if col not in df.columns or len(df) <= offset:
            return float("nan")
        return df[col].iloc[-1 - offset]

    def _safe_signal(self, signal: Signal, strength: float, reason: str, data: dict) -> StrategyResult:
        """安全构造结果"""
        return StrategyResult(
            signal=signal,
            strength=max(0.0, min(1.0, strength)),
            reason=reason,
            indicator_data=data,
        )
