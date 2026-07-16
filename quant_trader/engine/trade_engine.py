"""
公共交易循环 - 所有入口（backtest_v2 / backtest_300308 / main.py / backtest/engine.py）共用
单一路径：T+1清理 → 更新价格 → 风控熔断 → 止损止盈 → 加仓 → 策略信号执行 → 记录净值
"""
import datetime
from typing import Optional, Tuple, List
import pandas as pd
import numpy as np

from execution.simulator import SimulatorExecutor
from risk.manager import RiskManager
from risk.position_sizer import PositionSizer, PositionInfo
from strategy.base import Signal, StrategyResult
from strategy.combo import ComboStrategy
from utils.helpers import calc_atr, calc_sma, calc_trend_mode, round_lot
from utils.logger import log


def calc_daily_context(df: pd.DataFrame, i: int) -> dict:
    """计算当日交易上下文（ATR、MA60、趋势环境、涨幅偏离）"""
    current_price = float(df.iloc[i]["close"])
    current_date = df.iloc[i]["date"].date() if hasattr(df.iloc[i]["date"], 'date') else df.iloc[i]["date"]

    # ATR
    if i >= 19:
        atr_series = calc_atr(df["high"].iloc[:i+1], df["low"].iloc[:i+1], df["close"].iloc[:i+1], 20)
        current_atr = float(atr_series.iloc[-1]) if not np.isnan(atr_series.iloc[-1]) else 0.0
    else:
        current_atr = 0.0

    # MA60 / 趋势 / 涨幅偏离
    ma60_series = calc_sma(df["close"].iloc[:i+1], 60)
    ma60 = float(ma60_series.iloc[-1]) if len(ma60_series) > 0 and not np.isnan(ma60_series.iloc[-1]) else current_price
    price_gain_pct = (current_price - ma60) / ma60 if ma60 > 0 else 0.0
    trend_mode = calc_trend_mode(df.iloc[:i+1])

    # 截取到当日的数据（策略用）
    window = df.iloc[:i+1].copy()

    return {
        "price": current_price,
        "date": current_date,
        "atr": current_atr,
        "ma60": ma60,
        "price_gain_pct": price_gain_pct,
        "trend_mode": trend_mode,
        "window": window,
    }


def run_trading_day(
    ctx: dict,
    symbol: str,
    name: str,
    simulator: SimulatorExecutor,
    risk_mgr: RiskManager,
    position_sizer: PositionSizer,
    strategy: ComboStrategy,
    trade_log: list,
    signal_log: list,
    daily_nav: list,
    verbose: bool = True,
) -> bool:
    """
    执行一个交易日的完整流程。返回True表示正常完成，False表示熔断跳过。
    单一执行路径，所有回测/实盘入口共用此函数。
    """
    current_price = ctx["price"]
    current_date = ctx["date"]
    current_atr = ctx["atr"]
    price_gain_pct = ctx["price_gain_pct"]
    trend_mode = ctx["trend_mode"]
    window = ctx["window"]

    # 1. T+1清理
    simulator._check_date_rollover(current_date)

    # 2. 更新持仓现价（统一入口：含peak_price、trailing_active、profit_loss）
    simulator.update_prices({symbol: current_price})

    # 3. 计算净值 + 风控检查
    account = simulator.get_account()
    total_value = account.total_value
    risk_mgr.update_nav(total_value, current_date)
    position_sizer.update_capital(total_value)

    # 4. 熔断检查（优先于一切）
    allowed, risk_reason = risk_mgr.check_global_risk(total_value, current_date)
    if not allowed:
        _handle_circuit_breaker(
            simulator, risk_mgr, trade_log, daily_nav,
            current_date, current_price, risk_reason,
        )
        return False

    # 4.5 清理上一交易日熔断时因T+1未清仓的残留仓位
    if symbol in risk_mgr.status.pending_liquidate and symbol in simulator.positions:
        pos = simulator.positions[symbol]
        if symbol not in simulator.today_bought:
            order = simulator.sell(symbol, name, current_price, pos.shares, current_date)
            if order.status.value == "filled":
                realized_pnl = order.realized_pnl
                if realized_pnl < 0:
                    risk_mgr.record_realized_loss(realized_pnl)
                pnl_pct = (order.filled_price - pos.cost_price) / pos.cost_price if pos.cost_price > 0 else 0
                trade_log.append({
                    "date": current_date, "action": "SELL(熔断清理)",
                    "symbol": symbol, "name": name,
                    "price": order.filled_price, "shares": order.filled_shares,
                    "reason": "前日熔断T+1残留清理", "pnl": realized_pnl,
                    "pnl_pct": pnl_pct,
                })
                log.info(f"熔断残留清理: {symbol} @ {order.filled_price:.2f}, 盈亏={realized_pnl:+,.0f}")
                # 清理成功才从pending列表移除
                if symbol in risk_mgr.status.pending_liquidate:
                    risk_mgr.status.pending_liquidate.remove(symbol)
            else:
                # sell失败（如再次T+1），保留在pending列表等下一交易日再试
                log.warning(f"熔断残留清理失败({symbol}): {order.error_msg}, 保留待下一交易日")
        _record_nav(simulator, risk_mgr, daily_nav, current_date, current_price)
        return True

    # 即使仓位已不存在，也要从pending列表清理（仓位可能已被止损卖出）
    if symbol in risk_mgr.status.pending_liquidate:
        risk_mgr.status.pending_liquidate.remove(symbol)

    # 5. 止损/止盈检查
    if symbol in simulator.positions:
        pos = simulator.positions[symbol]
        should_stop, stop_reason = risk_mgr.check_stop_loss(pos, atr=current_atr)
        if should_stop and symbol not in simulator.today_bought:
            _handle_stop_loss(
                simulator, risk_mgr, trade_log,
                current_date, current_price, symbol, name,
                stop_reason, verbose,
            )
            _record_nav(simulator, risk_mgr, daily_nav, current_date, current_price)
            return True

    # 6. 加仓检查（不依赖BUY信号，每日检查）
    if (symbol in simulator.positions
            and risk_mgr.config.get("pyramid_allowed", False)
            and symbol not in simulator.today_bought
            and not risk_mgr.status.daily_halt):
        _check_pyramid(
            simulator, position_sizer, risk_mgr, trade_log,
            current_date, current_price, symbol, name,
            price_gain_pct, trend_mode, verbose,
        )

    # 7. 策略信号
    result = strategy.generate_signal(window)
    signal_log.append({
        "date": current_date,
        "close": current_price,
        "signal": result.signal.value,
        "strength": result.strength,
        "reason": result.reason,
    })

    # 8. 执行策略信号
    # Hold模式: 策略SELL被忽略, 只按止损退出(让利润奔跑, 避免正常波动被误杀)
    # trailing模式: 策略SELL正常执行
    stop_profit_mode = risk_mgr.config.get("stop_profit_mode", "trailing")
    if result.signal == Signal.BUY:
        if not risk_mgr.status.daily_halt and symbol not in simulator.positions:
            _handle_buy(
                simulator, position_sizer, trade_log,
                current_date, current_price, symbol, name,
                result, price_gain_pct, trend_mode, current_atr, verbose,
            )
    elif result.signal == Signal.SELL and symbol in simulator.positions:
        if symbol not in simulator.today_bought and stop_profit_mode != "hold":
            _handle_sell(
                simulator, risk_mgr, trade_log,
                current_date, current_price, symbol, name,
                result, verbose,
            )

    # 9. 记录净值
    _record_nav(simulator, risk_mgr, daily_nav, current_date, current_price)
    return True


