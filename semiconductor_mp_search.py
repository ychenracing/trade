#!/usr/bin/env python3
"""半导体池 max_positions 网格搜索 — 测试1~9哪个最优"""

import sys
sys.path.insert(0, "/tmp/.pip-global/lib/python3.12/site-packages")

from aquant import BacktestEngine, parse_symbols

POOL2_SYMBOLS = "688231,688347,300666,600206,688408,688361,300604,688120,688082,688981"
BASE_OVERRIDES = {
    "trail_atr_mult": 5.0,
    "max_units": 1,
}

symbols = parse_symbols(POOL2_SYMBOLS)
results = []

for mp in range(1, 10):
    cfg = {**BacktestEngine._default_config(), **BASE_OVERRIDES, "max_positions": mp}
    engine = BacktestEngine(initial_capital=2_000_000, cfg=cfg)
    result = engine.run(symbols, "2025-04-01", "2026-06-30")
    
    row = {
        "max_positions": mp,
        "return": result["total_return"],
        "drawdown": result["max_drawdown"],
        "sharpe": result["sharpe"],
        "win_rate": result["win_rate"],
        "trades": result["total_trades"],
    }
    results.append(row)
    print(f"  max_positions={mp}: 收益={row['return']:.2%} 回撤={row['drawdown']:.2%} "
          f"夏普={row['sharpe']:.2f} 胜率={row['win_rate']:.0%} 交易={row['trades']}笔")

# 找最优
print("\n" + "=" * 70)
best_ret = max(results, key=lambda x: x["return"])
best_sharpe = max(results, key=lambda x: x["sharpe"])
print(f"最高收益: max_positions={best_ret['max_positions']} → {best_ret['return']:.2%} / 夏普{best_ret['sharpe']:.2f}")
print(f"最高夏普: max_positions={best_sharpe['max_positions']} → {best_sharpe['return']:.2%} / 夏普{best_sharpe['sharpe']:.2f}")
print("=" * 70)
