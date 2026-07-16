#!/usr/bin/env python3
"""双池回测验证 — 确认代码修改后两套参数分别跑出与历史一致的结果"""

import sys
sys.path.insert(0, "/tmp/.pip-global/lib/python3.12/site-packages")

from aquant import BacktestEngine, parse_symbols, PerformanceReport

# ═══════════════════════════════════════════════════════════════════════
# 池1：通信/光模块 6标的回测池（与历史基准对比）
# 基准：1173.41% / -22.56% / 夏普3.50 / 123笔
# 参数：trail=3.0, mu=2, ep=8, xp=10, adx=3, mo=5, mp=4
# ═══════════════════════════════════════════════════════════════════════

POOL1_SYMBOLS = "300308,300502,300394,688008,603986,002409"
POOL1_OVERRIDES = {
    "trail_atr_mult": 3.0,
    "max_units": 2,
}

print("=" * 70)
print("池1回测：通信/光模块 6标的 (trail=3.0 / mu=2)")
print("基准：1173.41% / -22.56% / 夏普3.50 / 123笔")
print("=" * 70)

symbols_1 = parse_symbols(POOL1_SYMBOLS)
engine_1 = BacktestEngine(initial_capital=2_000_000, cfg={
    **BacktestEngine._default_config(),
    **POOL1_OVERRIDES,
})
result_1 = engine_1.run(symbols_1, "2025-01-01", "2026-06-30")
PerformanceReport.print_report(result_1, symbols_1)

# ═══════════════════════════════════════════════════════════════════════
# 池2：半导体设备/材料 10标的（与历史基准对比）
# 基准（9标的剔除中芯）：254.73% / -22.55% / 夏普2.31 / 106笔
# 基准（trail=5.0/mu=1优化后）：375% / -18.93% / 夏普2.80 / 72笔
# 参数：trail=5.0, mu=1, ep=8, xp=10, adx=3, mo=5, mp=4
# ═══════════════════════════════════════════════════════════════════════

POOL2_SYMBOLS = "688249,688347,300666,600206,688409,688361,300604,688120,688082,688981"
POOL2_OVERRIDES = {
    "trail_atr_mult": 5.0,
    "max_units": 1,
    "max_positions": 3,
}

print("\n" + "=" * 70)
print("池2回测：半导体设备/材料 10标的 (trail=5.0 / mu=1)")
print("基准：375% / -18.93% / 夏普2.80 / 72笔")
print("=" * 70)

symbols_2 = parse_symbols(POOL2_SYMBOLS)
engine_2 = BacktestEngine(initial_capital=2_000_000, cfg={
    **BacktestEngine._default_config(),
    **POOL2_OVERRIDES,
})
result_2 = engine_2.run(symbols_2, "2025-04-01", "2026-06-30")
PerformanceReport.print_report(result_2, symbols_2)

# ═══════════════════════════════════════════════════════════════════════
# 汇总对比
# ═══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("双池回测汇总对比")
print("=" * 70)
print(f"{'池':<12} {'收益':>12} {'最大回撤':>12} {'夏普':>8} {'胜率':>8} {'交易笔数':>8}")
print("-" * 70)
print(f"{'池1(基准)':<12} {'1173.41%':>12} {'-22.56%':>12} {'3.50':>8} {'33%':>8} {'123':>8}")
print(f"{'池1(本次)':<12} {result_1['total_return']:>11.2%} {result_1['max_drawdown']:>11.2%} "
      f"{result_1['sharpe_ratio']:>8.2f} {result_1['win_rate']:>7.0%} {result_1['total_trades']:>8}")
print(f"{'池2(基准)':<12} {'375.00%':>12} {'-18.93%':>12} {'2.80':>8} {'':>8} {'72':>8}")
print(f"{'池2(本次)':<12} {result_2['total_return']:>11.2%} {result_2['max_drawdown']:>11.2%} "
      f"{result_2['sharpe_ratio']:>8.2f} {result_2['win_rate']:>7.0%} {result_2['total_trades']:>8}")
print("=" * 70)
