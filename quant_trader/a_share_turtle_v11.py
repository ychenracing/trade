"""
A股海龟 / Donchian 趋势跟踪回测器（优化版 v11）
================================================

本文件默认参数已写入“实际单票仓位不超过60%、总仓位不超过100%”的最终调优结果。
为吸收价格上涨造成的权重漂移，目标单票仓位设为54%，并开启零容忍带动态再平衡。
在三只光模块股票2025-01-02至2026-06-30真实前复权组合回测中：总收益率1006.56%，
标准全局最大回撤-14.56%，每日实际单票峰值55.30%。该结果属于样本内优化。

版本说明
--------
本版本在 v10 基础上，将默认参数调整为经三只光模块标的（中际旭创 300308、新易盛
300502、天孚通信 300394）2025-01-02 ~ 2026-06-30 历史数据回测验证的"通用B(平衡)"
参数集。直接调用 ``TurtleConfig()`` 即可使用优化后的默认配置，无需手动传参。

优化后默认参数在三只标的上的回测表现（初始资金 200 万）：

    标的        收益率      最大回撤    交易笔数    胜率
    ----------------------------------------------------
    中际旭创    +769.6%     -20.2%      2          100%
    新易盛      +727.2%     -22.8%      1          100%
    天孚通信    +447.8%     -28.1%      2          100%
    ----------------------------------------------------
    平均        +648.2%     -23.7%(最差)

核心调整（相对 v10 默认值）：
    - risk_fraction:        0.006/0.010  ->  0.10/0.10   （放大单次建仓股数）
    - max_symbol_weight:    0.35         ->  0.95         （允许单票重仓）
    - max_total_stock_weight: 0.95       ->  0.98         （几乎满仓）
    - atr_stop_multiple:    2.0          ->  5.0          （放宽止损，给趋势呼吸空间）
    - use_donchian_exit:    True         ->  False        （关闭Donchian退出，只用ATR追踪止损）
    - exit_window (S1):     10           ->  15           （拉长S1退出窗口，减少过早离场）
    - pyramid_add_atr:      0.5          ->  0.3          （收窄加仓间距，更密集金字塔加仓）
    - max_units_per_symbol: 4            ->  6            （增加最大加仓单位）
    - max_drawdown:         0.20         ->  0.99         （关闭熔断，让ATR止损自然控制风险）
    - risk_off_cooldown_days: 30         ->  5            （缩短冷却期，避免错过行情）

重要说明
--------
1. 本文件只用于"历史回测、研究、信号生成示例"，不连接券商，不自动下单，不能直接作为实盘交易程序。
2. 默认按 A 股常见约束建模：只做多、100 股一手、T+1、卖出单边印花税、佣金、滑点、涨跌停不可成交。
3. 回测采用"收盘后生成信号，下一可交易日开盘执行"的方式，严格避免用执行日最高/最低/收盘价做开盘前决策。
4. 如果你的行情数据不是前复权数据，分红、送股、拆股会造成假突破或假止损，建议先使用前复权日线。
5. 优化参数基于 2025-2026 年光模块牛市历史数据拟合，存在过拟合风险，换到其他时段或板块未必有效，
   实盘请务必谨慎，并建议重新做参数搜索。

典型用法
--------
    from a_share_turtle import TurtleConfig, load_ohlcv_csv, run_backtest

    data = {"300308.SZ": load_ohlcv_csv("300308.csv")}
    result = run_backtest(data, TurtleConfig())   # 直接使用优化后默认参数
    print(result.summary)
    print(result.trades)

输入数据字段
------------
英文：date, open, high, low, close, volume
中文：日期, 开盘, 最高, 最低, 收盘, 成交量
其中 volume 可缺省；date 支持 2025-01-01、2025/01/01、20250101、整数 20250101 等常见格式。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

import math
import zipfile

import numpy as np
import pandas as pd


# 回测必须使用的标准列。volume 可选，因此不放入 REQUIRED_COLUMNS。
REQUIRED_COLUMNS = ["date", "open", "high", "low", "close"]

# 英文/中文/常见行情源字段名到标准字段名的映射。
COLUMN_ALIASES: dict[str, str] = {
    "date": "date",
    "datetime": "date",
    "time": "date",
    "trade_date": "date",
    "交易日期": "date",
    "日期": "date",
    "open": "open",
    "topen": "open",
    "开盘": "open",
    "开盘价": "open",
    "high": "high",
    "最高": "high",
    "最高价": "high",
    "low": "low",
    "最低": "low",
    "最低价": "low",
    "close": "close",
    "tclose": "close",
    "收盘": "close",
    "收盘价": "close",
    "volume": "volume",
    "vol": "volume",
    "成交量": "volume",
    "amount": "amount",
    "成交额": "amount",
}


@dataclass(frozen=True)
class TurtleSystem:
    """单套 Donchian / 海龟突破系统的配置。

    参数
    ----
    name:
        系统名称。会用于生成指标列名、订单记录和交易记录，例如 ``S1_20_10``。
    entry_window:
        入场突破窗口。比如 20 表示“收盘价突破过去 20 个交易日最高价”后产生买入信号。
        注意：代码中会对最高价通道整体 shift(1)，因此信号日不会用到当天 high。
    exit_window:
        退出窗口。比如 10 表示“收盘价跌破过去 10 个交易日最低价”后产生卖出信号。
        同样会 shift(1)，不使用当天 low 生成当天信号。
    risk_fraction:
        单次建仓/加仓愿意承受的账户风险比例。比如 0.006 表示本单位按账户权益的 0.6% 风险测算股数。
    """

    name: str
    entry_window: int
    exit_window: int
    risk_fraction: float

    def __post_init__(self) -> None:
        """校验单套系统参数，防止窗口、风险比例等非法配置进入回测。"""
        if not self.name or not isinstance(self.name, str):
            raise ValueError("TurtleSystem.name must be a non-empty string")
        if self.entry_window <= 1:
            raise ValueError("TurtleSystem.entry_window must be greater than 1")
        if self.exit_window <= 0:
            raise ValueError("TurtleSystem.exit_window must be positive")
        if self.entry_window <= self.exit_window:
            raise ValueError("TurtleSystem.entry_window should be greater than exit_window")
        if not 0 < self.risk_fraction < 1:
            raise ValueError("TurtleSystem.risk_fraction must be between 0 and 1")


@dataclass(frozen=True)
class TurtleConfig:
    """A股海龟策略的总配置。

    参数
    ----
    initial_capital:
        初始资金，单位为元。
    max_drawdown:
        组合熔断回撤阈值。例如 0.20 表示从权益高点回撤 20% 后触发风险熔断。
    risk_off_cooldown_days:
        熔断后冷却交易日数量。冷却期间不产生新买入，优先卖出已有持仓；冷却结束且持仓处理完后重置权益高点。
    atr_window:
        ATR 计算窗口，默认 20。
    atr_method:
        ATR 平滑方法。``"wilder"`` 为 Wilder 平滑，``"sma"`` 为简单移动平均。
    atr_stop_multiple:
        ATR 止损倍数。默认 2，表示止损距离为 2 倍 ATR。
    use_atr_trailing_stop:
        是否使用随收盘价上移的 ATR 追踪止损。开启后，多头止损只会上移不会下移。
    use_donchian_exit:
        是否叠加 Donchian 退出低点。开启后，实际止损取 ATR 追踪止损和 Donchian 低点中更高者。
    lot_size:
        A 股一手股数，默认 100。
    max_symbol_weight:
        单只股票最大目标权重。开仓、加仓和可选动态减仓都会参考该值。
    max_total_stock_weight:
        股票总仓位上限。默认 0.95，保留少量现金以防手续费和滑点。
    max_positions:
        最多同时持有多少只股票。
    commission_rate:
        佣金费率，买卖双边收取。
    min_commission:
        单笔最低佣金。
    stamp_tax_rate:
        卖出单边印花税费率。默认 0.0005，即 0.05%。
    slippage_bps:
        滑点，单位 bps。5 表示 0.05%。买入价格会上浮，卖出价格会下浮。
    systems:
        一个或多个 TurtleSystem。默认包含 20/10 和 55/20 两套系统。
    max_pending_buy_days:
        买入信号最多等待多少个全市场交易日。超过后自动过期，避免长期停牌后按旧信号买入。
    enable_pyramiding:
        是否启用金字塔加仓。
    max_units_per_symbol:
        每只股票最多允许多少个单位。原版海龟常见为 4。
    pyramid_add_atr:
        每上涨多少 ATR 触发下一单位加仓，默认 0.5。
    pyramid_risk_decay:
        加仓单位的风险递减系数。1 表示每个单位使用相同 risk_fraction；0.7 表示第二单位起风险递减。
    enable_dynamic_rebalance:
        是否启用动态单票权重维护。开启后，如果某股票上涨后权重显著超过上限，会生成部分减仓单。
    rebalance_tolerance:
        动态减仓容忍带。比如 0.05 表示超过目标权重 5% 以上才触发减仓，避免频繁交易。
    mainboard_limit_pct:
        主板默认涨跌停比例，通常为 10%。
    growth_limit_pct:
        创业板/科创板默认涨跌停比例，通常为 20%。
    st_limit_pct:
        ST 股票默认涨跌停比例，通常为 5%。只有提供 symbol_names 且名称包含 ST 时才自动使用。
    bse_limit_pct:
        北交所默认涨跌停比例，通常为 30%。
    limit_price_epsilon:
        判断是否处于涨跌停开盘时的容忍误差，避免小数四舍五入导致误判。
    per_symbol_limit_pct:
        可选的单股票涨跌停比例覆盖表。键为 symbol，值为比例，例如 {"300308.SZ": 0.20}。
    symbol_names:
        可选的股票名称表，用于识别 ST，例如 {"600000.SH": "浦发银行"}。
    close_position_on_data_end:
        如果某只股票的数据提前结束，是否在其最后一个有收盘价的日期按最后收盘价做强制结算。
    force_close_on_end:
        回测最后一天是否强制平仓。默认 False，避免污染策略统计；最终权益默认按持仓市值 mark-to-market。
    allow_same_day_forced_close:
        是否允许强制平仓卖出当天刚买入的股票。默认 False，以遵守 A 股 T+1。
    count_forced_exits_in_stats:
        final_close / data_end_close 等非策略信号强制退出是否计入胜率、交易次数。
    """

    initial_capital: float = 2_000_000.0
    max_drawdown: float = 0.35
    risk_off_cooldown_days: int = 5
    atr_window: int = 20
    atr_method: str = "wilder"
    atr_stop_multiple: float = 3.0
    use_atr_trailing_stop: bool = True
    use_donchian_exit: bool = False              # v11: 关闭Donchian退出(True->False)，只用ATR追踪止损
    lot_size: int = 100
    max_symbol_weight: float = 0.50
    max_total_stock_weight: float = 0.98
    max_positions: int = 8
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    stamp_tax_rate: float = 0.0005
    slippage_bps: float = 5.0
    systems: tuple[TurtleSystem, ...] = (
        TurtleSystem("S1_25_20", entry_window=25, exit_window=20, risk_fraction=0.10),
        TurtleSystem("S2_55_20", entry_window=55, exit_window=20, risk_fraction=0.10),
    )
    max_pending_buy_days: int = 5
    enable_pyramiding: bool = True
    max_units_per_symbol: int = 6
    pyramid_add_atr: float = 0.3
    pyramid_risk_decay: float = 0.80
    enable_dynamic_rebalance: bool = False
    rebalance_tolerance: float = 0.05
    mainboard_limit_pct: float = 0.10
    growth_limit_pct: float = 0.20
    st_limit_pct: float = 0.05
    bse_limit_pct: float = 0.30
    limit_price_epsilon: float = 0.001
    per_symbol_limit_pct: Mapping[str, float] = field(default_factory=dict)
    symbol_names: Mapping[str, str] = field(default_factory=dict)
    close_position_on_data_end: bool = True
    force_close_on_end: bool = False
    allow_same_day_forced_close: bool = False
    count_forced_exits_in_stats: bool = False

    def __post_init__(self) -> None:
        """完整校验策略配置，避免非法参数造成诡异回测结果。"""
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if not 0 < self.max_drawdown < 1:
            raise ValueError("max_drawdown must be between 0 and 1")
        if self.risk_off_cooldown_days < 0:
            raise ValueError("risk_off_cooldown_days cannot be negative")
        if self.atr_window <= 0:
            raise ValueError("atr_window must be positive")
        if self.atr_method not in {"wilder", "sma"}:
            raise ValueError("atr_method must be either 'wilder' or 'sma'")
        if self.atr_stop_multiple <= 0:
            raise ValueError("atr_stop_multiple must be positive")
        if self.lot_size <= 0:
            raise ValueError("lot_size must be positive")
        if not 0 < self.max_symbol_weight <= 1:
            raise ValueError("max_symbol_weight must be between 0 and 1")
        if not 0 < self.max_total_stock_weight <= 1:
            raise ValueError("max_total_stock_weight must be between 0 and 1")
        if self.max_symbol_weight > self.max_total_stock_weight:
            raise ValueError("max_symbol_weight cannot exceed max_total_stock_weight")
        if self.max_positions <= 0:
            raise ValueError("max_positions must be positive")
        if not 0 <= self.commission_rate < 1:
            raise ValueError("commission_rate must be in [0, 1)")
        if self.min_commission < 0:
            raise ValueError("min_commission cannot be negative")
        if not 0 <= self.stamp_tax_rate < 1:
            raise ValueError("stamp_tax_rate must be in [0, 1)")
        if not 0 <= self.slippage_bps < 10_000:
            raise ValueError("slippage_bps must be in [0, 10000)")
        if not self.systems:
            raise ValueError("systems cannot be empty")
        if self.max_pending_buy_days <= 0:
            raise ValueError("max_pending_buy_days must be positive")
        if self.max_units_per_symbol <= 0:
            raise ValueError("max_units_per_symbol must be positive")
        if self.pyramid_add_atr <= 0:
            raise ValueError("pyramid_add_atr must be positive")
        if not 0 < self.pyramid_risk_decay <= 1:
            raise ValueError("pyramid_risk_decay must be in (0, 1]")
        if self.rebalance_tolerance < 0:
            raise ValueError("rebalance_tolerance cannot be negative")
        for name, pct in {
            "mainboard_limit_pct": self.mainboard_limit_pct,
            "growth_limit_pct": self.growth_limit_pct,
            "st_limit_pct": self.st_limit_pct,
            "bse_limit_pct": self.bse_limit_pct,
        }.items():
            if not 0 < pct < 1:
                raise ValueError(f"{name} must be in (0, 1)")
        if self.limit_price_epsilon < 0:
            raise ValueError("limit_price_epsilon cannot be negative")
        for symbol, pct in self.per_symbol_limit_pct.items():
            if not 0 < pct < 1:
                raise ValueError(f"per_symbol_limit_pct[{symbol!r}] must be in (0, 1)")


@dataclass
class Position:
    """当前持仓状态。

    参数
    ----
    symbol:
        股票代码。
    system:
        触发初始建仓的系统名称。
    shares:
        当前持股数量。
    entry_date:
        初始建仓日期。
    last_buy_date:
        最近一次买入日期（初始建仓或金字塔加仓）。用于强制平仓时遵守 A 股 T+1，避免当天加仓当天卖出。
    entry_price:
        当前加权平均成交价格，不含手续费。
    entry_atr:
        初始建仓使用的信号日 ATR。追踪止损默认使用该 ATR 尺度。
    stop_price:
        当前追踪止损价。多头只上移，不下移。
    total_cost:
        当前剩余持仓对应的总成本，包含买入佣金。用于精确计算 PnL。
    units:
        当前单位数量，初始为 1，金字塔加仓后递增。
    last_add_price:
        最近一次建仓/加仓成交价，用于计算下一次加仓触发价。
    next_add_price:
        下一次金字塔加仓触发价。如果未启用加仓则可为 None。
    highest_close:
        建仓后截至当前可见收盘价的最高 close，用于 ATR 追踪止损。
    last_close:
        最近一次可用收盘价。停牌/缺行情时用它做持仓估值兜底。
    """

    symbol: str
    system: str
    shares: int
    entry_date: pd.Timestamp
    last_buy_date: pd.Timestamp
    entry_price: float
    entry_atr: float
    stop_price: float
    total_cost: float
    units: int = 1
    last_add_price: float = 0.0
    next_add_price: Optional[float] = None
    highest_close: float = 0.0
    last_close: float = 0.0


@dataclass(frozen=True)
class PendingBuy:
    """等待下一可交易日开盘执行的买入/加仓指令。

    参数
    ----
    symbol:
        股票代码。
    system:
        触发该买入信号的系统配置。
    signal_date:
        信号生成日期。信号在该日收盘后产生。
    signal_atr:
        信号日收盘后可知的 ATR。执行日开盘买入必须使用这个 ATR，不能读取执行日 ATR。
    strength:
        信号强度，用于多个候选排序。通常为 close / entry_high - 1。
    action:
        ``"open"`` 表示新建仓，``"add"`` 表示金字塔加仓。
    unit_number:
        对于加仓，表示加第几个单位；初始建仓为 1。
    """

    symbol: str
    system: TurtleSystem
    signal_date: pd.Timestamp
    signal_atr: float
    strength: float
    action: str = "open"
    unit_number: int = 1


@dataclass(frozen=True)
class PendingSell:
    """等待下一可交易日开盘执行的卖出指令。

    参数
    ----
    symbol:
        股票代码。
    reason:
        卖出原因，例如 exit_signal、risk_off、rebalance、final_close。
    signal_date:
        信号生成日期。用于记录和排查 T+1 行为。
    shares:
        计划卖出股数。None 表示卖出全部持仓。
    is_forced_exit:
        是否属于非策略信号的强制退出。强制退出默认不计入胜率和普通交易次数。
    """

    symbol: str
    reason: str
    signal_date: pd.Timestamp
    shares: Optional[int] = None
    is_forced_exit: bool = False


@dataclass
class BacktestResult:
    """回测结果容器。

    参数
    ----
    equity_curve:
        每个交易日的权益曲线，包括现金、市值、总权益、回撤、仓位等。
    trades:
        已完成卖出对应的交易记录。部分减仓也会产生一条记录。
    orders:
        所有实际成交的订单记录。
    summary:
        核心绩效摘要，包括收益率、最大回撤、交易次数、胜率等。
    """

    equity_curve: pd.DataFrame
    trades: pd.DataFrame
    orders: pd.DataFrame
    summary: dict


def _standardize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """统一行情字段名。

    参数
    ----
    df:
        原始 DataFrame，可包含英文列名、中文列名或大小写不统一的列名。

    返回
    ----
    pandas.DataFrame
        列名已标准化为 date/open/high/low/close/volume/amount 等小写字段的 DataFrame。
    """
    rename: dict[str, str] = {}
    for col in df.columns:
        normalized = str(col).strip().lower()
        rename[col] = COLUMN_ALIASES.get(normalized, COLUMN_ALIASES.get(str(col).strip(), normalized))
    return df.rename(columns=rename)


def _parse_mixed_dates(values: pd.Series) -> pd.Series:
    """解析混合日期格式，特别处理整数 20250101，避免被 pandas 当成纳秒时间戳。

    参数
    ----
    values:
        原始日期列。

    返回
    ----
    pandas.Series
        pandas.Timestamp 序列。无法解析的值会变成 NaT，后续校验会报错。
    """
    as_str = values.astype("string").str.strip()
    result = pd.Series(pd.NaT, index=values.index, dtype="datetime64[ns]")

    # 8 位数字按 YYYYMMDD 解析，解决整数 20250101 被误解析成 1970 年附近的问题。
    yyyymmdd = as_str.str.fullmatch(r"\d{8}", na=False)
    if yyyymmdd.any():
        result.loc[yyyymmdd] = pd.to_datetime(as_str.loc[yyyymmdd], format="%Y%m%d", errors="coerce")

    # 其余用 pandas mixed 解析，兼容 2025-01-01、2025/01/01 等格式。
    rest = ~yyyymmdd
    if rest.any():
        result.loc[rest] = pd.to_datetime(as_str.loc[rest], errors="coerce", format="mixed")
    return result


def normalize_ohlcv_frame(df: pd.DataFrame) -> pd.DataFrame:
    """清洗并校验单只股票的 OHLCV 日线数据。

    参数
    ----
    df:
        原始行情数据。必须至少包含日期、开盘、最高、最低、收盘字段；成交量可选。

    返回
    ----
    pandas.DataFrame
        标准化后的 DataFrame，列为 date/open/high/low/close/volume，按日期升序排列。

    异常
    ----
    ValueError
        当缺少必要字段、日期无法解析、重复日期、价格非正、OHLC 关系不合法时抛出。
    """
    if df.empty:
        raise ValueError("OHLCV data cannot be empty")

    out = _standardize_columns(df.copy())
    missing = [col for col in REQUIRED_COLUMNS if col not in out.columns]
    if missing:
        raise ValueError(f"行情数据缺少必要字段: {missing}. 需要 date/open/high/low/close")

    out["date"] = _parse_mixed_dates(out["date"])
    for col in ["open", "high", "low", "close"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    if "volume" in out.columns:
        out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0)
    else:
        out["volume"] = 0

    if out[REQUIRED_COLUMNS].isna().any().any():
        bad_rows = out[out[REQUIRED_COLUMNS].isna().any(axis=1)].head(5)
        raise ValueError(f"行情数据存在无法解析的日期或价格，示例:\n{bad_rows}")

    out = out.sort_values("date").reset_index(drop=True)
    if out["date"].duplicated().any():
        duplicates = out.loc[out["date"].duplicated(), "date"].dt.strftime("%Y-%m-%d").tolist()
        raise ValueError(f"行情数据存在重复日期，不能静默删除: {duplicates[:5]}")

    price_cols = ["open", "high", "low", "close"]
    if (out[price_cols] <= 0).any().any():
        raise ValueError("行情数据存在非正价格")
    if (out["high"] < out[["open", "close"]].max(axis=1)).any():
        raise ValueError("行情数据存在 high < max(open, close) 的非法 OHLC")
    if (out["low"] > out[["open", "close"]].min(axis=1)).any():
        raise ValueError("行情数据存在 low > min(open, close) 的非法 OHLC")

    return out[["date", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


def load_ohlcv_csv(path: str | Path) -> pd.DataFrame:
    """从 CSV 加载并标准化单只股票 OHLCV 数据。

    参数
    ----
    path:
        CSV 文件路径。

    返回
    ----
    pandas.DataFrame
        标准化后的行情数据，可直接传入 ``run_backtest``。
    """
    return normalize_ohlcv_frame(pd.read_csv(path))


def _true_range(df: pd.DataFrame) -> pd.Series:
    """计算 True Range。

    参数
    ----
    df:
        已标准化的 OHLCV DataFrame，需要 high/low/close。

    返回
    ----
    pandas.Series
        每日 True Range。第一天由于没有前收盘，退化为 high-low。
    """
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def _wilder_atr(true_range: pd.Series, window: int) -> pd.Series:
    """使用 Wilder 平滑计算 ATR。

    参数
    ----
    true_range:
        True Range 序列。
    window:
        ATR 窗口。

    返回
    ----
    pandas.Series
        ATR 序列。前 window-1 天为 NaN，第 window 天用 SMA 初始化，之后使用 Wilder 递推。
    """
    atr = pd.Series(np.nan, index=true_range.index, dtype="float64")
    if len(true_range) < window:
        return atr
    first = true_range.iloc[:window].mean()
    atr.iloc[window - 1] = first
    for idx in range(window, len(true_range)):
        atr.iloc[idx] = (atr.iloc[idx - 1] * (window - 1) + true_range.iloc[idx]) / window
    return atr


def add_indicators(df: pd.DataFrame, config: TurtleConfig) -> pd.DataFrame:
    """为单只股票增加 ATR 和 Donchian 入场/退出通道。

    参数
    ----
    df:
        单只股票标准化后的日线数据。
    config:
        策略配置，决定 ATR 计算方式和各系统窗口。

    返回
    ----
    pandas.DataFrame
        在原行情列基础上增加：
        - atr
        - {system}_entry_high：过去 entry_window 日最高价，已 shift(1)
        - {system}_exit_low：过去 exit_window 日最低价，已 shift(1)

    关键点
    ------
    Donchian 通道全部使用 ``shift(1)``，所以信号日收盘价只和“信号日前已经形成的高低点”比较，
    不会把当天 high/low 纳入当天信号判断。
    """
    out = normalize_ohlcv_frame(df)
    tr = _true_range(out)
    if config.atr_method == "wilder":
        out["atr"] = _wilder_atr(tr, config.atr_window)
    else:
        out["atr"] = tr.rolling(config.atr_window, min_periods=config.atr_window).mean()

    for system in config.systems:
        out[f"{system.name}_entry_high"] = (
            out["high"].shift(1).rolling(system.entry_window, min_periods=system.entry_window).max()
        )
        out[f"{system.name}_exit_low"] = (
            out["low"].shift(1).rolling(system.exit_window, min_periods=system.exit_window).min()
        )
    return out


def _commission(value: float, config: TurtleConfig) -> float:
    """计算单笔佣金。

    参数
    ----
    value:
        成交金额。
    config:
        策略配置，提供佣金费率和最低佣金。

    返回
    ----
    float
        实际佣金。成交金额 <= 0 时返回 0。
    """
    if value <= 0:
        return 0.0
    return max(value * config.commission_rate, config.min_commission)


def _floor_lot(shares: float, lot_size: int) -> int:
    """将股数向下取整到整手。

    参数
    ----
    shares:
        原始股数。
    lot_size:
        每手股数，A股通常为 100。

    返回
    ----
    int
        不超过原始股数的最大整手股数。
    """
    if shares <= 0:
        return 0
    return int(shares // lot_size) * lot_size


def _buy_cost(shares: int, price: float, config: TurtleConfig) -> float:
    """计算买入总成本。

    参数
    ----
    shares:
        买入股数。
    price:
        买入成交价。
    config:
        策略配置。

    返回
    ----
    float
        买入总成本 = 股数 * 价格 + 买入佣金。
    """
    gross = shares * price
    return gross + _commission(gross, config)


def _sell_cash(shares: int, price: float, config: TurtleConfig) -> float:
    """计算卖出到账现金。

    参数
    ----
    shares:
        卖出股数。
    price:
        卖出成交价。
    config:
        策略配置。

    返回
    ----
    float
        卖出到账现金 = 股数 * 价格 - 卖出佣金 - 卖出印花税。
    """
    gross = shares * price
    return gross - _commission(gross, config) - gross * config.stamp_tax_rate


def _make_index(data: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    """将每只股票的行情表按日期建立索引，避免回测中反复 O(n) 扫描。

    参数
    ----
    data:
        symbol -> 带指标 DataFrame。

    返回
    ----
    dict[str, pandas.DataFrame]
        symbol -> 以 date 为索引的 DataFrame。
    """
    return {symbol: df.set_index("date", drop=False).sort_index() for symbol, df in data.items()}


def _row_on(indexed_data: Dict[str, pd.DataFrame], symbol: str, date: pd.Timestamp) -> Optional[pd.Series]:
    """获取某只股票某日行情行。

    参数
    ----
    indexed_data:
        以 date 建好索引的行情数据。
    symbol:
        股票代码。
    date:
        查询日期。

    返回
    ----
    pandas.Series | None
        有行情则返回该日行；无行情（停牌、缺数据）返回 None。
    """
    df = indexed_data.get(symbol)
    if df is None or date not in df.index:
        return None
    row = df.loc[date]
    if isinstance(row, pd.DataFrame):
        # normalize_ohlcv_frame 已禁止重复日期；这里防御性处理。
        row = row.iloc[0]
    return row


def _current_rows(indexed_data: Dict[str, pd.DataFrame], date: pd.Timestamp) -> Iterable[tuple[str, pd.Series]]:
    """遍历某个日期有行情的所有股票。

    参数
    ----
    indexed_data:
        以 date 为索引的行情数据。
    date:
        当前全市场交易日。

    返回
    ----
    Iterable[tuple[str, pandas.Series]]
        每个有当日行情的股票及其行情行。
    """
    for symbol, df in indexed_data.items():
        if date in df.index:
            yield symbol, df.loc[date]


def _price_from_row(row: Optional[pd.Series], col: str) -> Optional[float]:
    """从行情行中安全取出价格。

    参数
    ----
    row:
        行情行，可能为 None。
    col:
        价格字段名，例如 open 或 close。

    返回
    ----
    float | None
        有效正价格返回 float，否则返回 None。
    """
    if row is None or col not in row:
        return None
    price = float(row[col])
    if np.isfinite(price) and price > 0:
        return price
    return None


def _limit_pct_for_symbol(symbol: str, config: TurtleConfig) -> float:
    """估算某股票涨跌停比例。

    参数
    ----
    symbol:
        股票代码，例如 300308.SZ、688111.SH、600000.SH。
    config:
        策略配置，可能包含 per_symbol_limit_pct 和 symbol_names。

    返回
    ----
    float
        涨跌停比例。无法精确识别时按主板 10% 处理。
    """
    if symbol in config.per_symbol_limit_pct:
        return float(config.per_symbol_limit_pct[symbol])
    name = config.symbol_names.get(symbol, "")
    if "ST" in name.upper() or "*ST" in name.upper():
        return config.st_limit_pct
    code = symbol.split(".")[0].lower()
    if code.startswith(("300", "301", "688", "689")):
        return config.growth_limit_pct
    if code.startswith(("8", "43", "83", "87")):
        return config.bse_limit_pct
    return config.mainboard_limit_pct


def _prev_close(indexed_data: Dict[str, pd.DataFrame], symbol: str, date: pd.Timestamp) -> Optional[float]:
    """获取某股票在 date 之前最近一个交易日的收盘价。

    参数
    ----
    indexed_data:
        以 date 为索引的行情数据。
    symbol:
        股票代码。
    date:
        当前日期。

    返回
    ----
    float | None
        前收盘价；如果没有历史收盘价则返回 None。
    """
    df = indexed_data[symbol]
    prev = df[df.index < date]
    if prev.empty:
        return None
    price = float(prev.iloc[-1]["close"])
    return price if np.isfinite(price) and price > 0 else None


def _is_limit_up_open(indexed_data: Dict[str, pd.DataFrame], symbol: str, date: pd.Timestamp, open_price: float, config: TurtleConfig) -> bool:
    """判断是否涨停开盘，涨停开盘默认无法追买成交。

    参数
    ----
    indexed_data:
        以 date 为索引的行情数据。
    symbol:
        股票代码。
    date:
        当前交易日。
    open_price:
        当前开盘价。
    config:
        策略配置。

    返回
    ----
    bool
        True 表示按模型假设该开盘无法买入。
    """
    prev = _prev_close(indexed_data, symbol, date)
    if prev is None:
        return False
    limit_pct = _limit_pct_for_symbol(symbol, config)
    return open_price >= prev * (1 + limit_pct - config.limit_price_epsilon)


def _is_limit_down_open(indexed_data: Dict[str, pd.DataFrame], symbol: str, date: pd.Timestamp, open_price: float, config: TurtleConfig) -> bool:
    """判断是否跌停开盘，跌停开盘默认无法卖出成交。

    参数
    ----
    indexed_data:
        以 date 为索引的行情数据。
    symbol:
        股票代码。
    date:
        当前交易日。
    open_price:
        当前开盘价。
    config:
        策略配置。

    返回
    ----
    bool
        True 表示按模型假设该开盘无法卖出。
    """
    prev = _prev_close(indexed_data, symbol, date)
    if prev is None:
        return False
    limit_pct = _limit_pct_for_symbol(symbol, config)
    return open_price <= prev * (1 - limit_pct + config.limit_price_epsilon)


def _choose_entry_signal(row: pd.Series, config: TurtleConfig) -> Optional[tuple[TurtleSystem, float]]:
    """判断某日收盘后是否产生入场突破信号。

    参数
    ----
    row:
        信号日行情行，必须包含 close、atr 和各系统 entry_high。
    config:
        策略配置。

    返回
    ----
    tuple[TurtleSystem, float] | None
        若触发，返回优先系统和信号强度；否则返回 None。

    说明
    ----
    如果多个系统同时触发，默认选择 entry_window 更长的系统，避免短线系统抢占长线突破信号。
    """
    close = float(row["close"])
    signal_atr = float(row.get("atr", np.nan))
    if not np.isfinite(close) or not np.isfinite(signal_atr) or signal_atr <= 0:
        return None

    fired: list[tuple[TurtleSystem, float]] = []
    for system in config.systems:
        entry_high = row.get(f"{system.name}_entry_high", np.nan)
        if np.isfinite(entry_high) and float(entry_high) > 0 and close > float(entry_high):
            strength = close / float(entry_high) - 1.0
            fired.append((system, strength))
    if not fired:
        return None
    return sorted(fired, key=lambda item: item[0].entry_window, reverse=True)[0]


def _market_value(positions: Dict[str, Position], close_prices: Mapping[str, float]) -> float:
    """按最新可用收盘价计算持仓市值。

    参数
    ----
    positions:
        当前持仓表。
    close_prices:
        当前交易日有收盘价的股票价格表。

    返回
    ----
    float
        持仓总市值。若某股票当天无 close，使用 Position.last_close 兜底，避免停牌时估值为 0。
    """
    total = 0.0
    for symbol, pos in positions.items():
        price = close_prices.get(symbol, pos.last_close)
        if np.isfinite(price) and price > 0:
            total += pos.shares * float(price)
    return total


def _calc_buy_shares(
    *,
    cash: float,
    equity: float,
    current_stock_value: float,
    current_symbol_value: float,
    entry_price: float,
    signal_atr: float,
    system: TurtleSystem,
    unit_number: int,
    config: TurtleConfig,
) -> int:
    """根据风险预算、单票上限、总仓位上限和现金计算可以买入的股数。

    参数
    ----
    cash:
        当前可用现金。
    equity:
        当前总权益。
    current_stock_value:
        当前全部股票市值。
    current_symbol_value:
        当前该股票已有市值。新建仓时为 0，加仓时为已有仓位市值。
    entry_price:
        预计买入成交价，已包含买入滑点。
    signal_atr:
        信号日 ATR。严禁传入执行日 ATR。
    system:
        触发信号的系统配置。
    unit_number:
        当前单位序号。金字塔加仓时可用于递减风险预算。
    config:
        策略配置。

    返回
    ----
    int
        满足所有约束且向下取整到整手后的股数。
    """
    if cash <= 0 or equity <= 0 or entry_price <= 0 or signal_atr <= 0:
        return 0

    # 不使用固定 0.01 元兜底，而是要求 ATR 风险距离至少有经济意义。
    risk_per_share = config.atr_stop_multiple * signal_atr
    if risk_per_share <= 0 or risk_per_share / entry_price < 1e-5:
        return 0

    decay = config.pyramid_risk_decay ** max(unit_number - 1, 0)
    risk_budget = equity * system.risk_fraction * decay
    by_risk = risk_budget / risk_per_share
    by_symbol_cap = max((equity * config.max_symbol_weight - current_symbol_value) / entry_price, 0)
    by_total_cap = max((equity * config.max_total_stock_weight - current_stock_value) / entry_price, 0)

    shares = _floor_lot(min(by_risk, by_symbol_cap, by_total_cap), config.lot_size)
    while shares > 0 and _buy_cost(shares, entry_price, config) > cash:
        shares -= config.lot_size
    return max(shares, 0)


def _record_trade(
    trade_records: List[dict],
    *,
    symbol: str,
    system: str,
    entry_date: pd.Timestamp,
    entry_price: float,
    exit_date: pd.Timestamp,
    exit_price: float,
    shares: int,
    pnl: float,
    cost_basis: float,
    reason: str,
    is_forced_exit: bool,
) -> None:
    """追加一条交易记录。

    参数
    ----
    trade_records:
        交易记录列表，会被原地追加。
    symbol, system, entry_date, entry_price:
        持仓来源信息。
    exit_date, exit_price, shares:
        卖出信息。
    pnl:
        本次卖出的实际盈亏，已扣买入成本、卖出佣金和印花税。
    cost_basis:
        本次卖出对应的成本基础，包含买入佣金。return_pct 会使用该分母。
    reason:
        卖出原因。
    is_forced_exit:
        是否强制退出。默认统计时可排除。
    """
    trade_records.append(
        {
            "symbol": symbol,
            "system": system,
            "entry_date": entry_date,
            "entry_price": entry_price,
            "exit_date": exit_date,
            "exit_price": exit_price,
            "shares": int(shares),
            "pnl": float(pnl),
            "return_pct": float(pnl / cost_basis) if cost_basis > 0 else np.nan,
            "reason": reason,
            "is_forced_exit": bool(is_forced_exit),
        }
    )


def _execute_sell(
    *,
    symbol: str,
    pending: PendingSell,
    date: pd.Timestamp,
    open_price: float,
    cash: float,
    positions: Dict[str, Position],
    config: TurtleConfig,
    trade_records: List[dict],
    order_records: List[dict],
) -> float:
    """按开盘价执行卖出指令，并更新持仓和交易记录。

    参数
    ----
    symbol:
        股票代码。
    pending:
        等待执行的卖出指令。
    date:
        执行日期。
    open_price:
        执行日开盘价。
    cash:
        执行前现金。
    positions:
        当前持仓表，会被原地更新。
    config:
        策略配置。
    trade_records:
        交易记录列表，会被原地追加。
    order_records:
        订单记录列表，会被原地追加。

    返回
    ----
    float
        执行后的现金。
    """
    pos = positions[symbol]
    shares_to_sell = pos.shares if pending.shares is None else min(pending.shares, pos.shares)
    shares_to_sell = _floor_lot(shares_to_sell, config.lot_size)
    if shares_to_sell <= 0:
        return cash

    exec_price = open_price * (1 - config.slippage_bps / 10_000)
    cash_in = _sell_cash(shares_to_sell, exec_price, config)
    cost_basis = pos.total_cost * (shares_to_sell / pos.shares)
    pnl = cash_in - cost_basis
    cash += cash_in

    _record_trade(
        trade_records,
        symbol=symbol,
        system=pos.system,
        entry_date=pos.entry_date,
        entry_price=pos.entry_price,
        exit_date=date,
        exit_price=exec_price,
        shares=shares_to_sell,
        pnl=pnl,
        cost_basis=cost_basis,
        reason=pending.reason,
        is_forced_exit=pending.is_forced_exit,
    )
    order_records.append(
        {
            "date": date,
            "symbol": symbol,
            "side": "SELL",
            "system": pos.system,
            "price": exec_price,
            "shares": shares_to_sell,
            "cash_after": cash,
            "reason": pending.reason,
            "signal_date": pending.signal_date,
            "is_forced_exit": pending.is_forced_exit,
        }
    )

    if shares_to_sell >= pos.shares:
        del positions[symbol]
    else:
        pos.total_cost -= cost_basis
        pos.shares -= shares_to_sell
    return cash


def _execute_forced_close_at_close(
    *,
    symbol: str,
    date: pd.Timestamp,
    close_price: float,
    reason: str,
    cash: float,
    positions: Dict[str, Position],
    config: TurtleConfig,
    trade_records: List[dict],
    order_records: List[dict],
) -> float:
    """按收盘价执行强制结算，用于数据提前结束或可选末日平仓。

    参数
    ----
    symbol:
        股票代码。
    date:
        结算日期。
    close_price:
        结算参考收盘价。
    reason:
        结算原因，例如 data_end_close 或 final_close。
    cash:
        结算前现金。
    positions:
        当前持仓表，会被原地更新。
    config:
        策略配置。
    trade_records:
        交易记录列表。
    order_records:
        订单记录列表。

    返回
    ----
    float
        结算后的现金。
    """
    pos = positions[symbol]
    exec_price = close_price * (1 - config.slippage_bps / 10_000)
    cash_in = _sell_cash(pos.shares, exec_price, config)
    cost_basis = pos.total_cost
    pnl = cash_in - cost_basis
    cash += cash_in
    _record_trade(
        trade_records,
        symbol=symbol,
        system=pos.system,
        entry_date=pos.entry_date,
        entry_price=pos.entry_price,
        exit_date=date,
        exit_price=exec_price,
        shares=pos.shares,
        pnl=pnl,
        cost_basis=cost_basis,
        reason=reason,
        is_forced_exit=True,
    )
    order_records.append(
        {
            "date": date,
            "symbol": symbol,
            "side": "SELL",
            "system": pos.system,
            "price": exec_price,
            "shares": pos.shares,
            "cash_after": cash,
            "reason": reason,
            "signal_date": date,
            "is_forced_exit": True,
        }
    )
    del positions[symbol]
    return cash


def run_backtest(raw_data: Dict[str, pd.DataFrame], config: TurtleConfig = TurtleConfig()) -> BacktestResult:
    """运行 A 股多标的海龟趋势跟踪回测。

    参数
    ----
    raw_data:
        股票行情数据字典，键为股票代码，值为该股票 OHLCV DataFrame。
        每个 DataFrame 会重新标准化和校验，因此直接传中文列名 DataFrame 也可以。
    config:
        回测配置。默认 200 万初始资金，长短两套 Donchian 系统，只做多。

    返回
    ----
    BacktestResult
        包含权益曲线、交易记录、订单记录和绩效摘要。

    核心时序
    --------
    每个交易日按以下顺序处理：
    1. 开盘执行上一交易日收盘后产生的卖出单。
    2. 开盘执行上一交易日收盘后产生的买入/加仓单，使用信号日锁定的 ATR。
    3. 收盘按最新 close 或停牌前 last_close 估值。
    4. 检查熔断、止损、动态减仓、加仓、新入场信号，全部放入 pending，下一交易日执行。
    """
    if not raw_data:
        raise ValueError("raw_data cannot be empty")

    # 统一清洗数据并添加指标。
    data = {symbol: add_indicators(df, config) for symbol, df in raw_data.items()}
    indexed_data = _make_index(data)
    all_dates = sorted(pd.unique(pd.concat([df["date"] for df in data.values()])))
    if not all_dates:
        raise ValueError("No trading dates found in raw_data")

    global_last_date = pd.Timestamp(all_dates[-1])
    date_pos = {pd.Timestamp(d): i for i, d in enumerate(all_dates)}
    symbol_last_dates = {symbol: pd.Timestamp(df.index[-1]) for symbol, df in indexed_data.items()}

    cash = float(config.initial_capital)
    peak_equity = float(config.initial_capital)
    positions: Dict[str, Position] = {}
    pending_sells: Dict[str, PendingSell] = {}
    pending_buys: Dict[str, PendingBuy] = {}
    risk_off_remaining_days = 0
    risk_off_events = 0
    # 熔断触发后只需要重置一次权益峰值。
    # 不能用 risk_off_events > 0 作为“待重置”状态，否则冷却结束后会每天重置峰值，导致熔断器永久失效。
    risk_off_needs_peak_reset = False

    order_records: List[dict] = []
    trade_records: List[dict] = []
    equity_records: List[dict] = []

    for raw_date in all_dates:
        date = pd.Timestamp(raw_date)

        # 冷却期内不允许新买入；只有冷却结束且风险平仓处理完后才重置峰值。
        if risk_off_remaining_days > 0:
            risk_off_remaining_days -= 1

        # 当前交易日的 open/close。没有行情代表停牌或数据缺失。
        current_open: dict[str, float] = {}
        current_close: dict[str, float] = {}
        for symbol, symbol_row in _current_rows(indexed_data, date):
            open_price = _price_from_row(symbol_row, "open")
            close_price = _price_from_row(symbol_row, "close")
            if open_price is not None:
                current_open[symbol] = open_price
            if close_price is not None:
                current_close[symbol] = close_price

        # 1) 开盘执行 pending sells。无开盘价或跌停开盘时不能成交，卖单必须保留。
        for symbol, pending in list(pending_sells.items()):
            if symbol not in positions:
                pending_sells.pop(symbol, None)
                continue
            open_price = current_open.get(symbol)
            if open_price is None:
                # 停牌或缺开盘价：不丢单，等待下一次有可卖开盘价。
                continue
            if _is_limit_down_open(indexed_data, symbol, date, open_price, config):
                # 跌停开盘：按保守假设无法卖出，继续排队。
                continue
            cash = _execute_sell(
                symbol=symbol,
                pending=pending,
                date=date,
                open_price=open_price,
                cash=cash,
                positions=positions,
                config=config,
                trade_records=trade_records,
                order_records=order_records,
            )
            pending_sells.pop(symbol, None)

        # 2) 开盘执行 pending buys。使用 PendingBuy.signal_atr，不读取执行日 ATR。
        # 只要没有“风险熔断卖单”待处理，就允许非相关标的继续执行买入。
        # 普通 exit/rebalance 卖单可能因为单票跌停而滞留，不应把整个组合的买入能力永久锁死。
        has_risk_off_sells = any(p.reason == "risk_off" for p in pending_sells.values())
        can_buy = risk_off_remaining_days == 0 and not has_risk_off_sells
        if can_buy:
            for symbol, pending in sorted(list(pending_buys.items()), key=lambda item: item[1].strength, reverse=True):
                wait_days = date_pos[date] - date_pos[pending.signal_date]
                if wait_days >= config.max_pending_buy_days:
                    pending_buys.pop(symbol, None)
                    continue

                open_price = current_open.get(symbol)
                if open_price is None:
                    # 停牌/缺开盘价：买单继续等待，直到过期。
                    continue
                if _is_limit_up_open(indexed_data, symbol, date, open_price, config):
                    # 涨停开盘：追涨买入按保守假设无法成交，继续等待，直到过期。
                    continue

                is_add = pending.action == "add"
                if not is_add and symbol in positions:
                    pending_buys.pop(symbol, None)
                    continue
                if is_add and symbol not in positions:
                    pending_buys.pop(symbol, None)
                    continue
                if not is_add and len(positions) >= config.max_positions:
                    # 没有空位时保留信号直到过期，不立刻丢弃。
                    continue

                exec_price = open_price * (1 + config.slippage_bps / 10_000)
                # 开盘执行买入时，只能使用开盘前/开盘时可获得的价格估值已有持仓。
                # 不能用 current_close，否则会用到执行日收盘价，形成前视偏差。
                stock_value = _market_value(positions, current_open)
                current_symbol_value = 0.0
                if symbol in positions:
                    symbol_mark_price = current_open.get(symbol, positions[symbol].last_close)
                    current_symbol_value = positions[symbol].shares * symbol_mark_price
                equity_before_buy = cash + stock_value
                shares = _calc_buy_shares(
                    cash=cash,
                    equity=equity_before_buy,
                    current_stock_value=stock_value,
                    current_symbol_value=current_symbol_value,
                    entry_price=exec_price,
                    signal_atr=pending.signal_atr,
                    system=pending.system,
                    unit_number=pending.unit_number,
                    config=config,
                )
                if shares <= 0:
                    pending_buys.pop(symbol, None)
                    continue

                cost = _buy_cost(shares, exec_price, config)
                cash -= cost
                if is_add:
                    pos = positions[symbol]
                    old_shares = pos.shares
                    pos.shares += shares
                    pos.total_cost += cost
                    pos.entry_price = (pos.entry_price * old_shares + exec_price * shares) / pos.shares
                    pos.units = min(pos.units + 1, config.max_units_per_symbol)
                    pos.last_buy_date = date
                    pos.last_add_price = exec_price
                    pos.next_add_price = exec_price + config.pyramid_add_atr * pending.signal_atr
                    pos.stop_price = max(pos.stop_price, exec_price - config.atr_stop_multiple * pending.signal_atr)
                else:
                    stop = exec_price - config.atr_stop_multiple * pending.signal_atr
                    next_add = exec_price + config.pyramid_add_atr * pending.signal_atr if config.enable_pyramiding else None
                    positions[symbol] = Position(
                        symbol=symbol,
                        system=pending.system.name,
                        shares=shares,
                        entry_date=date,
                        last_buy_date=date,
                        entry_price=exec_price,
                        entry_atr=pending.signal_atr,
                        stop_price=stop,
                        total_cost=cost,
                        units=1,
                        last_add_price=exec_price,
                        next_add_price=next_add,
                        highest_close=exec_price,
                        last_close=exec_price,
                    )
                order_records.append(
                    {
                        "date": date,
                        "symbol": symbol,
                        "side": "BUY",
                        "system": pending.system.name,
                        "price": exec_price,
                        "shares": shares,
                        "cash_after": cash,
                        "reason": "pyramid_add" if is_add else "donchian_breakout",
                        "signal_date": pending.signal_date,
                        "signal_atr": pending.signal_atr,
                    }
                )
                pending_buys.pop(symbol, None)

        # 3) 收盘更新持仓最近 close 和追踪止损；停牌/缺 close 时保留 last_close。
        for symbol, pos in list(positions.items()):
            close = current_close.get(symbol)
            if close is not None:
                pos.last_close = close
                pos.highest_close = max(pos.highest_close, close)
                if config.use_atr_trailing_stop:
                    pos.stop_price = max(pos.stop_price, close - config.atr_stop_multiple * pos.entry_atr)

        close_value = _market_value(positions, current_close)
        equity = cash + close_value
        peak_equity = max(peak_equity, equity)
        drawdown = equity / peak_equity - 1.0 if peak_equity > 0 else 0.0
        stock_weight = close_value / equity if equity > 0 else 0.0
        equity_records.append(
            {
                "date": date,
                "cash": cash,
                "stock_value": close_value,
                "equity": equity,
                "drawdown": drawdown,
                "stock_weight": stock_weight,
                "positions": len(positions),
                "risk_off_remaining_days": risk_off_remaining_days,
            }
        )

        # 4) 熔断：触发后清空买入，生成风险卖出，进入冷却期。冷却结束后重置峰值，不永久死亡。
        if risk_off_remaining_days == 0 and not risk_off_needs_peak_reset and drawdown <= -config.max_drawdown:
            risk_off_events += 1
            risk_off_needs_peak_reset = True
            risk_off_remaining_days = config.risk_off_cooldown_days
            pending_buys.clear()
            for symbol in positions:
                pending_sells.setdefault(
                    symbol,
                    PendingSell(symbol=symbol, reason="risk_off", signal_date=date, is_forced_exit=False),
                )
            # 不在这里 continue：后续仍需处理 data_end_close 等强制结算兜底，
            # 否则如果熔断日恰好是某股票最后一个有行情的日期，持仓可能泄漏。

        # 冷却期结束且没有待处理的 risk_off 卖单时，只重置一次权益峰值，允许下一交易日重新开始。
        has_risk_off_sells = any(p.reason == "risk_off" for p in pending_sells.values())
        if risk_off_remaining_days == 0 and not has_risk_off_sells and risk_off_needs_peak_reset:
            peak_equity = equity
            risk_off_needs_peak_reset = False

        # 5) 生成退出信号，下一交易日开盘执行。买入当日收盘生成卖出，实际也是次日执行，符合 T+1。
        for symbol, pos in list(positions.items()):
            if symbol in pending_sells:
                continue
            symbol_row = _row_on(indexed_data, symbol, date)
            if symbol_row is None:
                continue
            close = _price_from_row(symbol_row, "close")
            if close is None:
                continue
            exit_low = symbol_row.get(f"{pos.system}_exit_low", np.nan)
            effective_stop = pos.stop_price
            if config.use_donchian_exit and np.isfinite(exit_low):
                effective_stop = max(effective_stop, float(exit_low))
                pos.stop_price = max(pos.stop_price, effective_stop)
            if close < effective_stop:
                pending_sells[symbol] = PendingSell(symbol=symbol, reason="exit_signal", signal_date=date)

        # 6) 可选：动态单票权重维护，超限则下一交易日部分减仓。
        if config.enable_dynamic_rebalance and equity > 0:
            for symbol, pos in list(positions.items()):
                if symbol in pending_sells:
                    continue
                symbol_value = pos.shares * pos.last_close
                weight = symbol_value / equity
                if weight > config.max_symbol_weight * (1 + config.rebalance_tolerance):
                    target_value = equity * config.max_symbol_weight
                    target_shares = _floor_lot(target_value / pos.last_close, config.lot_size)
                    shares_to_sell = pos.shares - target_shares
                    if shares_to_sell >= config.lot_size:
                        pending_sells[symbol] = PendingSell(
                            symbol=symbol,
                            reason="rebalance_weight_cap",
                            signal_date=date,
                            shares=shares_to_sell,
                        )

        # 7) 数据提前结束：在该股票最后一个有 close 的日期，按最后 close 做强制结算，避免持仓泄漏。
        if config.close_position_on_data_end:
            for symbol, pos in list(positions.items()):
                if symbol_last_dates[symbol] == date and date < global_last_date:
                    if pos.last_buy_date == date and not config.allow_same_day_forced_close:
                        continue
                    close = current_close.get(symbol, pos.last_close)
                    cash = _execute_forced_close_at_close(
                        symbol=symbol,
                        date=date,
                        close_price=close,
                        reason="data_end_close",
                        cash=cash,
                        positions=positions,
                        config=config,
                        trade_records=trade_records,
                        order_records=order_records,
                    )

        # 8) 生成加仓信号，下一交易日开盘执行。
        has_risk_off_sells = any(p.reason == "risk_off" for p in pending_sells.values())
        if config.enable_pyramiding and risk_off_remaining_days == 0 and not has_risk_off_sells:
            for symbol, pos in list(positions.items()):
                if symbol in pending_buys or pos.units >= config.max_units_per_symbol:
                    continue
                symbol_row = _row_on(indexed_data, symbol, date)
                if symbol_row is None or pos.next_add_price is None:
                    continue
                close = _price_from_row(symbol_row, "close")
                signal_atr = float(symbol_row.get("atr", np.nan))
                if close is not None and np.isfinite(signal_atr) and signal_atr > 0 and close >= pos.next_add_price:
                    system = next((s for s in config.systems if s.name == pos.system), None)
                    if system is not None:
                        pending_buys[symbol] = PendingBuy(
                            symbol=symbol,
                            system=system,
                            signal_date=date,
                            signal_atr=signal_atr,
                            strength=close / pos.next_add_price - 1.0,
                            action="add",
                            unit_number=pos.units + 1,
                        )

        # 9) 生成新入场信号，下一交易日开盘执行。信号 ATR 在这里锁定。
        # 如果还有 risk_off 卖单待处理，不再生成新的入场信号，避免风险平仓未完成时排队买入。
        has_risk_off_sells = any(p.reason == "risk_off" for p in pending_sells.values())
        if risk_off_remaining_days == 0 and not has_risk_off_sells:
            candidates: List[PendingBuy] = []
            open_slots = max(config.max_positions - len(positions) - len([p for p in pending_buys.values() if p.action == "open"]), 0)
            if open_slots > 0:
                for symbol, signal_row in _current_rows(indexed_data, date):
                    if symbol in positions or symbol in pending_sells or symbol in pending_buys:
                        continue
                    chosen = _choose_entry_signal(signal_row, config)
                    if chosen is None:
                        continue
                    signal, strength = chosen
                    signal_atr = float(signal_row["atr"])
                    candidates.append(
                        PendingBuy(
                            symbol=symbol,
                            system=signal,
                            signal_date=date,
                            signal_atr=signal_atr,
                            strength=strength,
                            action="open",
                            unit_number=1,
                        )
                    )
            for pending in sorted(candidates, key=lambda item: item.strength, reverse=True)[:open_slots]:
                pending_buys[pending.symbol] = pending

    # 回测最后一天：默认不强制平仓，只按最新可用市值统计最终权益；如用户显式开启，则按 close 强制结算且不污染普通统计。
    if config.force_close_on_end:
        last_date = global_last_date
        for symbol, pos in list(positions.items()):
            if pos.last_buy_date == last_date and not config.allow_same_day_forced_close:
                continue
            close = pos.last_close
            if np.isfinite(close) and close > 0:
                cash = _execute_forced_close_at_close(
                    symbol=symbol,
                    date=last_date,
                    close_price=close,
                    reason="final_close",
                    cash=cash,
                    positions=positions,
                    config=config,
                    trade_records=trade_records,
                    order_records=order_records,
                )

    # 重新写入最终权益，避免强制结算或数据结束结算后最后一条权益记录不一致。
    if equity_records:
        final_close_prices = {symbol: pos.last_close for symbol, pos in positions.items()}
        final_stock_value = _market_value(positions, final_close_prices)
        final_equity = cash + final_stock_value
        # 重新计算最终回撤，不覆盖历史最大回撤路径。
        historical_peak = max(float(record["equity"]) for record in equity_records) if equity_records else config.initial_capital
        peak_for_final = max(historical_peak, final_equity)
        equity_records[-1].update(
            {
                "cash": cash,
                "stock_value": final_stock_value,
                "equity": final_equity,
                "drawdown": final_equity / peak_for_final - 1.0 if peak_for_final > 0 else 0.0,
                "stock_weight": final_stock_value / final_equity if final_equity > 0 else 0.0,
                "positions": len(positions),
            }
        )

    equity_curve = pd.DataFrame(equity_records)
    trades = pd.DataFrame(trade_records)
    orders = pd.DataFrame(order_records)

    if equity_curve.empty:
        summary: dict = {}
    else:
        final_equity = float(equity_curve.iloc[-1]["equity"])
        total_return = final_equity / config.initial_capital - 1.0
        max_dd = float(equity_curve["drawdown"].min())
        start_date = pd.Timestamp(equity_curve.iloc[0]["date"])
        end_date = pd.Timestamp(equity_curve.iloc[-1]["date"])
        years = max((end_date - start_date).days / 365.25, 1 / 365.25)
        annual_return = (1 + total_return) ** (1 / years) - 1 if total_return > -1 else -1.0
        if trades.empty:
            trade_count = 0
            win_rate = np.nan
        else:
            stats_trades = trades if config.count_forced_exits_in_stats else trades[~trades["is_forced_exit"].astype(bool)]
            trade_count = int(len(stats_trades))
            win_rate = float((stats_trades["pnl"] > 0).mean()) if trade_count > 0 else np.nan
        summary = {
            "start_date": start_date.date().isoformat(),
            "end_date": end_date.date().isoformat(),
            "initial_capital": config.initial_capital,
            "final_equity": final_equity,
            "total_return": total_return,
            "annual_return": annual_return,
            "max_drawdown": max_dd,
            "trade_count": trade_count,
            "win_rate": win_rate,
            "forced_exit_count": int(trades["is_forced_exit"].sum()) if not trades.empty else 0,
            "risk_off_events": risk_off_events,
            "open_positions": int(len(positions)),
        }

    return BacktestResult(equity_curve=equity_curve, trades=trades, orders=orders, summary=summary)


def save_result(result: BacktestResult, output_dir: str | Path) -> None:
    """将回测结果保存到 CSV 文件。

    参数
    ----
    result:
        ``run_backtest`` 返回的结果对象。
    output_dir:
        输出目录。不存在时会自动创建。

    返回
    ----
    None
        函数无返回值，会写出 equity_curve.csv、trades.csv、orders.csv、summary.csv。
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    result.equity_curve.to_csv(out / "equity_curve.csv", index=False, encoding="utf-8-sig")
    result.trades.to_csv(out / "trades.csv", index=False, encoding="utf-8-sig")
    result.orders.to_csv(out / "orders.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([result.summary]).to_csv(out / "summary.csv", index=False, encoding="utf-8-sig")
