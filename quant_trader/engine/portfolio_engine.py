"""
组合级回测引擎 - 多股票共享资金池

与单标的回测(trade_engine.py)的区别:
  - N只股票共享一个SimulatorExecutor（一个cash pool, 一个positions dict）
  - 每个交易日先做组合级操作（T+1清理、更新所有价格、组合风控），
    再逐股做个股操作（止损、加仓、策略信号、执行买卖）
  - 多只股票同时BUY时按信号强度排序，优先执行强信号
  - 熔断在组合净值层面触发，清仓所有持仓

复用trade_engine.py的: calc_daily_context, _handle_stop_loss, _check_pyramid, _handle_buy, _handle_sell
新增: run_portfolio_trading_day, _handle_portfolio_circuit_breaker,
      _handle_pending_liquidate, _record_portfolio_nav
"""
import datetime
from typing import Dict, List, Tuple
import pandas as pd
import numpy as np

from execution.simulator import SimulatorExecutor
from risk.manager import RiskManager
from risk.position_sizer import PositionSizer
from strategy.combo import ComboStrategy
from strategy.base import Signal
from utils.helpers import calc_atr, calc_sma, calc_trend_mode, round_lot
from utils.logger import log

# 复用单标的引擎的helper函数（已添加symbol/name字段到trade_log）
from engine.trade_engine import (
    calc_daily_context,
    _handle_stop_loss,
    _check_pyramid,
    _handle_buy,
    _handle_sell,
)


