"""Frozen cross-market overlay thresholds and priority tables."""

CATASTROPHE_STOP_PCT = 0.28
CATASTROPHE_COOLDOWN_DAYS = 10
COST_ABS_STOP_PCT = 0.18
LAYERED_ATR_MULTIPLIER = 6.0
PROFIT_TIER_GIVEBACK = ((0.30, 0.18), (0.80, 0.22), (1.50, 0.26), (3.00, 0.28))
MIN_LAYERED_STOP_PCT = 0.14
LAYERED_ARM_PORTFOLIO_DRAWDOWN = 0.05
RISK_BASKET = (
    "300308",  # 中际旭创 - 光模块
    "300502",  # 新易盛 - 光模块
    "300394",  # 天孚通信 - 光模块
    "688008",  # 澜起科技 - 存储接口
    "603986",  # 兆易创新 - 存储/设计
    "002409",  # 雅克科技 - 半导体材料
    "688072",  # 拓荆科技 - 半导体设备
    "688256",  # 寒武纪 - 国产算力
    "300054",  # 鼎龙股份 - 材料
    "688082",  # 盛美上海 - 设备
    "688300",  # 联瑞新材 - 材料
    "688205",  # 德科立 - 光通信
    "920045",  # 蘅东光 - 光通信
    "300776",  # 帝尔激光 - 设备
    "688535",  # 华海诚科 - 封装材料
    "688249",  # 晶合集成 - 晶圆制造
    "688347",  # 华虹宏力 - 晶圆制造
    "300666",  # 江丰电子 - 半导体材料
    "600206",  # 有研新材 - 半导体材料
    "688409",  # 富创精密 - 设备零部件
    "688361",  # 中科飞测 - 量测设备
    "300604",  # 长川科技 - 测试设备
    "688120",  # 华海清科 - CMP 设备
)
RISK_SUB_BASKETS = {
    "optical": ("300308", "300502", "300394", "688205", "920045"),
    "memory": ("688008", "603986"),
    "compute": ("688256",),
    "equipment": (
        "688072", "688082", "300776", "688249", "688347",
        "688409", "688361", "300604", "688120",
    ),
    "material": (
        "002409", "300054", "688300", "688535", "300666", "600206",
    ),
}
SYMBOL_SUB_INDUSTRY: dict[str, str] = {
    "300308": "optical", "300502": "optical", "300394": "optical",
    "688205": "optical", "920045": "optical",
    "688008": "memory", "603986": "memory",
    "688256": "compute",
    "688072": "equipment", "688082": "equipment", "300776": "equipment",
    "688249": "equipment", "688347": "equipment", "688409": "equipment",
    "688361": "equipment", "300604": "equipment", "688120": "equipment",
    "002409": "material", "300054": "material", "688300": "material",
    "688535": "material", "300666": "material", "600206": "material",
}
RISK_SUB_FAST_RETURN_SHOCK = -0.06
RISK_SUB_BREADTH_SHOCK = 0.60
RISK_FAST_DAYS = 3
RISK_FAST_RETURN_SHOCK = -0.06     # 3-day equal-weight return shock
RISK_BREADTH_SHOCK = 0.70          # fraction of basket declining -> shock
RISK_BELOW_MA20_SHOCK = 0.60       # fraction below MA20 -> shock
RISK_MIN_OBSERVED = 4              # need at least this many observed names
RISK_HOLD_BREADTH_SHOCK = 0.80     # held-names decline breadth for shock
RISK_MIN_HELD = 2                  # need at least 2 held names to judge breadth
RISK_ESCALATION_DAYS = 5
RISK_CONTINUOUS_CONFIRM_DAYS = 3
RISK_SEVERE_DIRECT_RETURN = -0.10
RISK_SEVERE_DIRECT_BREADTH = 0.80
RISK_LEVEL2_DRAWDOWN = 0.08        # portfolio must be >=8% off peak to trim
RISK_LEVEL3_DRAWDOWN = 0.12        # portfolio must be >=12% off peak to de-risk
RISK_TRIM_FAST_DAYS = 3
RISK_TRIM_REQUIRE_DECLINE = True
RISK_LEVEL2_TRIM_RATIO = 0.30      # trim weakest at Level 2 by this fraction
RISK_LEVEL3_TRIM_RATIO = 0.50      # de-risk weakest at Level 3 by this fraction
CONCENTRATION_CAP = 0.80          # a single sub-industry may not exceed 80%
CONCENTRATION_DRAWDOWN = 0.08     # portfolio must be >=8% off peak to trim
CONCENTRATION_MIN_CLUSTER = 2     # ignore single-name clusters (<=1 symbol)
CONCENTRATION_UNMAPPED_LIMIT = 0.05
CONCENTRATION_MAX_TRIM_RATIO = 0.25
SHOCK_FAST_DAYS = 3
SHOCK_FAST_RETURN = -0.06
SHOCK_BREADTH_THRESHOLD = 0.70
SHOCK_VOL_SURGE = 2.0
SHOCK_MIN_HELD = 2
SHOCK_TRIM_DRAWDOWN = 0.08
SHOCK_TRIM_RATIO = 0.30
RISK_ACTION_PRIORITY: dict[str, int] = {
    "catastrophe_stop": 100,      # full exit, always armed
    "cost_stop": 90,              # full exit, layered cost line
    "atr_stop": 90,               # full exit, layered ATR chandelier
    "profit_tier_stop": 90,       # full exit, layered profit-protection line
    "layered_stop": 90,           # generic full-exit tag
    "sector_risk_trim": 60,       # graded early-sector-risk trim (L2/L3)
    "drawdown_budget_reduction": 70,  # account-wide ex-ante risk budget
    "concentration_trim": 50,     # sub-industry concentration guard trim
    "shock_trim": 40,             # structural-shock fast de-risk (opt-in)
}
RISK_ACTION_DEFAULT_PRIORITY = 10
