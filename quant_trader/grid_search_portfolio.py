#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
组合回测网格搜索 — 搜索最优参数组合

搜索维度:
  1. max_total_drawdown (熔断阈值): 20%, 25%, 30%, 35%, 40%
  2. max_position_ratio (单股仓位): 25%, 30%, 35%, 40%, 50%
  3. peak_stop_multiple (峰值止损ATR倍数): 6, 8, 10, 12

数据只拉一次，所有参数组合共用。输出JSON + 热力图。
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
from utils.helpers import format_code
from utils.logger import log, setup_logger
from engine.portfolio_engine import run_portfolio_trading_day
from backtest_portfolio import fetch_stock_data, STOCK_POOL

setup_logger()
log.remove()
log.add(sys.stderr, level="ERROR")  # 网格搜索时只输出ERROR


# ─── 搜索参数空间 ───
PARAM_GRID = {
    "max_total_drawdown": [0.20, 0.25, 0.30, 0.35, 0.40],
    "max_position_ratio": [0.25, 0.30, 0.35, 0.40, 0.50],
    "peak_stop_multiple": [6.0, 8.0, 10.0, 12.0],
}
# 总组合数: 5 × 5 × 4 = 100

# ─── 固定参数 ───
INITIAL_CAPITAL = 2_000_000
START_DATE = "2024-07-01"
END_DATE = "2026-07-04"
WARMUP = 70


def _load_all_data():
    """一次性拉取所有股票数据，后续复用"""
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
    print(f"成功获取: {len(stocks_data)}/{len(STOCK_POOL)}只\n")
    return stocks_data, stock_names


def _build_date_index(stocks_data):
    """构建日期索引（每只股票的 date→row_idx 映射）"""
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
    return all_dates, stock_date_map


def _run_single_backtest(stocks_data, stock_names, all_dates, stock_date_map,
                         max_drawdown, max_position, peak_multiple):
    """用指定参数跑一次组合回测，返回统计指标"""

    # 深拷贝RISK_CONFIG，修改搜索维度
    from config.settings import RISK_CONFIG
    risk_config = RISK_CONFIG.copy()
    risk_config["max_total_drawdown"] = max_drawdown
    risk_config["max_position_ratio"] = max_position
    risk_config["peak_stop_multiple"] = peak_multiple
    # 深拷贝profit_protection_tiers（避免修改全局）
    risk_config["profit_protection_tiers"] = [t.copy() for t in RISK_CONFIG["profit_protection_tiers"]]

    # 初始化组件
    simulator = SimulatorExecutor(INITIAL_CAPITAL)
    risk_mgr = RiskManager(INITIAL_CAPITAL)
    risk_mgr.config = risk_config  # 覆盖配置
    position_sizer = PositionSizer(INITIAL_CAPITAL)
    position_sizer.risk = risk_config  # 覆盖配置
    position_sizer.total_capital = INITIAL_CAPITAL
    strategies = {symbol: ComboStrategy() for symbol in stocks_data}

    trade_log = []
    daily_nav = []
    signal_log = []
    circuit_breaker_count = 0

    # 逐日回测
    for date in all_dates:
        active_stocks = {}
        for symbol, date_map in stock_date_map.items():
            if date in date_map:
                idx = date_map[date]
                if idx >= WARMUP:
                    active_stocks[symbol] = idx

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
            signal_log=signal_log,
            daily_nav=daily_nav,
            verbose=False,
        )

        if not ok:
            circuit_breaker_count += 1

    # 计算统计
    if not daily_nav:
        return {"total_return": 0, "max_drawdown": 0, "sharpe": 0,
                "trades": 0, "circuit_breakers": 0, "win_rate": 0}

    final_nav = daily_nav[-1]["nav"]
    total_return = (final_nav - INITIAL_CAPITAL) / INITIAL_CAPITAL

    # 最大回撤
    navs = [d["nav"] for d in daily_nav]
    peak = navs[0]
    max_dd = 0
    for nav in navs:
        if nav > peak:
            peak = nav
        dd = (peak - nav) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    # 夏普比率（日收益年化）
    nav_series = pd.Series(navs)
    daily_returns = nav_series.pct_change().dropna()
    if len(daily_returns) > 1 and daily_returns.std() > 0:
        sharpe = daily_returns.mean() / daily_returns.std() * np.sqrt(252)
    else:
        sharpe = 0

    # 胜率
    sell_trades = [t for t in trade_log if "SELL" in t.get("action", "")]
    wins = sum(1 for t in sell_trades if t.get("pnl", 0) > 0)
    win_rate = wins / len(sell_trades) if sell_trades else 0

    return {
        "total_return": round(total_return * 100, 2),
        "max_drawdown": round(max_dd * 100, 2),
        "sharpe": round(sharpe, 2),
        "trades": len(trade_log),
        "circuit_breakers": circuit_breaker_count,
        "win_rate": round(win_rate * 100, 1) if sell_trades else 0,
        "final_nav": round(final_nav, 0),
    }


