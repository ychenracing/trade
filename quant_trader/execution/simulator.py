"""
模拟盘交易执行器
- 模拟A股交易规则（T+1、手续费、滑点）
- 用于策略验证和模拟运行
"""
import datetime
from typing import Optional
from collections import defaultdict

from execution.base import BaseExecutor, Order, OrderSide, OrderStatus, AccountInfo
from risk.position_sizer import PositionInfo, PositionSizer
from utils.helpers import (
    calc_commission, calc_stamp_tax, calc_transfer_fee, round_lot, format_code
)
from utils.logger import log
from config.settings import INITIAL_CAPITAL, TRADE_CONFIG, RISK_CONFIG


class SimulatorExecutor(BaseExecutor):
    """模拟盘执行器"""

    def __init__(self, initial_capital: float = INITIAL_CAPITAL):
        self.cash = initial_capital
        self.initial_capital = initial_capital
        self.positions: dict[str, PositionInfo] = {}
        # T+1: 记录今日买入的股票，今日不可卖
        self.today_bought: dict[str, int] = defaultdict(int)
        self.today_date = datetime.date.today().isoformat()
        self.trade_log: list[dict] = []
        self.slippage = TRADE_CONFIG["slippage"]

    def _check_date_rollover(self, current_date=None):
        """日期切换时清空T+1限制"""
        today = current_date.isoformat() if hasattr(current_date, 'isoformat') else (
            current_date if current_date else datetime.date.today().isoformat()
        )
        if today != self.today_date:
            self.today_date = today
            self.today_bought.clear()

    def _apply_slippage(self, price: float, side: OrderSide) -> float:
        """应用滑点"""
        if side == OrderSide.BUY:
            return round(price * (1 + self.slippage), 2)
        else:
            return round(price * (1 - self.slippage), 2)

    def _calc_fees(self, amount: float, is_sell: bool) -> float:
        """计算交易费用"""
        commission = calc_commission(amount)
        stamp_tax = calc_stamp_tax(amount, is_sell)
        transfer = calc_transfer_fee(amount)
        return commission + stamp_tax + transfer

    def buy(self, code: str, name: str, price: float, shares: int, current_date=None) -> Order:
        """买入"""
        self._check_date_rollover(current_date)
        code = format_code(code)
        shares = round_lot(shares)

        if shares <= 0:
            return Order(code, name, OrderSide.BUY, price, shares, OrderStatus.REJECTED,
                        error_msg="买入数量无效")

        # 应用滑点
        fill_price = self._apply_slippage(price, OrderSide.BUY)
        amount = fill_price * shares
        fees = self._calc_fees(amount, is_sell=False)
        total_cost = amount + fees

        # 检查资金
        if total_cost > self.cash:
            log.warning(f"[{code}] 买入资金不足: 需要{total_cost:.0f}, 可用{self.cash:.0f}")
            # 调整到可承受的数量
            max_shares = int((self.cash * 0.98) / (fill_price * (1 + TRADE_CONFIG["commission_rate"])))
            shares = round_lot(max_shares)
            if shares <= 0:
                return Order(code, name, OrderSide.BUY, price, shares, OrderStatus.REJECTED,
                            error_msg="资金不足")
            amount = fill_price * shares
            fees = self._calc_fees(amount, is_sell=False)
            total_cost = amount + fees

        # 执行买入
        self.cash -= total_cost

        # 更新持仓
        if code in self.positions:
            pos = self.positions[code]
            total_shares = pos.shares + shares
            total_cost_all = pos.cost_price * pos.shares + fill_price * shares
            pos.cost_price = total_cost_all / total_shares
            pos.shares = total_shares
            pos.current_price = fill_price
            pos.market_value = pos.shares * fill_price
            pos.profit_loss = pos.market_value - pos.cost_price * pos.shares
            pos.profit_pct = (fill_price - pos.cost_price) / pos.cost_price
        else:
            self.positions[code] = PositionInfo(
                code=code, name=name, shares=shares,
                cost_price=fill_price, current_price=fill_price,
                market_value=amount,
                profit_loss=0.0, profit_pct=0.0,
                peak_price=fill_price,
                base_shares=shares,
                entry_atr=0.0,
                price_gain_since_entry=0.0,
            )

        # T+1记录
        self.today_bought[code] += shares

        order = Order(
            code=code, name=name, side=OrderSide.BUY,
            price=price, shares=shares,
            status=OrderStatus.FILLED,
            filled_price=fill_price, filled_shares=shares,
        )

        trade_record = {
            "time": self.today_date + "T15:00:00" if self.today_date else datetime.datetime.now().isoformat(),
            "code": code, "name": name,
            "side": "buy", "price": fill_price,
            "shares": shares, "amount": amount,
            "fees": fees, "total_cost": total_cost,
        }
        self.trade_log.append(trade_record)

        log.info(
            f"💰 买入 {name}({code}): {shares}股 @ {fill_price:.2f} "
            f"金额={amount:.0f} 费用={fees:.2f} 总成本={total_cost:.0f}"
        )
        return order

    def sell(self, code: str, name: str, price: float, shares: int, current_date=None) -> Order:
        """卖出"""
        self._check_date_rollover(current_date)
        code = format_code(code)
        shares = round_lot(shares)

        if code not in self.positions:
            log.warning(f"[{code}] 无持仓, 无法卖出")
            return Order(code, name, OrderSide.SELL, price, shares, OrderStatus.REJECTED,
                        error_msg="无持仓")

        pos = self.positions[code]

        # T+1检查：今日买入的不可卖
        available = pos.shares - self.today_bought.get(code, 0)
        if shares > available:
            if available <= 0:
                log.warning(f"[{code}] T+1限制: 今日买入不可卖")
                return Order(code, name, OrderSide.SELL, price, shares, OrderStatus.REJECTED,
                            error_msg="T+1限制, 今日买入不可卖")
            shares = round_lot(available)
            log.info(f"[{code}] T+1限制, 调整卖出数量为{shares}股")

        if shares <= 0:
            return Order(code, name, OrderSide.SELL, price, shares, OrderStatus.REJECTED,
                        error_msg="可卖数量为0")

        # 应用滑点
        fill_price = self._apply_slippage(price, OrderSide.SELL)
        amount = fill_price * shares
        fees = self._calc_fees(amount, is_sell=True)
        net_proceeds = amount - fees

        # 计算盈亏
        cost_basis = pos.cost_price * shares
        realized_pnl = net_proceeds - cost_basis

        # 执行卖出
        self.cash += net_proceeds
        pos.shares -= shares
        pos.current_price = fill_price

        if pos.shares <= 0:
            del self.positions[code]
        else:
            pos.market_value = pos.shares * fill_price
            pos.profit_loss = pos.market_value - pos.cost_price * pos.shares
            pos.profit_pct = (fill_price - pos.cost_price) / pos.cost_price

        order = Order(
            code=code, name=name, side=OrderSide.SELL,
            price=price, shares=shares,
            status=OrderStatus.FILLED,
            filled_price=fill_price, filled_shares=shares,
            realized_pnl=realized_pnl,
        )

        trade_record = {
            "time": self.today_date + "T15:00:00" if self.today_date else datetime.datetime.now().isoformat(),
            "code": code, "name": name,
            "side": "sell", "price": fill_price,
            "shares": shares, "amount": amount,
            "fees": fees, "net_proceeds": net_proceeds,
            "realized_pnl": realized_pnl,
        }
        self.trade_log.append(trade_record)

        log.info(
            f"💸 卖出 {name}({code}): {shares}股 @ {fill_price:.2f} "
            f"金额={amount:.0f} 费用={fees:.2f} 净收入={net_proceeds:.0f} "
            f"盈亏={realized_pnl:+.0f}"
        )
        return order

    def update_prices(self, price_dict: dict):
        """更新持仓最新价格（统一入口，所有回测/实盘都应调用此方法）"""
        trailing_trigger = RISK_CONFIG.get("trailing_stop_trigger", 0.15)
        for code, price in price_dict.items():
            code = format_code(code)
            if code in self.positions:
                pos = self.positions[code]
                pos.current_price = price
                pos.market_value = pos.shares * price
                pos.profit_loss = pos.market_value - pos.cost_price * pos.shares
                pos.profit_pct = (price - pos.cost_price) / pos.cost_price
                # 更新峰值
                if price > pos.peak_price:
                    pos.peak_price = price
                # 激活移动止盈：盈利达到触发阈值后启用追踪
                if pos.profit_pct >= trailing_trigger:
                    pos.trailing_active = True

    def get_account(self) -> AccountInfo:
        """获取账户信息"""
        total_position_value = sum(p.market_value for p in self.positions.values())
        total_value = self.cash + total_position_value
        return AccountInfo(
            cash=self.cash,
            total_value=total_value,
            positions=dict(self.positions),
            today_trades=[t for t in self.trade_log if t["time"][:10] == self.today_date],
        )

    def get_positions(self) -> dict:
        """获取持仓"""
        return dict(self.positions)

    def get_total_value(self) -> float:
        """获取总市值"""
        return self.get_account().total_value

    def get_summary(self) -> str:
        """获取账户摘要"""
        acc = self.get_account()
        total_pnl = acc.total_value - self.initial_capital
        pnl_pct = total_pnl / self.initial_capital
        pos_count = len(acc.positions)
        pos_value = sum(p.market_value for p in acc.positions.values())
        pos_ratio = pos_value / acc.total_value if acc.total_value > 0 else 0

        lines = [
            f"\n{'='*60}",
            f"  模拟账户摘要",
            f"{'='*60}",
            f"  初始资金:   {self.initial_capital:>15,.0f}",
            f"  当前总资产: {acc.total_value:>15,.0f}",
            f"  可用现金:   {self.cash:>15,.0f}",
            f"  持仓市值:   {pos_value:>15,.0f}",
            f"  仓位比例:   {pos_ratio:>15.2%}",
            f"  持仓数量:   {pos_count:>15}",
            f"  总盈亏:     {total_pnl:>+15,.0f} ({pnl_pct:>+.2%})",
            f"  今日交易:   {len(acc.today_trades):>15}笔",
        ]

        if acc.positions:
            lines.append(f"\n  {'代码':<8} {'名称':<8} {'股数':>8} {'成本':>8} {'现价':>8} {'盈亏%':>8}")
            lines.append(f"  {'-'*54}")
            for p in acc.positions.values():
                lines.append(
                    f"  {p.code:<8} {p.name:<8} {p.shares:>8} "
                    f"{p.cost_price:>8.2f} {p.current_price:>8.2f} {p.profit_pct:>+8.2%}"
                )

        lines.append(f"{'='*60}")
        return "\n".join(lines)
