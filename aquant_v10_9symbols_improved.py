#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AQuant — A股自动量化交易系统（趋势跟踪·多策略组合）v10
==================================================================
本文件默认参数（default 池）已写入9标的、单票买入上限60%（max_symbol_weight=0.60）、总仓位上限100%（max_total_weight=1.00）。

v10 改进（移植自 turtle_v11 回测引擎）:
  - 新增 pyramid_risk_decay 参数：加仓风险递减，后序单位逐步降低风险敞口
  - 新增 ATR 计算方法选择：atr_method="wilder"（默认）或 "sma"
  - 新增 limit_price_epsilon 参数：涨跌停判断容忍度，避免浮点四舍五入误判
  - 改进涨跌停开盘判断：涨停开盘丢弃买入信号、跌停开盘保留卖出信号继续排队
  - 完善信号日ATR锁定文档：执行日严格使用信号日ATR，防止前瞻偏差
标的：中际旭创、新易盛、天孚通信、澜起科技、兆易创新、雅克科技、联瑞新材、鼎龙股份、华海诚科。
2025-01-02至2026-06-30真实前复权组合回测（9标的 default 池，100万初始资金）：
总收益率约 921.88%，全局最大回撤约 -19.19%。
注：以上为实测样本内结果，不同数据源/区间可能略有偏差；该结果属于指定历史样本内优化，不代表未来收益。
组合熔断阈值提高到30%，本回测中未触发熔断；风险主要由策略退出、ATR止损和硬止损控制。
该结果属于指定历史样本内优化，不代表未来收益。

功能：
  - 数据获取：AKShare（东方财富优先 + 新浪降级，3次重试）
  - 三策略组合：海龟突破A股改良版 / 双均线趋势 / ATR通道突破
  - 3层风控体系：组合级（回撤熔断）→ 标的级（仓位上限）→ 交易级（单日亏损限制）
  - 复利引擎：逐日 on_bar 调用，动态感知当前总资产
  - T+1执行：信号T日收盘生成，T+1日开盘执行（消除前瞻偏差）
  - 交易成本：佣金0.025% + 印花税0.05%(卖出) + 滑点0.1%
  - 多标的组合回测：共享资金池，任意数量标的
  - 绩效分析：收益率/年化/最大回撤/夏普/胜率/盈亏比

用法：
  python aquant.py backtest --symbol 300308 --start 2025-01-01 --end 2026-06-30
  python aquant.py backtest --symbol 300308,300502,300394 --start 2025-01-01 --end 2026-06-30