def run_grid_search():
    """主函数：执行网格搜索"""

    print("=" * 70)
    print("  组合回测网格搜索")
    print(f"  回测区间: {START_DATE} ~ {END_DATE}")
    print(f"  初始资金: {INITIAL_CAPITAL:,.0f}")
    print(f"  股票数量: {len(STOCK_POOL)}")
    print(f"  参数空间: {len(PARAM_GRID['max_total_drawdown'])} × "
          f"{len(PARAM_GRID['max_position_ratio'])} × "
          f"{len(PARAM_GRID['peak_stop_multiple'])} = "
          f"{np.prod([len(v) for v in PARAM_GRID.values()])} 组合")
    print("=" * 70)

    # 1. 加载数据（只拉一次）
    stocks_data, stock_names = _load_all_data()
    if not stocks_data:
        print("无可用数据")
        return

    all_dates, stock_date_map = _build_date_index(stocks_data)
    print(f"交易日总数: {len(all_dates)}\n")

    # 2. 生成所有参数组合
    keys = list(PARAM_GRID.keys())
    combos = list(itertools.product(*[PARAM_GRID[k] for k in keys]))
    total = len(combos)

    print(f"开始网格搜索 ({total} 组合)...\n")

    # 3. 逐个跑回测
    results = []
    best_return = -999
    best_combo = None
    start_time = time.time()

    for i, combo in enumerate(combos):
        params = dict(zip(keys, combo))
        t0 = time.time()
        stats = _run_single_backtest(
            stocks_data, stock_names, all_dates, stock_date_map,
            max_drawdown=params["max_total_drawdown"],
            max_position=params["max_position_ratio"],
            peak_multiple=params["peak_stop_multiple"],
        )
        elapsed = time.time() - t0

        result = {
            "max_drawdown_pct": params["max_total_drawdown"],
            "max_position_pct": params["max_position_ratio"],
            "peak_stop_multiple": params["peak_stop_multiple"],
            **stats,
        }
        results.append(result)

        # 进度
        ret = stats["total_return"]
        flag = " 🏆" if ret > best_return else ""
        if ret > best_return:
            best_return = ret
            best_combo = result

        eta = (time.time() - start_time) / (i + 1) * (total - i - 1)
        print(f"  [{i+1:>3}/{total}] DD={params['max_total_drawdown']:.0%} "
              f"Pos={params['max_position_ratio']:.0%} "
              f"Peak={params['peak_stop_multiple']:.0f}ATR → "
              f"收益={ret:>+7.1f}% 回撤={stats['max_drawdown']:>5.1f}% "
              f"夏普={stats['sharpe']:>4.1f} "
              f"熔断={stats['circuit_breakers']} "
              f"({elapsed:.1f}s, ETA {eta:.0f}s){flag}")

    total_time = time.time() - start_time
    print(f"\n总耗时: {total_time:.0f}秒 ({total_time/total:.1f}s/组合)")

    # 4. 排序
    results.sort(key=lambda x: x["total_return"], reverse=True)

    # 5. 打印Top 10
    print(f"\n{'='*90}")
    print(f"  Top 10 参数组合（按总收益排序）")
    print(f"{'='*90}")
    print(f"  {'排名':>3} {'熔断阈值':>6} {'仓位':>5} {'峰值ATR':>7} "
          f"{'收益%':>8} {'回撤%':>6} {'夏普':>5} {'交易':>4} {'熔断':>4} {'胜率%':>5}")
    print(f"  {'-'*80}")
    for rank, r in enumerate(results[:10], 1):
        print(f"  {rank:>3} "
              f"{r['max_drawdown_pct']:>6.0%} "
              f"{r['max_position_pct']:>5.0%} "
              f"{r['peak_stop_multiple']:>7.0f} "
              f"{r['total_return']:>+8.1f} "
              f"{r['max_drawdown']:>6.1f} "
              f"{r['sharpe']:>5.1f} "
              f"{r['trades']:>4} "
              f"{r['circuit_breakers']:>4} "
              f"{r['win_rate']:>5.1f}")

    # 6. 按夏普排序Top 10
    results_by_sharpe = sorted(results, key=lambda x: x["sharpe"], reverse=True)
    print(f"\n{'='*90}")
    print(f"  Top 10 参数组合（按夏普比率排序）")
    print(f"{'='*90}")
    print(f"  {'排名':>3} {'熔断阈值':>6} {'仓位':>5} {'峰值ATR':>7} "
          f"{'收益%':>8} {'回撤%':>6} {'夏普':>5} {'交易':>4} {'熔断':>4} {'胜率%':>5}")
    print(f"  {'-'*80}")
    for rank, r in enumerate(results_by_sharpe[:10], 1):
        print(f"  {rank:>3} "
              f"{r['max_drawdown_pct']:>6.0%} "
              f"{r['max_position_pct']:>5.0%} "
              f"{r['peak_stop_multiple']:>7.0f} "
              f"{r['total_return']:>+8.1f} "
              f"{r['max_drawdown']:>6.1f} "
              f"{r['sharpe']:>5.1f} "
              f"{r['trades']:>4} "
              f"{r['circuit_breakers']:>4} "
              f"{r['win_rate']:>5.1f}")

    # 7. 保存JSON
    output_file = "grid_search_portfolio_results.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump({
            "params": {
                "start_date": START_DATE,
                "end_date": END_DATE,
                "initial_capital": INITIAL_CAPITAL,
                "stock_count": len(stocks_data),
                "warmup": WARMUP,
                "grid": PARAM_GRID,
            },
            "best_by_return": results[0],
            "best_by_sharpe": results_by_sharpe[0],
            "all_results": results,
            "total_combos": total,
            "total_time_seconds": round(total_time, 1),
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n结果已保存: {output_file}")

    # 8. 生成热力图
    _plot_heatmaps(results)

    return results