def _handle_circuit_breaker(simulator, risk_mgr, trade_log, daily_nav,
                            current_date, current_price, risk_reason):
    """熔断清仓"""
    if simulator.positions:
        for code in list(simulator.positions.keys()):
            pos = simulator.positions[code]
            order = simulator.sell(code, pos.name, current_price, pos.shares, current_date)
            if order.status.value == "filled":
                realized_pnl = order.realized_pnl
                if realized_pnl < 0:
                    risk_mgr.record_realized_loss(realized_pnl)
                pnl_pct = (order.filled_price - pos.cost_price) / pos.cost_price if pos.cost_price > 0 else 0
                trade_log.append({
                    "date": current_date, "action": "SELL(熔断)",
                    "symbol": code, "name": pos.name,
                    "price": order.filled_price, "shares": order.filled_shares,
                    "reason": risk_reason, "pnl": realized_pnl,
                    "pnl_pct": pnl_pct,
                })
            elif order.status.value == "rejected":
                log.warning(f"熔断清仓失败({code}): {order.error_msg}, 标记为待清理")
                risk_mgr.status.pending_liquidate.append(code)
    account_after = simulator.get_account()
    # 清理pending_liquidate中已不存在的仓位
    risk_mgr.status.pending_liquidate = [
        s for s in risk_mgr.status.pending_liquidate if s in simulator.positions
    ]
    # 熔断清仓完毕后重置peak_nav，避免永久锁定
    if not simulator.positions and not risk_mgr.status.pending_liquidate:
        risk_mgr.reset_after_circuit_breaker(account_after.total_value)
    daily_nav.append({
        "date": current_date,
        "nav": account_after.total_value,
        "cash": simulator.cash,
        "position_value": account_after.total_value - simulator.cash,
        "close": current_price,
        "drawdown": risk_mgr.status.current_drawdown,
    })


