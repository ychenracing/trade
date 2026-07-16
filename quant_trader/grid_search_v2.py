#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
组合回测网格搜索 V2 — 两阶段搜索，预计算信号加速

阶段1: 固定peak_stop=8ATR，搜 DD×Pos = 5×5=25组合
阶段2: 用阶段1最优DD/Pos，搜 peak_stop = 4组合
总计29组合，约20分钟

关键优化: 策略信号预计算（信号不随风控参数变化，只算一次）
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import json
import itertools
import time
import datetime
import pandas as pd
import numpy as np

from execution.simulator import SimulatorExecutor
from risk.manager import RiskManager
from risk.position_sizer import PositionSizer
from strategy.combo import ComboStrategy
from strategy.base import Signal
from utils.helpers import format_code, calc_atr, calc_sma, calc_trend_mode, round_lot
from utils.logger import log, setup_logger
from backtest_portfolio import fetch_stock_data, STOCK_POOL

setup_logger()
log.remove()
log.add(sys.stderr, level="ERROR")

# ─── 搜索参数空间 ───
STAGE1_GRID = {
    "max_total_drawdown": [0.20, 0.25, 0.30, 0.35, 0.40],
    "max_position_ratio": [0.25, 0.30, 0.35, 0.40, 0.50],
}
STAGE2_GRID = {
    "peak_stop_multiple": [6.0, 8.0, 10.0, 12.0],
}
FIXED_PEAK_STOP = 8.0  # 阶段1固定值

START_DATE = "2024-07-01"
END_DATE = "2026-07-04"
INITIAL_CAPITAL = 2_000_000
WARMUP = 70


def preload_data():
    """加载所有股票数据，只拉一次"""
    print(f"加载数据 ({START_DATE} ~ {END_DATE})...")
    stocks_data = {}
    stock_names = {}
    for code, name in STOCK_POOL:
        code = format_code(code)
        df = fetch_stock_data(code, START_DATE, END_DATE)
        if not df.empty:
            stocks_data[code] = df
            stock_names[code] = name
            print(f"  {name}({code}): {len(df)}条")

    # 构建日期索引
    stock_date_map = {}
    for symbol, df in stocks_data.items():
        stock_date_map[symbol] = {}
        for i in range(len(df)):
            d = df.iloc[i]["date"]
            if hasattr(d, 'date'):
                d = d.date()
            elif isinstance(d, str):
                d = pd.to_datetime(d).date()
            stock_date_map[symbol][d] = i

    all_dates = sorted(set().union(*[set(m.keys()) for m in stock_date_map.values()]))
    print(f"成功获取: {len(stocks_data)}/{len(STOCK_POOL)}只, 交易日: {len(all_dates)}")
    return stocks_data, stock_names, stock_date_map, all_dates


def precompute_signals(stocks_data, stock_date_map, all_dates):
    """预计算所有股票在所有日期的策略信号（信号不随风控参数变化）

    返回: {symbol: {date: (signal, strength, reason, ctx)}}
    """
    print("预计算策略信号...")
    signals = {}
    strategies = {symbol: ComboStrategy() for symbol in stocks_data}
    t0 = time.time()

    for symbol, df in stocks_data.items():
        signals[symbol] = {}
        strategy = strategies[symbol]
        date_map = stock_date_map[symbol]

        for i in range(WARMUP, len(df)):
            date = df.iloc[i]["date"]
            if hasattr(date, 'date'):
                date = date.date()
            elif isinstance(date, str):
                date = pd.to_datetime(date).date()

            # calc_daily_context (inline for speed)
            current_price = float(df.iloc[i]["close"])
            if i >= 20:
                atr_series = calc_atr(df["high"].iloc[:i+1], df["low"].iloc[:i+1], df["close"].iloc[:i+1], 20)
                current_atr = float(atr_series.iloc[-1]) if not np.isnan(atr_series.iloc[-1]) else 0.0
            else:
                current_atr = 0.0
            ma60_series = calc_sma(df["close"].iloc[:i+1], 60)
            ma60 = float(ma60_series.iloc[-1]) if len(ma60_series) > 0 and not np.isnan(ma60_series.iloc[-1]) else current_price
            price_gain_pct = (current_price - ma60) / ma60 if ma60 > 0 else 0.0
            trend_mode = calc_trend_mode(df.iloc[:i+1])
            window = df.iloc[:i+1].copy()

            ctx = {
                "price": current_price,
                "date": date,
                "atr": current_atr,
                "ma60": ma60,
                "price_gain_pct": price_gain_pct,
                "trend_mode": trend_mode,
            }

            # 策略信号
            result = strategy.generate_signal(window)
            signals[symbol][date] = (result.signal, result.strength, result.reason, ctx)

    elapsed = time.time() - t0
    total_signals = sum(len(v) for v in signals.values())
    print(f"  预计算完成: {total_signals}个信号, 耗时{elapsed:.1f}s")
    return signals


