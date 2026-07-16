#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
组合回测网格搜索 V3 — 直接复用portfolio_engine，保证结果可复现

优化: 数据只拉一次缓存到全局变量, 每次组合重新初始化simulator/risk_mgr
回测循环直接调run_portfolio_trading_day, 与backtest_portfolio.py完全一致
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import json
import time
import copy
import datetime
import pandas as pd
import numpy as np

from execution.simulator import SimulatorExecutor
from risk.manager import RiskManager
from risk.position_sizer import PositionSizer
from strategy.combo import ComboStrategy
from strategy.base import Signal
from utils.helpers import format_code
from utils.logger import log, setup_logger
from engine.portfolio_engine import run_portfolio_trading_day
from backtest_portfolio import fetch_stock_data, STOCK_POOL

setup_logger()
log.remove()
log.add(sys.stderr, level="ERROR")

START_DATE = "2024-07-01"
END_DATE = "2026-07-04"
INITIAL_CAPITAL = 2_000_000
WARMUP = 70

# ─── 参数空间 ───
STAGE1_GRID = {
    "max_total_drawdown": [0.20, 0.25, 0.30, 0.35, 0.40],
    "max_position_ratio": [0.25, 0.30, 0.35, 0.40, 0.50],
}
FIXED_PEAK_STOP = 8.0

STAGE2_GRID = {
    "peak_stop_multiple": [6.0, 8.0, 10.0, 12.0],
}

# ─── 全局数据缓存 ───
_CACHED_DATA = None  # (stocks_data, stock_names, stock_date_map, all_dates)


def preload_data():
    """加载数据并缓存到全局变量，后续调用直接返回缓存"""
    global _CACHED_DATA
    if _CACHED_DATA is not None:
        return _CACHED_DATA

    print(f"加载数据 ({START_DATE} ~ {END_DATE})...")
    stocks_data = {}
    stock_names = {}
    for code, name in STOCK_POOL:
        fc = format_code(code)
        df = fetch_stock_data(fc, START_DATE, END_DATE)
        if not df.empty:
            stocks_data[fc] = df
            stock_names[fc] = name
            print(f"  {name}({fc}): {len(df)}条")

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

    _CACHED_DATA = (stocks_data, stock_names, stock_date_map, all_dates)
    return _CACHED_DATA


def run_single_backtest(
    stock_names, stocks_data, stock_date_map, all_dates,
    max_total_drawdown, max_position_ratio, peak_stop_multiple,
):
    """用portfolio_engine跑单次回测，保证与backtest_portfolio.py结果一致"""
    from config.settings import RISK_CONFIG, TRADE_CONFIG

    # 深拷贝config并覆盖参数
    risk_config = copy.deepcopy(RISK_CONFIG)
    risk_config["max_total_drawdown"] = max_total_drawdown
    risk_config["max_position_ratio"] = max_position_ratio
    risk_config["peak_stop_multiple"] = peak_stop_multiple

    # 用自定义config初始化组件
    simulator = SimulatorExecutor(INITIAL_CAPITAL)
    risk_mgr = RiskManager(INITIAL_CAPITAL)
    risk_mgr.config = risk_config
    position_sizer = PositionSizer(INITIAL_CAPITAL)
    position_sizer.risk = risk_config
    position_sizer.trade = TRADE_CONFIG.copy()
    strategies = {symbol: ComboStrategy() for symbol in stocks_data}

    trade_log = []
    daily_nav = []
    signal_log = []
    circuit_breaker_count = 0

    for date in all_dates:
        # 当日有数据且过warmup的股票
        active_stocks = {}
        for symbol, date_map in stock_date_map.items():
            if date in date_map and date_map[date] >= WARMUP:
                active_stocks[symbol] = date_map[date]
        if not active_stocks:
            continue

        ok = run_portfolio_trading_day(
            date=date,
            stocks_data=stocks_data,
            stock_indices=active_stocks,
            stock_names=stock_names,
            simulator=simulator,
            risk_mgr=risk_mgr,
            position_sizer=position_sizer,
            strategies=strategies,
            trade_log=trade_log,
            daily_nav=daily_nav,
            signal_log=signal_log,
            verbose=False,
        )
        if ok is False:
            circuit_breaker_count += 1

    # 计算统计
    if not daily_nav:
        return None

    nav_values = [d["nav"] for d in daily_nav]
    final_nav = nav_values[-1]
    total_return = (final_nav - INITIAL_CAPITAL) / INITIAL_CAPITAL
    days = len(daily_nav)
    annual_return = ((1 + total_return) ** (252 / days) - 1) if days > 0 else 0

    # 最大回撤: 逐日计算从历史峰值到当日谷底的最大跌幅
    peak_so_far = nav_values[0]
    max_drawdown = 0
    for v in nav_values:
        if v > peak_so_far:
            peak_so_far = v
        dd = (v - peak_so_far) / peak_so_far if peak_so_far > 0 else 0
        if dd < max_drawdown:
            max_drawdown = dd

    # 夏普比率
    returns = pd.Series(nav_values).pct_change().dropna()
    sharpe = (returns.mean() / returns.std() * np.sqrt(252)) if len(returns) > 1 and returns.std() > 0 else 0

    # 胜率
    sell_trades = [t for t in trade_log if "SELL" in t.get("action", "")]
    wins = [t for t in sell_trades if t.get("pnl", 0) > 0]
    win_rate = len(wins) / len(sell_trades) * 100 if sell_trades else 0

    # 盈亏比
    profits = [t["pnl"] for t in sell_trades if t.get("pnl", 0) > 0]
    losses = [abs(t["pnl"]) for t in sell_trades if t.get("pnl", 0) < 0]
    avg_profit = np.mean(profits) if profits else 0
    avg_loss = np.mean(losses) if losses else 1
    profit_loss_ratio = avg_profit / avg_loss if avg_loss > 0 else 0

    return {
        "max_total_drawdown": max_total_drawdown,
        "max_position_ratio": max_position_ratio,
        "peak_stop_multiple": peak_stop_multiple,
        "final_nav": final_nav,
        "total_return_pct": total_return * 100,
        "annual_return_pct": annual_return * 100,
        "max_drawdown_pct": max_drawdown * 100,
        "sharpe": sharpe,
        "win_rate_pct": win_rate,
        "profit_loss_ratio": profit_loss_ratio,
        "trade_count": len(trade_log),
        "circuit_breaker_count": circuit_breaker_count,
        "daily_nav_count": days,
    }