def _handle_stop_loss(simulator, risk_mgr, trade_log,
                      current_date, current_price, symbol, name,
                      stop_reason, verbose):
    """止损/止盈卖出"""
    pos = simulator.positions[symbol]
    order = simulator.sell(symbol, name, current_price, pos.shares, current_date)
    if order.status.value == "filled":
        realized_pnl = order.realized_pnl
        risk_mgr.record_realized_loss(realized_pnl)
        action = "SELL(止盈)" if realized_pnl > 0 else "SELL(止损)"
        pnl_pct = (order.filled_price - pos.cost_price) / pos.cost_price if pos.cost_price > 0 else 0
        trade_log.append({
            "date": current_date, "action": action,
            "symbol": symbol, "name": name,
            "price": order.filled_price, "shares": order.filled_shares,
            "reason": stop_reason, "pnl": realized_pnl,
            "pnl_pct": pnl_pct,
        })
        if verbose:
            if realized_pnl > 0:
                print(f"  止盈卖出: {current_date} @ {order.filled_price:.2f}, "
                      f"盈利={realized_pnl:+,.0f} ({pnl_pct:+.2%})")
            else:
                print(f"  止损卖出: {current_date} @ {order.filled_price:.2f}, "
                      f"亏损={realized_pnl:+,.0f} ({pnl_pct:+.2%}) | {stop_reason}")


def _check_pyramid(simulator, position_sizer, risk_mgr, trade_log,
                   current_date, current_price, symbol, name,
                   price_gain_pct, trend_mode, verbose):
    """独立加仓检查"""
    pos = simulator.positions[symbol]
    if (pos.profit_pct >= risk_mgr.config["pyramid_trigger"]
            and pos.pyramid_count < risk_mgr.config["pyramid_max_adds"]):
        if trend_mode != "down":
            add_shares = position_sizer.calc_buy_size(
                current_price, simulator.cash, len(simulator.positions),
                position_value=pos.market_value,
                price_gain_pct=price_gain_pct,
                is_pyramid=True, base_shares=pos.base_shares,
                trend_mode=trend_mode,
            )
            add_shares = round_lot(add_shares)
            if add_shares > 0:
                order = simulator.buy(symbol, name, current_price, add_shares, current_date)
                if order.status.value == "filled":
                    pos.pyramid_count += 1
                    trade_log.append({
                        "date": current_date, "action": "BUY(加码)",
                        "symbol": symbol, "name": name,
                        "price": order.filled_price, "shares": add_shares,
                        "reason": f"盈利{pos.profit_pct:+.0%}加仓第{pos.pyramid_count}次",
                        "strength": 0.5,
                    })
                    if verbose:
                        print(f"  加仓: {current_date} @ {order.filled_price:.2f}, "
                              f"+{add_shares}股 (第{pos.pyramid_count}次, 共{pos.shares}股)")


def _handle_buy(simulator, position_sizer, trade_log,
                current_date, current_price, symbol, name,
                result, price_gain_pct, trend_mode, current_atr, verbose):
    """新建仓"""
    shares = position_sizer.calc_buy_size(
        current_price, simulator.cash, len(simulator.positions),
        price_gain_pct=price_gain_pct,
        trend_mode=trend_mode,
        atr=current_atr,
    )
    if shares > 0:
        order = simulator.buy(symbol, name, current_price, shares, current_date)
        if order.status.value == "filled":
            pos = simulator.positions[symbol]
            pos.peak_price = order.filled_price
            pos.base_shares = shares
            pos.entry_atr = current_atr
            trade_log.append({
                "date": current_date, "action": "BUY",
                "symbol": symbol, "name": name,
                "price": order.filled_price, "shares": shares,
                "reason": result.reason, "strength": result.strength,
                "price_gain": price_gain_pct, "trend": trend_mode,
            })
            if verbose:
                print(f"  买入: {current_date} @ {order.filled_price:.2f}, "
                      f"{shares}股, 涨幅={price_gain_pct:.0%}, 趋势={trend_mode}")


def _handle_sell(simulator, risk_mgr, trade_log,
                 current_date, current_price, symbol, name,
                 result, verbose):
    """策略信号卖出"""
    pos = simulator.positions[symbol]
    order = simulator.sell(symbol, name, current_price, pos.shares, current_date)
    if order.status.value == "filled":
        realized_pnl = order.realized_pnl
        risk_mgr.record_realized_loss(realized_pnl)
        pnl_pct = (order.filled_price - pos.cost_price) / pos.cost_price if pos.cost_price > 0 else 0
        trade_log.append({
            "date": current_date, "action": "SELL",
            "symbol": symbol, "name": name,
            "price": order.filled_price, "shares": order.filled_shares,
            "reason": result.reason, "pnl": realized_pnl,
            "pnl_pct": pnl_pct,
        })
        if verbose:
            print(f"  策略卖出: {current_date} @ {order.filled_price:.2f}, "
                  f"盈亏={realized_pnl:+,.0f} ({pnl_pct:+.2%})")


def _record_nav(simulator, risk_mgr, daily_nav, current_date, current_price):
    """记录当日净值"""
    account = simulator.get_account()
    total_value = account.total_value
    prev_nav = daily_nav[-1]["nav"] if daily_nav else total_value
    daily_return = (total_value - prev_nav) / prev_nav if prev_nav > 0 else 0
    daily_nav.append({
        "date": current_date,
        "nav": total_value,
        "cash": simulator.cash,
        "position_value": total_value - simulator.cash,
        "close": current_price,
        "drawdown": risk_mgr.status.current_drawdown,
        "daily_return": daily_return,
    })
