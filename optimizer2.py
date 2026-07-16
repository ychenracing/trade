#!/usr/bin/env python3
"""AQuant 精细参数微调 — 在网格搜索最优基础上突破800%"""

import sys
sys.path.insert(0, "/tmp/.pip-global/lib/python3.12/site-packages")

import itertools
import time
from aquant import BacktestEngine, DataFetcher, Indicators, parse_symbols

SYMBOLS = "300308,300502,300394"
START = "2025-01-01"
END = "2026-06-30"
CAPITAL = 2_000_000

print("预取数据...")
symbols_dict = parse_symbols(SYMBOLS)
data_cache = {}
for code, name in symbols_dict.items():
    df = DataFetcher.fetch_stock_data(code, START, END)
    data_cache[code] = df
    print(f"  {name}({code}): {len(df)}条")

# 网格搜索最优参数作为基础
BEST_CFG = {
    "entry_period": 10,
    "exit_period": 5,
    "adx_threshold": 10,
    "adx_period": 14,
    "atr_period": 20,
    "rsi_period": 14,
    "ma_short": 20,
    "ma_long": 40,
    "atr_multiplier": 1.0,
    "trail_atr_mult": 2.0,
    "channel_mult": 1.5,
    "channel_lower_mult": 1.5,
    "risk_pct": 0.05,
    "hard_stop": 0.05,
    "strategy_weight": 0.80,
    "max_symbol_weight": 0.90,
    "max_total_weight": 0.98,
    "max_units": 10,
    "max_drawdown": 0.15,
    "cooldown_days": 5,
    "daily_loss_limit": 0.06,
    "momentum_lookback": 10,
    "max_positions": 2,
    "liquidate_on_circuit_breaker": True,
    "commission_rate": 0.00025,
    "stamp_duty": 0.0005,
    "slippage": 0.001,
}

def run_backtest(cfg_override: dict) -> dict:
    cfg = {**BEST_CFG, **cfg_override}
    engine = BacktestEngine(initial_capital=CAPITAL, cfg=cfg)
    
    original_fetch = DataFetcher.fetch_stock_data
    DataFetcher.fetch_stock_data = staticmethod(lambda s, sd, ed: data_cache[s].copy())
    
    for code, df in data_cache.items():
        Indicators.compute_all(df, cfg)  # 重新计算指标
    
    try:
        result = engine.run(symbols_dict, START, END)
    finally:
        DataFetcher.fetch_stock_data = original_fetch
    
    return result

def evaluate(result: dict) -> dict:
    return {
        "return": result["total_return"],
        "max_dd": result["max_drawdown"],
        "sharpe": result["sharpe"],
        "win_rate": result["win_rate"],
        "profit_factor": result["profit_factor"],
        "trades": result["total_trades"],
    }

# 精细搜索空间：在最优参数附近微调
# 关键方向：放宽仓位限制 + 放宽风控 + 更短的入场周期 + 更高仓位
fine_search = {
    "entry_period": [5, 8, 10, 12],
    "exit_period": [3, 5, 8],
    "adx_threshold": [8, 10, 12, 15],
    "trail_atr_mult": [1.5, 2.0, 2.5, 3.0],
    "atr_multiplier": [0.5, 1.0, 1.5],
    "hard_stop": [0.05, 0.07, 0.10],
    "strategy_weight": [0.85, 0.90, 0.95],
    "max_symbol_weight": [0.90, 0.95, 0.98],
    "max_drawdown": [0.15, 0.18, 0.20],
    "risk_pct": [0.05, 0.08, 0.10],
    "momentum_lookback": [5, 10, 15],
    "max_positions": [1, 2, 3],
    "channel_mult": [1.0, 1.5, 2.0],
    "ma_long": [30, 40, 50],
    "cooldown_days": [3, 5, 7],
    "liquidate_on_circuit_breaker": [True, False],
}

# 太大的搜索空间，改为分步微调
# 阶段A：放宽仓位+风控（核心瓶颈）
phaseA = {
    "strategy_weight": [0.85, 0.90, 0.95, 0.98],
    "max_symbol_weight": [0.90, 0.95, 0.98],
    "max_drawdown": [0.15, 0.18, 0.20, 0.25],
    "risk_pct": [0.05, 0.08, 0.10, 0.12],
    "hard_stop": [0.05, 0.07, 0.10, 0.12],
}

# 阶段B：入场信号灵敏度
phaseB = {
    "entry_period": [5, 8, 10, 12],
    "exit_period": [3, 5, 8],
    "adx_threshold": [8, 10, 12, 15],
    "channel_mult": [1.0, 1.5, 2.0],
    "ma_long": [30, 40, 50],
}

