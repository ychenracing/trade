"""
全局配置 - 资金、风控、运行模式
"""
import os
from pathlib import Path

# ============ 基础路径 ============
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data_cache"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = BASE_DIR / "quant_trader.db"

for d in [DATA_DIR, LOG_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ============ 运行模式 ============
# "backtest" = 回测模式
# "paper"    = 模拟盘
# "live"     = 实盘（需配置券商接口）
RUN_MODE = os.getenv("QUANT_MODE", "paper")

# ============ 资金配置 ============
INITIAL_CAPITAL = 2_000_000.0   # 初始资金 200万

# ============ 风控参数 ============
RISK_CONFIG = {
    "max_total_drawdown": 0.20,     # 最大总回撤 20%（2026-07-05组合网格搜索：20%熔断+35%仓位=回撤-24.3%为回撤最优方案，4次熔断均恢复）
    "max_position_loss": 0.08,      # 单笔最大亏损 8%（保底，ATR止损优先）
    "max_daily_loss": 0.05,         # 单日最大亏损 5% = 10万
    "max_positions": 8,             # 最大持仓数
    "min_position_ratio": 0.05,     # 最小仓位比例 5%
    "max_position_ratio": 0.35,     # 单股仓位 35%（2026-07-05网格搜索：DD=20%+Pos=35%→夏普1.93/回撤-24.3%/收益+302.8%）
    "trailing_stop_trigger": 0.15,  # 盈利15%启动移动止盈
    "trailing_stop_ratio": 0.70,    # 回吐70%盈利才止盈
    "daily_loss_halt_days": 1,      # 触发单日限额后暂停天数
    # 优化6: 盈利保护机制（盈利越大，止盈越紧）
    "profit_protection_tiers": [
        {"profit_above": 1.50, "trailing_ratio": 0.40},  # 盈利>150%: 回吐40%就止盈
        {"profit_above": 1.00, "trailing_ratio": 0.50},  # 盈利>100%: 回吐50%就止盈
        {"profit_above": 0.50, "trailing_ratio": 0.60},  # 盈利>50%:  回吐60%就止盈
        {"profit_above": 0.00, "trailing_ratio": 0.70},  # 盈利>0%:   回吐70%才止盈（默认）
    ],
    # 优化B: ATR自适应止损（下限提高，避免低波动股太紧）
    "atr_stop_multiple": 2.5,       # 成本止损线 = 买入价 - 2.5×ATR
    "atr_stop_max_loss": 0.15,      # 成本止损上限15%
    "atr_stop_min_loss": 0.08,      # 成本止损下限8%
    # 峰值trailing止损: 从持仓峰值回撤时触发, 锁住浮盈防坐电梯
    # 2026-07-04: 回测8×ATR cap20-35%为最优拐点, 总收益+5390%(vs旧Hold +5701%), 4笔防住大幅回撤
    "peak_stop_multiple": 8.0,      # 峰值止损线 = 峰值价 - 8×ATR
    "peak_stop_max_loss": 0.35,     # 峰值止损上限35%
    "peak_stop_min_loss": 0.20,     # 峰值止损下限20%（正常回调不触发, 只防崩盘）
    # 优化C: 突破确认加码（更激进）
    "pyramid_allowed": True,        # 允许盈利后加仓
    "pyramid_trigger": 0.08,        # 盈利8%即可加仓（原10%）
    "pyramid_max_adds": 2,          # 最多加仓2次（原1次）
    "pyramid_size_ratio": 0.50,     # 每次加仓量=首次仓位的50%
    # 优化D: 盈利加仓单独追踪止盈（加仓部分有独立止损）
    "pyramid_stop_from_avg": True,  # 加仓后按均价止损
    # 止盈模式: "trailing" = 分级移动止盈, "hold" = 只用ATR止损不止盈(让利润奔跑)
    # 2026-07-03: 13只股票ATR/价格均>3%, 回测显示Hold模式总收益+5706% vs Trailing +3292%
    # 判定规则: ATR/价格>3% → Hold(高波动, Trailing会被正常回调扫出去); <3% → Trailing
    "stop_profit_mode": "hold",
    # 仓位模式: "fixed" = 固定比例30%(默认), "atr" = ATR仓位(1%风险/ATR)
    "position_sizing_mode": "fixed",
    "atr_risk_per_trade": 0.01,     # ATR仓位模式: 每笔风险1%账户
    "atr_position_max_ratio": 0.30, # ATR仓位模式: 单股上限30%
}

# ============ 交易参数 ============
TRADE_CONFIG = {
    "min_lot_size": 100,            # A股最小交易单位 100股
    "commission_rate": 0.0003,       # 佣金费率 万三
    "commission_min": 5.0,           # 佣金最低 5元
    "stamp_tax_rate": 0.001,         # 印花税 千一（卖出时）
    "transfer_fee_rate": 0.00002,    # 过户费 万分之0.2
    "slippage": 0.001,              # 滑点 0.1%
}

# ============ 数据源 ============
DATA_CONFIG = {
    "source": "akshare",            # 数据源
    "cache_enabled": True,          # 启用本地缓存
    "cache_expire_hours": 12,       # 缓存过期时间
}

# ============ 选股池 ============
STOCK_UNIVERSE = {
    # 沪深300成分股（流动性好，适合大资金）
    "pool": "hs300",                # hs300 / zz500 / all
    # 排除条件
    "exclude_st": True,             # 排除ST股
    "exclude_new_days": 60,         # 排除上市不足60天的新股
    "min_market_cap": 5e9,          # 最小市值 50亿
    "min_avg_volume_20d": 1e7,      # 20日平均成交量最低 1000万
}

# ============ 通知配置 ============
NOTIFY_CONFIG = {
    "enabled": True,
    "console": True,                # 控制台输出
    "log_file": True,               # 日志文件
    # "wechat_webhook": "",         # 企业微信机器人（可选）
    # "telegram_token": "",         # Telegram Bot（可选）
}

# ============ QMT实盘配置 ============
QMT_CONFIG = {
    "default_path": os.getenv("QMT_PATH", r"C:\国金QMT交易端\userdata_mini"),
    "session_id": 9999,
}

# ============ 调度配置 ============
SCHEDULE_CONFIG = {
    # 运行时间：交易日 9:25 集合竞价准备, 15:05 收盘后总结
    "pre_market_time": "09:25",
    "market_open_time": "09:30",
    "market_close_time": "15:00",
    "post_market_time": "15:05",
    # 策略运行间隔（分钟）
    "run_interval": 5,
}
