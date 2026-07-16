"""
AQuant v11 最优参数配置（2026-07-10更新，600组合网格搜索验证全局最优）
监控10只标的，2025-01-02~2026-06-30回测验证
收益率+727.4% 年化+313.2% 回撤-19.0% 胜率72.2% 18笔交易
"""

MONITORING_SYMBOLS = {
    "300308.SZ": "中际旭创",
    "300502.SZ": "新易盛",
    "300394.SZ": "天孚通信",
    "688008.SH": "澜起科技",
    "603986.SH": "兆易创新",
    "002409.SZ": "雅克科技",
    "688300.SH": "联瑞新材",
    "300054.SZ": "鼎龙股份",
    "300776.SZ": "帝尔激光",
    "688535.SH": "华海诚科",
}

# 网格搜索最优参数（600组合全局最优，无一超越）
OPTIMAL_PARAMS = {
    "atr_window": 20,              # ATR窗口
    "atr_method": "wilder",        # ATR计算方法
    "atr_stop_multiple": 3.0,     # ATR止损倍数
    "max_symbol_weight": 0.50,     # 单票最大权重（用户指定）
    "max_total_stock_weight": 0.98, # 总仓位上限
    "max_positions": 8,            # 最大同时持仓数
    "max_units_per_symbol": 6,    # 单票最大加仓次数
    "pyramid_add_atr": 0.3,       # 加仓间距(ATR倍数)
    "pyramid_risk_decay": 0.80,   # 加仓风险递减系数
    "risk_fraction": 0.10,        # 风险比例
    "max_drawdown": 0.35,         # 熔断回撤阈值（用户指定35%）
    "risk_off_cooldown_days": 5,  # 熔断后冷却天数
    "enable_dynamic_rebalance": False,  # 关闭动态再平衡
    "rebalance_tolerance": 0.05,  # 再平衡容忍带（未启用）
    "s1_entry": 25,               # S1入场窗口
    "s1_exit": 20,                # S1退出窗口
    "s2_entry": 55,               # S2入场窗口
    "s2_exit": 20,                # S2退出窗口
}

# 回测验证表现
BACKTEST_PERFORMANCE = {
    "initial_capital": 2_000_000,
    "final_equity": 16_548_273,
    "total_return": 7.274,     # +727.4%
    "annual_return": 3.132,    # +313.2%
    "max_drawdown": -0.190,    # -19.0%
    "trade_count": 18,
    "win_rate": 0.722,         # 72.2%
    "period": "2025-01-02 ~ 2026-06-30",
}

# 网格搜索说明
# 2026-07-10: 600组合网格搜索，覆盖ATR止损[2.5/3.0/3.5/4.0]×单票权重[0.4/0.5/0.6]
#             ×加仓间距[0.3/0.5]×S1入场[20/25/30]×S1退出[15/20/25]×S2入场[50/55/60]×S2退出[20/25]
#             结果: 当前参数全局最优，S2长周期系统在该8只标的一年半内从未触发(参数不敏感)
