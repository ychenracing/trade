"""
多策略组合引擎
- 多个策略投票表决
- 支持多数表决/加权表决/全票通过
- 综合信号强度
"""
import pandas as pd
from typing import List
from strategy.base import BaseStrategy, Signal, StrategyResult
from strategy.ma_cross import EMACrossStrategy
from strategy.donchian import DonchianBreakoutStrategy
from strategy.macd_trend import MACDTrendStrategy
from config.strategy_config import (
    EMA_CROSS_CONFIG, DONCHIAN_CONFIG, MACD_TREND_CONFIG, COMBO_CONFIG,
)
from utils.logger import log


class ComboStrategy(BaseStrategy):
    """多策略组合"""

    def __init__(self):
        combo = COMBO_CONFIG
        self.vote_mode = combo["vote_mode"]
        self.min_buy_votes = combo["min_buy_votes"]
        self.min_sell_votes = combo["min_sell_votes"]

        # 动态加载策略
        self.strategies: List[BaseStrategy] = []
        for cfg in combo["strategies"]:
            if not cfg["enabled"]:
                continue
            strategy = self._create_strategy(cfg)
            if strategy:
                self.strategies.append(strategy)
                log.info(f"加载策略: {strategy.name} (权重={strategy.weight})")

        super().__init__(
            name=combo["name"],
            params={},
            weight=1.0,
        )
        self.min_data_days = max(s.min_data_days for s in self.strategies) if self.strategies else 60

    def _create_strategy(self, cfg: dict) -> BaseStrategy:
        """根据配置创建策略实例"""
        name = cfg["name"]
        params = cfg["params"]
        weight = cfg.get("weight", 1.0)

        if name == "EMA_Cross":
            return EMACrossStrategy(params, weight)
        elif name == "Donchian_Breakout":
            return DonchianBreakoutStrategy(params, weight)
        elif name == "MACD_Trend":
            return MACDTrendStrategy(params, weight)
        else:
            log.warning(f"未知策略: {name}")
            return None

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """所有策略的指标合并"""
        result = df.copy()
        for s in self.strategies:
            try:
                result = s.calculate_indicators(result)
            except Exception as e:
                log.warning(f"策略{s.name}计算指标失败: {e}")
        return result

    def generate_signal(self, df: pd.DataFrame) -> StrategyResult:
        """
        组合信号生成
        收集所有策略信号 -> 投票表决
        """
        if not self.strategies:
            return self._safe_signal(Signal.HOLD, 0, "无可用策略", {})

        if not self._check_data_sufficient(df):
            return self._safe_signal(Signal.HOLD, 0, "数据不足", {})

        # 收集各策略信号
        results = []
        for s in self.strategies:
            try:
                r = s.generate_signal(df)
                results.append((s, r))
                log.debug(f"  [{s.name}] -> {r.signal.value} (强度={r.strength:.2f}) {r.reason}")
            except Exception as e:
                log.warning(f"策略{s.name}生成信号失败: {e}")
                results.append((s, StrategyResult(Signal.HOLD, 0, f"异常: {e}", {})))

        # 投票统计
        buy_votes = [(s, r) for s, r in results if r.signal == Signal.BUY]
        sell_votes = [(s, r) for s, r in results if r.signal == Signal.SELL]
        hold_votes = [(s, r) for s, r in results if r.signal == Signal.HOLD]

        total = len(results)
        buy_count = len(buy_votes)
        sell_count = len(sell_votes)

        # 合并指标数据
        all_data = {}
        for s, r in results:
            all_data[s.name] = r.indicator_data

        # ---- 表决 ----
        if self.vote_mode == "majority":
            # 多数表决
            if buy_count >= self.min_buy_votes and buy_count > sell_count:
                avg_strength = sum(r.strength * s.weight for s, r in buy_votes) / sum(s.weight for s, _ in buy_votes)
                return self._safe_signal(
                    Signal.BUY, avg_strength,
                    f"多数表决看多({buy_count}/{total}票买入)",
                    all_data
                )
            elif sell_count >= self.min_sell_votes and sell_count > buy_count:
                avg_strength = sum(r.strength * s.weight for s, r in sell_votes) / sum(s.weight for s, _ in sell_votes)
                return self._safe_signal(
                    Signal.SELL, avg_strength,
                    f"多数表决看空({sell_count}/{total}票卖出)",
                    all_data
                )

        elif self.vote_mode == "weighted":
            # 加权表决
            buy_score = sum(r.strength * s.weight for s, r in buy_votes)
            sell_score = sum(r.strength * s.weight for s, r in sell_votes)

            if buy_score > sell_score and buy_count >= self.min_buy_votes:
                return self._safe_signal(
                    Signal.BUY, buy_score / sum(s.weight for s, _ in buy_votes),
                    f"加权表决看多(score={buy_score:.2f} vs {sell_score:.2f})",
                    all_data
                )
            elif sell_score > buy_score and sell_count >= self.min_sell_votes:
                return self._safe_signal(
                    Signal.SELL, sell_score / sum(s.weight for s, _ in sell_votes),
                    f"加权表决看空(score={sell_score:.2f} vs {buy_score:.2f})",
                    all_data
                )

        elif self.vote_mode == "unanimous":
            # 全票通过
            if buy_count == total:
                avg_strength = sum(r.strength for _, r in buy_votes) / total
                return self._safe_signal(
                    Signal.BUY, avg_strength,
                    f"全票看多({buy_count}/{total})",
                    all_data
                )
            elif sell_count == total:
                avg_strength = sum(r.strength for _, r in sell_votes) / total
                return self._safe_signal(
                    Signal.SELL, avg_strength,
                    f"全票看空({sell_count}/{total})",
                    all_data
                )

        # 默认持有
        return self._safe_signal(
            Signal.HOLD, 0,
            f"信号不一致(买{buy_count}/卖{sell_count}/持{len(hold_votes)})",
            all_data
        )

    def get_strategy_names(self) -> list:
        """获取所有子策略名称"""
        return [s.name for s in self.strategies]
