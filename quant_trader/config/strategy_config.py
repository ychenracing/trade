"""
策略参数配置
"""

# ============ 策略1: EMA均线交叉 ============
EMA_CROSS_CONFIG = {
    "name": "EMA_Cross",
    "enabled": True,
    "weight": 1.0,                  # 策略权重（组合投票用）
    "params": {
        "ema_short": 10,            # 短期EMA
        "ema_long": 30,             # 长期EMA
        "ema_trend": 60,            # 趋势过滤EMA（价格在其上方才做多）
        "volume_ratio": 1.5,        # 放量确认：成交量 > 5日均量 × 1.5
    },
}

# ============ 策略2: 唐奇安通道突破 ============
DONCHIAN_CONFIG = {
    "name": "Donchian_Breakout",
    "enabled": True,
    "weight": 1.0,
    "params": {
        "entry_period": 20,         # 突破N日最高价买入
        "exit_period": 10,          # 跌破N日最低价卖出
        "atr_period": 20,           # ATR计算周期
        "atr_filter_multiple": 0.5, # 突破幅度需 > ATR × 0.5（过滤假突破）
    },
}

# ============ 策略3: MACD趋势确认 ============
MACD_TREND_CONFIG = {
    "name": "MACD_Trend",
    "enabled": True,
    "weight": 1.0,
    "params": {
        "fast": 12,
        "slow": 26,
        "signal": 9,
        "ma_filter": 60,            # 价格在MA60上方才允许做多
        "hist_threshold": 0,        # MACD柱状图阈值
    },
}

# ============ 组合策略配置 ============
COMBO_CONFIG = {
    "name": "Multi_Trend_Combo",
    # 投票模式：majority = 多数表决, weighted = 加权表决, unanimous = 全部同意
    "vote_mode": "majority",
    "min_buy_votes": 2,             # 至少2个策略看多才买入
    "min_sell_votes": 2,            # 至少2个策略看空才卖出
    # 策略列表（动态加载）
    "strategies": [
        EMA_CROSS_CONFIG,
        DONCHIAN_CONFIG,
        MACD_TREND_CONFIG,
    ],
}

# ============ 股票池筛选 ============
SCREENING_CONFIG = {
    # 趋势过滤：只选处于上升通道的股票
    "trend_filter_ma": 60,          # 价格在MA60上方
    "trend_filter_ma_short": 20,    # MA20 > MA60（均线多头排列）
    "momentum_filter_days": 20,     # 20日涨幅
    "momentum_min_gain": 0.05,      # 20日至少涨5%
    "max_pe": 100,                  # PE < 100（排除亏损股）
    "max_pb": 20,                   # PB < 20
}
