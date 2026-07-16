#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
定向网格搜索: DD=25%, Pos=25%/30%/35% × Peak=6/8/10/12 = 12组合
复用grid_search_v2.py的预计算和回测逻辑
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import json
import time
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
from grid_search_v2 import precompute_signals, run_single_backtest, preload_data

setup_logger()
log.remove()
log.add(sys.stderr, level="ERROR")

START_DATE = "2024-07-01"
END_DATE = "2026-07-04"
INITIAL_CAPITAL = 2_000_000
WARMUP = 70

# 固定DD=25%, 搜Pos×Peak
DD = 0.25
POS_LIST = [0.25, 0.30, 0.35]
PEAK_LIST = [6.0, 8.0, 10.0, 12.0]


def main():
    print(f"{'='*80}")
    print(f"  定向网格搜索: DD={DD:.0%} × Pos×Peak = {len(POS_LIST)}×{len(PEAK_LIST)}={len(POS_LIST)*len(PEAK_LIST)}组合")
    print(f"  回测区间: {START_DATE} ~ {END_DATE}")
    print(f"{'='*80}")

    # 1. 加载数据 + 构建日期索引
    print(f"\n加载数据...")
    stocks_data, stock_names, stock_date_map, all_dates = preload_data()
    print(f"交易日总数: {len(all_dates)}")

    # 2. 预计算信号
    print(f"\n预计算策略信号...")
    t0 = time.time()
    signals = precompute_signals(stocks_data, stock_date_map, all_dates)
    print(f"  预计算完成: 耗时{time.time()-t0:.1f}s")

    # 3. 跑12组合
    print(f"\n{'─'*90}")
    print(f"{'Pos':>6} {'Peak':>6} {'收益':>8} {'回撤':>8} {'夏普':>6} {'胜率':>6} {'熔断':>4} {'交易':>4}")
    print(f"{'─'*90}")

    results = []
    for pos in POS_LIST:
        for peak in PEAK_LIST:
            t0 = time.time()
            stats = run_single_backtest(
                stocks_data, stock_date_map, all_dates, signals, stock_names,
                DD, pos, peak,
            )
            elapsed = time.time() - t0

            if stats:
                r = {
                    "max_total_drawdown": DD,
                    "max_position_ratio": pos,
                    "peak_stop_multiple": peak,
                    "total_return_pct": stats["total_return_pct"],
                    "max_drawdown_pct": stats["max_drawdown_pct"],
                    "sharpe": stats["sharpe"],
                    "win_rate_pct": stats["win_rate_pct"],
                    "trade_count": stats["trade_count"],
                    "circuit_breaker_count": stats["circuit_breaker_count"],
                    "final_nav": stats["final_nav"],
                }
                results.append(r)
                print(f"{pos:>5.0%} {peak:>5.0f}ATR {stats['total_return_pct']:>+7.1f}% "
                      f"{stats['max_drawdown_pct']:>+7.1f}% {stats['sharpe']:>5.2f} "
                      f"{stats['win_rate_pct']:>5.1f}% {stats['circuit_breaker_count']:>4} "
                      f"{stats['trade_count']:>4} ({elapsed:.1f}s)")
            else:
                print(f"{pos:>5.0%} {peak:>5.0f}ATR  FAILED")

    # 5. 排序
    print(f"\n{'─'*90}")
    print(f"\n按收益排序:")
    by_return = sorted(results, key=lambda x: x["total_return_pct"], reverse=True)
    for i, r in enumerate(by_return):
        print(f"  {i+1}. Pos={r['max_position_ratio']:.0%} Peak={r['peak_stop_multiple']:.0f}ATR "
              f"→ 收益={r['total_return_pct']:+.1f}% 回撤={r['max_drawdown_pct']:+.1f}% "
              f"夏普={r['sharpe']:.2f} 胜率={r['win_rate_pct']:.1f}% "
              f"熔断={r['circuit_breaker_count']} 交易={r['trade_count']}")

    print(f"\n按夏普排序:")
    by_sharpe = sorted(results, key=lambda x: x["sharpe"], reverse=True)
    for i, r in enumerate(by_sharpe):
        print(f"  {i+1}. Pos={r['max_position_ratio']:.0%} Peak={r['peak_stop_multiple']:.0f}ATR "
              f"→ 夏普={r['sharpe']:.2f} 收益={r['total_return_pct']:+.1f}% "
              f"回撤={r['max_drawdown_pct']:+.1f}% 胜率={r['win_rate_pct']:.1f}%")

    print(f"\n按回撤排序(回撤最小):")
    by_dd = sorted(results, key=lambda x: x["max_drawdown_pct"], reverse=True)
    for i, r in enumerate(by_dd):
        print(f"  {i+1}. Pos={r['max_position_ratio']:.0%} Peak={r['peak_stop_multiple']:.0f}ATR "
              f"→ 回撤={r['max_drawdown_pct']:+.1f}% 收益={r['total_return_pct']:+.1f}% "
              f"夏普={r['sharpe']:.2f}")

    # 6. 保存JSON
    output = {
        "search_date": "2026-07-05",
        "fixed_dd": DD,
        "grid": {
            "max_position_ratio": POS_LIST,
            "peak_stop_multiple": PEAK_LIST,
        },
        "total_combos": len(results),
        "results": results,
        "best_by_return": max(results, key=lambda x: x["total_return_pct"]),
        "best_by_sharpe": max(results, key=lambda x: x["sharpe"]),
        "best_by_drawdown": max(results, key=lambda x: x["max_drawdown_pct"]),
    }
    output_file = "grid_search_dd25_targeted.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {output_file}")


if __name__ == "__main__":
    main()
