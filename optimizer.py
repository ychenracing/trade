#!/usr/bin/env python3
"""AQuant 参数优化器 — 网格搜索最优策略参数"""

import sys
sys.path.insert(0, "/tmp/.pip-global/lib/python3.12/site-packages")

import itertools
import time
from aquant import BacktestEngine, DataFetcher, Indicators, parse_symbols

SYMBOLS = "300308,300502,300394"
START = "2025-01-01"
END = "2026-06-30"
CAPITAL = 2_000_000

# 预取数据（避免每次重复下载）
print("预取数据...")
symbols_dict = parse_symbols(SYMBOLS)
data_cache = {}
ind_cache = {}
for code, name in symbols_dict.items():
    df = DataFetcher.fetch_stock_data(code, START, END)
    data_cache[code] = df
    print(f"  {name}({code}): {len(df)}条")

# 基准配置（从 _default_config 复制，只改搜索参数）
BASE_CFG = {
    "entry_period": 15,
    "exit_period": 5,
    "adx_threshold": 15,
    "adx_period": 14,
    "atr_period": 20,
    "rsi_period": 14,
    "ma_short": 20,
    "ma_long": 60,
    "atr_multiplier": 1.0,
    "trail_atr_mult": 1.5,
    "channel_mult": 2.0,
    "channel_lower_mult": 1.5,
    "risk_pct": 0.05,
    "hard_stop": 0.07,
    "strategy_weight": 0.80,
    "max_symbol_weight": 0.90,
    "max_total_weight": 0.98,
    "max_units": 10,
    "max_drawdown": 0.12,
    "cooldown_days": 5,
    "daily_loss_limit": 0.06,
    "momentum_lookback": 20,
    "max_positions": 2,
    "liquidate_on_circuit_breaker": True,
    "commission_rate": 0.00025,
    "stamp_duty": 0.0005,
    "slippage": 0.001,
}

def run_backtest(cfg_override: dict) -> dict:
    """用缓存的预取数据跑回测"""
    cfg = {**BASE_CFG, **cfg_override}
    engine = BacktestEngine(initial_capital=CAPITAL, cfg=cfg)
    
    # 注入缓存数据
    for code in symbols_dict:
        engine._cached_data = data_cache
        engine._cached_ind = ind_cache
    
    # Monkey-patch fetch_stock_data to use cache
    original_fetch = DataFetcher.fetch_stock_data
    DataFetcher.fetch_stock_data = staticmethod(lambda s, sd, ed: data_cache[s].copy())
    
    # Monkey-patch compute_all to use cache
    original_compute = Indicators.compute_all
    Indicators.compute_all = staticmethod(lambda df, c: ind_cache.get(id(df), original_compute(df, c)))
    
    # Pre-compute indicators for each stock with this cfg
    for code, df in data_cache.items():
        ind_cache[code] = Indicators.compute_all(df, cfg)
    
    try:
        result = engine.run(symbols_dict, START, END)
    finally:
        DataFetcher.fetch_stock_data = original_fetch
        Indicators.compute_all = original_compute
    
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

# ========== 搜索空间 ==========
# 第一轮：粗搜索关键参数
search_space = {
    "entry_period": [10, 15, 20],
    "exit_period": [5, 10],
    "trail_atr_mult": [1.5, 2.0, 2.5, 3.0],
    "atr_multiplier": [0.5, 1.0, 1.5, 2.0],
    "hard_stop": [0.05, 0.07, 0.10, 0.15],
    "max_drawdown": [0.15, 0.20, 0.25],
    "strategy_weight": [0.80, 0.90, 0.95],
    "max_symbol_weight": [0.80, 0.90, 0.95],
    "risk_pct": [0.03, 0.05, 0.08],
    "momentum_lookback": [10, 20, 30],
    "max_positions": [1, 2, 3],
    "liquidate_on_circuit_breaker": [True, False],
    "channel_mult": [1.5, 2.0, 2.5],
    "adx_threshold": [10, 15, 20],
    "ma_short": [10, 20],
    "ma_long": [40, 60],
}

# 分阶段搜索：先搜索最关键的参数
# 阶段1：风控参数（影响最大）
phase1 = {
    "max_drawdown": [0.15, 0.20, 0.25, 0.30],
    "liquidate_on_circuit_breaker": [True, False],
    "trail_atr_mult": [1.5, 2.0, 2.5, 3.0],
    "atr_multiplier": [0.5, 1.0, 1.5, 2.0],
    "hard_stop": [0.05, 0.07, 0.10, 0.15],
}