def run_single_backtest(
    stocks_data, stock_date_map, all_dates, signals, stock_names,
    max_total_drawdown, max_position_ratio, peak_stop_multiple,
):
    """用预计算的信号跑单次回测"""
    from config.settings import RISK_CONFIG, TRADE_CONFIG

    # 深拷贝并覆盖参数
    risk_config = RISK_CONFIG.copy()
    risk_config["max_total_drawdown"] = max_total_drawdown
    risk_config["max_position_ratio"] = max_position_ratio
    risk_config["peak_stop_multiple"] = peak_stop_multiple
    risk_config["profit_protection_tiers"] = RISK_CONFIG["profit_protection_tiers"].copy()
    risk_config["pyramid_allowed"] = RISK_CONFIG.get("pyramid_allowed", True)

    simulator = SimulatorExecutor(INITIAL_CAPITAL)
    # 覆盖simulator的config引用
    from risk import manager as rm_module
    from execution import simulator as sim_module
    # RiskManager用自定义config
    risk_mgr = RiskManager(INITIAL_CAPITAL)
    risk_mgr.config = risk_config
    risk_mgr.config["pyramid_trigger"] = RISK_CONFIG.get("pyramid_trigger", 0.08)
    risk_mgr.config["pyramid_max_adds"] = RISK_CONFIG.get("pyramid_max_adds", 2)
    risk_mgr.config["pyramid_size_ratio"] = RISK_CONFIG.get("pyramid_size_ratio", 0.50)
    risk_mgr.config["atr_stop_multiple"] = RISK_CONFIG.get("atr_stop_multiple", 2.5)
    risk_mgr.config["atr_stop_max_loss"] = RISK_CONFIG.get("atr_stop_max_loss", 0.15)
    risk_mgr.config["atr_stop_min_loss"] = RISK_CONFIG.get("atr_stop_min_loss", 0.08)
    risk_mgr.config["peak_stop_max_loss"] = RISK_CONFIG.get("peak_stop_max_loss", 0.35)
    risk_mgr.config["peak_stop_min_loss"] = RISK_CONFIG.get("peak_stop_min_loss", 0.20)
    risk_mgr.config["stop_profit_mode"] = RISK_CONFIG.get("stop_profit_mode", "hold")
    risk_mgr.config["max_positions"] = RISK_CONFIG.get("max_positions", 8)
    risk_mgr.config["max_position_loss"] = RISK_CONFIG.get("max_position_loss", 0.08)
    risk_mgr.config["max_daily_loss"] = RISK_CONFIG.get("max_daily_loss", 0.05)
    risk_mgr.config["trailing_stop_trigger"] = RISK_CONFIG.get("trailing_stop_trigger", 0.15)
    risk_mgr.config["trailing_stop_ratio"] = RISK_CONFIG.get("trailing_stop_ratio", 0.70)
    risk_mgr.config["daily_loss_halt_days"] = RISK_CONFIG.get("daily_loss_halt_days", 1)
    risk_mgr.config["position_sizing_mode"] = RISK_CONFIG.get("position_sizing_mode", "fixed")
    risk_mgr.config["min_position_ratio"] = RISK_CONFIG.get("min_position_ratio", 0.05)
    risk_mgr.config["pyramid_stop_from_avg"] = RISK_CONFIG.get("pyramid_stop_from_avg", True)

    position_sizer = PositionSizer(INITIAL_CAPITAL)
    position_sizer.risk = risk_mgr.config
    position_sizer.trade = TRADE_CONFIG.copy()

    trade_log = []
    daily_nav = []
    circuit_breaker_count = 0

    for date in all_dates:
        # 当日有数据且过warmup的股票
        active_stocks = {}
        for symbol, date_map in stock_date_map.items():
            if date in date_map and date_map[date] >= WARMUP:
                active_stocks[symbol] = date_map[date]
        if not active_stocks:
            continue

        # Step 1: T+1清理
        simulator._check_date_rollover(date)

        # Step 2: 更新所有持仓现价
        price_updates = {}
        for symbol in active_stocks:
            idx = active_stocks[symbol]
            price_updates[symbol] = float(stocks_data[symbol].iloc[idx]["close"])
        # 也更新非active持仓的现价（用上次价格）
        for symbol in simulator.positions:
            if symbol not in price_updates:
                price_updates[symbol] = simulator.positions[symbol].current_price
        simulator.update_prices(price_updates)

        # Step 3: 组合风控
        account = simulator.get_account()
        total_value = account.total_value
        risk_mgr.update_nav(total_value, date)
        position_sizer.update_capital(total_value)

        # Step 4: 熔断检查
        allowed, risk_reason = risk_mgr.check_global_risk(total_value, date)
        if not allowed:
            circuit_breaker_count += 1
            # 清仓所有
            for code in list(simulator.positions.keys()):
                pos = simulator.positions[code]
                price = price_updates.get(code, pos.current_price)
                if code not in simulator.today_bought:
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
                            "reason": risk_reason, "pnl": realized_pnl, "pnl_pct": pnl_pct,
                        })
                    else:
                        risk_mgr.status.pending_liquidate.append(code)

            # 清理pending_liquidate
            risk_mgr.status.pending_liquidate = [
                s for s in risk_mgr.status.pending_liquidate if s in simulator.positions
            ]
            if not simulator.positions and not risk_mgr.status.pending_liquidate:
                risk_mgr.reset_after_circuit_breaker(total_value)

            daily_nav.append({
                "date": date, "nav": total_value, "cash": simulator.cash,
                "position_value": total_value - simulator.cash,
                "position_count": len(simulator.positions),
                "drawdown": risk_mgr.status.current_drawdown,
            })
            continue

        # Step 4.5: 清理pending_liquidate
        for symbol in list(risk_mgr.status.pending_liquidate):
            if symbol in simulator.positions and symbol not in simulator.today_bought:
                pos = simulator.positions[symbol]
                price = price_updates.get(symbol, pos.current_price)
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
                        "reason": "前日熔断T+1残留清理", "pnl": realized_pnl, "pnl_pct": pnl_pct,
                    })
                    if symbol in risk_mgr.status.pending_liquidate:
                        risk_mgr.status.pending_liquidate.remove(symbol)

        # Step 5: 止损止盈 + 加仓 + 策略信号 → 收集操作
        buy_candidates = []
        for symbol, idx in active_stocks.items():
            if symbol not in signals or date not in signals[symbol]:
                continue
            sig, strength, reason, ctx = signals[symbol][date]
            current_price = ctx["price"]
            current_atr = ctx["atr"]
            price_gain_pct = ctx["price_gain_pct"]
            trend_mode = ctx["trend_mode"]

            # 止损止盈检查
            if symbol in simulator.positions:
                pos = simulator.positions[symbol]
                need_stop, stop_reason = risk_mgr.check_stop_loss(pos, current_atr)
                if need_stop and symbol not in simulator.today_bought:
                    order = simulator.sell(symbol, pos.name, current_price, pos.shares, date)
                    if order.status.value == "filled":
                        realized_pnl = order.realized_pnl
                        risk_mgr.record_realized_loss(realized_pnl)
                        pnl_pct = (order.filled_price - pos.cost_price) / pos.cost_price if pos.cost_price > 0 else 0
                        action = "SELL(止盈)" if realized_pnl > 0 else "SELL(止损)"
                        trade_log.append({
                            "date": date, "action": action,
                            "symbol": symbol, "name": pos.name,
                            "price": order.filled_price, "shares": order.filled_shares,
                            "reason": stop_reason, "pnl": realized_pnl, "pnl_pct": pnl_pct,
                        })
                    continue  # 止损后不再检查加仓/信号

                # 加仓检查
                if (risk_mgr.config.get("pyramid_allowed", True)
                        and pos.profit_pct >= risk_mgr.config["pyramid_trigger"]
                        and pos.pyramid_count < risk_mgr.config["pyramid_max_adds"]
                        and trend_mode != "down"):
                    add_shares = position_sizer.calc_buy_size(
                        current_price, simulator.cash, len(simulator.positions),
                        position_value=pos.market_value,
                        price_gain_pct=price_gain_pct,
                        is_pyramid=True, base_shares=pos.base_shares,
                    )
                    if add_shares > 0:
                        order = simulator.buy(symbol, pos.name, current_price, add_shares, date)
                        if order.status.value == "filled":
                            pos.pyramid_count += 1
                            trade_log.append({
                                "date": date, "action": "BUY(加码)",
                                "symbol": symbol, "name": pos.name,
                                "price": order.filled_price, "shares": add_shares,
                                "reason": f"盈利{pos.profit_pct:+.0%}加仓第{pos.pyramid_count}次",
                            })
                    continue  # 加仓后不再检查卖出信号

                # 策略卖出信号
                if sig == Signal.SELL and symbol not in simulator.today_bought:
                    order = simulator.sell(symbol, pos.name, current_price, pos.shares, date)
                    if order.status.value == "filled":
                        realized_pnl = order.realized_pnl
                        risk_mgr.record_realized_loss(realized_pnl)
                        pnl_pct = (order.filled_price - pos.cost_price) / pos.cost_price if pos.cost_price > 0 else 0
                        trade_log.append({
                            "date": date, "action": "SELL",
                            "symbol": symbol, "name": pos.name,
                            "price": order.filled_price, "shares": order.filled_shares,
                            "reason": reason, "pnl": realized_pnl, "pnl_pct": pnl_pct,
                        })
                    continue

            # 买入信号
            if sig == Signal.BUY and symbol not in simulator.positions:
                if len(simulator.positions) >= risk_mgr.config["max_positions"]:
                    continue
                buy_candidates.append((symbol, strength, reason, ctx))

        # Step 6: 执行买入（按信号强度排序）
        buy_candidates.sort(key=lambda x: x[1], reverse=True)
        for symbol, strength, reason, ctx in buy_candidates:
            current_price = ctx["price"]
            current_atr = ctx["atr"]
            price_gain_pct = ctx["price_gain_pct"]
            trend_mode = ctx["trend_mode"]
            name = stock_names.get(symbol, symbol)

            shares = position_sizer.calc_buy_size(
                current_price, simulator.cash, len(simulator.positions),
                price_gain_pct=price_gain_pct,
            )
            if shares <= 0:
                continue
            order = simulator.buy(symbol, name, current_price, shares, date)
            if order.status.value == "filled":
                pos = simulator.positions[symbol]
                pos.peak_price = order.filled_price
                pos.base_shares = shares
                pos.entry_atr = current_atr
                trade_log.append({
                    "date": date, "action": "BUY",
                    "symbol": symbol, "name": name,
                    "price": order.filled_price, "shares": shares,
                    "reason": reason, "strength": strength,
                    "price_gain": price_gain_pct, "trend": trend_mode,
                })

        # Step 7: 记录净值
        account = simulator.get_account()
        total_value = account.total_value
        prev_nav = daily_nav[-1]["nav"] if daily_nav else total_value
        daily_return = (total_value - prev_nav) / prev_nav if prev_nav > 0 else 0
        daily_nav.append({
            "date": date, "nav": total_value, "cash": simulator.cash,
            "position_value": total_value - simulator.cash,
            "position_count": len(simulator.positions),
            "drawdown": risk_mgr.status.current_drawdown,
            "daily_return": daily_return,
        })

    # 计算统计
    if not daily_nav:
        return None

    nav_series = pd.Series([d["nav"] for d in daily_nav])
    final_nav = daily_nav[-1]["nav"]
    total_return = (final_nav - INITIAL_CAPITAL) / INITIAL_CAPITAL
    peak = nav_series.cummax()
    drawdown = (nav_series - peak) / peak
    max_drawdown = drawdown.min()
    days = len(daily_nav)
    annual_return = ((1 + total_return) ** (252 / days) - 1) if days > 0 else 0

    daily_returns = pd.Series([d.get("daily_return", 0) for d in daily_nav])
    if daily_returns.std() > 0:
        sharpe = (daily_returns.mean() / daily_returns.std()) * np.sqrt(252)
    else:
        sharpe = 0

    sell_trades = [t for t in trade_log if "SELL" in t.get("action", "")]
    wins = [t for t in sell_trades if t.get("pnl", 0) > 0]
    win_rate = len(wins) / len(sell_trades) * 100 if sell_trades else 0

    return {
        "total_return_pct": total_return * 100,
        "annual_return_pct": annual_return * 100,
        "max_drawdown_pct": max_drawdown * 100,
        "sharpe": sharpe,
        "win_rate_pct": win_rate,
        "trade_count": len(sell_trades),
        "circuit_breaker_count": circuit_breaker_count,
        "final_nav": final_nav,
        "daily_nav_count": len(daily_nav),
    }