# 阶段C：止损/加仓/动量
phaseC = {
    "trail_atr_mult": [1.5, 2.0, 2.5, 3.0, 3.5],
    "atr_multiplier": [0.5, 1.0, 1.5, 2.0],
    "momentum_lookback": [5, 10, 15, 20],
    "max_positions": [1, 2, 3],
    "max_units": [5, 8, 10, 15],
    "liquidate_on_circuit_breaker": [True, False],
    "cooldown_days": [3, 5, 7],
}

def grid_search(param_grid: dict, base_cfg_override: dict = None, tag: str = ""):
    keys = list(param_grid.keys())
    values = [param_grid[k] for k in keys]
    combos = list(itertools.product(*values))
    
    print(f"\n{'='*60}")
    print(f"网格搜索 [{tag}]: {len(combos)} 个组合")
    print(f"{'='*60}")
    
    best_score = -1
    best_params = None
    best_result = None
    top5 = []
    
    for idx, combo in enumerate(combos):
        cfg_override = dict(zip(keys, combo))
        if base_cfg_override:
            cfg_override = {**base_cfg_override, **cfg_override}
        
        result = run_backtest(cfg_override)
        ev = evaluate(result)
        
        # 评分：收益为主，回撤<=20%为约束
        if ev["max_dd"] >= -0.20:
            score = ev["return"]
        else:
            score = -1
        
        if score > 0:
            top5.append((score, cfg_override, ev))
        
        if score > best_score:
            best_score = score
            best_params = cfg_override
            best_result = ev
            star = "★" if ev["return"] >= 8.0 else ""
            print(f"  [{idx+1}/{len(combos)}] 新最优{star} "
                  f"ret={ev['return']:.1%} dd={ev['max_dd']:.1%} "
                  f"sharpe={ev['sharpe']:.2f} win={ev['win_rate']:.0%} "
                  f"trades={ev['trades']}")
        
        if (idx + 1) % 50 == 0:
            print(f"  [{idx+1}/{len(combos)}] 进行中... "
                  f"当前最优: ret={best_result['return']:.1%} dd={best_result['max_dd']:.1%}")
    
    # 打印 Top5
    top5.sort(key=lambda x: x[0], reverse=True)
    print(f"\n  Top 5 ({tag}):")
    for i, (score, params, ev) in enumerate(top5[:5]):
        print(f"    #{i+1}: ret={ev['return']:.1%} dd={ev['max_dd']:.1%} "
              f"sharpe={ev['sharpe']:.2f} | {params}")
    
    return best_params, best_result, top5

# ========== 执行 ==========
print("\n" + "="*60)
print("阶段A：仓位+风控参数微调")
print("="*60)
pA_best, pA_ev, pA_top5 = grid_search(phaseA, tag="仓位风控")

# 在最优基础上搜索入场参数
print("\n" + "="*60)
print("阶段B：入场信号参数微调")
print("="*60)
pB_best, pB_ev, pB_top5 = grid_search(phaseB, base_cfg_override=pA_best, tag="入场信号")

# 在最优基础上搜索止损/加仓/动量
print("\n" + "="*60)
print("阶段C：止损/加仓/动量参数微调")
print("="*60)
pC_best, pC_ev, pC_top5 = grid_search(phaseC, base_cfg_override={**pA_best, **pB_best}, tag="止损动量")

# 汇总
print("\n" + "="*60)
print("最终最优参数汇总")
print("="*60)
final_best = {**pA_best, **pB_best, **pC_best}
print(f"参数:")
for k, v in sorted(final_best.items()):
    print(f"  {k}: {v}")
print(f"\n收益: {pC_ev['return']:.1%}")
print(f"回撤: {pC_ev['max_dd']:.1%}")
print(f"夏普: {pC_ev['sharpe']:.2f}")
print(f"胜率: {pC_ev['win_rate']:.0%}")
print(f"盈亏比: {pC_ev['profit_factor']:.2f}")
print(f"交易次数: {pC_ev['trades']}")

# 检查是否达到目标
if pC_ev['return'] >= 8.0 and pC_ev['max_dd'] >= -0.20:
    print("\n✅ 目标达成！收益>=800% 且 回撤<=20%")
else:
    print(f"\n❌ 未达标: 收益={pC_ev['return']:.1%} (目标800%), 回撤={pC_ev['max_dd']:.1%} (目标-20%)")
    # 如果接近，尝试在全局Top5中找最优
    all_top5 = pA_top5 + pB_top5 + pC_top5
    all_top5.sort(key=lambda x: x[0], reverse=True)
    print("\n全局 Top 5:")
    for i, (score, params, ev) in enumerate(all_top5[:5]):
        star = "✅" if ev['return'] >= 8.0 and ev['max_dd'] >= -0.20 else ""
        print(f"  #{i+1} {star}: ret={ev['return']:.1%} dd={ev['max_dd']:.1%} "
              f"sharpe={ev['sharpe']:.2f} trades={ev['trades']}")
