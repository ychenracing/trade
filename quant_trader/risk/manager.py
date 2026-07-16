"""
风控管理器 V2
- 成本止损: 2.5×ATR, cap 8-15% (建仓初期保护)
- 峰值trailing止损: 8×ATR, cap 20-35% (盈利后防坐电梯, 只防崩盘)
- 移动止盈: trailing模式启用, hold模式禁用
- 突破确认加码
"""
import datetime
from dataclasses import dataclass
from typing import Optional

from utils.logger import log
from config.settings import RISK_CONFIG
from risk.position_sizer import PositionInfo


@dataclass
class RiskStatus:
    """风控状态"""
    peak_nav: float = 0.0
    current_drawdown: float = 0.0
    today_realized_loss: float = 0.0
    today: str = ""
    global_halt: bool = False
    daily_halt: bool = False
    warning_count: int = 0
    pending_liquidate: list = None  # 熔断时因T+1未清仓的股票列表，下一交易日优先清理

    def __post_init__(self):
        if self.pending_liquidate is None:
            self.pending_liquidate = []


class RiskManager:
    """风控管理 V2"""

    def __init__(self, initial_capital: float):
        self.initial = initial_capital
        self.config = RISK_CONFIG.copy()  # copy防止实例修改污染全局配置
        self.status = RiskStatus(
            peak_nav=initial_capital,
            today=datetime.date.today().isoformat(),
        )

    def _check_date_rollover(self, current_date=None):
        today = current_date.isoformat() if hasattr(current_date, 'isoformat') else str(current_date)
        if today != self.status.today:
            self.status.today = today
            self.status.today_realized_loss = 0.0
            self.status.daily_halt = False

    def update_nav(self, current_nav: float, current_date=None):
        self._check_date_rollover(current_date)
        if current_nav > self.status.peak_nav:
            self.status.peak_nav = current_nav
        if self.status.peak_nav > 0:
            self.status.current_drawdown = (
                (self.status.peak_nav - current_nav) / self.status.peak_nav
            )

    def check_global_risk(self, current_nav: float, current_date=None) -> tuple:
        self.update_nav(current_nav, current_date)  # 含日期滚动+peak/drawdown更新

        # 浮点容差：避免边界值因精度问题不触发（如 0.2499999999 应触发 0.25 熔断）
        epsilon = 1e-9

        # 全局回撤熔断
        if self.status.current_drawdown >= self.config["max_total_drawdown"] - epsilon:
            if not self.status.global_halt:
                self.status.global_halt = True
                self.status.warning_count += 1
                log.error(f"触发最大回撤熔断! 回撤={self.status.current_drawdown:.2%}")
            return False, f"最大回撤熔断: {self.status.current_drawdown:.2%}"

        # 全局绝对亏损熔断
        total_loss = (self.initial - current_nav) / self.initial
        if total_loss >= self.config["max_total_drawdown"] - epsilon:
            if not self.status.global_halt:
                self.status.global_halt = True
            return False, f"绝对亏损熔断: {total_loss:.2%}"

        # 全局熔断恢复：回撤回落到阈值的一半以下时解除
        if self.status.global_halt:
            recover_threshold = self.config["max_total_drawdown"] * 0.5
            if self.status.current_drawdown < recover_threshold:
                self.status.global_halt = False
                log.info(f"全局熔断解除: 回撤回落至{self.status.current_drawdown:.2%} < {recover_threshold:.2%}")

        # 单日亏损限额
        if self.status.today_realized_loss / self.initial >= self.config["max_daily_loss"]:
            if not self.status.daily_halt:
                self.status.daily_halt = True
                self.status.warning_count += 1
            return False, f"单日亏损限额: {self.status.today_realized_loss:.0f}"

        return True, ""

    def check_stop_loss(self, position: PositionInfo, atr: float = 0.0) -> tuple:
        """
        检查止损止盈 V2
        atr: 当前ATR值，用于自适应止损

        止损逻辑（两道防线）:
          1. 成本止损（保底）: 亏损达到 max(8%, min(15%, 2.5×ATR/成本价)) 时止损
             - 建仓初期未盈利时的保护，防止深套
          2. 峰值trailing止损: 从持仓峰值回撤达到 max(20%, min(35%, 8×ATR/峰值价)) 时止损
             - 盈利后随峰值上移，锁住浮盈，不会从+100%坐电梯回到成本

        止盈逻辑（可选）:
          - trailing模式: 盈利15%后激活，峰值回吐40-70%盈利时止盈
          - hold模式: 不止盈，只用两道止损线
        """
        # ---- 第一道: 成本保底止损 ----
        # 建仓初期（尚未显著盈利）的保护
        if atr > 0 and position.cost_price > 0:
            atr_loss_pct = (atr * self.config["atr_stop_multiple"]) / position.cost_price
            atr_loss_pct = max(
                self.config["atr_stop_min_loss"],
                min(self.config["atr_stop_max_loss"], atr_loss_pct)
            )
            if position.profit_pct <= -atr_loss_pct:
                return True, (
                    f"ATR止损: 亏损{position.profit_pct:.2%} >= -{atr_loss_pct:.2%} "
                    f"(ATR={atr:.2f}, {self.config['atr_stop_multiple']}×ATR={atr*self.config['atr_stop_multiple']:.2f})"
                )
        else:
            if position.profit_pct <= -self.config["max_position_loss"]:
                return True, f"固定止损: 亏损{position.profit_pct:.2%}"

        # ---- 第二道: 峰值trailing止损 ----
        # 盈利后止损线随峰值上移，锁住浮盈
        # 阈值比成本止损宽很多(8×ATR cap20-35%), 只防崩盘不防正常波动
        if atr > 0 and position.peak_price > position.cost_price:
            peak_loss_pct = (atr * self.config["peak_stop_multiple"]) / position.peak_price
            peak_loss_pct = max(
                self.config["peak_stop_min_loss"],
                min(self.config["peak_stop_max_loss"], peak_loss_pct)
            )
            peak_stop_price = position.peak_price * (1 - peak_loss_pct)
            if position.current_price <= peak_stop_price:
                profit_from_peak = (position.peak_price - position.cost_price) / position.cost_price
                return True, (
                    f"峰值trailing止损: 峰值{position.peak_price:.2f}回撤"
                    f"{(1 - position.current_price/position.peak_price):.1%}, "
                    f"止损线={peak_stop_price:.2f}({peak_loss_pct:.1%}), "
                    f"盈利{profit_from_peak:.0%}"
                )

        # ---- 移动止盈（仅trailing模式）----
        # hold模式: 跳过移动止盈，只用两道止损线
        stop_profit_mode = self.config.get("stop_profit_mode", "trailing")
        if stop_profit_mode == "hold":
            return False, ""

        if position.trailing_active and position.peak_price > position.cost_price:
            drop_amount = position.peak_price - position.current_price
            profit_amount = position.peak_price - position.cost_price
            pullback_ratio = drop_amount / profit_amount if profit_amount > 0 else 1.0

            # 分级盈利保护: 盈利越大，止盈阈值越紧
            effective_ratio = self.config["trailing_stop_ratio"]  # 默认70%
            tiers = self.config.get("profit_protection_tiers", [])
            if tiers:
                profit_from_cost = (position.peak_price - position.cost_price) / position.cost_price
                for tier in tiers:
                    if profit_from_cost >= tier["profit_above"]:
                        effective_ratio = tier["trailing_ratio"]
                        break

            if pullback_ratio >= effective_ratio:
                return True, (
                    f"移动止盈: 峰值{position.peak_price:.2f}回吐{pullback_ratio:.0%}盈利, "
                    f"触发止盈(阈值{effective_ratio:.0%}, 盈利{position.profit_pct:.0%})"
                )

        return False, ""

    def record_realized_loss(self, loss: float):
        if loss < 0:
            self.status.today_realized_loss += abs(loss)

    def reset_after_circuit_breaker(self, current_nav: float):
        """熔断清仓完毕后重置peak_nav和initial，允许系统恢复交易。

        问题：熔断触发后全仓变现，peak_nav冻结在旧高点，drawdown永远>阈值，
        且absolute_loss = (initial - current) / initial 永远 > 阈值，系统永久锁定。

        修复：清仓完毕后（无持仓残留），将peak_nav和initial都重置为当前净值，
        相当于"用剩余资金重新开始"，drawdown和absolute_loss都归零。
        """
        self.initial = current_nav
        self.status.peak_nav = current_nav
        self.status.current_drawdown = 0.0
        self.status.global_halt = False
        log.info(f"熔断清仓完毕，重置peak_nav={current_nav:.0f}，initial={current_nav:.0f}，解除熔断")

    def get_status_summary(self) -> str:
        s = self.status
        return (
            f"风控: 峰值={s.peak_nav:.0f}, 回撤={s.current_drawdown:.2%}, "
            f"今日亏损={s.today_realized_loss:.0f}, 熔断={'是' if s.global_halt else '否'}"
        )