# 阶段2：仓位参数
phase2 = {
    "strategy_weight": [0.80, 0.90, 0.95],
    "max_symbol_weight": [0.80, 0.90, 0.95, 0.98],
    "risk_pct": [0.03, 0.05, 0.08, 0.10],
    "max_positions": [1, 2, 3],
}

# 阶段3：入场参数
phase3 = {
    "entry_period": [10, 15, 20],
    "exit_period": [5, 10],
    "adx_threshold": [10, 15, 20],
    "channel_mult": [1.5, 2.0, 2.5],
    "ma_short": [10, 20],
    "ma_long": [40, 60],
    "momentum_lookback": [10, 20, 30],
}

def grid_search(param_grid: dict, base_cfg_override: dict = None, tag: str = ""):
    """网格搜索"""
    keys = list(param_grid.keys())
    values = [param_grid[k] for k in keys]
    combos = list(itertools.product(*values))
    
    print(f"\n{'='*60}")
    print(f"网格搜索 [{tag}]: {len(combos)} 个组合")
    print(f"{'='*60}")
    
    best_score = -1
    best_params = None
    best_result = None
    results = []
    
    for idx, combo in enumerate(combos):
        cfg_override = dict(zip(keys, combo))
        if base_cfg_override:
            cfg_override = {**base_cfg_override, **cfg_override}
        
        t0 = time.time()
        result = run_backtest(cfg_override)
        elapsed = time.time() - t0
        
        ev = evaluate(result)
        results.append((cfg_override, ev))
        
        # 评分：收益为主，回撤为约束，夏普为加分
        if ev["max_dd"] > -0.25:  # 回撤约束
            score = ev["return"] * (1 + ev["sharpe"] * 0.1)
        else:
            score = -1
        
        if score > best_score:
            best_score = score
            best_params = cfg_override
            best_result = ev
            print(f"  [{idx+1}/{len(combos)}] 新最优! "
                  f"ret={ev['return']:.1%} dd={ev['max_dd']:.1%} "
                  f"sharpe={ev['sharpe']:.2f} win={ev['win_rate']:.0%} "
                  f"trades={ev['trades']} ({elapsed:.1f}s)")
        elif (idx + 1) % 10 == 0:
            print(f"  [{idx+1}/{len(combos)}] 进行中... "
                  f"当前最优: ret={best_result['return']:.1%} dd={best_result['max_dd']:.1%}")
    
    print(f"\n  最佳参数: {best_params}")
    print(f"  最佳结果: ret={best_result['return']:.1%} dd={best_result['max_dd']:.1%} "
          f"sharpe={best_result['sharpe']:.2f}")
    
    return best_params, best_result, results

# ========== 执行搜索 ==========
# 阶段1：风控参数
print("\n" + "="*60)
print("阶段1：风控参数搜索")
print("="*60)
p1_best, p1_ev, p1_results = grid_search(phase1, tag="风控")

# 阶段2：在阶段1最优基础上搜索仓位参数
print("\n" + "="*60)
print("阶段2：仓位参数搜索（基于阶段1最优）")
print("="*60)
p2_best, p2_ev, p2_results = grid_search(phase2, base_cfg_override=p1_best, tag="仓位")

# 阶段3：在阶段1+2最优基础上搜索入场参数
print("\n" + "="*60)
print("阶段3：入场参数搜索（基于阶段1+2最优）")
print("="*60)
p3_best, p3_ev, p3_results = grid_search(phase3, base_cfg_override={**p1_best, **p2_best}, tag="入场")

# 汇总
print("\n" + "="*60)
print("最终最优参数汇总")
print("="*60)
final_best = {**p1_best, **p2_best, **p3_best}
print(f"参数: {final_best}")
print(f"收益: {p3_ev['return']:.1%}")
print(f"回撤: {p3_ev['max_dd']:.1%}")
print(f"夏普: {p3_ev['sharpe']:.2f}")
print(f"胜率: {p3_ev['win_rate']:.0%}")
print(f"盈亏比: {p3_ev['profit_factor']:.2f}")
print(f"交易次数: {p3_ev['trades']}")