def run_portfolio_trading_day(
    date,
    stocks_data: Dict[str, pd.DataFrame],
    stock_indices: Dict[str, int],
    stock_names: Dict[str, str],
    simulator: SimulatorExecutor,
    risk_mgr: RiskManager,
    position_sizer: PositionSizer,
    strategies: Dict[str, ComboStrategy],
    trade_log: list,
    signal_log: list,
    daily_nav: list,
    verbose: bool = True,
) -> bool:
    """
    执行组合的一个交易日。

    Args:
        date: 当前日期
        stocks_data: {symbol: DataFrame} 所有股票的日K数据
        stock_indices: {symbol: 当日row index} 当日有数据且过warmup的股票
        stock_names: {symbol: name}
        simulator, risk_mgr, position_sizer: 共享的组件实例
        strategies: {symbol: ComboStrategy} 每只股票一个策略实例
        trade_log, signal_log, daily_nav: 日志列表

    Returns:
        True=正常完成, False=熔断触发
    """
    # 1. T+1清理（每天一次）
    simulator._check_date_rollover(date)

    # 2. 更新所有持仓的现价
    price_dict = {}
    for symbol in list(simulator.positions.keys()):
        if symbol in stock_indices:
            df = stocks_data[symbol]
            idx = stock_indices[symbol]
            if idx < len(df):
                price_dict[symbol] = float(df.iloc[idx]["close"])
    if price_dict:
        simulator.update_prices(price_dict)

    # 3. 计算组合净值 + 风控更新
    account = simulator.get_account()
    total_value = account.total_value
    risk_mgr.update_nav(total_value, date)
    position_sizer.update_capital(total_value)

    # 4. 组合级熔断检查
    allowed, risk_reason = risk_mgr.check_global_risk(total_value, date)
    if not allowed:
        _handle_portfolio_circuit_breaker(
            simulator, risk_mgr, trade_log, daily_nav,
            date, stocks_data, stock_indices, risk_reason,
        )
        return False

    # 4.5 清理pending_liquidate（上一日熔断时因T+1未清仓的）
    _handle_pending_liquidate(
        simulator, risk_mgr, trade_log,
        date, stocks_data, stock_indices, verbose,
    )

    # 5-8. 逐股处理: 止损 → 加仓 → 信号 → 执行
    buy_signals = []  # [(symbol, name, result, ctx), ...] 待统一排序执行

    for symbol, idx in stock_indices.items():
        df = stocks_data[symbol]
        if idx >= len(df) or idx < 70:
            continue
        name = stock_names.get(symbol, symbol)

        # 计算当日上下文（复用单标的函数）
        ctx = calc_daily_context(df, idx)
        current_price = ctx["price"]
        current_atr = ctx["atr"]

        # 5. 止损/止盈检查（仅对持仓股）
        if symbol in simulator.positions:
            pos = simulator.positions[symbol]
            should_stop, stop_reason = risk_mgr.check_stop_loss(pos, atr=current_atr)
            if should_stop and symbol not in simulator.today_bought:
                _handle_stop_loss(
                    simulator, risk_mgr, trade_log,
                    date, current_price, symbol, name,
                    stop_reason, verbose,
                )
                signal_log.append({
                    "date": date, "symbol": symbol, "name": name,
                    "close": current_price, "signal": "HOLD",
                    "strength": 0, "reason": f"已止损: {stop_reason}",
                })
                continue  # 止损后跳过该股的后续步骤

        # 6. 加仓检查（仅对持仓股）
        if (symbol in simulator.positions
                and risk_mgr.config.get("pyramid_allowed", False)
                and symbol not in simulator.today_bought
                and not risk_mgr.status.daily_halt):
            _check_pyramid(
                simulator, position_sizer, risk_mgr, trade_log,
                date, current_price, symbol, name,
                ctx["price_gain_pct"], ctx["trend_mode"], verbose,
            )

        # 7. 生成策略信号
        result = strategies[symbol].generate_signal(ctx["window"])
        signal_log.append({
            "date": date, "symbol": symbol, "name": name,
            "close": current_price,
            "signal": result.signal.value,
            "strength": result.strength,
            "reason": result.reason,
        })

        # 8a. SELL信号立即执行（Hold模式忽略SELL）
        stop_profit_mode = risk_mgr.config.get("stop_profit_mode", "trailing")
        if result.signal == Signal.SELL and symbol in simulator.positions:
            if symbol not in simulator.today_bought and stop_profit_mode != "hold":
                _handle_sell(
                    simulator, risk_mgr, trade_log,
                    date, current_price, symbol, name,
                    result, verbose,
                )

        # 8b. BUY信号收集，稍后统一排序执行
        elif result.signal == Signal.BUY:
            if not risk_mgr.status.daily_halt and symbol not in simulator.positions:
                buy_signals.append((symbol, name, result, ctx))

    # 8c. 按信号强度排序，优先执行强信号（资金有限时分配给最确定的信号）
    buy_signals.sort(key=lambda x: x[2].strength, reverse=True)
    for symbol, name, result, ctx in buy_signals:
        if risk_mgr.status.daily_halt:
            break
        if symbol in simulator.positions:
            continue  # 可能因加仓导致已持有
        # 再次检查仓位上限（前面的买入可能已填满）
        if len(simulator.positions) >= risk_mgr.config["max_positions"]:
            break
        _handle_buy(
            simulator, position_sizer, trade_log,
            date, ctx["price"], symbol, name,
            result, ctx["price_gain_pct"], ctx["trend_mode"],
            ctx["atr"], verbose,
        )

    # 9. 记录组合净值
    _record_portfolio_nav(simulator, risk_mgr, daily_nav, date)
    return True


