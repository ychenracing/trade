#!/usr/bin/env python3
"""
AQuant 网格搜索 v3 — 单进程串行
约束：max_symbol_weight=0.60, max_positions=5, max_drawdown=0.25
"""

import itertools
import sys
import time
import warnings
import io
import contextlib

warnings.filterwarnings("ignore")
sys.path.insert(0, ".")
from aquant import BacktestEngine

SYMBOLS = {"300308": "中际旭创", "300502": "新易盛", "300394": "天孚通信"}
START = "2025-01-01"
END = "2026-06-30"
CAPITAL = 2_000_000

FIXED = {
    "max_symbol_weight": 0.60,
    "max_positions": 5,
    "max_drawdown": 0.25,
    "liquidate_on_circuit_breaker": True,
    "commission_rate": 0.00025,
    "stamp_duty": 0.0005,
    "slippage": 0.001,
    "max_pending_buy_days": 3,
    "force_close_on_end": True,
    "cooldown_days": 5,
    "daily_loss_limit": 0.06,
    "channel_lower_mult": 1.5,
    "adx_period": 14,
    "atr_period": 20,
    "rsi_period": 14,
    "ma_short": 20,
    "max_total_weight": 0.98,
}

# 精选搜索空间：基于旧版最优参数和经验缩小范围
SEARCH = {
    "entry_period":     [5, 8],
    "exit_period":      [3, 5],
    "adx_threshold":    [5, 8],
    "ma_long":          [40, 50],
    "channel_mult":     [1.0, 1.5],
    "strategy_weight":  [0.95],
    "risk_pct":         [0.08, 0.10],
    "atr_multiplier":   [0.5, 1.0],
    "trail_atr_mult":  [2.0, 3.0],
    "hard_stop":        [0.07],
    "momentum_lookback": [5, 10],
    "max_units":        [5, 10],
}

def run_backtest(params):
    cfg = {**FIXED, **params}
    try:
        engine = BacktestEngine(initial_capital=CAPITAL, cfg=cfg)
        with contextlib.redirect_stdout(io.StringIO()):
            result = engine.run(SYMBOLS, START, END)
        if "error" in result:
            return None
        return {
            "total_return": result["total_return"],
            "max_drawdown": result["max_drawdown"],
            "sharpe": result["sharpe"],
            "win_rate": result["win_rate"],
            "profit_factor": result["profit_factor"],
            "total_trades": result["total_trades"],
            "params": params,
        }
    except Exception:
        return None

def main():
    keys = list(SEARCH.keys())
    values = list(SEARCH.values())
    combos = list(itertools.product(*values))
    total = len(combos)
    print(f"搜索空间：{total} 组合（单进程）")
    print(f"固定：max_symbol_weight=0.60, max_positions=5, max_drawdown=0.25\n")

    results = []
    count = 0
    start_time = time.time()

    for combo in combos:
        params = dict(zip(keys, combo))
        m = run_backtest(params)
        count += 1

        if m and m["max_drawdown"] > -0.25 and m["total_trades"] >= 8:
            results.append(m)

        if count % 50 == 0:
            elapsed = time.time() - start_time
            rate = count / elapsed if elapsed > 0 else 1
            remaining = (total - count) / rate
            print(f"  {count}/{total} ({count/total*100:.1f}%) | {elapsed:.0f}s | ~{remaining:.0f}s | 有效:{len(results)}", flush=True)

    results.sort(key=lambda x: x["total_return"], reverse=True)

    print(f"\n{'='*100}")
    print(f"搜索完成：{total}组合，有效结果{len(results)}个")
    print(f"耗时：{time.time()-start_time:.0f}s")
    print(f"{'='*100}\n")

    print(f"{'#':>3} {'收益率':>9} {'回撤':>8} {'夏普':>7} {'胜率':>7} {'盈亏比':>7} {'交易':>5}  参数")
    print(f"{'─'*100}")
    for i, m in enumerate(results[:20]):
        print(f"{i+1:>3} {m['total_return']:>8.2%} {m['max_drawdown']:>7.2%} {m['sharpe']:>7.2f} "
              f"{m['win_rate']:>6.0%} {m['profit_factor']:>7.2f} {m['total_trades']:>5}  {m['params']}")

    if results:
        print(f"\n收益率最优参数：")
        b = results[0]
        print(f"  收益率={b['total_return']:.2%} 回撤={b['max_drawdown']:.2%} 夏普={b['sharpe']:.2f} 胜率={b['win_rate']:.0%} 盈亏比={b['profit_factor']:.2f}")
        for k, v in b["params"].items():
            print(f"  {k}: {v}")

        by_sharpe = sorted(results, key=lambda x: x["sharpe"], reverse=True)
        s = by_sharpe[0]
        print(f"\n夏普最优参数：")
        print(f"  收益率={s['total_return']:.2%} 回撤={s['max_drawdown']:.2%} 夏普={s['sharpe']:.2f} 胜率={s['win_rate']:.0%} 盈亏比={s['profit_factor']:.2f}")
        for k, v in s["params"].items():
            print(f"  {k}: {v}")

if __name__ == "__main__":
    main()
