#!/usr/bin/env python3
"""新标的分类器 — 用两套参数分别回测，自动判断归属哪个池"""

import sys
sys.path.insert(0, "/tmp/.pip-global/lib/python3.12/site-packages")

from aquant import BacktestEngine, parse_symbols

# 光通信池参数
POOL1_OVERRIDES = {
    "trail_atr_mult": 3.0,
    "max_units": 2,
    "max_positions": 4,
}

# 半导体池参数
POOL2_OVERRIDES = {
    "trail_atr_mult": 5.0,
    "max_units": 1,
    "max_positions": 3,
}

# 待分类标的
TEST_SYMBOLS = "002371,688012"

# 回测区间
START = "2025-04-01"
END = "2026-06-30"

symbols = parse_symbols(TEST_SYMBOLS)
base_cfg = BacktestEngine._default_config()

print("=" * 70)
print("新标的自动分类器 — 双参数回测对比")
print(f"标的: {symbols}")
print(f"区间: {START} ~ {END}")
print("=" * 70)

for code, name in symbols.items():
    print(f"\n{'━' * 70}")
    print(f"{name}({code})")
    print(f"{'━' * 70}")

    sym_dict = {code: name}
    results = {}

    for pool_name, overrides in [("光通信池", POOL1_OVERRIDES), ("半导体池", POOL2_OVERRIDES)]:
        cfg = {**base_cfg, **overrides}
        engine = BacktestEngine(initial_capital=2_000_000, cfg=cfg)
        result = engine.run(sym_dict, START, END)

        ret = result["total_return"]
        dd = result["max_drawdown"]
        sharpe = result["sharpe"]
        trades = result["total_trades"]
        win_rate = result["win_rate"]

        results[pool_name] = {"ret": ret, "dd": dd, "sharpe": sharpe, "trades": trades}
        print(f"\n  {pool_name} (trail={overrides['trail_atr_mult']} mu={overrides['max_units']} mp={overrides['max_positions']})")
        print(f"    收益={ret:.2%} 回撤={dd:.2%} 夏普={sharpe:.2f} 胜率={win_rate:.0%} 交易={trades}笔")

    # 判断
    ret1 = results["光通信池"]["ret"]
    ret2 = results["半导体池"]["ret"]
    sharpe1 = results["光通信池"]["sharpe"]
    sharpe2 = results["半导体池"]["sharpe"]

    print(f"\n  对比:")
    print(f"    收益: 光通信={ret1:.2%} vs 半导体={ret2:.2%} → {'光通信' if ret1 > ret2 else '半导体'}更优")
    print(f"    夏普: 光通信={sharpe1:.2f} vs 半导体={sharpe2:.2f} → {'光通信' if sharpe1 > sharpe2 else '半导体'}更优")

    if ret1 > ret2 and sharpe1 >= sharpe2:
        verdict = "光通信池"
    elif ret2 > ret1 and sharpe2 >= sharpe1:
        verdict = "半导体池"
    elif ret1 > ret2:
        verdict = "光通信池（收益更高，但夏普接近）"
    else:
        verdict = "半导体池（收益更高，但夏普接近）"

    print(f"\n  >>> 归类结论: {name}({code}) → {verdict}")

print(f"\n{'=' * 70}")