def _plot_heatmaps(results):
    """生成热力图：收益、夏普、回撤"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    df = pd.DataFrame(results)

    # 固定peak_stop_multiple=最优值，画 熔断×仓位 热力图
    best_peak = df.loc[df["total_return"].idxmax(), "peak_stop_multiple"]

    fig, axes = plt.subplots(2, 2, figsize=(16, 12))

    for ax_idx, (metric, title, fmt) in enumerate([
        ("total_return", "总收益率 (%)", "{:.0f}"),
        ("sharpe", "夏普比率", "{:.1f}"),
        ("max_drawdown", "最大回撤 (%)", "{:.0f}"),
        ("circuit_breakers", "熔断次数", "{:.0f}"),
    ]):
        ax = axes[ax_idx // 2][ax_idx % 2]

        # 筛选最优peak_stop_multiple的数据
        sub = df[df["peak_stop_multiple"] == best_peak]
        pivot = sub.pivot_table(
            index="max_drawdown_pct",
            columns="max_position_pct",
            values=metric,
            aggfunc="first",
        )

        # 画热力图
        im = ax.imshow(pivot.values, cmap="RdYlGn" if metric != "max_drawdown" else "RdYlGn_r",
                       aspect="auto")
        ax.set_title(f"{title} (峰值止损={best_peak:.0f}×ATR)", fontsize=12, fontweight="bold")
        ax.set_xlabel("单股仓位比例")
        ax.set_ylabel("熔断阈值")

        # 设置刻度
        ax.set_xticks(range(len(pivot.columns)))
        ax.set_xticklabels([f"{c:.0%}" for c in pivot.columns])
        ax.set_yticks(range(len(pivot.index)))
        ax.set_yticklabels([f"{c:.0%}" for c in pivot.index])

        # 标注数值
        for i in range(len(pivot.index)):
            for j in range(len(pivot.columns)):
                val = pivot.values[i, j]
                if not np.isnan(val):
                    color = "white" if abs(val - np.nanmin(pivot.values)) / (np.nanmax(pivot.values) - np.nanmin(pivot.values) + 0.01) > 0.6 else "black"
                    ax.text(j, i, fmt.format(val), ha="center", va="center",
                            fontsize=10, color=color, fontweight="bold")

        plt.colorbar(im, ax=ax, shrink=0.8)

    plt.suptitle("组合回测网格搜索 — 参数热力图\n"
                 f"回测区间: {START_DATE} ~ {END_DATE} | "
                 f"初始资金: {INITIAL_CAPITAL:,.0f} | "
                 f"股票数: {len(STOCK_POOL)}",
                 fontsize=14, fontweight="bold", y=1.02)
    plt.tight_layout()

    output_file = "grid_search_portfolio_heatmap.png"
    plt.savefig(output_file, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"热力图已保存: {output_file}")


if __name__ == "__main__":
    run_grid_search()