def run_grid_search():
    print(f"{'='*80}")
    print(f"  组合回测网格搜索 V3 (直接复用portfolio_engine)")
    print(f"  回测区间: {START_DATE} ~ {END_DATE}")
    print(f"  初始资金: {INITIAL_CAPITAL:,.0f}")
    print(f"  股票数量: {len(STOCK_POOL)}")
    print(f"{'='*80}")

    stocks_data, stock_names, stock_date_map, all_dates = preload_data()

    # ─── 阶段1: DD×Pos = 25组合 ───
    import itertools
    stage1_combos = list(itertools.product(
        STAGE1_GRID["max_total_drawdown"],
        STAGE1_GRID["max_position_ratio"],
    ))
    print(f"\n阶段1: 搜索 DD×Pos = {len(stage1_combos)}组合 (peak_stop={FIXED_PEAK_STOP}ATR)")
    print(f"{'─'*90}")
    print(f"{'#':>3} {'DD':>4} {'Pos':>4} {'收益':>8} {'回撤':>8} {'夏普':>6} {'胜率':>6} {'熔断':>4} {'耗时':>6}")
    print(f"{'─'*90}")

    stage1_results = []
    t_start = time.time()

    for i, (dd, pos) in enumerate(stage1_combos):
        t0 = time.time()
        stats = run_single_backtest(
            stock_names, stocks_data, stock_date_map, all_dates,
            dd, pos, FIXED_PEAK_STOP,
        )
        elapsed = time.time() - t0
        total_elapsed = time.time() - t_start
        eta = total_elapsed / (i + 1) * (len(stage1_combos) - i - 1)

        if stats:
            stage1_results.append(stats)
            marker = " 🏆" if stats["sharpe"] > 1.8 else ""
            print(f"{i+1:>3} {dd:>3.0%} {pos:>3.0%} "
                  f"{stats['total_return_pct']:>+7.1f}% {stats['max_drawdown_pct']:>+7.1f}% "
                  f"{stats['sharpe']:>5.2f} {stats['win_rate_pct']:>5.1f}% {stats['circuit_breaker_count']:>3}"
                  f"  {elapsed:>5.1f}s ETA {eta:>5.0f}s{marker}")

    # 阶段1最优（按夏普）
    stage1_best = max(stage1_results, key=lambda x: x["sharpe"])
    print(f"\n阶段1最优 (Sharpe): DD={stage1_best['max_total_drawdown']:.0%} "
          f"Pos={stage1_best['max_position_ratio']:.0%} → "
          f"收益={stage1_best['total_return_pct']:+.1f}% 回撤={stage1_best['max_drawdown_pct']:+.1f}% "
          f"夏普={stage1_best['sharpe']:.2f}")

    # ─── 阶段2: Peak = 4组合 ───
    print(f"\n阶段2: 搜索 peak_stop = {len(STAGE2_GRID['peak_stop_multiple'])}组合 "
          f"(DD={stage1_best['max_total_drawdown']:.0%}, Pos={stage1_best['max_position_ratio']:.0%})")
    print(f"{'─'*90}")
    print(f"{'#':>3} {'Peak':>6} {'收益':>8} {'回撤':>8} {'夏普':>6} {'胜率':>6} {'熔断':>4} {'耗时':>6}")
    print(f"{'─'*90}")

    stage2_results = []
    for i, peak in enumerate(STAGE2_GRID["peak_stop_multiple"]):
        t0 = time.time()
        stats = run_single_backtest(
            stock_names, stocks_data, stock_date_map, all_dates,
            stage1_best["max_total_drawdown"],
            stage1_best["max_position_ratio"],
            peak,
        )
        elapsed = time.time() - t0

        if stats:
            stage2_results.append(stats)
            print(f"{i+1:>3} {peak:>5.0f}ATR "
                  f"{stats['total_return_pct']:>+7.1f}% {stats['max_drawdown_pct']:>+7.1f}% "
                  f"{stats['sharpe']:>5.2f} {stats['win_rate_pct']:>5.1f}% {stats['circuit_breaker_count']:>3}"
                  f"  {elapsed:>5.1f}s")

    all_results = stage1_results + stage2_results
    best_by_return = max(all_results, key=lambda x: x["total_return_pct"])
    best_by_sharpe = max(all_results, key=lambda x: x["sharpe"])
    best_by_drawdown = max(all_results, key=lambda x: x["max_drawdown_pct"])

    total_elapsed = time.time() - t_start
    print(f"\n{'='*80}")
    print(f"  网格搜索完成: {len(all_results)}组合, 耗时{total_elapsed:.1f}s")
    print(f"{'='*80}")
    print(f"  📊 最优(收益): DD={best_by_return['max_total_drawdown']:.0%} "
          f"Pos={best_by_return['max_position_ratio']:.0%} Peak={best_by_return['peak_stop_multiple']:.0f}ATR")
    print(f"     收益={best_by_return['total_return_pct']:+.1f}% 回撤={best_by_return['max_drawdown_pct']:+.1f}% "
          f"夏普={best_by_return['sharpe']:.2f}")
    print(f"  📊 最优(夏普): DD={best_by_sharpe['max_total_drawdown']:.0%} "
          f"Pos={best_by_sharpe['max_position_ratio']:.0%} Peak={best_by_sharpe['peak_stop_multiple']:.0f}ATR")
    print(f"     收益={best_by_sharpe['total_return_pct']:+.1f}% 回撤={best_by_sharpe['max_drawdown_pct']:+.1f}% "
          f"夏普={best_by_sharpe['sharpe']:.2f}")
    print(f"  📊 最优(回撤): DD={best_by_drawdown['max_total_drawdown']:.0%} "
          f"Pos={best_by_drawdown['max_position_ratio']:.0%} Peak={best_by_drawdown['peak_stop_multiple']:.0f}ATR")
    print(f"     收益={best_by_drawdown['total_return_pct']:+.1f}% 回撤={best_by_drawdown['max_drawdown_pct']:+.1f}% "
          f"夏普={best_by_drawdown['sharpe']:.2f}")

    # 保存结果
    output = {
        "search_date": "2026-07-05",
        "version": "V3-direct",
        "backtest_window": f"{START_DATE} ~ {END_DATE}",
        "initial_capital": INITIAL_CAPITAL,
        "stock_count": len(STOCK_POOL),
        "stage1_grid": STAGE1_GRID,
        "stage2_grid": STAGE2_GRID,
        "total_combos": len(all_results),
        "elapsed_seconds": total_elapsed,
        "best_by_return": best_by_return,
        "best_by_sharpe": best_by_sharpe,
        "best_by_drawdown": best_by_drawdown,
        "all_results": all_results,
    }
    output_file = "grid_search_v3_results.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {output_file}")


if __name__ == "__main__":
    run_grid_search()