def run_grid_search():
    print(f"{'='*70}")
    print(f"  组合回测网格搜索 V2 (两阶段+预计算)")
    print(f"  回测区间: {START_DATE} ~ {END_DATE}")
    print(f"  初始资金: {INITIAL_CAPITAL:,.0f}")
    print(f"  股票数量: {len(STOCK_POOL)}")
    print(f"{'='*70}")

    # 1. 加载数据（只拉一次）
    stocks_data, stock_names, stock_date_map, all_dates = preload_data()

    # 2. 预计算信号
    signals = precompute_signals(stocks_data, stock_date_map, all_dates)

    # 3. 阶段1: 搜DD×Pos (固定peak_stop=8)
    stage1_combos = list(itertools.product(
        STAGE1_GRID["max_total_drawdown"],
        STAGE1_GRID["max_position_ratio"],
    ))

    print(f"\n阶段1: 搜索 DD×Pos = {len(stage1_combos)}组合 (peak_stop={FIXED_PEAK_STOP}ATR)")
    print(f"{'─'*90}")
    print(f"{'#':>3} {'DD':>5} {'Pos':>5} {'收益':>8} {'回撤':>8} {'夏普':>6} {'胜率':>6} {'熔断':>4} {'耗时':>6}")
    print(f"{'─'*90}")

    stage1_results = []
    t_start = time.time()

    for i, (dd, pos) in enumerate(stage1_combos):
        t0 = time.time()
        stats = run_single_backtest(
            stocks_data, stock_date_map, all_dates, signals, stock_names,
            dd, pos, FIXED_PEAK_STOP,
        )
        elapsed = time.time() - t0
        total_elapsed = time.time() - t_start
        eta = total_elapsed / (i + 1) * (len(stage1_combos) - i - 1)

        if stats:
            stage1_results.append({
                "max_total_drawdown": dd,
                "max_position_ratio": pos,
                "peak_stop_multiple": FIXED_PEAK_STOP,
                **stats,
            })
            emoji = "🏆" if stats["total_return_pct"] == max(r["total_return_pct"] for r in stage1_results) else "  "
            print(f"{i+1:>3} {dd:>5.0%} {pos:>5.0%} {stats['total_return_pct']:>+7.1f}% "
                  f"{stats['max_drawdown_pct']:>+7.1f}% {stats['sharpe']:>6.2f} "
                  f"{stats['win_rate_pct']:>5.1f}% {stats['circuit_breaker_count']:>4} "
                  f"{elapsed:>5.1f}s ETA{eta:>5.0f}s {emoji}")
        else:
            print(f"{i+1:>3} {dd:>5.0%} {pos:>5.0%}  FAILED")

    # 阶段1最优
    stage1_best = max(stage1_results, key=lambda r: r["sharpe"])  # 用sharpe做综合排名
    print(f"\n阶段1最优 (Sharpe): DD={stage1_best['max_total_drawdown']:.0%} "
          f"Pos={stage1_best['max_position_ratio']:.0%} "
          f"→ 收益={stage1_best['total_return_pct']:+.1f}% "
          f"回撤={stage1_best['max_drawdown_pct']:.1f}% "
          f"夏普={stage1_best['sharpe']:.2f}")

    # 4. 阶段2: 搜peak_stop (用阶段1最优DD/Pos)
    print(f"\n阶段2: 搜索 peak_stop = {len(STAGE2_GRID['peak_stop_multiple'])}组合 "
          f"(DD={stage1_best['max_total_drawdown']:.0%}, Pos={stage1_best['max_position_ratio']:.0%})")
    print(f"{'─'*90}")
    print(f"{'#':>3} {'Peak':>6} {'收益':>8} {'回撤':>8} {'夏普':>6} {'胜率':>6} {'熔断':>4} {'耗时':>6}")
    print(f"{'─'*90}")

    stage2_results = []
    for i, peak in enumerate(STAGE2_GRID["peak_stop_multiple"]):
        t0 = time.time()
        stats = run_single_backtest(
            stocks_data, stock_date_map, all_dates, signals, stock_names,
            stage1_best["max_total_drawdown"],
            stage1_best["max_position_ratio"],
            peak,
        )
        elapsed = time.time() - t0

        if stats:
            stage2_results.append({
                "max_total_drawdown": stage1_best["max_total_drawdown"],
                "max_position_ratio": stage1_best["max_position_ratio"],
                "peak_stop_multiple": peak,
                **stats,
            })
            print(f"{i+1:>3} {peak:>5.0f}ATR {stats['total_return_pct']:>+7.1f}% "
                  f"{stats['max_drawdown_pct']:>+7.1f}% {stats['sharpe']:>6.2f} "
                  f"{stats['win_rate_pct']:>5.1f}% {stats['circuit_breaker_count']:>4} "
                  f"{elapsed:>5.1f}s")

    # 5. 全部结果汇总
    all_results = stage1_results + stage2_results
    best_by_return = max(all_results, key=lambda r: r["total_return_pct"])
    best_by_sharpe = max(all_results, key=lambda r: r["sharpe"])
    best_by_dd = min(all_results, key=lambda r: abs(r["max_drawdown_pct"]))

    total_elapsed = time.time() - t_start
    print(f"\n{'='*70}")
    print(f"  网格搜索完成 ({total_elapsed:.0f}s = {total_elapsed/60:.1f}min)")
    print(f"{'='*70}")

    print(f"\n  📊 最优(收益): DD={best_by_return['max_total_drawdown']:.0%} "
          f"Pos={best_by_return['max_position_ratio']:.0%} "
          f"Peak={best_by_return['peak_stop_multiple']:.0f}ATR\n"
          f"     收益={best_by_return['total_return_pct']:+.1f}% "
          f"回撤={best_by_return['max_drawdown_pct']:.1f}% "
          f"夏普={best_by_return['sharpe']:.2f} "
          f"熔断={best_by_return['circuit_breaker_count']}次")

    print(f"\n  📊 最优(夏普): DD={best_by_sharpe['max_total_drawdown']:.0%} "
          f"Pos={best_by_sharpe['max_position_ratio']:.0%} "
          f"Peak={best_by_sharpe['peak_stop_multiple']:.0f}ATR\n"
          f"     收益={best_by_sharpe['total_return_pct']:+.1f}% "
          f"回撤={best_by_sharpe['max_drawdown_pct']:.1f}% "
          f"夏普={best_by_sharpe['sharpe']:.2f} "
          f"熔断={best_by_sharpe['circuit_breaker_count']}次")

    print(f"\n  📊 最优(回撤): DD={best_by_dd['max_total_drawdown']:.0%} "
          f"Pos={best_by_dd['max_position_ratio']:.0%} "
          f"Peak={best_by_dd['peak_stop_multiple']:.0f}ATR\n"
          f"     收益={best_by_dd['total_return_pct']:+.1f}% "
          f"回撤={best_by_dd['max_drawdown_pct']:.1f}% "
          f"夏普={best_by_dd['sharpe']:.2f} "
          f"熔断={best_by_dd['circuit_breaker_count']}次")

    # 6. 保存结果
    result = {
        "search_date": datetime.date.today().isoformat(),
        "backtest_window": f"{START_DATE} ~ {END_DATE}",
        "initial_capital": INITIAL_CAPITAL,
        "stock_count": len(STOCK_POOL),
        "stage1_grid": STAGE1_GRID,
        "stage2_grid": STAGE2_GRID,
        "total_combos": len(all_results),
        "elapsed_seconds": total_elapsed,
        "best_by_return": best_by_return,
        "best_by_sharpe": best_by_sharpe,
        "best_by_drawdown": best_by_dd,
        "all_results": all_results,
    }
    output_file = "grid_search_portfolio_v2_results.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n结果已保存: {output_file}")

    # 7. 热力图
    _plot_heatmap(stage1_results, best_by_sharpe)

    return result