def _handle_portfolio_circuit_breaker(
    simulator, risk_mgr, trade_log, daily_nav,
    date, stocks_data, stock_indices, risk_reason,
):
    """组合级熔断: 用各股票当日收盘价清仓所有持仓"""
    if simulator.positions:
        for code in list(simulator.positions.keys()):
            pos = simulator.positions[code]
            # 获取该股票当日价格
            if code in stock_indices:
                df = stocks_data[code]
                idx = stock_indices[code]
                price = float(df.iloc[idx]["close"]) if idx < len(df) else pos.current_price
            else:
                price = pos.current_price  # 该股当日无数据，用上次价格

            order = simulator.sell(code, pos.name, price, pos.shares, date)
            if order.status.value == "filled":
                realized_pnl = order.realized_pnl
                if realized_pnl < 0:
                    risk_mgr.record_realized_loss(realized_pnl)
                pnl_pct = (order.filled_price - pos.cost_price) / pos.cost_price if pos.cost_price > 0 else 0
                trade_log.append({
                    "date": date, "action": "SELL(熔断)",
                    "symbol": code, "name": pos.name,
                    "price": order.filled_price, "shares": order.filled_shares,
                    "reason": risk_reason, "pnl": realized_pnl,
                    "pnl_pct": pnl_pct,
                })
            elif order.status.value == "rejected":
                log.warning(f"熔断清仓失败({code}): {order.error_msg}, 标记为待清理")
                risk_mgr.status.pending_liquidate.append(code)

    account = simulator.get_account()
    # 清理pending_liquidate中已不存在的仓位（本轮熔断已清仓成功）
    risk_mgr.status.pending_liquidate = [
        s for s in risk_mgr.status.pending_liquidate if s in simulator.positions
    ]
    # 熔断清仓完毕后重置peak_nav，避免永久锁定
    if not simulator.positions and not risk_mgr.status.pending_liquidate:
        risk_mgr.reset_after_circuit_breaker(account.total_value)

    daily_nav.append({
        "date": date,
        "nav": account.total_value,
        "cash": simulator.cash,
        "position_value": account.total_value - simulator.cash,
        "position_count": len(simulator.positions),
        "drawdown": risk_mgr.status.current_drawdown,
        "daily_return": 0,
    })


def _handle_pending_liquidate(
    simulator, risk_mgr, trade_log,
    date, stocks_data, stock_indices, verbose,
):
    """清理上一交易日熔断时因T+1未清仓的残留仓位"""
    if not risk_mgr.status.pending_liquidate:
        return

    for symbol in list(risk_mgr.status.pending_liquidate):
        if symbol not in simulator.positions:
            # 仓位已不存在（可能已被止损卖出）
            risk_mgr.status.pending_liquidate.remove(symbol)
            continue

        pos = simulator.positions[symbol]
        if symbol in simulator.today_bought:
            # 今日又买入了（不应该发生，防御性处理）
            continue

        # 获取当日价格
        if symbol in stock_indices:
            df = stocks_data[symbol]
            idx = stock_indices[symbol]
            price = float(df.iloc[idx]["close"]) if idx < len(df) else pos.current_price
        else:
            price = pos.current_price

        order = simulator.sell(symbol, pos.name, price, pos.shares, date)
        if order.status.value == "filled":
            realized_pnl = order.realized_pnl
            if realized_pnl < 0:
                risk_mgr.record_realized_loss(realized_pnl)
            pnl_pct = (order.filled_price - pos.cost_price) / pos.cost_price if pos.cost_price > 0 else 0
            trade_log.append({
                "date": date, "action": "SELL(熔断清理)",
                "symbol": symbol, "name": pos.name,
                "price": order.filled_price, "shares": order.filled_shares,
                "reason": "前日熔断T+1残留清理", "pnl": realized_pnl,
                "pnl_pct": pnl_pct,
            })
            log.info(f"熔断残留清理: {symbol} @ {order.filled_price:.2f}, 盈亏={realized_pnl:+,.0f}")
            risk_mgr.status.pending_liquidate.remove(symbol)
        else:
            log.warning(f"熔断残留清理失败({symbol}): {order.error_msg}, 保留待下一交易日")


def _record_portfolio_nav(simulator, risk_mgr, daily_nav, date):
    """记录组合当日净值"""
    account = simulator.get_account()
    total_value = account.total_value
    prev_nav = daily_nav[-1]["nav"] if daily_nav else total_value
    daily_return = (total_value - prev_nav) / prev_nav if prev_nav > 0 else 0

    # 记录持仓明细
    positions_detail = {}
    for code, pos in simulator.positions.items():
        positions_detail[code] = {
            "name": pos.name,
            "shares": pos.shares,
            "cost_price": pos.cost_price,
            "current_price": pos.current_price,
            "market_value": pos.market_value,
            "profit_pct": pos.profit_pct,
        }

    daily_nav.append({
        "date": date,
        "nav": total_value,
        "cash": simulator.cash,
        "position_value": total_value - simulator.cash,
        "position_count": len(simulator.positions),
        "drawdown": risk_mgr.status.current_drawdown,
        "daily_return": daily_return,
        "positions": positions_detail,
    })
