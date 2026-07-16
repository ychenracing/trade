#!/usr/bin/env python3
"""
AQuant 网格搜索 v4 — 断点续跑版
支持增量保存结果，进程中断后重启可从上次断点继续。
"""

import sys
import os

_AKSHARE_PATH = "/tmp/.pip-global/lib/python3.12/site-packages"
if _AKSHARE_PATH not in sys.path:
    sys.path.insert(0, _AKSHARE_PATH)

import itertools
import time
import json
import warnings
import io
import contextlib

warnings.filterwarnings("ignore")
sys.path.insert(0, ".")

try:
    import akshare as _ak_test
    print(f"akshare {_ak_test.__version__} OK")
except ImportError:
    print("installing akshare...")
    os.system("pip install akshare -q")
    import akshare as _ak_test
    print(f"akshare {_ak_test.__version__} OK (after install)")

SYMBOLS = {
    "300308": "中际旭创", "300502": "新易盛", "300394": "天孚通信",
    "688008": "澜起科技", "603986": "兆易创新", "002409": "雅克科技",
}
START = "2025-01-01"
END = "2026-06-30"
CAPITAL = 2_000_000

FIXED = {
    "max_symbol_weight": 0.60,
    "max_positions": 6,
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
    "ma_long": 50,
    "channel_mult": 1.5,
    "strategy_weight": 0.95,
    "hard_stop": 0.07,
}

SEARCH = {
    "entry_period":       [8, 10, 12],
    "exit_period":        [8, 10, 12],
    "adx_threshold":      [5, 8, 10],
    "risk_pct":           [0.08, 0.10, 0.12],
    "atr_multiplier":     [0.3, 0.5, 1.0],
    "trail_atr_mult":     [4.0, 5.0],
    "momentum_lookback":  [5, 10],
    "max_units":          [2, 3],
}

RESULTS_FILE = "optimizer4_results.json"
PROGRESS_FILE = "optimizer4_progress.txt"

def run_backtest(params):
    cfg = {**FIXED, **params}
    try:
        from aquant import BacktestEngine
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

def load_results():
    """加载已有的结果"""
    done_keys = set()
    results = []
    if os.path.exists(RESULTS_FILE):
        try:
            with open(RESULTS_FILE, "r") as f:
                data = json.load(f)
            results = data.get("results", [])
            done_keys = set(data.get("done_keys", []))
            print(f"加载已有结果：{len(results)}条，已完成{len(done_keys)}个组合")
        except Exception:
            pass
    return results, done_keys

def save_results(results, done_keys):
    """增量保存结果"""
    with open(RESULTS_FILE, "w") as f:
        json.dump({"results": results, "done_keys": list(done_keys)}, f)
    with open(PROGRESS_FILE, "a") as f:
        f.write(f"  saved: {len(results)} results, {len(done_keys)} done\n")

def main():
    keys = list(SEARCH.keys())
    values = list(SEARCH.values())
    combos = list(itertools.product(*values))
    total = len(combos)

    results, done_keys = load_results()
    print(f"6标的网格搜索：{total} 组合（断点续跑版）\n")

    count = len(done_keys)
    start_time = time.time()
    last_save = 0

    for i, combo in enumerate(combos):
        combo_key = str(combo)
        if combo_key in done_keys:
            continue

        params = dict(zip(keys, combo))
        m = run_backtest(params)
        done_keys.add(combo_key)
        count += 1

        if m and m["max_drawdown"] > -0.25 and m["total_trades"] >= 8:
            results.append(m)

        # 每50个组合打印进度
        if count % 50 == 0:
            elapsed = time.time() - start_time
            processed_this_run = count - (len(done_keys) - count)
            rate = max(count - (len(done_keys) - count), 1) / max(elapsed, 1)
            remaining = (total - count) / max(rate, 0.01)
            msg = f"  {count}/{total} ({count/total*100:.1f}%) | {elapsed:.0f}s | ~{remaining:.0f}s | 有效:{len(results)}"
            print(msg, flush=True)
            with open(PROGRESS_FILE, "a") as f:
                f.write(msg + "\n")

        # 每100个组合保存一次
        if count - last_save >= 100:
            save_results(results, done_keys)
            last_save = count

    # 最终保存
    save_results(results, done_keys)

    # 排序输出 Top20
    results.sort(key=lambda x: x["total_return"], reverse=True)

    print(f"\n{'='*100}")
    print(f"搜索完成：{total}组合，有效结果{len(results)}个")
    print(f"{'='*100}\n")

    print(f"{'#':>3} {'收益率':>9} {'回撤':>8} {'夏普':>7} {'胜率':>7} {'盈亏比':>7} {'交易':>5}  参数")
    print(f"{'─'*100}")
    for i, m in enumerate(results[:20]):
        print(f"{i+1:>3} {m['total_return']:>8.2%} {m['max_drawdown']:>7.2%} {m['sharpe']:>7.2f} "
              f"{m['win_rate']:>6.0%} {m['profit_factor']:>7.2f} {m['total_trades']:>5}  {m['params']}")

    if results:
        b = results[0]
        print(f"\n收益率最优：{b['total_return']:.2%} 回撤={b['max_drawdown']:.2%} 夏普={b['sharpe']:.2f} 胜率={b['win_rate']:.0%} 盈亏比={b['profit_factor']:.2f} 交易={b['total_trades']}")
        for k, v in b["params"].items():
            print(f"  {k}: {v}")

        by_sharpe = sorted(results, key=lambda x: x["sharpe"], reverse=True)
        s = by_sharpe[0]
        print(f"\n夏普最优：{s['total_return']:.2%} 回撤={s['max_drawdown']:.2%} 夏普={s['sharpe']:.2f} 胜率={s['win_rate']:.0%} 盈亏比={s['profit_factor']:.2f} 交易={s['total_trades']}")
        for k, v in s["params"].items():
            print(f"  {k}: {v}")

if __name__ == "__main__":
    main()
