#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证脚本：用「我们」的引擎 + 「Marvis 的半导体参数」跑 9 半导体，
看收益是否逼近 Marvis 报的 324.05%。
若逼近 → 证明差距来自参数未对齐，而非策略组合差异。
"""
import os
os.environ.setdefault("REQUESTS_CA_BUNDLE", "/etc/ssl/certs/ca-certificates.crt")
os.environ.setdefault("PYTHONHTTPSVERIFY", "0")
import importlib.util

SPEC_PATH = "/workspace/aquant_v10_9symbols_improved.py"

# 用 importlib 加载（避免与文件名冲突）
spec = importlib.util.spec_from_file_location("aquant_ours", SPEC_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

BacktestEngine = mod.BacktestEngine

# 9 半导体标的（与历史一致）
SYMS = {
    "688249": "晶合集成",
    "688347": "华虹公司",
    "300666": "江丰电子",
    "600206": "有研新材",
    "688409": "富创精密",
    "688361": "中科飞测",
    "300604": "长川科技",
    "688120": "华海清科",
    "688082": "盛美上海",
}

# Marvis 的半导体参数（来自他文件 1081-1110 行）
marvis_semi = {
    "entry_period": 30,
    "exit_period": 18,
    "adx_threshold": 16,
    "adx_period": 20,
    "atr_period": 20,
    "atr_multiplier": 2.0,
    "trail_atr_mult": 8.0,
    "hard_stop": 0.25,
    "risk_pct": 0.025,
    "max_units": 3,
    "pyramid_add_atr": 2.5,
    "ma_long": 100,
    "channel_mult": 3.5,
    "strategy_weight": 0.75,
}

# 构造 per_symbol_config：9 只全用 Marvis 半导体参数
psc = {code: marvis_semi for code in SYMS}

engine = BacktestEngine(initial_capital=1_000_000)
res = engine.run(
    SYMS,
    start_date="2025-01-01",
    end_date="2026-06-30",
    per_symbol_config=psc,
    profile=None,
    config_route="none",  # 全部用我们 default 基线 + per_symbol_config 覆盖
)

print("\n" + "=" * 60)
print("【我们引擎 + Marvis 半导体参数】9 半导体回测结果")
print("=" * 60)
print(f"总收益率:   {res.get('total_return'):.2%}")
print(f"年化:       {res.get('annual_return'):.2%}")
print(f"最大回撤:   {res.get('max_drawdown'):.2%}")
print(f"夏普:       {res.get('sharpe'):.3f}")
print(f"胜率:       {res.get('win_rate'):.2%}")
print(f"交易数:     {res.get('num_trades')}")
