"""
仓位管理器 V2
- 基础仓位分配
- 优化3: 高位动态降仓（涨幅超200%自动降仓）
- 优化4: 突破确认加码（盈利10%后允许加仓50%）
"""
from dataclasses import dataclass
from typing import Optional
from utils.logger import log
from config.settings import RISK_CONFIG, TRADE_CONFIG
from utils.helpers import round_lot


@dataclass
class PositionInfo:
    """持仓信息"""
    code: str
    name: str
    shares: int
    cost_price: float
    current_price: float
    market_value: float
    profit_loss: float
    profit_pct: float
    # 移动止盈追踪
    peak_price: float = 0.0
    trailing_active: bool = False
    # 加码追踪
    pyramid_count: int = 0            # 已加仓次数
    base_shares: int = 0              # 初始建仓股数
    # 买入时的ATR（用于后续止损参考）
    entry_atr: float = 0.0
    # 股票买入以来的涨幅（用于高位降仓判断）
    price_gain_since_entry: float = 0.0


class PositionSizer:
    """仓位管理 V2"""

    def __init__(self, total_capital: float):
        self.total_capital = total_capital
        self.risk = RISK_CONFIG.copy()    # copy防止实例修改污染全局配置
        self.trade = TRADE_CONFIG.copy()  # 同上

    def calc_buy_size(
        self,
        price: float,
        available_cash: float,
        current_positions: int,
        position_value: float = 0,
        # 优化3: 高位降仓参数
        price_gain_pct: float = 0.0,
        # 优化4: 是否加仓
        is_pyramid: bool = False,
        base_shares: int = 0,
        # 优化5: 趋势环境过滤
        trend_mode: str = "up",        # "up"=多头趋势, "down"=空头趋势, "mixed"=震荡
        # ATR仓位模式（海龟式）
        atr: float = 0.0,
    ) -> int:
        """
        计算买入数量 V3
        trend_mode: 趋势环境，决定基础仓位倍率
          - up:    价格>MA60 且 MA20>MA60，满仓（1.0×）
          - mixed: 价格>MA60 但 MA20<MA60 或反过来，降仓（0.6×）
          - down:  价格<MA60 且 MA20<MA60，轻仓（0.4×）
        """
        if current_positions >= self.risk["max_positions"] and position_value == 0:
            return 0

        max_ratio = self.risk["max_position_ratio"]

        # ---- 优化5: 趋势环境过滤（核心调整）----
        if trend_mode == "down":
            max_ratio *= 0.40       # 空头趋势：仓位打4折（30%→12%）
        elif trend_mode == "mixed":
            max_ratio *= 0.60       # 震荡市：仓位打6折（30%→18%）
        # up: 不打折

        # ---- 优化3: 高位动态降仓 ----
        if price_gain_pct > 3.0:       # 涨幅>300%
            max_ratio *= 0.6
        elif price_gain_pct > 2.0:     # 涨幅>200%
            max_ratio *= 0.8

        max_per_stock = self.total_capital * max_ratio

        # ---- ATR仓位模式（海龟式：1%风险/ATR）----
        sizing_mode = self.risk.get("position_sizing_mode", "fixed")
        if sizing_mode == "atr" and not is_pyramid and position_value == 0:
            if atr is None or atr <= 0:
                log.warning("ATR仓位模式已启用但atr<=0，回退到固定仓位模式（调用方未传atr）")
            else:
                risk_per_trade = self.risk.get("atr_risk_per_trade", 0.01)
                max_ratio_atr = self.risk.get("atr_position_max_ratio", 0.30)
                # 趋势降仓同样适用
                if trend_mode == "down":
                    max_ratio_atr *= 0.40
                elif trend_mode == "mixed":
                    max_ratio_atr *= 0.60
                # ATR仓位 = (风险金额 / ATR) 向下取整到100股
                risk_amount = self.total_capital * risk_per_trade
                atr_shares = int(risk_amount / atr)
                atr_shares = round_lot(atr_shares)
                # 不超过单股上限
                max_shares_by_ratio = int(self.total_capital * max_ratio_atr / price) if price > 0 else 0
                atr_shares = min(atr_shares, max_shares_by_ratio)
                atr_shares = round_lot(atr_shares)
                # 不超过可用资金
                max_by_cash = int(available_cash * 0.95 / price) if price > 0 else 0
                atr_shares = min(atr_shares, max_by_cash)
                atr_shares = round_lot(atr_shares)
                if atr_shares > 0:
                    log.info(f"ATR仓位: ATR={atr:.2f}, 风险金={risk_amount:.0f}, 买{atr_shares}股")
                return atr_shares

        # ---- 优化4: 加仓逻辑 ----
        if is_pyramid:
            if not self.risk.get("pyramid_allowed", False):
                return 0
            # 下跌趋势中不允许加仓
            if trend_mode == "down":
                log.info(f"加仓取消: 趋势环境={trend_mode}, 不加仓")
                return 0
            # base_shares=0时无法计算加仓比例，拒绝加仓
            if base_shares <= 0:
                log.info("加仓取消: base_shares=0, 无法计算加仓比例")
                return 0
            # 加仓量 = 首次仓位 × 50%
            target_shares = int(base_shares * self.risk["pyramid_size_ratio"])
            target_shares = round_lot(target_shares)
            # 不超过最大仓位上限
            max_additional = max_per_stock - position_value
            max_add_shares = int(max_additional / price) if price > 0 else 0
            target_shares = min(target_shares, max_add_shares)
            target_shares = round_lot(target_shares)
            if target_shares <= 0:
                log.info(f"加仓计算: 已达仓位上限, 跳过")
                return 0
            log.info(f"加仓计算: 首仓={base_shares}股, 加仓={target_shares}股")
            return target_shares

        # 正常建仓
        if position_value > 0:
            available_for_this = max_per_stock - position_value
        else:
            available_for_this = max_per_stock

        usable_cash = available_cash * 0.98
        buy_amount = min(available_for_this, usable_cash)

        if buy_amount < price * self.trade["min_lot_size"]:
            return 0

        shares = int(buy_amount / price)
        shares = round_lot(shares)

        if shares <= 0:
            return 0

        # 高位降仓提示
        if price_gain_pct > 1.0:
            log.info(
                f"仓位计算(高位降仓): 涨幅={price_gain_pct:.0%}, "
                f"仓位比例={max_ratio:.0%}(基准{self.risk['max_position_ratio']:.0%}), 买入={shares}股"
            )
        else:
            log.info(
                f"仓位计算: 价格={price:.2f}, 仓位={max_ratio:.0%}, 买入={shares}股"
            )
        return shares

    def calc_position_ratio(self, position_value: float) -> float:
        if self.total_capital <= 0:
            return 0
        return position_value / self.total_capital

    def update_capital(self, current_total: float):
        self.total_capital = current_total