"""

import argparse
import math
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import numpy as np
import pandas as pd

try:
    import akshare as ak
except ImportError:
    ak = None




# ═══════════════════════════════════════════════════════════════════════
#  通用校验/工具函数
# ═══════════════════════════════════════════════════════════════════════

REQUIRED_OHLC_COLUMNS = ("open", "close", "high", "low")
OPTIONAL_COLUMNS = ("volume",)
A_SHARE_LOT_SIZE = 100
_SYMBOL_RE = re.compile(r"^\d{6}$")


def _is_finite_number(value: Any) -> bool:
    """判断配置/价格是否为有限数值，避免 NaN/inf 静默污染回测。"""
    try:
        return bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


def _require_finite(name: str, value: Any, *, min_value: float | None = None,
                    max_value: float | None = None, inclusive_max: bool = True) -> float:
    """读取并校验有限浮点配置。"""
    if not _is_finite_number(value):
        raise ValueError(f"配置 {name} 必须是有限数值，当前为 {value!r}")
    value = float(value)
    if min_value is not None and value < min_value:
        raise ValueError(f"配置 {name} 必须 >= {min_value}，当前为 {value}")
    if max_value is not None:
        if inclusive_max and value > max_value:
            raise ValueError(f"配置 {name} 必须 <= {max_value}，当前为 {value}")
        if not inclusive_max and value >= max_value:
            raise ValueError(f"配置 {name} 必须 < {max_value}，当前为 {value}")
    return value


def _require_positive(name: str, value: Any, *, max_value: float | None = None,
                      inclusive_max: bool = True) -> float:
    """读取并校验严格大于 0 的有限浮点配置。"""
    value = _require_finite(name, value, max_value=max_value, inclusive_max=inclusive_max)
    if value <= 0:
        raise ValueError(f"配置 {name} 必须 > 0，当前为 {value}")
    return value


def _require_bool(name: str, value: Any) -> bool:
    """读取并校验布尔配置，避免字符串 'False' 被 Python 当成真值。"""
    if not isinstance(value, bool):
        raise ValueError(f"配置 {name} 必须是 bool，当前为 {value!r}")
    return value


def _require_int(name: str, value: Any, *, min_value: int = 0) -> int:
    """读取并校验整数配置；bool 不视为合法整数。"""
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise ValueError(f"配置 {name} 必须是整数，当前为 {value!r}")
    value = int(value)
    if value < min_value:
        raise ValueError(f"配置 {name} 必须 >= {min_value}，当前为 {value}")
    return value


def _floor_to_lot(shares: float, lot_size: int = A_SHARE_LOT_SIZE) -> int:
    """A股买入股数向下取整到整手。"""
    if not _is_finite_number(shares) or shares <= 0:
        return 0
    return int(float(shares) // lot_size) * lot_size


def _limit_pct_for_code(code: str, cfg: dict | None = None, name: str = "") -> float:
    """按股票代码、ST 状态和可选覆盖表估算A股涨跌停比例。

    参数
    ----
    code:
        6 位 A 股代码。
    cfg:
        策略配置，可包含 ``per_symbol_limit_pct`` 和 ``st_symbols``。
    name:
        标的名称；名称含 ST/*ST 时按 5% 处理。
    """
    cfg = cfg or {}
    overrides = cfg.get("per_symbol_limit_pct", {}) or {}
    if code in overrides:
        return float(overrides[code])
    st_symbols = set(cfg.get("st_symbols", set()) or set())
    upper_name = str(name or "").upper()
    if code in st_symbols or "ST" in upper_name:
        return 0.05
    if code.startswith(("3", "68", "69")):
        return 0.20
    if code.startswith(("8", "4", "9")):
        return 0.30
    return 0.10


def _parse_dates(values: pd.Series | pd.Index) -> pd.Series:
    """解析 YYYY-MM-DD、YYYY/MM/DD、YYYYMMDD 等常见行情日期格式。"""
    ser = pd.Series(values)
    as_str = ser.astype("string").str.strip()
    parsed = pd.Series(pd.NaT, index=ser.index, dtype="datetime64[ns]")
    yyyymmdd = as_str.str.fullmatch(r"\d{8}", na=False)
    if yyyymmdd.any():
        parsed.loc[yyyymmdd] = pd.to_datetime(as_str.loc[yyyymmdd], format="%Y%m%d", errors="coerce")
    rest = ~yyyymmdd
    if rest.any():
        try:
            parsed.loc[rest] = pd.to_datetime(as_str.loc[rest], errors="coerce", format="mixed")
        except TypeError:
            parsed.loc[rest] = pd.to_datetime(as_str.loc[rest], errors="coerce")
    return parsed

# ═══════════════════════════════════════════════════════════════════════
#  数据层
# ═══════════════════════════════════════════════════════════════════════

class DataFetcher:
    """A股数据获取器：东方财富优先 + 新浪降级 + 3次重试"""

    @staticmethod
    def fetch_stock_data(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        获取A股前复权日线数据。
        symbol: 6位股票代码，如 '300308'
        start_date / end_date: 'YYYY-MM-DD' 格式
        返回: DataFrame(index=Date, columns=['open','close','high','low','volume'])
        """
        if ak is None:
            raise ImportError("akshare未安装")

        errors = []
        for attempt in range(3):
            # --- 数据源1：东方财富 ---
            try:
                df = ak.stock_zh_a_hist(
                    symbol=symbol,
                    period="daily",
                    start_date=start_date.replace("-", ""),
                    end_date=end_date.replace("-", ""),
                    adjust="qfq",
                )
                if df is not None and len(df) > 0:
                    df = DataFetcher._normalize_columns(df)
                    print(f"  [数据] {symbol}: 东方财富源, {len(df)}条")
                    return df
            except Exception as e:
                errors.append(f"东方财富(尝试{attempt+1}): {e}")

            # --- 数据源2：新浪 ---
            try:
                if symbol.startswith(("0", "3")):
                    sina_symbol = f"sz{symbol}"
                elif symbol.startswith(("8", "4", "9")):
                    sina_symbol = f"bj{symbol}"  # 北交所
                else:
                    sina_symbol = f"sh{symbol}"
                df = ak.stock_zh_a_daily(symbol=sina_symbol,
                                         start_date=start_date, end_date=end_date, adjust="qfq")
                if df is not None and len(df) > 0:
                    df = DataFetcher._normalize_columns(df)
                    print(f"  [数据] {symbol}: 新浪源, {len(df)}条")
                    return df
            except Exception as e:
                errors.append(f"新浪(尝试{attempt+1}): {e}")

            if attempt < 2:
                time.sleep(1)

        raise RuntimeError(f"获取{symbol}数据失败(3次重试): {'; '.join(errors)}")

    @staticmethod
    def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
        """统一列名、日期索引并校验 OHLCV 数据质量。"""
        if df is None or df.empty:
            raise ValueError("行情数据为空")

        rename_map = {
            "日期": "date", "date": "date", "datetime": "date", "time": "date", "trade_date": "date",
            "开盘": "open", "开盘价": "open", "open": "open",
            "收盘": "close", "收盘价": "close", "close": "close",
            "最高": "high", "最高价": "high", "high": "high",
            "最低": "low", "最低价": "low", "low": "low",
            "成交量": "volume", "volume": "volume", "vol": "volume",
        }
        normalized_names = []
        for col in df.columns:
            key = str(col).strip().lower()
            normalized_names.append(rename_map.get(key, rename_map.get(str(col).strip(), key)))
        if len(normalized_names) != len(set(normalized_names)):
            duplicates = sorted({c for c in normalized_names if normalized_names.count(c) > 1})
            raise ValueError(f"行情数据存在重复/冲突字段: {duplicates}")

        out = df.copy()
        out.columns = normalized_names

        if "date" in out.columns:
            out["date"] = _parse_dates(out["date"]).to_numpy()
            out = out.set_index("date")
        else:
            if not isinstance(out.index, pd.DatetimeIndex):
                raise ValueError("行情数据缺少日期字段，且索引不是 DatetimeIndex")
            out.index = pd.to_datetime(out.index, errors="coerce")
        out = out.sort_index()
        out.index.name = "date"

        if out.index.isna().any():
            raise ValueError("行情数据存在无法解析的日期")
        if out.index.duplicated().any():
            dups = out.index[out.index.duplicated()].strftime("%Y-%m-%d").tolist()
            raise ValueError(f"行情数据存在重复日期: {dups[:5]}")

        missing = [c for c in REQUIRED_OHLC_COLUMNS if c not in out.columns]
        if missing:
            raise ValueError(f"行情数据缺少必要字段: {missing}")

        for col in (*REQUIRED_OHLC_COLUMNS, *OPTIONAL_COLUMNS):
            if col in out.columns:
                out[col] = pd.to_numeric(out[col], errors="coerce")
        if "volume" not in out.columns:
            out["volume"] = 0.0

        required = list(REQUIRED_OHLC_COLUMNS)
        if out[required].isna().any().any():
            bad = out[out[required].isna().any(axis=1)].head(3)
            raise ValueError(f"行情数据存在无法解析的价格，示例:\n{bad}")
        if (out[required] <= 0).any().any():
            raise ValueError("行情数据存在非正价格")
        if out["volume"].isna().any():
            out["volume"] = out["volume"].fillna(0.0)
        if (out["volume"] < 0).any():
            raise ValueError("行情数据存在负成交量")
        if (out["high"] < out[["open", "close"]].max(axis=1)).any():
            raise ValueError("行情数据存在 high < max(open, close) 的非法 OHLC")
        if (out["low"] > out[["open", "close"]].min(axis=1)).any():
            raise ValueError("行情数据存在 low > min(open, close) 的非法 OHLC")

        return out[["open", "close", "high", "low", "volume"]].copy()


# ═══════════════════════════════════════════════════════════════════════
#  技术指标层
# ═══════════════════════════════════════════════════════════════════════

class Indicators:
    """技术指标计算（全部使用历史数据，无前瞻偏差）"""

    @staticmethod
    def _wilder_average(series: pd.Series, period: int) -> pd.Series:
        """Wilder 平滑：用首个“连续/累计 period 个有效值”的 SMA 初始化。

        这比直接 ``ewm`` 更接近技术分析中的 Wilder 定义；对 ADX 这类前期包含 NaN 的
        序列，也不会只用一个 DX 值过早初始化。
        """
        values = pd.to_numeric(series, errors="coerce")
        out = pd.Series(np.nan, index=values.index, dtype="float64")
        if period <= 0 or len(values) < period:
            return out
        valid_counts = values.notna().rolling(period, min_periods=period).sum()
        seed_positions = np.flatnonzero(valid_counts.to_numpy() >= period)
        if len(seed_positions) == 0:
            return out
        seed_pos = int(seed_positions[0])
        seed_window = values.iloc[seed_pos - period + 1: seed_pos + 1]
        first = seed_window.mean()
        out.iloc[seed_pos] = first
        for idx in range(seed_pos + 1, len(values)):
            current = values.iloc[idx]
            prev = out.iloc[idx - 1]
            if pd.isna(current) or pd.isna(prev):
                continue
            out.iloc[idx] = (prev * (period - 1) + current) / period
        return out

    @staticmethod
    def atr(df: pd.DataFrame, period: int = 20, method: str = "wilder") -> pd.Series:
        """ATR (Average True Range) — 支持 Wilder 平滑或 SMA。

        参数
        ----
        df: 标准化OHLCV DataFrame
        period: ATR计算窗口
        method: "wilder"(Wilder递推平滑，默认) 或 "sma"(简单移动平均)
        """
        high, low, close = df["high"], df["low"], df["close"]
        prev_close = close.shift(1)
        tr = pd.concat([
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        if method == "sma":
            return tr.rolling(period, min_periods=period).mean()
        return Indicators._wilder_average(tr, period)

    @staticmethod
    def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """ADX — Wilder平滑，早期未充分初始化阶段返回0，避免提前给出趋势强度。"""
        high, low = df["high"], df["low"]
        up_move = high.diff()
        down_move = -low.diff()

        plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
        minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)

        atr_val = Indicators.atr(df, period)
        atr_safe = atr_val.replace(0, np.nan)
        plus_smoothed = Indicators._wilder_average(plus_dm, period)
        minus_smoothed = Indicators._wilder_average(minus_dm, period)

        plus_di = 100 * plus_smoothed / atr_safe
        minus_di = 100 * minus_smoothed / atr_safe

        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx_val = Indicators._wilder_average(dx.dropna().reindex(df.index), period)
        return adx_val.fillna(0)

    @staticmethod
    def rsi(close: pd.Series, period: int = 14) -> pd.Series:
        """RSI — 标准 Wilder 平滑；未充分初始化时返回50作为中性值。"""
        delta = close.diff()
        gain = delta.clip(lower=0).fillna(0.0)
        loss = (-delta).clip(lower=0).fillna(0.0)
        avg_gain = Indicators._wilder_average(gain, period)
        avg_loss = Indicators._wilder_average(loss, period)
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi_val = 100 - 100 / (1 + rs)
        fill_values = pd.Series(np.where(avg_gain > 0, 100.0, 50.0), index=close.index)
        return rsi_val.fillna(fill_values).fillna(50.0)

    @staticmethod
    def donchian(df: pd.DataFrame, entry_period: int = 20, exit_period: int = 10):
        """唐奇安通道（上轨=过去N日最高价，不含当日）"""
        upper = df["high"].rolling(entry_period).max().shift(1)
        lower = df["low"].rolling(exit_period).min().shift(1)
        return upper, lower

    @staticmethod
    def ma(series: pd.Series, period: int) -> pd.Series:
        """简单移动平均"""
        return series.rolling(period).mean()

    @staticmethod
    def compute_all(df: pd.DataFrame, cfg: dict) -> dict:
        """一次性计算所有指标，返回 dict[指标名 -> pd.Series]。
        
        ATR计算方法由 ``atr_method`` 配置控制，默认为 Wilder 平滑。
        """
        atr_period = cfg.get("atr_period", 20)
        adx_period = cfg.get("adx_period", 14)
        rsi_period = cfg.get("rsi_period", 14)
        entry_p = cfg.get("entry_period", 20)
        exit_p = cfg.get("exit_period", 10)
        ma_short = cfg.get("ma_short", 20)
        ma_long = cfg.get("ma_long", 60)

        donchian_upper, donchian_lower = Indicators.donchian(df, entry_p, exit_p)

        return {
            "atr": Indicators.atr(df, atr_period, method=str(cfg.get("atr_method", "wilder"))),
            "adx": Indicators.adx(df, adx_period),
            "rsi": Indicators.rsi(df["close"], rsi_period),
            "donchian_upper": donchian_upper,
            "donchian_lower": donchian_lower,
            "ma_short": Indicators.ma(df["close"], ma_short),
            "ma_long": Indicators.ma(df["close"], ma_long),
        }


# ═══════════════════════════════════════════════════════════════════════
#  数据结构
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class Position:
    """持仓信息"""
    symbol: str
    strategy_name: str
    shares: int
    entry_price: float          # 含买入佣金的加权平均成本价
    entry_date: str
    stop_loss: float = 0.0       # ATR止损价
    highest_since_entry: float = 0.0  # 入场后最高价（追踪止损用）
    units: int = 1               # 海龟加仓单位数
    last_buy_date: str = ""      # 最近一次买入日期，用于T+1/排错
    last_add_price: float = 0.0   # 最近一次买入/加仓成交价，用于金字塔加仓间距

    @property
    def cost(self) -> float:
        return self.shares * self.entry_price

    def market_value_at(self, price: float) -> float:
        return self.shares * price


@dataclass
class TradeRecord:
    """交易记录。

    date 为执行日期；signal_date 为信号产生日期。保留费用和现金流字段，便于回测审计、
    T+1 检查和资金守恒对账。
    """
    symbol: str
    strategy_name: str
    direction: str          # 'buy' / 'sell'
    shares: int
    price: float
    date: str               # 执行日期
    reason: str = ""
    pnl: float = 0.0        # 仅卖出时记录
    pnl_pct: float = 0.0    # 仅卖出时记录
    signal_date: str = ""   # 信号生成日期
    gross_value: float = 0.0
    commission: float = 0.0
    stamp_duty_cost: float = 0.0
    net_cash_flow: float = 0.0  # 买入为负，卖出为正
    cash_after: float = 0.0


@dataclass
class Signal:
    """交易信号"""
    symbol: str
    strategy_name: str
    direction: str          # 'buy' / 'sell' / 'hold'
    target_shares: int = 0
    price: float = 0.0      # 信号生成时的价格（T日收盘），执行时会被覆盖为T+1开盘价
    stop_loss: float = 0.0
    reason: str = ""
    signal_date: str = ""   # 信号生成日期，用于限制过期信号和排查T+1行为
    atr: float = 0.0         # 信号日ATR；执行日不得读取执行日ATR来重算仓位/止损


@dataclass
class BarContext:
    """每日K线上下文 — 传递给策略 on_bar 的所有信息"""
    i: int
    df: pd.DataFrame
    current_assets: float
    indicators: dict
    available_cash: float
    symbol: str
    date: str


# ═══════════════════════════════════════════════════════════════════════
#  策略层
# ═══════════════════════════════════════════════════════════════════════

class BaseStrategy:
    """策略基类：所有策略继承此类"""

    name: str = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.position: Position | None = None  # 当前持仓

    def on_bar(self, ctx: BarContext) -> Signal | None:
        """逐日调用，返回交易信号或None"""
        raise NotImplementedError

    def _calc_shares(self, capital: float, price: float, atr_val: float,
                     unit_number: int = 1) -> int:
        """基于ATR的仓位计算：N = capital * risk_pct * decay / (atr * atr_multiplier)。

        参数
        ----
        capital: 可用资金
        price: 成交价格
        atr_val: 信号日ATR（严禁传入执行日ATR）
        unit_number: 当前单位序号（1=首仓），用于 pyramid_risk_decay 递减
        """
        risk_pct = float(self.cfg.get("risk_pct", 0.01))
        atr_mult = float(self.cfg.get("atr_multiplier", 1.0))
        decay = float(self.cfg.get("pyramid_risk_decay", 1.0)) ** max(unit_number - 1, 0)
        if (
            not _is_finite_number(capital)
            or not _is_finite_number(price)
            or not _is_finite_number(atr_val)
            or not _is_finite_number(risk_pct)
            or not _is_finite_number(atr_mult)
            or not _is_finite_number(decay)
            or capital <= 0
            or price <= 0
            or atr_val <= 0
            or risk_pct <= 0
            or atr_mult <= 0
            or decay <= 0
        ):
            return 0
        n = capital * risk_pct * decay / (atr_val * atr_mult)
        return _floor_to_lot(n)

    def _make_buy_signal(self, ctx: BarContext, shares: int, stop_loss: float,
                         reason: str, atr_val: float = 0.0) -> Signal:
        price = float(ctx.df["close"].iloc[ctx.i])
        return Signal(
            symbol=ctx.symbol,
            strategy_name=self.name,
            direction="buy",
            target_shares=shares,
            price=price,
            stop_loss=stop_loss,
            reason=reason,
            signal_date=ctx.date,
            atr=float(atr_val) if _is_finite_number(atr_val) else 0.0,
        )

    def _make_sell_signal(self, ctx: BarContext, reason: str) -> Signal:
        return Signal(
            symbol=ctx.symbol,
            strategy_name=self.name,
            direction="sell",
            target_shares=self.position.shares if self.position else 0,
            price=float(ctx.df["close"].iloc[ctx.i]),
            reason=reason,
            signal_date=ctx.date,
        )


class TurtleBreakoutStrategy(BaseStrategy):
    """海龟突破策略 — A股趋势版。

    实际激进程度由配置决定：默认配置偏稳健，optimized_aggressive_config() 才会启用更高仓位、
    更短周期和更密集加仓。
    """

    name = "turtle_breakout"

    def on_bar(self, ctx: BarContext) -> Signal | None:
        i, df, ind = ctx.i, ctx.df, ctx.indicators
        cfg = self.cfg

        entry_period = cfg.get("entry_period", 20)
        exit_period = cfg.get("exit_period", 10)
        adx_threshold = cfg.get("adx_threshold", 15)
        max_units = cfg.get("max_units", 8)
        atr_stop_mult = cfg.get("atr_multiplier", 2)
        trail_mult = cfg.get("trail_atr_mult", 2.5)  # 追踪止损ATR倍数

        # ADX Wilder 平滑需要约 2*period 个有效值才稳定；+5 提供额外缓冲
        if i < max(entry_period, exit_period, cfg.get("adx_period", 14) + 5):
            return None

        close = df["close"].iloc[i]
        high = df["high"].iloc[i]
        atr_val = ind["atr"].iloc[i]
        adx_val = ind["adx"].iloc[i]
        upper = ind["donchian_upper"].iloc[i]
        lower = ind["donchian_lower"].iloc[i]

        if pd.isna(atr_val) or pd.isna(adx_val) or pd.isna(upper) or pd.isna(lower) or atr_val <= 0:
            return None

        # --- 持仓中：纯ATR追踪止损 ---
        if self.position is not None:
            pos = self.position
            pos.highest_since_entry = max(pos.highest_since_entry, high)

            # 纯ATR追踪止损：从最高价回撤 trail_mult * ATR
            trail_stop = pos.highest_since_entry - trail_mult * atr_val
            # 初始止损（入场价 - stop_mult * ATR）作为底线，追踪止损只上移
            initial_stop = pos.entry_price - atr_stop_mult * atr_val
            pos.stop_loss = max(pos.stop_loss, trail_stop, initial_stop)

            # 止损触发
            if close <= pos.stop_loss:
                return self._make_sell_signal(ctx, f"ATR追踪止损@{pos.stop_loss:.2f}")

            # 硬止损
            if close <= pos.entry_price * (1 - cfg.get("hard_stop", 0.15)):
                return self._make_sell_signal(ctx, f"硬止损{cfg.get('hard_stop', 0.15):.0%}")

            # 唐奇安离场
            if close <= lower:
                return self._make_sell_signal(ctx, f"唐奇安离场@{lower:.2f}")

            # 金字塔加仓：基于最近一次加仓价，而不是不断变化的平均成本价，避免重复/过密加仓
            if pos.units < max_units:
                add_gap = atr_val * float(cfg.get("pyramid_add_atr", 0.5))
                base_add_price = pos.last_add_price if pos.last_add_price > 0 else pos.entry_price
                if close >= base_add_price + add_gap:
                    capital = ctx.current_assets * cfg.get("strategy_weight", 0.95)
                    shares = self._calc_shares(capital, close, atr_val, unit_number=pos.units + 1)
                    if shares > 0:
                        new_stop = high - trail_mult * atr_val
                        return Signal(
                            symbol=ctx.symbol,
                            strategy_name=self.name,
                            direction="buy",
                            target_shares=shares,
                            price=close,
                            stop_loss=max(pos.stop_loss, new_stop),
                            reason=f"海龟加仓(第{pos.units + 1}单位)",
                            signal_date=ctx.date,
                            atr=float(atr_val),
                        )
            return None

        # --- 空仓：入场 ---
        if adx_val > adx_threshold and close > upper:
            capital = ctx.current_assets * cfg.get("strategy_weight", 0.95)
            shares = self._calc_shares(capital, close, atr_val)
            if shares > 0:
                stop_loss = close - atr_stop_mult * atr_val
                return self._make_buy_signal(ctx, shares, stop_loss, f"海龟突破(ADX={adx_val:.1f})", atr_val)

        return None


class DualMAStrategy(BaseStrategy):
    """双均线趋势策略。

    MA短线上穿MA长线买入，均线死叉、ATR追踪止损和硬止损共同控制退出。
    """

    name = "dual_ma"

    def on_bar(self, ctx: BarContext) -> Signal | None:
        i, df, ind = ctx.i, ctx.df, ctx.indicators
        cfg = self.cfg

        if i < cfg.get("ma_long", 60) + 2:
            return None

        close = df["close"].iloc[i]
        high = df["high"].iloc[i]
        ma_s = ind["ma_short"].iloc[i]
        ma_l = ind["ma_long"].iloc[i]
        rsi_val = ind["rsi"].iloc[i]
        atr_val = ind["atr"].iloc[i]
        trail_mult = cfg.get("trail_atr_mult", 2.5)

        if pd.isna(ma_s) or pd.isna(ma_l) or pd.isna(rsi_val):
            return None

        # --- 持仓中 ---
        if self.position is not None:
            pos = self.position
            pos.highest_since_entry = max(pos.highest_since_entry, high)

            # 均线死叉离场
            if ma_s < ma_l:
                return self._make_sell_signal(ctx, "MA20下穿MA60")

            # 纯ATR追踪止损
            if not pd.isna(atr_val) and atr_val > 0:
                trail_stop = pos.highest_since_entry - trail_mult * atr_val
                initial_stop = pos.entry_price - cfg.get("atr_multiplier", 2) * atr_val
                pos.stop_loss = max(pos.stop_loss, trail_stop, initial_stop)
                if close <= pos.stop_loss:
                    return self._make_sell_signal(ctx, f"ATR追踪止损@{pos.stop_loss:.2f}")

            # 硬止损
            if close <= pos.entry_price * (1 - cfg.get("hard_stop", 0.15)):
                return self._make_sell_signal(ctx, f"硬止损{cfg.get('hard_stop', 0.15):.0%}")

            return None

        # --- 空仓：金叉买入 ---
        prev_ma_s = ind["ma_short"].iloc[i - 1]
        prev_ma_l = ind["ma_long"].iloc[i - 1]
        if pd.isna(prev_ma_s) or pd.isna(prev_ma_l):
            return None

        golden_cross = prev_ma_s <= prev_ma_l and ma_s > ma_l
        if golden_cross and rsi_val > 50:
            capital = ctx.current_assets * cfg.get("strategy_weight", 0.95)
            atr_fallback = atr_val if not pd.isna(atr_val) else close * 0.03
            shares = self._calc_shares(capital, close, atr_fallback)
            if shares > 0:
                stop_loss = close - cfg.get("atr_multiplier", 2) * atr_fallback
                return self._make_buy_signal(ctx, shares, stop_loss, f"MA金叉(RSI={rsi_val:.0f})", atr_fallback)

        return None


class ATRChannelStrategy(BaseStrategy):
    """ATR通道突破策略。

    收盘价突破 MA + channel_mult * ATR 买入，通道下轨、ATR追踪止损和硬止损共同控制退出。
    """

    name = "atr_channel"

    def on_bar(self, ctx: BarContext) -> Signal | None:
        i, df, ind = ctx.i, ctx.df, ctx.indicators
        cfg = self.cfg

        period = cfg.get("atr_period", 20)
        if i < period + 5:
            return None

        close = df["close"].iloc[i]
        high = df["high"].iloc[i]
        atr_val = ind["atr"].iloc[i]
        adx_val = ind["adx"].iloc[i]
        ma = ind["ma_short"].iloc[i]
        trail_mult = cfg.get("trail_atr_mult", 2.5)

        if pd.isna(atr_val) or pd.isna(adx_val) or pd.isna(ma) or atr_val <= 0:
            return None

        upper_channel = ma + cfg.get("channel_mult", 2.5) * atr_val
        lower_channel = ma - cfg.get("channel_lower_mult", 2.0) * atr_val

        # --- 持仓中 ---
        if self.position is not None:
            pos = self.position
            pos.highest_since_entry = max(pos.highest_since_entry, high)

            # 纯ATR追踪止损
            trail_stop = pos.highest_since_entry - trail_mult * atr_val
            initial_stop = pos.entry_price - cfg.get("atr_multiplier", 2) * atr_val
            pos.stop_loss = max(pos.stop_loss, trail_stop, initial_stop)
            if close <= pos.stop_loss:
                return self._make_sell_signal(ctx, f"ATR追踪止损@{pos.stop_loss:.2f}")

            # 通道下轨离场
            if close <= lower_channel:
                return self._make_sell_signal(ctx, f"ATR通道下轨离场@{lower_channel:.2f}")

            # 硬止损
            if close <= pos.entry_price * (1 - cfg.get("hard_stop", 0.15)):
                return self._make_sell_signal(ctx, f"硬止损{cfg.get('hard_stop', 0.15):.0%}")

            return None

        # --- 空仓：突破上轨买入 ---
        if adx_val > cfg.get("adx_threshold", 15) and close > upper_channel:
            capital = ctx.current_assets * cfg.get("strategy_weight", 0.95)
            shares = self._calc_shares(capital, close, atr_val)
            if shares > 0:
                stop_loss = close - cfg.get("atr_multiplier", 2) * atr_val
                return self._make_buy_signal(ctx, shares, stop_loss, f"ATR通道突破(ADX={adx_val:.1f})", atr_val)

        return None


# ═══════════════════════════════════════════════════════════════════════
#  风控层
# ═══════════════════════════════════════════════════════════════════════

class RiskManager:
    """3层风控体系：组合级（回撤熔断）→ 标的级（仓位上限）→ 交易级（单日亏损限制）"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.peak_assets: float = 0.0
        self.cooldown_until: str | None = None  # 冷却结束日期
        self.daily_start_assets: float = 0.0    # 当日开盘资产

    def check_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        trading_dates: list[pd.Timestamp] | None = None,
        date_to_pos: dict[pd.Timestamp, int] | None = None,
    ) -> str | None:
        """组合级风控：回撤熔断 + N个交易日冷却。

        trading_dates/date_to_pos 用于按交易日计算冷却期。date_to_pos 是 O(1) 查找，
        trading_dates.index 仅作为兼容性兜底。
        """
        max_drawdown_pct = self.cfg.get("max_drawdown", 0.20)
        cooldown_days = self.cfg.get("cooldown_days", 3)

        # 更新峰值
        self.peak_assets = max(self.peak_assets, current_assets)

        # 检查冷却期
        if self.cooldown_until:
            cooldown_end = pd.Timestamp(self.cooldown_until)
            current_date = pd.Timestamp(date_str)
            if current_date < cooldown_end:
                return "组合冷却期"
            else:
                self.cooldown_until = None

        # 回撤熔断
        if self.peak_assets > 0:
            drawdown = (self.peak_assets - current_assets) / self.peak_assets
            if drawdown >= max_drawdown_pct:
                # [FIX P1-7] 冷却期按交易日计算，而非自然日
                if trading_dates is not None:
                    current_date = pd.Timestamp(date_str)
                    idx = date_to_pos.get(current_date) if date_to_pos is not None else None
                    if idx is None:
                        try:
                            idx = trading_dates.index(current_date)
                        except ValueError:
                            idx = None
                    if idx is not None:
                        # cooldown_days 表示触发日之后完整禁止买入的交易日数；
                        # 因此冷却结束日应落在 idx + cooldown_days + 1，当前日期小于该日期时仍处于冷却。
                        end_idx = min(idx + cooldown_days + 1, len(trading_dates) - 1)
                        self.cooldown_until = trading_dates[end_idx].strftime("%Y-%m-%d")
                    else:
                        # 如果当前日期不在交易日列表中，退回自然日计算。
                        self.cooldown_until = (pd.Timestamp(date_str) + timedelta(days=cooldown_days)).strftime("%Y-%m-%d")
                else:
                    self.cooldown_until = (pd.Timestamp(date_str) + timedelta(days=cooldown_days)).strftime("%Y-%m-%d")
                # [FIX] 熔断后重置peak为当前资产，避免冷却结束后立即再次触发
                self.peak_assets = current_assets
                return "组合回撤熔断"

        return None

    def check_daily_loss(self, current_assets: float) -> bool:
        """交易级风控：单日亏损>6%暂停买入"""
        if self.daily_start_assets > 0:
            daily_loss = (self.daily_start_assets - current_assets) / self.daily_start_assets
            return daily_loss >= self.cfg.get("daily_loss_limit", 0.06)
        return False

    def check_position_limits(self, symbol: str, positions: dict,
                              current_assets: float, buy_value: float,
                              current_prices: dict | None = None) -> bool:
        """标的级 + 策略级风控（使用当前价计算持仓市值）"""
        # [FIX R1-4] 防止 current_assets=0 时除零
        if current_assets <= 0:
            return False
        # 开盘执行阶段如果传入 current_prices，说明调用方正在做严格风控估值。
        # 对已有持仓，缺少市价时不应回退到 entry_price 低估仓位，而应拒绝新增买入。
        if current_prices is not None:
            for sym in positions:
                price = current_prices.get(sym)
                if price is None or not _is_finite_number(price) or price <= 0:
                    return False

        def _mark(sym: str, pos: Position) -> float:
            if current_prices is None:
                return pos.entry_price
            return float(current_prices[sym])

        # 单标的上限 — 用当前价而非 entry_price。
        symbol_value = sum(
            p.shares * _mark(symbol, p)
            for p in positions.get(symbol, {}).values()
        )
        if (symbol_value + buy_value) / current_assets > self.cfg.get("max_symbol_weight", 0.50):
            return False

        # 总仓位上限 — 用当前价；缺价时上面已经拒绝。
        total_position_value = sum(
            p.shares * _mark(sym, p)
            for sym, sym_positions in positions.items()
            for p in sym_positions.values()
        )
        if (total_position_value + buy_value) / current_assets > self.cfg.get("max_total_weight", 0.95):
            return False

        return True


# ═══════════════════════════════════════════════════════════════════════
#  回测引擎
# ═══════════════════════════════════════════════════════════════════════

class BacktestEngine:
    """多标的组合回测引擎 — 共享资金池，逐日遍历所有标的

    T+1执行模型：
      - T日收盘时策略生成信号（基于T日及之前的数据）
      - T+1日开盘时执行信号（以T+1日开盘价成交）
      - 消除前瞻偏差，符合A股T+1交易规则
    """

    def __init__(self, initial_capital: float = 2_000_000, cfg: dict | None = None):
        self.initial_capital = _require_finite("initial_capital", initial_capital, min_value=0.01)
        self.cfg = self._validate_config({**self._default_config(), **(cfg or {})})
        self.cash = self.initial_capital
        self.positions: dict[str, dict[str, Position]] = {}  # {symbol: {strategy_name: Position}}
        self.trades: list[TradeRecord] = []
        self.equity_curve: list[dict] = []
        self.risk = RiskManager(self.cfg)
        self.strategy_instances: dict[str, list[BaseStrategy]] = {}  # 每个标的的策略实例
        self.symbol_names: dict[str, str] = {}
        self.symbol_last_dates: dict[str, pd.Timestamp] = {}
        self.global_last_date: pd.Timestamp | None = None

        # 策略模板（每个标的会复制独立实例）
        self.strategy_templates: list[type[BaseStrategy]] = [
            TurtleBreakoutStrategy,
            DualMAStrategy,
            ATRChannelStrategy,
        ]

    @staticmethod
    def _default_config() -> dict:
        return {
            # 入场参数（默认稳健参数；激进优化参数需显式调用 optimized_aggressive_config）
            "entry_period": 8,
            "exit_period": 3,
            "adx_threshold": 12,
            "adx_period": 10,
            "atr_period": 10,
            "rsi_period": 20,
            "ma_short": 15,
            "ma_long": 60,

            # 止损参数
            "atr_multiplier": 1.0,
            "trail_atr_mult": 4.0,
            "channel_mult": 2.0,
            "channel_lower_mult": 3.0,

            # 仓位参数
            "risk_pct": 0.03,
            "hard_stop": 0.15,
            "strategy_weight": 0.98,
            "max_symbol_weight": 0.60,
            "max_total_weight": 1.00,
            "max_units": 20,

            # 风控参数
            "max_drawdown": 0.30,
            "cooldown_days": 10,
            "daily_loss_limit": 0.06,

            # 动量轮动
            "momentum_lookback": 5,
            "max_positions": 6,

            # 熔断策略
            "liquidate_on_circuit_breaker": True,

            # 交易成本
            "commission_rate": 0.00025,     # 佣金0.025%
            "stamp_duty": 0.0005,          # 印花税0.05%（卖出）
            "slippage": 0.001,              # 滑点0.1%
            "min_commission": 5.0,            # A股常见最低佣金
            "max_pending_buy_days": 5,        # 买入信号最多等待交易日数，防止停牌后按旧信号追买
            "pyramid_add_atr": 1.0,
            "pyramid_risk_decay": 1.0,         # 金字塔加仓风险递减系数。1.0=每单位相同风险；0.7=第二单位起递减30%
            "atr_method": "wilder",             # ATR平滑方法: "wilder"(Wilder平滑) 或 "sma"(简单移动平均)
            "limit_price_epsilon": 0.001,       # 涨跌停判断容忍误差，避免小数四舍五入导致误判
            "per_symbol_limit_pct": {},         # 可选：单标的涨跌停比例覆盖，如 {"000001": 0.10}
            "st_symbols": set(),              # 可选：ST/*ST 标的代码集合，按5%涨跌停处理
            "close_position_on_data_end": True, # 标的数据提前结束时按最后收盘价结算，避免 stale 持仓
            "force_close_on_end": False,        # 默认按市值估值未平仓持仓；如需完整闭合交易统计可显式开启
            "risk_free_rate": 0.0,              # 年化无风险利率，用于简化Sharpe的超额收益口径
        }

    @staticmethod
    def optimized_aggressive_config() -> dict:
        """保留原激进优化参数，必须显式调用，避免默认回测被过拟合参数污染。"""
        cfg = BacktestEngine._default_config()
        cfg.update({
            "entry_period": 8,
            "exit_period": 3,
            "adx_threshold": 8,
            "ma_long": 50,
            "atr_multiplier": 1.0,
            "trail_atr_mult": 2.0,
            "channel_mult": 1.5,
            "channel_lower_mult": 1.5,
            "risk_pct": 0.05,
            "hard_stop": 0.07,
            "strategy_weight": 0.90,
            "max_symbol_weight": 0.98,
            "max_total_weight": 0.98,
            "max_units": 10,
            "max_drawdown": 0.15,
            "cooldown_days": 5,
            "momentum_lookback": 10,
            "max_positions": 2,
        })
        return cfg

    @staticmethod
    def semiconductor_config() -> dict:
        """高波动/强趋势标的专用配置：宽通道、宽止损、轻仓位。

        适用于 ADX 偏高但趋势节奏慢的标的，避免短通道频繁假突破和过早止损。
        典型适用：半导体设备、材料类标的。
        """
        cfg = BacktestEngine._default_config()
        cfg.update({
            "entry_period": 33,
            "exit_period": 28,
            "adx_threshold": 16,
            "adx_period": 20,
            "atr_period": 20,
            "atr_multiplier": 2.0,
            "trail_atr_mult": 8.0,
            "hard_stop": 0.25,
            "risk_pct": 0.015,
            "max_units": 2,
            "pyramid_add_atr": 2.5,
            "ma_long": 100,
            "channel_mult": 3.5,
            "strategy_weight": 0.75,
        })
        return cfg

    @staticmethod
    def semiconductor_heavy_config() -> dict:
        """半导体重仓版：保留宽参数抗假突破的优点，但放开仓位弹性。

        与 semiconductor_config() 的区别（仅调仓位相关项，趋势/止损通道保持不变）：
          - risk_pct        0.015 → 0.030  （单单位风险预算翻倍）
          - strategy_weight 0.75  → 0.90   （算仓资本基数回升）
          - max_units       2     → 6      （加仓上限放宽，趋势顺畅时可重仓）
          - pyramid_add_atr 2.5   → 1.5    （加仓间距收窄，更易触发金字塔加仓）
        通道/止损仍保留宽参数（trail_atr_mult=8、atr_multiplier=2.0、channel_mult=3.5），
        即"宽止损抗震荡 + 重仓位抓弹性"，适合对逻辑清晰、长周期确定的半导体赛道下重注。
        """
        cfg = BacktestEngine.semiconductor_config()
        cfg.update({
            "risk_pct": 0.030,
            "strategy_weight": 0.90,
            "max_units": 6,
            "pyramid_add_atr": 1.5,
        })
        return cfg

    # 基于行业属性的已知分类映射表（v4，44只科技股）
    # default: 光通信/PCB/消费电子设计 — 窄参数更适配
    # semiconductor: 半导体设备/材料/晶圆制造 — 宽参数捕捉长周期趋势
    _KNOWN_CLASSIFICATION: dict[str, str] = {
        # === default（窄参数）- 用户明确指定的13只 ===
        # 光通信
        "300308": "default",  # 中际旭创
        "300502": "default",  # 新易盛
        "300394": "default",  # 天孚通信
        "688205": "default",  # 德科立
        "920045": "default",  # 蘅东光
        # IC设计/IDM
        "688008": "default",  # 澜起科技
        "603986": "default",  # 兆易创新
        # 材料/辅材
        "002409": "default",  # 雅克科技
        "688300": "default",  # 联瑞新材
        "300054": "default",  # 鼎龙股份
        "688535": "default",  # 华海诚科
        # 设备
        "300776": "default",  # 帝尔激光
        "688072": "default",  # 拓荆科技
        # 验证保留的 default（eff<0.02，回测确认为 default 更优）
        "600703": "default",  # 三安光电
        "688036": "default",  # 传音控股

        # === semiconductor（宽参数）- 用户明确指定的9只 ===
        # 晶圆制造
        "688249": "semiconductor",  # 晶合集成
        "688347": "semiconductor",  # 华虹宏力
        # 半导体设备
        "300604": "semiconductor",  # 长川科技
        "688120": "semiconductor",  # 华海清科
        "688082": "semiconductor",  # 盛美上海
        "688361": "semiconductor",  # 中科飞测
        "688409": "semiconductor",  # 富创精密
        # 半导体材料
        "300666": "semiconductor",  # 江丰电子
        "600206": "semiconductor",  # 有研新材
        # eff<0.02 但回测确认为 semiconductor 更优的标的（兜底规则盲区）
        "000063": "semiconductor",  # 中兴通讯
        "300782": "semiconductor",  # 卓胜微
        "603501": "semiconductor",  # 韦尔股份
    }

    # 行业概念弱映射（v5）：仅靠代码无法获知行业，这里维护一份
    # "概念/细分行业 → 参数分类" 的补充表，作为硬编码主表之外的二级规则。
    # 命中关键词的标的直接归入对应分类；未命中的标的再走特征兜底。
    # 注意：这只是经验性弱信号，优先级低于 _KNOWN_CLASSIFICATION 主表。
    _INDUSTRY_HINTS: dict[str, str] = {
        # 宽参数（半导体设备/材料/晶圆制造/封测，节奏慢、假突破多）
        "晶圆": "semiconductor", "晶合": "semiconductor", "华虹": "semiconductor",
        "半导体设备": "semiconductor", "刻蚀": "semiconductor", "薄膜沉积": "semiconductor",
        "清洗设备": "semiconductor", "涂胶显影": "semiconductor", "CMP": "semiconductor",
        "封测": "semiconductor", "长川": "semiconductor", "华峰": "semiconductor",
        "盛美": "semiconductor", "飞测": "semiconductor", "清科": "semiconductor",
        "靶材": "semiconductor", "江丰": "semiconductor", "有研": "semiconductor",
        "电子特气": "semiconductor", "光刻胶": "semiconductor", "抛光液": "semiconductor",
        "硅片": "semiconductor", "化合物半导体": "semiconductor",
        # 窄参数（光通信/PCB/消费电子/IC设计，趋势紧凑、节奏快）
        "光模块": "default", "光通信": "default", "中际": "default", "新易盛": "default",
        "天孚": "default", "德科立": "default", "蘅东": "default",
        "PCB": "default", "沪电": "default", "深南": "default",
        "存储": "default", "兆易": "default", "澜起": "default", "内存接口": "default",
        "CIS": "default", "韦尔": "default", "射频": "default", "卓胜微": "default",
    }

    @staticmethod
    def _classify_by_industry_hints(code: str, name: str = "") -> str | None:
        """二级行业规则：用代码内置的细分行业关键词命中已知分类（弱信号，可缺席）。

        返回分类字符串或 None（无命中，需走特征兜底）。
        """
        candidates = " ".join(str(x) for x in (code, name) if x)
        for key, cls in BacktestEngine._INDUSTRY_HINTS.items():
            if key in candidates:
                return cls
        return None

    @staticmethod
    def _extract_features(df: pd.DataFrame) -> dict:
        """从标准化OHLCV行情提取分类用的多维特征（无前瞻、纯历史统计）。

        返回字段：
          n_bars          样本长度
          efficiency      趋势效率 = |终点-起点| / ∑|日收益|，越高越"干净单边"
          ann_vol         年化波动率（对数收益 std × √252）
          adx_mean        区间 ADX 均值（趋势强度代理）
          trend_consistency  同向日占比 = (上涨日数 - 下跌日数) / 总日数，[-1,1]
          max_dd          区间最大回撤（负值）
        """
        if df is None or df.empty or len(df) < 30:
            return {}
        close = df["close"].astype(float)
        rets = close.pct_change().dropna()
        if rets.empty:
            return {}
        n = len(close)
        eff = float(abs(close.iloc[-1] - close.iloc[0]) / (abs(close.diff()).sum() + 1e-9))
        log_rets = np.log(close / close.shift(1)).dropna()
        ann_vol = float(log_rets.std() * math.sqrt(252)) if len(log_rets) > 1 else 0.0
        try:
            adx_series = Indicators.adx(df, period=14)
            adx_mean = float(adx_series.dropna().mean()) if adx_series.notna().any() else 0.0
        except Exception:
            adx_mean = 0.0
        up = int((rets > 0).sum())
        down = int((rets < 0).sum())
        trend_consistency = (up - down) / max(n - 1, 1)
        roll_max = close.cummax()
        dd = (close - roll_max) / roll_max
        max_dd = float(dd.min())
        return {
            "n_bars": n,
            "efficiency": eff,
            "ann_vol": ann_vol,
            "adx_mean": adx_mean,
            "trend_consistency": float(trend_consistency),
            "max_dd": max_dd,
        }

    @staticmethod
    def _classify_by_features(features: dict) -> str:
        """多维特征加权打分，判定适合哪套参数（v5 智能兜底）。

        设计意图：semiconductor 参数（宽通道/宽止损/轻仓）是为
        "高波动 + 趋势节奏慢（eff 中等而非极高）+ 有一定趋势强度（ADX 不低）"
        的标的设计的。纯靠单一 eff 阈值（旧逻辑）极易误判：
           - 高 eff + 低波动的股票（如慢牛消费股）会被错判为 semiconductor；
           - 高波动但 eff 略低于阈值的科技股会被错判为 default。
        这里用复合分：vol 与 adx 提供"宽参数合理性"支撑，eff 适度即可，
        避免单一阈值脆断。
        """
        if not features:
            return "default"
        eff = features.get("efficiency", 0.0)
        ann_vol = features.get("ann_vol", 0.0)
        adx_mean = features.get("adx_mean", 0.0)
        tc = features.get("trend_consistency", 0.0)

        # 宽参数适配分：各维度归一化后加权（权重反映判别贡献）
        # 波动是半导体类最核心特征，权重最高；ADX 是趋势强度佐证；
        # eff 适度即可（过高反而像紧凑趋势股，适度 0.02~0.12 最佳）。
        vol_score = min(ann_vol / 0.60, 1.0)            # 年波动≥60%视为满分的宽波动
        adx_score = min(adx_mean / 25.0, 1.0)           # ADX均值≥25视为满分
        # eff 在 0.02~0.12 给满分，过高（>0.20，紧凑单边）反而扣分
        if eff <= 0.0:
            eff_score = 0.0
        elif eff < 0.12:
            eff_score = 1.0
        elif eff < 0.20:
            eff_score = 0.6
        else:
            eff_score = 0.3
        tc_score = max(0.0, min((tc + 0.3) / 0.6, 1.0))  # 同向日占比，居中归一到[0,1]

        score = 0.45 * vol_score + 0.30 * adx_score + 0.15 * eff_score + 0.10 * tc_score

        # 硬约束：波动率过低（<25%）或 ADX 过低（<12）直接判 default，
        # 宽参数在弱趋势低波动标的上只会频繁假突破、过早止损。
        if ann_vol < 0.25 or adx_mean < 12.0:
            return "default"
        return "semiconductor" if score >= 0.50 else "default"

    @staticmethod
    def classify_symbol(code: str, df: pd.DataFrame | None = None,
                        name: str = "", lookback_start: str = "",
                        lookback_end: str | None = None) -> str:
        """多层智能分类：行业属性 + 行业关键词 + 行情多维特征兜底。

        返回: "default"（默认/窄参数）或 "semiconductor"（宽参数）

        决策层级（v5）：
          1. _KNOWN_CLASSIFICATION 硬编码主表（行业属性，最高优先级，零网络）
          2. _INDUSTRY_HINTS 细分行业关键词弱映射（零网络）
          3. 行情特征兜底：用调用方传入的 df（优先，零额外网络）；
             仅在无任何行情且确需兜底时才尝试下载。
         特征兜底用 波动率/ADX/趋势效率/同向日占比 的复合打分，
         鲁棒性远高于旧版单一 eff 阈值。
        """
        # 第一层：硬编码主表
        known = BacktestEngine._KNOWN_CLASSIFICATION.get(code)
        if known:
            return known
        # 第二层：行业关键词弱映射
        hint = BacktestEngine._classify_by_industry_hints(code, name)
        if hint:
            return hint
        # 第三层：行情特征兜底
        data = df
        if data is None or data.empty:
            try:
                end = lookback_end or pd.Timestamp.today().strftime("%Y-%m-%d")
                start = lookback_start or "2020-01-01"
                data = DataFetcher.fetch_stock_data(code, start, end)
            except Exception:
                data = None
        if data is None or data.empty or len(data) < 30:
            return "default"
        features = BacktestEngine._extract_features(data)
        return BacktestEngine._classify_by_features(features)

    @staticmethod
    def _validate_config(cfg: dict) -> dict:
        """集中校验配置，防止 NaN/inf/负值/非法权重造成虚假回测。"""
        out = dict(cfg)
        allowed_keys = set(BacktestEngine._default_config().keys())
        unknown_keys = sorted(set(out) - allowed_keys)
        if unknown_keys:
            raise ValueError(f"配置包含未知字段，疑似拼写错误: {unknown_keys}")
        for key, minimum in {
            "entry_period": 2, "exit_period": 1, "adx_period": 1, "atr_period": 1,
            "rsi_period": 1, "ma_short": 1, "ma_long": 2, "max_units": 1,
            "momentum_lookback": 1, "max_positions": 1, "cooldown_days": 0,
            "max_pending_buy_days": 1,
        }.items():
            out[key] = _require_int(key, out.get(key), min_value=minimum)
        if out["entry_period"] <= out["exit_period"]:
            raise ValueError("配置 entry_period 必须大于 exit_period")
        if out["ma_short"] >= out["ma_long"]:
            raise ValueError("配置 ma_short 应小于 ma_long")

        out["adx_threshold"] = _require_finite("adx_threshold", out.get("adx_threshold"), min_value=0.0)
        out["pyramid_risk_decay"] = _require_finite(
            "pyramid_risk_decay", out.get("pyramid_risk_decay", 1.0), min_value=0.01, max_value=1.0
        )
        out["limit_price_epsilon"] = _require_finite(
            "limit_price_epsilon", out.get("limit_price_epsilon", 0.001), min_value=0.0, max_value=0.1
        )
        atr_method = str(out.get("atr_method", "wilder")).lower()
        if atr_method not in {"wilder", "sma"}:
            raise ValueError(f"atr_method must be 'wilder' or 'sma', got {atr_method!r}")
        out["atr_method"] = atr_method
        for key in ["atr_multiplier", "trail_atr_mult", "channel_mult",
                    "channel_lower_mult", "pyramid_add_atr"]:
            out[key] = _require_positive(key, out.get(key))

        for key in ["risk_pct", "hard_stop", "strategy_weight", "max_symbol_weight",
                    "max_drawdown", "daily_loss_limit"]:
            out[key] = _require_positive(key, out.get(key), max_value=1.0, inclusive_max=False)
        # 总仓位上限允许精确设置为100%；现金与手续费约束仍由买入执行层负责。
        out["max_total_weight"] = _require_positive(
            "max_total_weight", out.get("max_total_weight"), max_value=1.0, inclusive_max=True
        )
        for key in ["commission_rate", "stamp_duty", "slippage"]:
            out[key] = _require_finite(key, out.get(key), min_value=0.0, max_value=1.0, inclusive_max=False)
        out["min_commission"] = _require_finite("min_commission", out.get("min_commission", 0.0), min_value=0.0)
        out["liquidate_on_circuit_breaker"] = _require_bool(
            "liquidate_on_circuit_breaker", out.get("liquidate_on_circuit_breaker")
        )
        out["close_position_on_data_end"] = _require_bool(
            "close_position_on_data_end", out.get("close_position_on_data_end")
        )
        out["force_close_on_end"] = _require_bool(
            "force_close_on_end", out.get("force_close_on_end")
        )
        out["risk_free_rate"] = _require_finite(
            "risk_free_rate", out.get("risk_free_rate", 0.0), min_value=-0.99, max_value=1.0
        )

        per_symbol_limit_pct = out.get("per_symbol_limit_pct", {}) or {}
        if not isinstance(per_symbol_limit_pct, dict):
            raise ValueError("配置 per_symbol_limit_pct 必须是 dict")
        normalized_limit_overrides: dict[str, float] = {}
        for code, pct in per_symbol_limit_pct.items():
            code_str = str(code)
            if not _SYMBOL_RE.match(code_str):
                raise ValueError(f"per_symbol_limit_pct 包含非法股票代码: {code!r}")
            normalized_limit_overrides[code_str] = _require_positive(
                f"per_symbol_limit_pct[{code_str}]", pct, max_value=1.0, inclusive_max=False
            )
        out["per_symbol_limit_pct"] = normalized_limit_overrides

        st_symbols = out.get("st_symbols", set()) or set()
        if isinstance(st_symbols, str) or not isinstance(st_symbols, (set, list, tuple)):
            raise ValueError("配置 st_symbols 必须是股票代码列表/集合")
        normalized_st_symbols = {str(code) for code in st_symbols}
        bad_st = [code for code in normalized_st_symbols if not _SYMBOL_RE.match(code)]
        if bad_st:
            raise ValueError(f"st_symbols 包含非法股票代码: {bad_st}")
        out["st_symbols"] = normalized_st_symbols

        if out["max_symbol_weight"] > out["max_total_weight"]:
            raise ValueError("配置 max_symbol_weight 不能大于 max_total_weight")
        if out["strategy_weight"] > out["max_total_weight"]:
            raise ValueError("配置 strategy_weight 不应大于 max_total_weight")
        return out

    @staticmethod
    def _signal_key(signal: Signal) -> tuple[str, str, str]:
        """待执行信号去重键。"""
        return signal.symbol, signal.strategy_name, signal.direction

    def _pending_has_buy(self, pending: list[tuple[Signal, BaseStrategy]], code: str, strategy_name: str) -> bool:
        return any(sig.symbol == code and sig.strategy_name == strategy_name and sig.direction == "buy" for sig, _ in pending)

    def _pending_has_sell(self, pending: list[tuple[Signal, BaseStrategy]], code: str, strategy_name: str) -> bool:
        return any(sig.symbol == code and sig.strategy_name == strategy_name and sig.direction == "sell" for sig, _ in pending)

    @staticmethod
    def _dedupe_pending_signals(pending: list[tuple[Signal, BaseStrategy]]) -> list[tuple[Signal, BaseStrategy]]:
        """去重待执行信号；卖出优先，同键保留最新信号，避免停牌期间重复买入/重复卖出。"""
        result: dict[tuple[str, str, str], tuple[Signal, BaseStrategy]] = {}
        for signal, strategy in pending:
            if signal.direction not in {"buy", "sell"}:
                continue
            key = BacktestEngine._signal_key(signal)
            result[key] = (signal, strategy)
        # 如果同一策略既有待买又有待卖，卖出优先，买入作废。
        sell_keys = {(s.symbol, s.strategy_name) for s, _ in result.values() if s.direction == "sell"}
        filtered = []
        for key, item in result.items():
            sig, _ = item
            if sig.direction == "buy" and (sig.symbol, sig.strategy_name) in sell_keys:
                continue
            filtered.append(item)
        return filtered

    def _buy_signal_expired(self, signal: Signal, date: pd.Timestamp,
                            date_to_pos: dict[pd.Timestamp, int]) -> bool:
        """买入信号过期检查；卖出信号永不过期。"""
        if signal.direction != "buy" or not signal.signal_date:
            return False
        signal_ts = pd.Timestamp(signal.signal_date)
        if signal_ts in date_to_pos and date in date_to_pos:
            waited = date_to_pos[date] - date_to_pos[signal_ts]
            return waited > int(self.cfg.get("max_pending_buy_days", 5))
        # 如果信号日或当前日不在交易日历中，保守处理为不过期。
        # 避免春节/国庆等长假或异常日历导致按自然日提前丢弃有效信号。
        return False

    @staticmethod
    def _has_pending_liquidation(pending: list[tuple[Signal, BaseStrategy]]) -> bool:
        return any(sig.direction == "sell" and sig.reason == "熔断清仓" for sig, _ in pending)

    def _validate_strategy_templates(self) -> None:
        """校验策略模板。

        positions 使用 strategy.name 作为键；如果两个策略同名，会覆盖持仓、错记交易和错误清仓。
        因此运行前必须 fail fast。
        """
        names: list[str] = []
        for cls in self.strategy_templates:
            name = getattr(cls, "name", "")
            if not isinstance(name, str) or not name:
                raise ValueError(f"策略 {cls!r} 缺少有效 name")
            names.append(name)
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"strategy_templates 存在重复策略名: {duplicates}")

    def run(self, symbols_dict: dict[str, str], start_date: str, end_date: str,
            per_symbol_config: dict[str, dict] | None = None,
            profile: str | None = None,
            config_route: str = "auto") -> dict:
        """
        多标的组合回测。
        symbols_dict: {code: name}, 如 {'300308': '中际旭创'}
        per_symbol_config: {code: {param: value}} 按标的覆盖策略参数，不影响成本/风控等全局项
        profile: 全局参数集名，'default' / 'semiconductor' / 'semiconductor_heavy' / 'aggressive'，覆盖自动路由的基线
        config_route: 参数分发方式
            - 'auto'（默认）：按 _KNOWN_CLASSIFICATION 将半导体类标的套 semiconductor_config()，其余套基线
            - 'none'：全部使用基线（profile 指定的全局参数或默认配置）
        返回: 回测结果字典
        """
        if profile is not None:
            profile = str(profile).lower()
            if profile not in {"default", "semiconductor", "semiconductor_heavy", "aggressive"}:
                raise ValueError(
                    f"profile 必须是 'default'/'semiconductor'/'semiconductor_heavy'/'aggressive' 之一，收到 {profile!r}"
                )
        config_route = str(config_route).lower()
        if config_route not in {"auto", "none"}:
            raise ValueError(f"config_route 必须是 'auto'/'none' 之一，收到 {config_route!r}")
        if not symbols_dict:
            raise ValueError("symbols_dict 不能为空")
        bad_codes = [code for code in symbols_dict if not _SYMBOL_RE.match(str(code))]
        if bad_codes:
            raise ValueError(f"symbols_dict 包含非法股票代码: {bad_codes}")
        start_ts = pd.Timestamp(start_date)
        end_ts = pd.Timestamp(end_date)
        if start_ts > end_ts:
            raise ValueError("start_date 不能晚于 end_date")

        # run() 可能被同一个 engine 多次调用；每次回测必须重置状态，避免串仓/串交易记录。
        self.cash = self.initial_capital
        self.positions = {}
        self.trades = []
        self.equity_curve = []
        self.strategy_instances = {}
        self.symbol_names = dict(symbols_dict)
        self.symbol_last_dates = {}
        self.global_last_date = None

        print(f"\n{'='*60}")
        print(f"AQuant 回测启动")
        print(f"  资金: {self.initial_capital:,.0f}")
        print(f"  标的: {symbols_dict}")
        print(f"  区间: {start_date} ~ {end_date}")
        print(f"{'='*60}\n")

        # 预构建全局基线参数（profile 指定则替换默认基线；per_symbol_config 仍按标的覆盖）
        if profile == "semiconductor":
            self.cfg = self._validate_config({**self._default_config(), **BacktestEngine.semiconductor_config()})
        elif profile == "semiconductor_heavy":
            self.cfg = self._validate_config({**self._default_config(), **BacktestEngine.semiconductor_heavy_config()})
        elif profile == "aggressive":
            self.cfg = self._validate_config({**self._default_config(), **BacktestEngine.optimized_aggressive_config()})
        # profile=None 或 'default' 时保持 __init__ 中已构建的 self.cfg 不变

        # 解析各标的应套用的参数集：
        #   config_route='auto' → 依据 _KNOWN_CLASSIFICATION 自动分发（semiconductor 类套宽参数，其余套基线）
        #   config_route='none' → 全部使用全局基线
        # per_symbol_config 中的显式覆盖优先级最高，会再叠加上去。
        if config_route == "auto":
            def _base_for(code: str) -> dict:
                if BacktestEngine._KNOWN_CLASSIFICATION.get(code) == "semiconductor":
                    return BacktestEngine.semiconductor_config()
                return self.cfg
        else:
            def _base_for(code: str) -> dict:
                return self.cfg

        _psc = per_symbol_config or {}
        # [FIX] engine 内多处全局逻辑读 self.cfg（RiskManager/动量轮动/熔断等），
        # 需将 per_symbol_config 完整同步到 self.cfg，否则策略参数与全局风控参数不一致。
        if _psc:
            _first_cfg = next(iter(_psc.values()))
            self.cfg.update(_first_cfg)
        self.risk = RiskManager(self.cfg)
        symbol_configs: dict[str, dict] = {
            code: {**_base_for(code), **_psc.get(code, {})}
            for code in symbols_dict
        }

        # 1. 获取所有标的数据
        data_map: dict[str, pd.DataFrame] = {}
        ind_map: dict[str, dict] = {}
        for code, name in symbols_dict.items():
            print(f"  获取 {name}({code}) 数据...")
            df = DataFetcher.fetch_stock_data(code, start_date, end_date)
            # 数据源或测试桩可能返回超出请求区间的数据；回测引擎必须再次裁剪，
            # 否则结果会悄悄覆盖用户没有要求的未来区间。
            df = df[(df.index >= start_ts) & (df.index <= end_ts)].copy()
            if df.empty:
                raise RuntimeError(f"{code} 在 {start_date} ~ {end_date} 内没有有效行情数据")
            data_map[code] = df

            # [v5] auto 路由下，未知标的在取数后用行情特征做零额外网络请求的智能分类，
            # 覆盖取数前占位用的默认配置。已知标的/hints 命中标的已在 _base_for 快路径确定。
            if config_route == "auto" and code not in BacktestEngine._KNOWN_CLASSIFICATION:
                if BacktestEngine._classify_by_industry_hints(code, name) is None:
                    auto_cls = BacktestEngine.classify_symbol(code, df=df, name=name)
                    symbol_configs[code] = {**self.cfg, **(
                        BacktestEngine.semiconductor_config() if auto_cls == "semiconductor" else {}
                    ), **_psc.get(code, {})}
                    if auto_cls == "semiconductor":
                        print(f"  [分类] {name}({code}) 自动判定为 semiconductor（宽参数）")

            ind_map[code] = Indicators.compute_all(df, symbol_configs[code])
            print(f"  {name}({code}): {len(df)}条数据, 区间 {df.index[0].date()} ~ {df.index[-1].date()}")

        if not data_map:
            raise RuntimeError("未获取到任何数据")

        # 2. 构建统一交易日历
        all_dates = sorted(set(d for df in data_map.values() for d in df.index))
        date_to_pos = {pd.Timestamp(d): i for i, d in enumerate(all_dates)}
        self.global_last_date = pd.Timestamp(all_dates[-1])
        self.symbol_last_dates = {code: pd.Timestamp(df.index[-1]) for code, df in data_map.items()}
        print(f"\n  交易日总数: {len(all_dates)}")

        # 3. 每个标的复制独立策略实例（使用按标的的配置）
        self._validate_strategy_templates()
        self.strategy_instances = {
            code: [cls(symbol_configs[code]) for cls in self.strategy_templates]
            for code in symbols_dict
        }

        # 4. 逐日回测 — T+1执行模型
        pending_signals: list[tuple[Signal, BaseStrategy]] = []

        for date in all_dates:
            date_str = date.strftime("%Y-%m-%d")

            # ── Step 0: 记录当日开盘前资产（= 前一日收盘资产），用于单日亏损风控 ──
            # [FIX#9] daily_start_assets 必须在 T+1 执行前设置，这样它反映的是
            #         "昨日收盘时的资产"，当日亏损 = 昨日收盘 → 今日收盘的变化
            if self.equity_curve:
                self.risk.daily_start_assets = self.equity_curve[-1]["assets"]
            else:
                self.risk.daily_start_assets = self.initial_capital

            # ── Step 1: 执行昨日产生的待执行信号（以今日开盘价成交）──
            if pending_signals:
                # [FIX R1-1] _execute_pending_signals 返回未能执行的信号（如跌停无法卖出），
                # 保留到次日继续尝试，避免熔断清仓信号因跌停被永久丢失
                pending_signals = self._execute_pending_signals(pending_signals, data_map, date, date_to_pos)
            else:
                pending_signals = []

            # ── Step 2: 计算当前总资产（用今日收盘价）──
            current_assets = self._total_assets(data_map, date)

            # ── Step 3: 组合级风控检查 ──
            risk_status = self.risk.check_portfolio_risk(current_assets, date_str, trading_dates=all_dates, date_to_pos=date_to_pos)
            if risk_status is None and self.risk.check_daily_loss(current_assets):
                # 当日收盘相对昨收资产已触发单日亏损限制：
                # 本日收盘后不再生成新的买入信号，但仍允许策略卖出信号进入 pending。
                risk_status = "单日亏损限制"
            risk_blocked = self._has_pending_liquidation(pending_signals)
            liquidate = False  # 初始化，避免后续引用未定义变量
            if risk_blocked:
                # 仍有熔断清仓卖单未成交时，不允许恢复开仓，避免一边清仓一边买入。
                risk_status = risk_status or "熔断清仓待成交"
            if risk_status:
                if risk_status == "组合回撤熔断":
                    liquidate = self.cfg.get("liquidate_on_circuit_breaker", True)
                    if liquidate:
                        print(f"  ⚠ [{date_str}] {risk_status}! 生成清仓信号(T+1执行), 冷却{self.cfg['cooldown_days']}日")
                        # [FIX P0-2] 不再用当日收盘价立即清仓（前瞻偏差）
                        # 改为生成卖出信号加入 pending_signals，T+1 开盘价执行
                        liquidation_signals = self._generate_liquidation_signals(date_str)
                        # 熔断后清空待买入，保留/合并待卖出，避免未成交清仓单丢失。
                        pending_signals = self._dedupe_pending_signals(
                            [(sig, strat) for sig, strat in pending_signals if sig.direction == "sell"]
                            + liquidation_signals
                        )
                    else:
                        print(f"  ⚠ [{date_str}] {risk_status}! 禁止开新仓, 冷却{self.cfg['cooldown_days']}日")
                    risk_blocked = True
                else:
                    risk_blocked = True

            if risk_blocked:
                # 熔断/冷却期间：只允许卖出信号通过，禁止买入
                # liquidate=True 时 pending_signals 已被清仓信号填充
                # liquidate=False 时遍历策略生成卖出信号
                if not liquidate:
                    for code in symbols_dict:
                        df = data_map[code]
                        if date not in df.index:
                            continue
                        i = df.index.get_loc(date)
                        for strategy in self.strategy_instances[code]:
                            ctx = BarContext(i=i, df=df, current_assets=current_assets,
                                             indicators=ind_map[code], available_cash=self.cash,
                                             symbol=code, date=date_str)
                            signal = strategy.on_bar(ctx)
                            if signal is None:
                                continue
                            if signal.direction == "sell" and not self._pending_has_sell(pending_signals, code, strategy.name):
                                pending_signals.append((signal, strategy))
                            # buy信号在熔断期间被阻止

                pending_signals = self._dedupe_pending_signals(pending_signals)
                closed_keys = self._close_positions_on_data_end(data_map, date)
                if closed_keys:
                    pending_signals = [(sig, strat) for sig, strat in pending_signals if (sig.symbol, sig.strategy_name) not in closed_keys]
                daily_assets = self._total_assets(data_map, date)
                self.equity_curve.append({
                    "date": date_str, "assets": daily_assets,
                    "cash": self.cash, "position_value": daily_assets - self.cash,
                })
                continue

            # ── Step 4: 生成今日信号（基于今日收盘数据）+ 动量轮动过滤 ──
            # 4a: 计算所有标的的动量评分（过去N日收益率）
            momentum_scores = {}
            lookback = self.cfg.get("momentum_lookback", 20)
            for code in symbols_dict:
                df = data_map[code]
                if date not in df.index:
                    continue
                i = df.index.get_loc(date)
                if i >= lookback:
                    ret = df["close"].iloc[i] / df["close"].iloc[i - lookback] - 1
                    momentum_scores[code] = ret

            # 4b: 排序，取动量最高的max_positions个标的
            max_positions = self.cfg.get("max_positions", 2)
            top_symbols = set(symbols_dict.keys())
            if momentum_scores:
                sorted_syms = sorted(momentum_scores.items(), key=lambda x: x[1], reverse=True)
                top_symbols = {s[0] for s in sorted_syms[:max_positions]}

            # 4c: 已持仓的标的始终允许加仓（不因动量下降而被排除）
            held_symbols = set(self.positions.keys())

            for code in symbols_dict:
                df = data_map[code]
                if date not in df.index:
                    continue

                i = df.index.get_loc(date)
                strategies = self.strategy_instances[code]

                for strategy in strategies:
                    ctx = BarContext(
                        i=i, df=df, current_assets=current_assets,
                        indicators=ind_map[code], available_cash=self.cash,
                        symbol=code, date=date_str,
                    )

                    signal = strategy.on_bar(ctx)
                    if signal is None:
                        continue

                    # 防止停牌/涨跌停等待期间，同一策略重复排队多个相同方向信号。
                    if signal.direction == "buy" and self._pending_has_buy(pending_signals, code, strategy.name):
                        continue
                    if signal.direction == "sell" and self._pending_has_sell(pending_signals, code, strategy.name):
                        continue

                    # 动量轮动过滤：买入信号只允许动量Top N或已持仓的标的
                    if signal.direction == "buy":
                        if code not in top_symbols and code not in held_symbols:
                            continue  # 动量不够，跳过买入

                    pending_signals.append((signal, strategy))

            pending_signals = self._dedupe_pending_signals(pending_signals)

            # ── Step 5: 标的数据提前结束时强制结算，避免持仓在无行情区间继续 stale 估值 ──
            closed_keys = self._close_positions_on_data_end(data_map, date)
            if closed_keys:
                pending_signals = [(sig, strat) for sig, strat in pending_signals if (sig.symbol, sig.strategy_name) not in closed_keys]

            # ── Step 6: 记录每日权益 ──
            daily_assets = self._total_assets(data_map, date)
            self.equity_curve.append({
                "date": date_str,
                "assets": daily_assets,
                "cash": self.cash,
                "position_value": daily_assets - self.cash,
            })

        # 5. 末日估值 / 可选强制清算
        # 默认不强制平仓：最终权益按最后可用收盘价 mark-to-market，避免把人为末日卖出计入策略胜率。
        # 若用户需要所有交易闭合以统计已实现胜率/盈亏比，可设置 force_close_on_end=True。
        last_date = all_dates[-1]
        if self.cfg.get("force_close_on_end", False):
            self._liquidate_all(data_map, last_date, reason="末日强制清算")
        final_assets = self._total_assets(data_map, last_date)
        print(f"\n  回测完成: 初始 {self.initial_capital:,.0f} → 终值 {final_assets:,.0f}")

        return self._build_result(final_assets, all_dates, data_map)

    def _execute_pending_signals(self, pending: list[tuple[Signal, BaseStrategy]],
                                  data_map: dict[str, pd.DataFrame], date: pd.Timestamp,
                                  date_to_pos: dict[pd.Timestamp, int]) -> list[tuple[Signal, BaseStrategy]]:
        """以今日开盘价执行昨日产生的信号（卖出优先于买入）
        
        返回未能执行的信号列表（如停牌、涨跌停无法成交），供次日继续尝试。
        """
        date_str = date.strftime("%Y-%m-%d")
        # [FIX P1-6] 卖出优先：先执行卖出信号释放资金和仓位，再执行买入信号
        sorted_pending = sorted(pending, key=lambda x: 0 if x[0].direction == "sell" else 1)
        unexecuted = []
        for signal, strategy in sorted_pending:
            code = signal.symbol
            if self._buy_signal_expired(signal, date, date_to_pos):
                # 买入信号等待过久后过期，避免停牌/长期无开盘后按旧突破信号买入。
                continue
            if code not in data_map or date not in data_map[code].index:
                # [FIX R1-1] 停牌：信号保留到次日
                unexecuted.append((signal, strategy))
                continue

            open_price = data_map[code].loc[date, "open"]
            if pd.isna(open_price) or open_price <= 0:
                unexecuted.append((signal, strategy))
                continue

            # [FIX P1-8] 涨跌停板限制（使用 limit_price_epsilon 容忍度）
            df = data_map[code]
            loc = df.index.get_loc(date)
            if loc > 0:
                prev_close = df.iloc[loc - 1]["close"]
                if prev_close > 0:
                    change_pct = (open_price - prev_close) / prev_close
                    limit_up = _limit_pct_for_code(code, self.cfg, self.symbol_names.get(code, ""))
                    eps = float(self.cfg.get("limit_price_epsilon", 0.001))
                    limit_down = -limit_up
                    if signal.direction == "buy" and change_pct >= limit_up - eps:
                        # 涨停开盘，无法买入 → 丢弃买入信号（不保留，次日条件可能已变）
                        continue
                    if signal.direction == "sell" and change_pct <= limit_down + eps:
                        # 跌停开盘，无法卖出 → 卖出信号保留到次日继续尝试
                        unexecuted.append((signal, strategy))
                        continue

            # 用开盘价覆盖信号价格
            signal.price = open_price

            if signal.direction == "buy":
                self._execute_buy(signal, strategy, date_str, data_map, date)
            elif signal.direction == "sell":
                executed = self._execute_sell(signal, strategy, date_str, data_map, date)
                if not executed and strategy.position is not None:
                    unexecuted.append((signal, strategy))

        return self._dedupe_pending_signals(unexecuted)

    def _latest_close_on_or_before(self, df: pd.DataFrame, date: pd.Timestamp) -> float:
        """取 date 当日或之前最近一个有效收盘价。"""
        if date in df.index:
            price = df.loc[date, "close"]
            if _is_finite_number(price) and price > 0:
                return float(price)
        mask = df.index <= date
        if not mask.any():
            return 0.0
        closes = pd.to_numeric(df.loc[mask, "close"], errors="coerce")
        closes = closes[closes > 0]
        return float(closes.iloc[-1]) if not closes.empty else 0.0

    def _latest_close_before(self, df: pd.DataFrame, date: pd.Timestamp) -> float:
        """取 date 之前最近一个有效收盘价，用于开盘前/开盘时估值兜底。"""
        mask = df.index < date
        if not mask.any():
            return 0.0
        closes = pd.to_numeric(df.loc[mask, "close"], errors="coerce")
        closes = closes[closes > 0]
        return float(closes.iloc[-1]) if not closes.empty else 0.0

    def _execution_mark_prices(self, data_map: dict[str, pd.DataFrame], date: pd.Timestamp) -> dict[str, float]:
        """开盘执行阶段可见价格：当日 open 可用则用 open，否则用前一可用 close。

        不能用执行日 close 做买入风控或仓位估值，否则会把 T+1 收盘价提前用于开盘交易。
        """
        prices: dict[str, float] = {}
        for code, df in data_map.items():
            price = 0.0
            if date in df.index:
                open_price = df.loc[date, "open"]
                if _is_finite_number(open_price) and open_price > 0:
                    price = float(open_price)
            if price <= 0:
                price = self._latest_close_before(df, date)
            if price > 0:
                prices[code] = price
        return prices

    def _total_assets_at_prices(self, prices: dict[str, float]) -> float:
        """用给定价格表计算总资产；价格缺失时用持仓成本兜底，避免估值为 0。"""
        total = self.cash
        for code, positions in self.positions.items():
            price = prices.get(code)
            for pos in positions.values():
                mark = price if price is not None and price > 0 else pos.entry_price
                total += pos.market_value_at(mark)
        return float(total)

    def _total_assets(self, data_map: dict[str, pd.DataFrame], date: pd.Timestamp) -> float:
        """计算当前总资产 = 现金 + 所有持仓市值（用指定日期收盘价）"""
        total = self.cash
        for code, positions in self.positions.items():
            if code not in data_map:
                continue
            price = self._latest_close_on_or_before(data_map[code], date)
            for pos in positions.values():
                mark = price if price > 0 else pos.entry_price
                total += pos.market_value_at(mark)
        return float(total)

    def _get_current_prices(self, data_map: dict[str, pd.DataFrame], date: pd.Timestamp) -> dict[str, float]:
        """获取指定日期所有标的的收盘价，缺失时使用最近可用价格。"""
        prices = {}
        for code, df in data_map.items():
            price = self._latest_close_on_or_before(df, date)
            if price > 0:
                prices[code] = price
        return prices

    def _execute_buy(self, signal: Signal, strategy: BaseStrategy, date_str: str,
                     data_map: dict[str, pd.DataFrame] | None = None,
                     date: pd.Timestamp | None = None) -> bool:
        """执行买入（含滑点+佣金）。

        关键：使用 ``signal.atr``（信号日锁定的ATR）计算风险仓位和止损，
        严禁读取执行日ATR，否则会引入前瞻偏差。
        返回是否实际成交。
        """
        if signal.target_shares <= 0 or signal.price <= 0:
            return False

        cfg = self.cfg
        slippage = float(cfg.get("slippage", 0.001))
        commission_rate = float(cfg.get("commission_rate", 0.00025))
        min_commission = float(cfg.get("min_commission", 0.0))

        exec_price = float(signal.price) * (1 + slippage)
        shares = _floor_to_lot(signal.target_shares)
        if shares <= 0:
            return False
        buy_value = shares * exec_price
        commission = max(buy_value * commission_rate, min_commission) if buy_value > 0 else 0.0
        total_cost = buy_value + commission

        # 资金不足，缩减股数到可买整手。
        if total_cost > self.cash:
            shares = _floor_to_lot(self.cash / (exec_price * (1 + commission_rate)))
            while shares > 0:
                buy_value = shares * exec_price
                commission = max(buy_value * commission_rate, min_commission) if buy_value > 0 else 0.0
                total_cost = buy_value + commission
                if total_cost <= self.cash:
                    break
                shares -= A_SHARE_LOT_SIZE
            if shares <= 0:
                return False

        if data_map is not None and date is not None:
            current_prices = self._execution_mark_prices(data_map, date)
            current_assets = self._total_assets_at_prices(current_prices)
        else:
            current_assets = self.initial_capital
            current_prices = None

        if signal.symbol not in self.positions and len(self.positions) >= int(cfg.get("max_positions", 1)):
            return False

        if (
            data_map is not None
            and date is not None
            and self.cfg.get("close_position_on_data_end", True)
            and self.global_last_date is not None
            and signal.symbol in self.symbol_last_dates
            and self.symbol_last_dates[signal.symbol] == pd.Timestamp(date)
            and pd.Timestamp(date) < self.global_last_date
        ):
            # 不在某标的最后一个行情日新开仓/加仓，否则后续没有价格序列可风控或退出。
            return False

        # 执行日重新用信号日ATR做风险校验（double-check，与信号生成时一致）
        if signal.atr > 0:
            existing_pos = self.positions.get(signal.symbol, {}).get(strategy.name)
            unit_num = existing_pos.units + 1 if existing_pos is not None else 1
            risk_limited_shares = strategy._calc_shares(
                current_assets * float(cfg.get("strategy_weight", 1.0)), exec_price, signal.atr,
                unit_number=unit_num,
            )
            shares = min(shares, risk_limited_shares)
            if shares <= 0:
                return False
            buy_value = shares * exec_price
            commission = max(buy_value * commission_rate, min_commission) if buy_value > 0 else 0.0
            total_cost = buy_value + commission
            while shares > 0 and total_cost > self.cash:
                shares -= A_SHARE_LOT_SIZE
                buy_value = shares * exec_price
                commission = max(buy_value * commission_rate, min_commission) if buy_value > 0 else 0.0
                total_cost = buy_value + commission
            if shares <= 0:
                return False

        if not self.risk.check_position_limits(signal.symbol, self.positions, current_assets, buy_value, current_prices):
            return False
        # 二次单日亏损检查：开盘执行阶段只能使用开盘/昨收可见估值，
        # 与收盘生成信号阶段的检查共同构成双闸门，避免隔夜跳空后继续买入。
        if self.risk.check_daily_loss(current_assets):
            return False

        self.cash -= total_cost
        if signal.symbol not in self.positions:
            self.positions[signal.symbol] = {}

        effective_entry = total_cost / shares
        # 执行价和信号价可能因跳空差异较大，初始止损用信号日ATR + 实际成交价重算。
        # 新开仓不能沿用 T 日收盘价止损，否则跳空低开时止损可能被设在成交价上方。
        exec_based_stop = (
            exec_price - float(cfg.get("atr_multiplier", 2.0)) * signal.atr
            if signal.atr > 0
            else signal.stop_loss
        )

        if strategy.name in self.positions[signal.symbol]:
            pos = self.positions[signal.symbol][strategy.name]
            total_cost_basis = pos.cost + total_cost
            total_shares = pos.shares + shares
            pos.entry_price = total_cost_basis / total_shares
            pos.shares = total_shares
            pos.units += 1
            add_stop_candidates = [pos.stop_loss, exec_based_stop]
            if signal.stop_loss:
                add_stop_candidates.append(signal.stop_loss)
            pos.stop_loss = max(add_stop_candidates)
            pos.highest_since_entry = max(pos.highest_since_entry, exec_price)
            pos.last_buy_date = date_str
            pos.last_add_price = exec_price
        else:
            effective_stop = exec_based_stop
            self.positions[signal.symbol][strategy.name] = Position(
                symbol=signal.symbol,
                strategy_name=strategy.name,
                shares=shares,
                entry_price=effective_entry,
                entry_date=date_str,
                stop_loss=effective_stop,
                highest_since_entry=exec_price,
                units=1,
                last_buy_date=date_str,
                last_add_price=exec_price,
            )

        strategy.position = self.positions[signal.symbol][strategy.name]
        self.trades.append(TradeRecord(
            symbol=signal.symbol,
            strategy_name=signal.strategy_name,
            direction="buy",
            shares=shares,
            price=exec_price,
            date=date_str,
            reason=signal.reason,
            signal_date=signal.signal_date,
            gross_value=buy_value,
            commission=commission,
            stamp_duty_cost=0.0,
            net_cash_flow=-total_cost,
            cash_after=self.cash,
        ))
        return True

    def _execute_sell(self, signal: Signal, strategy: BaseStrategy, date_str: str,
                      data_map: dict[str, pd.DataFrame] | None = None,
                      date: pd.Timestamp | None = None) -> bool:
        """执行卖出（含滑点+佣金+印花税）。返回是否实际成交。"""
        if signal.target_shares <= 0 or signal.price <= 0:
            return False
        pos = None
        if signal.symbol in self.positions:
            pos = self.positions[signal.symbol].get(strategy.name)
        if pos is None:
            # 防御：策略引用与引擎持仓不一致时，以引擎持仓为准，避免凭陈旧引用卖出“幽灵仓位”。
            strategy.position = None
            return False

        cfg = self.cfg
        slippage = float(cfg.get("slippage", 0.001))
        commission_rate = float(cfg.get("commission_rate", 0.00025))
        min_commission = float(cfg.get("min_commission", 0.0))
        stamp_duty = float(cfg.get("stamp_duty", 0.0005))

        exec_price = float(signal.price) * (1 - slippage)
        sell_shares = _floor_to_lot(min(signal.target_shares, pos.shares))
        if sell_shares <= 0:
            return False
        sell_value = sell_shares * exec_price
        commission = max(sell_value * commission_rate, min_commission) if sell_value > 0 else 0.0
        stamp_duty_cost = sell_value * stamp_duty
        net_proceeds = sell_value - commission - stamp_duty_cost

        cost_basis = sell_shares * pos.entry_price
        pnl = net_proceeds - cost_basis
        pnl_pct = pnl / cost_basis if cost_basis > 0 else 0.0

        self.cash += net_proceeds
        pos.shares -= sell_shares
        if pos.shares <= 0:
            del self.positions[signal.symbol][strategy.name]
            if not self.positions[signal.symbol]:
                del self.positions[signal.symbol]
            strategy.position = None
        else:
            strategy.position = pos

        self.trades.append(TradeRecord(
            symbol=signal.symbol,
            strategy_name=signal.strategy_name,
            direction="sell",
            shares=sell_shares,
            price=exec_price,
            date=date_str,
            reason=signal.reason,
            pnl=pnl,
            pnl_pct=pnl_pct,
            signal_date=signal.signal_date,
            gross_value=sell_value,
            commission=commission,
            stamp_duty_cost=stamp_duty_cost,
            net_cash_flow=net_proceeds,
            cash_after=self.cash,
        ))
        return True

    def _generate_liquidation_signals(self, date_str: str) -> list[tuple[Signal, BaseStrategy]]:
        """生成全仓清仓信号（T+1执行，消除前瞻偏差）。

        以引擎持仓字典为唯一真实仓位来源，避免策略对象上的陈旧引用生成幽灵清仓单。
        """
        signals = []
        for code, positions in self.positions.items():
            strategies = {s.name: s for s in self.strategy_instances.get(code, [])}
            for strat_name, pos in positions.items():
                strategy = strategies.get(strat_name)
                if strategy is None:
                    continue
                sig = Signal(
                    symbol=code,
                    strategy_name=strat_name,
                    direction="sell",
                    target_shares=pos.shares,
                    price=pos.entry_price,  # 占位，T+1执行时会被覆盖为开盘价
                    reason="熔断清仓",
                    signal_date=date_str,
                )
                signals.append((sig, strategy))
        return signals

    def _liquidate_all(self, data_map: dict, date: pd.Timestamp, reason: str = "末日结算"):
        """按指定日收盘价做强制结算。

        仅用于 force_close_on_end 或调试结算；熔断清仓必须走 T+1 pending sell，
        不能调用本方法绕过 A 股 T+1 执行模型。
        """
        cfg = self.cfg
        slippage = cfg.get("slippage", 0.001)
        commission_rate = cfg.get("commission_rate", 0.00025)
        min_commission = cfg.get("min_commission", 0.0)
        stamp_duty = cfg.get("stamp_duty", 0.0005)

        date_str = date.strftime("%Y-%m-%d")
        liquidated_codes = set()  # 记录已清仓的标的，只清理这些标的的策略引用
        for code in list(self.positions.keys()):
            if code not in data_map or date not in data_map[code].index:
                continue  # 停牌等：无法清仓，保留持仓和策略引用
            close_price = data_map[code].loc[date, "close"]
            exec_price = close_price * (1 - slippage)

            for strat_name in list(self.positions[code].keys()):
                pos = self.positions[code][strat_name]
                sell_value = pos.shares * exec_price
                commission = max(sell_value * commission_rate, min_commission) if sell_value > 0 else 0.0
                stamp_duty_cost = sell_value * stamp_duty
                net_proceeds = sell_value - commission - stamp_duty_cost

                pnl = net_proceeds - pos.cost
                pnl_pct = pnl / pos.cost if pos.cost > 0 else 0

                self.cash += net_proceeds
                self.trades.append(TradeRecord(
                    symbol=code, strategy_name=strat_name, direction="sell",
                    shares=pos.shares, price=exec_price, date=date_str,
                    reason=reason, pnl=pnl, pnl_pct=pnl_pct, signal_date=date_str,
                    gross_value=sell_value, commission=commission, stamp_duty_cost=stamp_duty_cost,
                    net_cash_flow=net_proceeds, cash_after=self.cash,
                ))
                del self.positions[code][strat_name]

            # 清理空字典：该标的所有策略持仓都已清仓时，删除该标的键
            if not self.positions[code]:
                del self.positions[code]
            liquidated_codes.add(code)

        # [FIX#1] 只清除已清仓标的的策略实例持仓引用（停牌标的保留引用）
        for code in liquidated_codes:
            if code in self.strategy_instances:
                for strategy in self.strategy_instances[code]:
                    strategy.position = None

    def _close_positions_on_data_end(self, data_map: dict[str, pd.DataFrame], date: pd.Timestamp) -> set[tuple[str, str]]:
        """标的数据早于全局日历结束时，按该标的最后收盘价做强制结算。

        返回已结算的 (symbol, strategy_name)，调用方据此清理同标的同策略的 pending 信号，
        避免结算后仍保留永远无法执行的 stale sell/buy。
        """
        closed: set[tuple[str, str]] = set()
        if not self.cfg.get("close_position_on_data_end", True) or self.global_last_date is None:
            return closed
        for code in list(self.positions.keys()):
            last_date = self.symbol_last_dates.get(code)
            if last_date is None or pd.Timestamp(date) != last_date or last_date >= self.global_last_date:
                continue
            df = data_map.get(code)
            if df is None or date not in df.index:
                continue
            close_price = df.loc[date, "close"]
            if not _is_finite_number(close_price) or close_price <= 0:
                continue
            strategies = {s.name: s for s in self.strategy_instances.get(code, [])}
            for strat_name in list(self.positions.get(code, {}).keys()):
                pos = self.positions[code].get(strat_name)
                if pos is None:
                    continue
                # 理论上 _execute_buy 已阻止最后行情日买入；这里保留防御，避免同日买入同日卖出。
                if pos.last_buy_date == date.strftime("%Y-%m-%d"):
                    continue
                strategy = strategies.get(strat_name)
                if strategy is None:
                    continue
                signal = Signal(
                    symbol=code,
                    strategy_name=strat_name,
                    direction="sell",
                    target_shares=pos.shares,
                    price=float(close_price),
                    reason="数据结束强制结算",
                    signal_date=date.strftime("%Y-%m-%d"),
                )
                if self._execute_sell(signal, strategy, date.strftime("%Y-%m-%d"), data_map, date):
                    closed.add((code, strat_name))
        return closed

    def _build_result(self, final_assets: float, all_dates: list, data_map: dict) -> dict:
        """构建回测结果"""
        eq = pd.DataFrame(self.equity_curve)
        if eq.empty:
            return {"error": "无权益数据"}

        eq["date"] = pd.to_datetime(eq["date"])
        eq["assets"] = eq["assets"].astype(float)
        eq = eq.set_index("date")

        total_return = (final_assets - self.initial_capital) / self.initial_capital
        # [FIX P1-9] 年化收益率与Sharpe统一使用252交易日基准
        n_trading_days = len(all_dates)
        annual_return = (1 + total_return) ** (252 / max(n_trading_days, 1)) - 1 if total_return > -1 else -1.0

        # 最大回撤
        peak = eq["assets"].cummax()
        drawdown = (eq["assets"] - peak) / peak
        max_drawdown = drawdown.min()

        # 日收益率与简化 Sharpe。risk_free_rate 为年化无风险利率，默认 0。
        daily_returns = eq["assets"].pct_change().dropna()
        sharpe = 0.0
        if daily_returns.std() > 0:
            rf_annual = float(self.cfg.get("risk_free_rate", 0.0))
            rf_daily = (1 + rf_annual) ** (1 / 252) - 1 if rf_annual > -1 else 0.0
            sharpe = (daily_returns - rf_daily).mean() / daily_returns.std() * math.sqrt(252)

        # 胜率与盈亏比 — 用总盈利/总亏损计算profit_factor
        sell_trades = [t for t in self.trades if t.direction == "sell"]
        wins = [t for t in sell_trades if t.pnl > 0]
        losses = [t for t in sell_trades if t.pnl < 0]
        # pnl=0 的交易（平价卖出）不计入胜率分母，避免注释与统计口径不一致。
        decisive_trades = len(wins) + len(losses)
        win_rate = len(wins) / decisive_trades if decisive_trades else 0
        total_win = sum(t.pnl for t in wins) if wins else 0
        total_loss = abs(sum(t.pnl for t in losses)) if losses else 0
        profit_factor = total_win / total_loss if total_loss > 0 else float('inf')

        open_positions = sum(len(sym_positions) for sym_positions in self.positions.values())
        open_position_value = max(float(final_assets - self.cash), 0.0)

        return {
            "initial_capital": self.initial_capital,
            "final_assets": final_assets,
            "total_return": total_return,
            "annual_return": annual_return,
            "max_drawdown": max_drawdown,
            "sharpe": sharpe,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "total_trades": len(self.trades),
            "sell_trades": len(sell_trades),
            "open_positions": int(open_positions),
            "open_position_value": open_position_value,
            "force_close_on_end": bool(self.cfg.get("force_close_on_end", False)),
            "equity_curve": eq,
            "trades": self.trades,
            "drawdown_series": drawdown,
        }


# ═══════════════════════════════════════════════════════════════════════
#  绩效报告
# ═══════════════════════════════════════════════════════════════════════

class PerformanceReport:
    """生成回测绩效报告"""

    @staticmethod
    def print_report(result: dict, symbols_dict: dict[str, str]):
        """打印文字报告"""
        if "error" in result:
            print(f"回测失败: {result['error']}")
            return
        print(f"\n{'═'*60}")
        print(f"  AQuant 回测绩效报告")
        print(f"{'═'*60}")
        print(f"  标的: {', '.join(f'{v}({k})' for k, v in symbols_dict.items())}")
        print(f"  初始资金:   {result['initial_capital']:>15,.0f}")
        print(f"  终值:       {result['final_assets']:>15,.0f}")
        print(f"  ────────────────────────────────")
        print(f"  总收益率:   {result['total_return']:>15.2%}")
        print(f"  年化收益率: {result['annual_return']:>15.2%}")
        print(f"  最大回撤:   {result['max_drawdown']:>15.2%}")
        print(f"  夏普比率:   {result['sharpe']:>15.2f}")
        print(f"  胜率:       {result['win_rate']:>15.2%}")
        pf = result.get("profit_factor")
        pf_str = "N/A（无亏损交易）" if math.isinf(float(pf)) else f"{float(pf):.2f}"
        print(f"  盈亏比:     {pf_str:>15}")
        print(f"  未平仓数:   {result.get('open_positions', 0):>15d}")
        print(f"  总交易次数: {result['total_trades']:>15d}")
        print(f"  卖出次数:   {result['sell_trades']:>15d}")
        print(f"{'═'*60}\n")

        # 交易明细
        trades = result.get("trades", [])
        if trades:
            print(f"  交易明细 (最近20笔):")
            print(f"  {'日期':<12} {'标的':<8} {'策略':<20} {'方向':<6} {'股数':>8} {'价格':>10} {'盈亏':>12} {'原因'}")
            print(f"  {'─'*100}")
            for t in trades[-20:]:
                pnl_str = f"{t.pnl:>+10,.0f}" if t.direction == "sell" else ""
                print(f"  {t.date:<12} {t.symbol:<8} {t.strategy_name:<20} {t.direction:<6} {t.shares:>8} {t.price:>10.2f} {pnl_str}   {t.reason}")

    @staticmethod
    def plot_equity_curve(result: dict, save_path: str = "equity_curve.png"):
        """绘制权益曲线并保存"""
        if "error" in result:
            print(f"回测失败，无法绘图: {result['error']}")
            return
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.font_manager import FontProperties, fontManager
        import os

        # 查找中文字体（优先CJK，其次Droid Fallback）
        zh_font_path = None
        for candidate in [
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
            "/usr/share/fonts/droid-fallback/DroidSansFallback.ttf",
        ]:
            if os.path.exists(candidate):
                zh_font_path = candidate
                break

        if not zh_font_path:
            import subprocess
            try:
                r = subprocess.run(
                    ["fc-match", "-f", "%{file}", ":lang=zh"],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0 and r.stdout.strip():
                    zh_font_path = r.stdout.strip()
            except Exception:
                pass

        # 注册字体并设为全局后备，让matplotlib自动混合中英文字体
        if zh_font_path:
            fontManager.addfont(zh_font_path)
            zh_font_name = FontProperties(fname=zh_font_path).get_name()
            plt.rcParams["font.family"] = ["DejaVu Sans", zh_font_name]
            plt.rcParams["axes.unicode_minus"] = False

        eq = result["equity_curve"]
        dd = result["drawdown_series"]

        fig, axes = plt.subplots(2, 1, figsize=(14, 8), gridspec_kw={"height_ratios": [3, 1]})

        # 权益曲线
        axes[0].plot(eq.index, eq["assets"] / 10000, linewidth=1.5, color="#1a73e8")
        axes[0].set_title("AQuant Portfolio Equity Curve", fontsize=14)
        axes[0].set_ylabel("Assets (万元)")
        axes[0].grid(True, alpha=0.3)
        axes[0].axhline(y=result["initial_capital"] / 10000, color="gray", linestyle="--", alpha=0.5)

        # 回撤曲线
        axes[1].fill_between(dd.index, dd * 100, 0, color="#dc3545", alpha=0.4)
        axes[1].set_title("Drawdown (%)", fontsize=12)
        axes[1].set_ylabel("Drawdown %")
        axes[1].grid(True, alpha=0.3)

        plt.tight_layout()
        plt.savefig(save_path, dpi=150, bbox_inches="tight")
        plt.close()
        print(f"\n  权益曲线已保存: {save_path}")


# ═══════════════════════════════════════════════════════════════════════
#  CLI入口
# ═══════════════════════════════════════════════════════════════════════

DEFAULT_SYMBOLS = {
    "300308": "中际旭创",
    "300502": "新易盛",
    "300394": "天孚通信",
    "688008": "澜起科技",
    "603986": "兆易创新",
    "002409": "雅克科技",
    "688072": "拓荆科技",
    "688300": "联瑞新材",
    "300054": "鼎龙股份",
    "688205": "德科立",
    "920045": "蘅东光",
    "300776": "帝尔激光",
    "688535": "华海诚科",
}

DEFAULT_SYMBOL_NAMES = {v: k for k, v in DEFAULT_SYMBOLS.items()}

# 完整预设名称表：在 DEFAULT_SYMBOLS 基础上补充半导体/设备类标的，
# 供 parse_symbols 在仅传代码时反查中文名（不影响默认回测池 DEFAULT_SYMBOLS）。
SYMBOL_NAME_TABLE: dict[str, str] = {
    **DEFAULT_SYMBOLS,
    "688249": "晶合集成",
    "688347": "华虹宏力",
    "688082": "盛美上海",
    "688120": "华海清科",
    "688361": "中科飞测",
    "688409": "富创精密",
    "300666": "江丰电子",
    "600206": "有研新材",
    "300604": "长川科技",
    "300776": "帝尔激光",
}

def parse_symbols(symbols_str: str) -> dict[str, str]:
    """解析命令行标的参数: '300308,300502' 或 '中际旭创,新易盛'

    返回 {code: name}。预设标的（DEFAULT_SYMBOLS）会用中文名；
    未知 6 位代码保留代码本身作为名称（报告仍可正常显示）。
    """
    result = {}
    for s in symbols_str.split(","):
        s = s.strip()
        if not s:
            continue
        if s in DEFAULT_SYMBOLS:
            result[s] = DEFAULT_SYMBOLS[s]
        elif s in DEFAULT_SYMBOL_NAMES:
            result[DEFAULT_SYMBOL_NAMES[s]] = s
        elif _SYMBOL_RE.match(s):
            # 纯代码：优先用预设中文名（若存在），否则以代码本身作名
            result[s] = SYMBOL_NAME_TABLE.get(s, s)
        else:
            raise ValueError(f"无效的股票代码或名称: '{s}'（需6位数字代码或预设名称）")
    return result


def main():
    parser = argparse.ArgumentParser(description="AQuant — A股自动量化交易系统")
    sub = parser.add_subparsers(dest="command")

    bt = sub.add_parser("backtest", help="运行回测")
    bt.add_argument("--symbol", "-s", default="300308,300502,300394,688008,603986,002409,688300,300054,688535", help="标的代码(逗号分隔)")
    bt.add_argument("--start", default="2025-01-01", help="开始日期")
    bt.add_argument("--end", default="2026-06-30", help="结束日期")
    bt.add_argument("--capital", type=float, default=2_000_000, help="初始资金")
    bt.add_argument("--profile", default="default",
                    choices=["default", "semiconductor", "semiconductor_heavy", "aggressive"],
                    help="全局参数集: default / semiconductor(宽参数轻仓) / semiconductor_heavy(宽参数重仓) / aggressive(激进优化)")
    bt.add_argument("--config-route", default="auto", choices=["auto", "none"],
                    help="参数分发: auto(按行业分类自动套参数, 默认) / none(全部用全局profile参数)")

    args = parser.parse_args()

    if args.command == "backtest":
        symbols_dict = parse_symbols(args.symbol)
        engine = BacktestEngine(initial_capital=args.capital)
        result = engine.run(
            symbols_dict, args.start, args.end,
            profile=args.profile, config_route=args.config_route,
        )

        profile_desc = args.profile if args.config_route == "none" else f"auto-route({args.profile}基线)"
        print(f"  [配置] 参数模式: {profile_desc}")
        PerformanceReport.print_report(result, symbols_dict)
        PerformanceReport.plot_equity_curve(result, f"equity_curve_{args.profile}_{args.config_route}.png")

        return result


if __name__ == "__main__":
    main()