def _plot_heatmap(stage1_results, best_sharpe):
    """画阶段1热力图"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    df = pd.DataFrame(stage1_results)
    fig, axes = plt.subplots(1, 3, figsize=(18, 6))

    titles = ['总收益率 %', '最大回撤 %', '夏普比率']
    keys = ['total_return_pct', 'max_drawdown_pct', 'sharpe']
    cmaps = ['RdYlGn', 'RdYlGn_r', 'RdYlGn']

    for ax, title, key, cmap in zip(axes, titles, keys, cmaps):
        pivot = df.pivot_table(
            index='max_total_drawdown',
            columns='max_position_ratio',
            values=key,
        )
        im = ax.imshow(pivot.values, cmap=cmap, aspect='auto')
        ax.set_title(title, fontsize=13, fontweight='bold')
        ax.set_xlabel('单股仓位 %')
        ax.set_ylabel('熔断阈值 %')
        ax.set_xticks(range(len(pivot.columns)))
        ax.set_xticklabels([f"{c*100:.0f}" for c in pivot.columns])
        ax.set_yticks(range(len(pivot.index)))
        ax.set_yticklabels([f"{r*100:.0f}" for r in pivot.index])

        for i in range(len(pivot.index)):
            for j in range(len(pivot.columns)):
                val = pivot.values[i, j]
                if not np.isnan(val):
                    color = "white" if abs(val - np.nanmin(pivot.values)) / (np.nanmax(pivot.values) - np.nanmin(pivot.values) + 0.01) > 0.6 else "black"
                    fmt = "{:+.1f}" if key != "sharpe" else "{:.2f}"
                    ax.text(j, i, fmt.format(val), ha="center", va="center",
                            fontsize=11, color=color, fontweight="bold")
        plt.colorbar(im, ax=ax, shrink=0.8)

    plt.suptitle("组合回测网格搜索 — 阶段1热力图\n"
                 f"回测区间: {START_DATE} ~ {END_DATE} | "
                 f"初始资金: {INITIAL_CAPITAL:,.0f} | "
                 f"峰值止损: {FIXED_PEAK_STOP}ATR\n"
                 f"最优(夏普): DD={best_sharpe['max_total_drawdown']:.0%} "
                 f"Pos={best_sharpe['max_position_ratio']:.0%} "
                 f"Peak={best_sharpe['peak_stop_multiple']:.0f}ATR → "
                 f"收益={best_sharpe['total_return_pct']:+.1f}% "
                 f"夏普={best_sharpe['sharpe']:.2f}",
                 fontsize=13, fontweight="bold", y=1.04)
    plt.tight_layout()
    output_file = "grid_search_portfolio_v2_heatmap.png"
    plt.savefig(output_file, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"热力图已保存: {output_file}")


if __name__ == "__main__":
    run_grid_search()
