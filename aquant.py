#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AQuant — A股自动量化交易系统（趋势跟踪·多策略组合）
======================================================
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
import os
import re
import time
from dataclasses import dataclass
from datetime import timedelta

import numpy as np
import pandas as pd

try:
    import akshare as ak
except ImportError:
    ak = None
    print("[WARN] akshare未安装，数据获取功能不可用。请运行: pip install akshare")


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
        """统一列名为英文标准名，并设置日期索引"""
        rename_map = {
            "日期": "date", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low", "成交量": "volume",
        }
        df = df.rename(columns=rename_map)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date").sort_index()
        keep = ["open", "close", "high", "low", "volume"]
        return df[[c for c in keep if c in df.columns]]

    @staticmethod
    def validate_ohlcv(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """数据验证：重复日期检测、非正价格检测、OHLC关系校验"""
        if df.empty:
            raise RuntimeError(f"{symbol}: 数据为空")

        # 重复日期检测
        if df.index.duplicated().any():
            dup_count = df.index.duplicated().sum()
            print(f"  [WARN] {symbol}: 发现 {dup_count} 个重复日期，保留最后一条")
            df = df[~df.index.duplicated(keep="last")]

        # 非正价格检测
        for col in ["open", "close", "high", "low"]:
            if col in df.columns:
                bad = (df[col] <= 0) | df[col].isna()
                if bad.any():
                    bad_count = bad.sum()
                    print(f"  [WARN] {symbol}: {col}列有 {bad_count} 个非正值或NaN，将用前值填充")
                    df[col] = df[col].replace(0, np.nan).ffill()

        # OHLC关系校验：high >= max(open,close), low <= min(open,close)
        if all(c in df.columns for c in ["open", "close", "high", "low"]):
            ohlc_max = df[["open", "close"]].max(axis=1)
            ohlc_min = df[["open", "close"]].min(axis=1)
            high_bad = df["high"] < ohlc_max
            low_bad = df["low"] > ohlc_min
            if high_bad.any() or low_bad.any():
                bad_count = high_bad.sum() + low_bad.sum()
                print(f"  [WARN] {symbol}: {bad_count} 个OHLC关系异常，修正high/low")
                df.loc[high_bad, "high"] = ohlc_max[high_bad]
                df.loc[low_bad, "low"] = ohlc_min[low_bad]

        return df


# ═══════════════════════════════════════════════════════════════════════
#  技术指标层
# ═══════════════════════════════════════════════════════════════════════

class Indicators:
    """技术指标计算（全部使用历史数据，无前瞻偏差）"""

    @staticmethod
    def atr(df: pd.DataFrame, period: int = 20) -> pd.Series:
        """ATR (Average True Range) — Wilder平滑"""
        high, low, close = df["high"], df["low"], df["close"]
        prev_close = close.shift(1)
        tr = pd.concat([
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        return tr.ewm(alpha=1 / period, adjust=False).mean()

    @staticmethod
    def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """ADX — Wilder平滑，使用np.where避免+DM/-DM赋值互扰"""
        high, low, close = df["high"], df["low"], df["close"]
        up_move = high.diff()
        down_move = -low.diff()

        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        atr_val = Indicators.atr(df, period)
        atr_safe = atr_val.replace(0, np.nan)

        plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr_safe
        minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr_safe

        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx_val = dx.ewm(alpha=1 / period, adjust=False).mean()
        return adx_val.fillna(0)

    @staticmethod
    def rsi(close: pd.Series, period: int = 14) -> pd.Series:
        """RSI — Wilder平滑 (ewm alpha=1/period)"""
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
        # 当 avg_loss=0 时（连续上涨），RSI 应为 100；当 avg_gain=0 时（连续下跌），RSI 应为 0
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi_val = 100 - 100 / (1 + rs)
        # 修正：avg_loss=0 且 avg_gain>0 时 RSI=100；两者都为0时 RSI=50（无方向）
        fill_values = pd.Series(np.where(avg_gain > 0, 100.0, 50.0), index=close.index)
        rsi_val = rsi_val.fillna(fill_values)
        return rsi_val

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
        """一次性计算所有指标，返回 dict[指标名 -> pd.Series]"""
        atr_period = cfg.get("atr_period", 20)
        adx_period = cfg.get("adx_period", 14)
        rsi_period = cfg.get("rsi_period", 14)
        entry_p = cfg.get("entry_period", 20)
        exit_p = cfg.get("exit_period", 10)
        ma_short = cfg.get("ma_short", 20)
        ma_long = cfg.get("ma_long", 60)

        return {
            "atr": Indicators.atr(df, atr_period),
            "adx": Indicators.adx(df, adx_period),
            "rsi": Indicators.rsi(df["close"], rsi_period),
            "donchian_upper": Indicators.donchian(df, entry_p, exit_p)[0],
            "donchian_lower": Indicators.donchian(df, entry_p, exit_p)[1],
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
    entry_price: float          # 含佣金的加权平均成本价
    entry_date: str
    stop_loss: float = 0.0       # ATR止损价
    highest_since_entry: float = 0.0  # 入场后最高价（追踪止损用）
    units: int = 1               # 海龟加仓单位数

    @property
    def cost(self) -> float:
        return self.shares * self.entry_price

    def market_value_at(self, price: float) -> float:
        return self.shares * price


@dataclass
class TradeRecord:
    """交易记录（含完整现金流明细）"""
    symbol: str
    strategy_name: str
    direction: str          # 'buy' / 'sell'
    shares: int
    price: float
    date: str
    reason: str = ""
    pnl: float = 0.0        # 仅卖出时记录
    pnl_pct: float = 0.0    # 仅卖出时记录
    # 扩展字段：完整现金流追踪
    signal_date: str = ""       # 信号生成日期（T日），区别于执行日期（T+1）
    gross_value: float = 0.0    # 不含费用的成交金额 = shares * price
    commission: float = 0.0     # 佣金
    stamp_duty: float = 0.0     # 印花税（仅卖出）
    slippage_cost: float = 0.0  # 滑点成本
    net_cash_flow: float = 0.0  # 净现金流（买入为负，卖出为正）
    cash_after: float = 0.0     # 交易后账户现金余额


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
    signal_date: str = ""   # 信号生成日期（T日），用于过期检测


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

    def _calc_shares(self, capital: float, price: float, atr_val: float) -> int:
        """基于ATR的仓位计算：N = capital * risk_pct / (atr * atr_multiplier)"""
        risk_pct = self.cfg.get("risk_pct", 0.01)  # 单笔风险1%
        atr_mult = self.cfg.get("atr_multiplier", 3)
        if atr_val <= 0 or price <= 0 or atr_mult <= 0:
            return 0
        n = capital * risk_pct / (atr_val * atr_mult)
        shares = int(n / 100) * 100  # A股100股整数倍
        return max(shares, 0)

    def _make_buy_signal(self, ctx: BarContext, shares: int, stop_loss: float, reason: str) -> Signal:
        price = ctx.df["close"].iloc[ctx.i]
        return Signal(
            symbol=ctx.symbol,
            strategy_name=self.name,
            direction="buy",
            target_shares=shares,
            price=price,
            stop_loss=stop_loss,
            reason=reason,
            signal_date=ctx.date,
        )

    def _make_sell_signal(self, ctx: BarContext, reason: str) -> Signal:
        return Signal(
            symbol=ctx.symbol,
            strategy_name=self.name,
            direction="sell",
            target_shares=self.position.shares if self.position else 0,
            price=ctx.df["close"].iloc[ctx.i],
            reason=reason,
            signal_date=ctx.date,
        )


class TurtleBreakoutStrategy(BaseStrategy):
    """海龟突破策略 — A股激进趋势版
    
    与保守版的核心差异：
      - 纯ATR追踪止损（无30%门槛）：入场即设0.5N止损，之后每日追踪highest-3.0N
        → 强趋势中不会因固定百分比过早离场，让利润充分奔跑
      - 激进加仓：每上涨0.3N加仓一次，最多2次
      - 入场即满仓：strategy_weight=95%，risk_pct=10%
      - 唐奇安离场改为更宽松的exit_period
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

            # 激进加仓：每上涨0.3N加仓（比0.5N更激进）
            if pos.units < max_units:
                add_threshold = atr_val * 0.3  # 上涨0.3N即可加仓
                if close > pos.entry_price and (close - pos.entry_price) > add_threshold * (pos.units):
                    capital = ctx.current_assets * cfg.get("strategy_weight", 0.95)
                    shares = self._calc_shares(capital, close, atr_val)
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
                        )
            return None

        # --- 空仓：入场 ---
        if adx_val > adx_threshold and close > upper:
            capital = ctx.current_assets * cfg.get("strategy_weight", 0.95)
            shares = self._calc_shares(capital, close, atr_val)
            if shares > 0:
                stop_loss = close - atr_stop_mult * atr_val
                return self._make_buy_signal(ctx, shares, stop_loss, f"海龟突破(ADX={adx_val:.1f})")

        return None


class DualMAStrategy(BaseStrategy):
    """双均线趋势策略 — 激进趋势版
    - MA20上穿MA50买入
    - 纯ATR追踪止损替代固定30%门槛
    - 均线死叉离场 + ATR追踪止损双保险
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
                return self._make_sell_signal(ctx, "MA20下穿MA50")

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
                return self._make_buy_signal(ctx, shares, stop_loss, f"MA金叉(RSI={rsi_val:.0f})")

        return None


class ATRChannelStrategy(BaseStrategy):
    """ATR通道突破策略 — 激进趋势版
    - 收盘价突破 MA + 1.5*ATR 买入
    - 纯ATR追踪止损（无固定门槛）
    - 通道下轨离场
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
                return self._make_buy_signal(ctx, shares, stop_loss, f"ATR通道突破(ADX={adx_val:.1f})")

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

    def check_portfolio_risk(self, current_assets: float, date_str: str,
                             trading_dates: list[pd.Timestamp] | None = None) -> str | None:
        """组合级风控：回撤熔断 + N个交易日冷却"""
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
                    try:
                        idx = trading_dates.index(current_date)
                        end_idx = min(idx + cooldown_days, len(trading_dates) - 1)
                        self.cooldown_until = trading_dates[end_idx].strftime("%Y-%m-%d")
                    except ValueError:
                        # 如果当前日期不在交易日列表中，退回自然日计算
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
        # 单标的上限 — 用当前价而非entry_price
        symbol_value = sum(
            p.shares * (current_prices.get(symbol, p.entry_price) if current_prices else p.entry_price)
            for p in positions.get(symbol, {}).values()
        )
        if (symbol_value + buy_value) / current_assets > self.cfg.get("max_symbol_weight", 0.50):
            return False

        # 总仓位上限95% — 用当前价
        total_position_value = sum(
            p.shares * (current_prices.get(sym, p.entry_price) if current_prices else p.entry_price)
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
        self.initial_capital = initial_capital
        self.cfg = cfg or self._default_config()
        self.cash = initial_capital
        self.positions: dict[str, dict[str, Position]] = {}  # {symbol: {strategy_name: Position}}
        self.trades: list[TradeRecord] = []
        self.equity_curve: list[dict] = []
        self.risk = RiskManager(self.cfg)
        self.strategy_instances: dict[str, list[BaseStrategy]] = {}  # 每个标的的策略实例
        self.global_last_date: pd.Timestamp | None = None
        self.symbol_last_dates: dict[str, pd.Timestamp] = {}

        # 策略模板（每个标的会复制独立实例）
        self.strategy_templates: list[type[BaseStrategy]] = [
            TurtleBreakoutStrategy,
            DualMAStrategy,
            ATRChannelStrategy,
        ]

    @staticmethod
    def _default_config() -> dict:
        return {
            # 入场参数（v4网格搜索优化：6标的池max_positions=4最优）
            "entry_period": 8,              # 唐奇安通道入场周期
            "exit_period": 10,              # 唐奇安通道离场周期（拉长→减少过早离场）
            "adx_threshold": 3,            # ADX趋势强度阈值（3/5/8等效，仓位被max_symbol_weight封顶）
            "adx_period": 14,
            "atr_period": 20,
            "rsi_period": 14,
            "ma_short": 20,
            "ma_long": 50,

            # 止损参数
            "atr_multiplier": 0.5,          # ATR初始止损0.5N（紧止损→单笔风险可控）
            "trail_atr_mult": 3.0,          # 追踪止损：从最高价回撤3.0N（更紧止损→锁定更多利润）
            "channel_mult": 1.5,            # ATR通道上轨倍数
            "channel_lower_mult": 1.5,

            # 仓位参数
            "risk_pct": 0.10,               # 单次建仓风险比例10%
            "hard_stop": 0.07,              # 硬止损7%
            "strategy_weight": 0.95,        # 策略仓位权重
            "max_symbol_weight": 0.60,      # 单标的仓位上限60%
            "max_total_weight": 0.98,
            "max_units": 2,                 # 单标的最大加仓单位数（含首仓，即最多加仓1次）

            # 风控参数
            "max_drawdown": 0.25,           # 25%回撤熔断
            "cooldown_days": 5,
            "daily_loss_limit": 0.06,

            # 动量轮动
            "momentum_lookback": 5,         # 动量回看周期5天
            "max_positions": 4,             # 最多同时持有4个标的（6标的池中集中持仓）

            # 熔断策略
            "liquidate_on_circuit_breaker": True,

            # 交易成本
            "commission_rate": 0.00025,     # 佣金0.025%
            "stamp_duty": 0.0005,          # 印花税0.05%（卖出）
            "slippage": 0.001,              # 滑点0.1%

            # 信号管理
            "max_pending_buy_days": 3,      # 买入信号最大保留天数（过期丢弃）
            "force_close_on_end": True,     # 数据结束时强制平仓
        }

    @staticmethod
    def _require_finite(value, name: str):
        """校验参数为有限数"""
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"配置项 '{name}' 必须是有限数, 当前: {value!r}")

    @staticmethod
    def _require_positive(value, name: str):
        """校验参数为正数"""
        BacktestEngine._require_finite(value, name)
        if value <= 0:
            raise ValueError(f"配置项 '{name}' 必须为正数, 当前: {value}")

    @staticmethod
    def _require_int(value, name: str):
        """校验参数为整数"""
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"配置项 '{name}' 必须是整数, 当前: {value!r}")

    @staticmethod
    def _require_range(value, name: str, lo: float, hi: float):
        """校验参数在 [lo, hi] 范围内"""
        BacktestEngine._require_finite(value, name)
        if not (lo <= value <= hi):
            raise ValueError(f"配置项 '{name}' 必须在 [{lo}, {hi}] 范围内, 当前: {value}")

    def _validate_config(self):
        """启动前校验全部关键配置参数"""
        c = self.cfg
        # 正数校验
        for k in ["entry_period", "exit_period", "adx_period", "atr_period",
                   "rsi_period", "ma_short", "ma_long", "max_units", "cooldown_days",
                   "momentum_lookback", "max_positions", "max_pending_buy_days"]:
            self._require_int(c.get(k, 0), k)
        for k in ["initial_capital"] if False else []:
            pass  # initial_capital 在 __init__ 校验
        # 正浮点数校验
        for k in ["atr_multiplier", "trail_atr_mult", "channel_mult", "channel_lower_mult",
                   "risk_pct", "hard_stop", "strategy_weight", "max_symbol_weight",
                   "max_total_weight", "max_drawdown", "daily_loss_limit",
                   "commission_rate", "stamp_duty", "slippage"]:
            self._require_positive(c.get(k, 0), k)
        # 范围校验
        self._require_range(c.get("strategy_weight", 0.9), "strategy_weight", 0, 1)
        self._require_range(c.get("max_symbol_weight", 0.98), "max_symbol_weight", 0, 1)
        self._require_range(c.get("max_total_weight", 0.98), "max_total_weight", 0, 1)
        self._require_range(c.get("max_drawdown", 0.15), "max_drawdown", 0, 1)
        self._require_range(c.get("daily_loss_limit", 0.06), "daily_loss_limit", 0, 1)
        self._require_range(c.get("hard_stop", 0.07), "hard_stop", 0, 1)
        self._require_range(c.get("risk_pct", 0.05), "risk_pct", 0, 1)
        self._require_range(c.get("commission_rate", 0.00025), "commission_rate", 0, 0.01)
        self._require_range(c.get("stamp_duty", 0.0005), "stamp_duty", 0, 0.01)
        self._require_range(c.get("slippage", 0.001), "slippage", 0, 0.05)
        self._require_range(c.get("adx_threshold", 8), "adx_threshold", 0, 100)
        # 逻辑关系校验
        if c.get("ma_short", 20) >= c.get("ma_long", 50):
            raise ValueError(f"ma_short({c['ma_short']}) 必须小于 ma_long({c['ma_long']})")
        if c.get("entry_period", 8) < 1:
            raise ValueError(f"entry_period 必须 >= 1")

    def run(self, symbols_dict: dict[str, str], start_date: str, end_date: str) -> dict:
        """
        多标的组合回测。
        symbols_dict: {code: name}, 如 {'300308': '中际旭创'}
        返回: 回测结果字典
        """
        # 配置校验
        self._validate_config()

        # 重置引擎状态（防止同一实例多次调用 run() 时状态残留）
        self.cash = self.initial_capital
        self.positions = {}
        self.trades = []
        self.equity_curve = []
        self.risk = RiskManager(self.cfg)
        self.strategy_instances = {}
        self.global_last_date = None
        self.symbol_last_dates = {}
        pending_signals = []

        start_ts = pd.Timestamp(start_date)
        end_ts = pd.Timestamp(end_date)

        print(f"\n{'='*60}")
        print(f"AQuant 回测启动")
        print(f"  资金: {self.initial_capital:,.0f}")
        print(f"  标的: {symbols_dict}")
        print(f"  区间: {start_date} ~ {end_date}")
        print(f"{'='*60}\n")

        # 1. 获取所有标的数据（含验证 + 区间裁剪）
        data_map: dict[str, pd.DataFrame] = {}
        ind_map: dict[str, dict] = {}
        for code, name in symbols_dict.items():
            print(f"  获取 {name}({code}) 数据...")
            df = DataFetcher.fetch_stock_data(code, start_date, end_date)
            # 数据源可能返回超出请求区间的数据，必须再次裁剪
            df = df[(df.index >= start_ts) & (df.index <= end_ts)].copy()
            # 数据验证：重复日期、非正价格、OHLC关系
            df = DataFetcher.validate_ohlcv(df, code)
            if df.empty:
                raise RuntimeError(f"{code} 在 {start_date} ~ {end_date} 内没有有效行情数据")
            data_map[code] = df
            ind_map[code] = Indicators.compute_all(df, self.cfg)
            print(f"  {name}({code}): {len(df)}条数据, 区间 {df.index[0].date()} ~ {df.index[-1].date()}")

        if not data_map:
            raise RuntimeError("未获取到任何数据")

        # 2. 构建统一交易日历 + 记录每个标的的最后交易日
        all_dates = sorted(set(d for df in data_map.values() for d in df.index))
        self.global_last_date = pd.Timestamp(all_dates[-1])
        self.symbol_last_dates = {code: pd.Timestamp(df.index[-1]) for code, df in data_map.items()}
        print(f"\n  交易日总数: {len(all_dates)}")

        # 3. 每个标的复制独立策略实例
        self.strategy_instances = {
            code: [cls(self.cfg) for cls in self.strategy_templates]
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
                pending_signals = self._execute_pending_signals(pending_signals, data_map, date, all_dates)
            else:
                pending_signals = []

            # ── Step 1b: 数据结束处理 — 某标的最后交易日已过，强制平仓 ──
            if self.cfg.get("force_close_on_end", True):
                self._close_positions_on_data_end(data_map, date, all_dates, symbols_dict)

            # ── Step 2: 计算当前总资产（用今日收盘价）──
            current_assets = self._total_assets(data_map, date)

            # ── Step 3: 组合级风控检查 ──
            risk_status = self.risk.check_portfolio_risk(current_assets, date_str, trading_dates=all_dates)
            risk_blocked = False
            liquidate = False  # 初始化，避免后续引用未定义变量
            if risk_status:
                if risk_status == "组合回撤熔断":
                    liquidate = self.cfg.get("liquidate_on_circuit_breaker", True)
                    if liquidate:
                        print(f"  ⚠ [{date_str}] {risk_status}! 生成清仓信号(T+1执行), 冷却{self.cfg['cooldown_days']}日")
                        # [FIX P0-2] 不再用当日收盘价立即清仓（前瞻偏差）
                        # 改为生成卖出信号加入 pending_signals，T+1 开盘价执行
                        liquidation_signals = self._generate_liquidation_signals(date_str)
                        pending_signals = liquidation_signals
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
                            if signal.direction == "sell":
                                pending_signals.append((signal, strategy))
                            # buy信号在熔断期间被阻止

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
            top_symbols = set()
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

                    # 动量轮动过滤：买入信号只允许动量Top N或已持仓的标的
                    if signal.direction == "buy":
                        if code not in top_symbols and code not in held_symbols:
                            continue  # 动量不够，跳过买入

                    pending_signals.append((signal, strategy))

            # ── Step 5: 记录每日权益 ──
            daily_assets = self._total_assets(data_map, date)
            self.equity_curve.append({
                "date": date_str,
                "assets": daily_assets,
                "cash": self.cash,
                "position_value": daily_assets - self.cash,
            })

        # 5. 末日清算
        last_date = all_dates[-1]
        final_assets = self._total_assets(data_map, last_date)
        print(f"\n  回测完成: 初始 {self.initial_capital:,.0f} → 终值 {final_assets:,.0f}")

        return self._build_result(final_assets, all_dates, data_map)

    def _execution_mark_prices(self, data_map: dict[str, pd.DataFrame],
                                date: pd.Timestamp) -> dict[str, float]:
        """获取T+1开盘执行时可用于估值的价格：
        - 优先用当日开盘价（此时收盘价还不可用）
        - 如果当日无开盘价（停牌），用最近的历史收盘价
        这消除了原版用当日收盘价做开盘风控的前瞻偏差。
        """
        prices = {}
        for code, df in data_map.items():
            if date in df.index:
                open_price = df.loc[date, "open"]
                if not pd.isna(open_price) and open_price > 0:
                    prices[code] = open_price
                else:
                    # 当日无开盘价，用最近收盘价
                    mask = df.index <= date
                    if mask.any():
                        prices[code] = df.loc[mask, "close"].iloc[-1]
            else:
                mask = df.index <= date
                if mask.any():
                    prices[code] = df.loc[mask, "close"].iloc[-1]
        return prices

    def _total_assets_at_prices(self, prices: dict[str, float]) -> float:
        """用给定价格集计算总资产（用于开盘执行阶段，避免使用收盘价）"""
        total = self.cash
        for code, positions in self.positions.items():
            price = prices.get(code, 0)
            for pos in positions.values():
                total += pos.market_value_at(price)
        return total

    def _buy_signal_expired(self, signal: Signal, current_date: pd.Timestamp,
                             all_dates: list[pd.Timestamp]) -> bool:
        """检查买入信号是否过期（超过max_pending_buy_days个交易日）"""
        if not signal.signal_date or signal.direction != "buy":
            return False
        max_days = self.cfg.get("max_pending_buy_days", 3)
        try:
            sig_date = pd.Timestamp(signal.signal_date)
            current_idx = all_dates.index(current_date)
            sig_idx = all_dates.index(sig_date)
            return (current_idx - sig_idx) > max_days
        except (ValueError, KeyError):
            return False

    def _close_positions_on_data_end(self, data_map: dict, date: pd.Timestamp,
                                      all_dates: list, symbols_dict: dict):
        """数据结束处理：某标的的最后交易日已过，强制平仓该标的的持仓"""
        date_str = date.strftime("%Y-%m-%d")
        codes_to_close = []
        for code in list(self.positions.keys()):
            if code not in self.symbol_last_dates:
                continue
            if date > self.symbol_last_dates[code]:
                # 该标的数据已结束，用最后交易日收盘价强制平仓
                codes_to_close.append(code)

        if not codes_to_close:
            return

        cfg = self.cfg
        slippage = cfg.get("slippage", 0.001)
        commission_rate = cfg.get("commission_rate", 0.00025)
        stamp_duty = cfg.get("stamp_duty", 0.0005)

        for code in codes_to_close:
            last_data_date = self.symbol_last_dates[code]
            close_price = data_map[code].loc[last_data_date, "close"]
            exec_price = close_price * (1 - slippage)

            for strat_name in list(self.positions[code].keys()):
                pos = self.positions[code][strat_name]
                sell_value = pos.shares * exec_price
                commission = sell_value * commission_rate
                stamp_duty_cost = sell_value * stamp_duty
                net_proceeds = sell_value - commission - stamp_duty_cost
                pnl = net_proceeds - pos.cost
                pnl_pct = pnl / pos.cost if pos.cost > 0 else 0

                self.cash += net_proceeds
                self.trades.append(TradeRecord(
                    symbol=code, strategy_name=strat_name, direction="sell",
                    shares=pos.shares, price=exec_price, date=last_data_date.strftime("%Y-%m-%d"),
                    reason="数据结束强制平仓", pnl=pnl, pnl_pct=pnl_pct,
                    signal_date=date_str,
                    gross_value=sell_value, commission=commission, stamp_duty=stamp_duty_cost,
                    slippage_cost=pos.shares * close_price * slippage,
                    net_cash_flow=net_proceeds, cash_after=self.cash,
                ))
                del self.positions[code][strat_name]

            if not self.positions[code]:
                del self.positions[code]

            # 清除策略实例持仓引用
            if code in self.strategy_instances:
                for strategy in self.strategy_instances[code]:
                    strategy.position = None

            print(f"  [{date_str}] {code} 数据已结束，强制平仓")

    def _dedupe_pending_signals(self, pending: list[tuple[Signal, BaseStrategy]]
                                 ) -> list[tuple[Signal, BaseStrategy]]:
        """信号去重：同一标的+策略只保留最新信号，卖出优先于买入"""
        seen: dict[tuple[str, str], tuple[Signal, BaseStrategy]] = {}
        # 先按原始顺序遍历，后出现的覆盖先出现的
        for signal, strategy in pending:
            key = (signal.symbol, signal.strategy_name)
            if key in seen:
                existing = seen[key]
                # 如果新信号是卖出，优先覆盖
                if signal.direction == "sell" and existing[0].direction != "sell":
                    seen[key] = (signal, strategy)
                # 如果同方向，用新的覆盖旧的
                elif signal.direction == existing[0].direction:
                    seen[key] = (signal, strategy)
            else:
                seen[key] = (signal, strategy)
        return list(seen.values())

    def _has_pending_liquidation(self, pending: list[tuple[Signal, BaseStrategy]]) -> bool:
        """检查是否有待执行的熔断清仓信号"""
        return any(s.reason == "熔断清仓" for s, _ in pending)

    def _execute_pending_signals(self, pending: list[tuple[Signal, BaseStrategy]],
                                  data_map: dict[str, pd.DataFrame], date: pd.Timestamp,
                                  all_dates: list) -> list[tuple[Signal, BaseStrategy]]:
        """以今日开盘价执行昨日产生的信号（卖出优先于买入）
        
        返回未能执行的信号列表（如停牌、涨跌停无法成交），供次日继续尝试。
        """
        date_str = date.strftime("%Y-%m-%d")

        # 信号去重
        pending = self._dedupe_pending_signals(pending)

        # 买入信号过期检查
        filtered = []
        for signal, strategy in pending:
            if signal.direction == "buy" and self._buy_signal_expired(signal, date, all_dates):
                print(f"  [{date_str}] 买入信号过期丢弃: {signal.symbol} {signal.strategy_name} (信号日:{signal.signal_date})")
                continue
            filtered.append((signal, strategy))
        pending = filtered

        # 卖出优先：先执行卖出信号释放资金和仓位，再执行买入信号
        sorted_pending = sorted(pending, key=lambda x: 0 if x[0].direction == "sell" else 1)

        # 获取开盘执行时的标记价格（用开盘价，而非收盘价）
        mark_prices = self._execution_mark_prices(data_map, date)

        unexecuted = []
        for signal, strategy in sorted_pending:
            code = signal.symbol
            if code not in data_map or date not in data_map[code].index:
                # 停牌：信号保留到次日
                unexecuted.append((signal, strategy))
                continue

            open_price = data_map[code].loc[date, "open"]
            if pd.isna(open_price) or open_price <= 0:
                unexecuted.append((signal, strategy))
                continue

            # 涨跌停板限制
            df = data_map[code]
            loc = df.index.get_loc(date)
            if loc > 0:
                prev_close = df.iloc[loc - 1]["close"]
                if prev_close > 0:
                    change_pct = (open_price - prev_close) / prev_close
                    if code.startswith(("3", "68")):
                        limit_up = 0.20
                    elif code.startswith(("8", "4", "9")):
                        limit_up = 0.30
                    else:
                        limit_up = 0.10
                    limit_down = -limit_up
                    if signal.direction == "buy" and change_pct >= limit_up - 0.001:
                        # 开盘涨停，无法买入 → 丢弃买入信号
                        continue
                    if signal.direction == "sell" and change_pct <= limit_down + 0.001:
                        # 开盘跌停，无法卖出 → 卖出信号保留到次日继续尝试
                        unexecuted.append((signal, strategy))
                        continue

            # 用开盘价覆盖信号价格
            signal.price = open_price

            if signal.direction == "buy":
                self._execute_buy(signal, strategy, date_str, mark_prices)
            elif signal.direction == "sell":
                self._execute_sell(signal, strategy, date_str)

        return unexecuted

    def _total_assets(self, data_map: dict[str, pd.DataFrame], date: pd.Timestamp) -> float:
        """计算当前总资产 = 现金 + 所有持仓市值（用指定日期收盘价）"""
        total = self.cash
        for code, positions in self.positions.items():
            if code not in data_map:
                continue
            df = data_map[code]
            if date not in df.index:
                mask = df.index <= date
                if mask.any():
                    price = df.loc[mask, "close"].iloc[-1]
                else:
                    price = 0  # 标的在该日期之前无数据（极端边界：不会实际触发）
            else:
                price = df.loc[date, "close"]

            for pos in positions.values():
                total += pos.market_value_at(price)
        return total

    def _get_current_prices(self, data_map: dict[str, pd.DataFrame], date: pd.Timestamp) -> dict[str, float]:
        """获取指定日期所有标的的收盘价，缺失时使用最近可用价格"""
        prices = {}
        for code, df in data_map.items():
            if date in df.index:
                prices[code] = df.loc[date, "close"]
            else:
                mask = df.index <= date
                if mask.any():
                    prices[code] = df.loc[mask, "close"].iloc[-1]
                # 如果没有任何历史数据，不设该标的的价格（风控中将使用entry_price兜底）
        return prices

    def _execute_buy(self, signal: Signal, strategy: BaseStrategy, date_str: str,
                     mark_prices: dict[str, float] | None = None):
        """执行买入（含滑点+佣金），使用开盘标记价格做风控（消除前瞻偏差）"""
        if signal.target_shares <= 0 or signal.price <= 0:
            return

        cfg = self.cfg
        slippage = cfg.get("slippage", 0.001)
        commission_rate = cfg.get("commission_rate", 0.00025)

        # 滑点：买入价上浮
        exec_price = signal.price * (1 + slippage)
        shares = signal.target_shares
        buy_value = shares * exec_price
        commission = buy_value * commission_rate
        total_cost = buy_value + commission

        # 资金不足，缩减股数
        if total_cost > self.cash:
            shares = int(self.cash / (exec_price * (1 + commission_rate)) / 100) * 100
            if shares <= 0:
                return
            buy_value = shares * exec_price
            commission = buy_value * commission_rate
            total_cost = buy_value + commission

        # 用开盘标记价格计算总资产做风控（消除前瞻偏差）
        if mark_prices is not None:
            current_assets = self._total_assets_at_prices(mark_prices)
        else:
            # mark_prices 不可用时，用现金+持仓入场价估算（保守值）
            current_assets = self.cash + sum(
                p.market_value_at(p.entry_price)
                for sym_positions in self.positions.values()
                for p in sym_positions.values()
            )

        # 仓位上限约束：当目标买入金额超过 max_symbol_weight 或 max_total_weight 时，
        # 自动缩减股数到上限以内，而非直接拒绝（避免策略完全无法建仓）
        cfg = self.cfg
        max_symbol_w = cfg.get("max_symbol_weight", 0.60)
        max_total_w = cfg.get("max_total_weight", 0.98)
        # 该标的当前持仓市值
        symbol_value = sum(
            p.shares * mark_prices.get(signal.symbol, p.entry_price)
            for p in self.positions.get(signal.symbol, {}).values()
        ) if mark_prices else 0
        # 总持仓市值
        total_position_value = sum(
            p.shares * mark_prices.get(sym, p.entry_price)
            for sym, sym_positions in self.positions.items()
            for p in sym_positions.values()
        ) if mark_prices else 0

        # 单标的可买入金额上限
        symbol_room = current_assets * max_symbol_w - symbol_value
        # 总仓位可买入金额上限
        total_room = current_assets * max_total_w - total_position_value
        # 资金可买入金额上限
        cash_room = self.cash
        # 取三重约束的最小值
        max_buy_value = min(symbol_room, total_room, cash_room)
        if max_buy_value <= 0:
            return
        # 如果目标买入金额超限，缩减股数
        if buy_value > max_buy_value:
            shares = int(max_buy_value / (exec_price * (1 + commission_rate)) / 100) * 100
            if shares <= 0:
                return
            buy_value = shares * exec_price
            commission = buy_value * commission_rate
            total_cost = buy_value + commission

        # 单日亏损检查（双闸门：执行阶段也检查）
        if self.risk.check_daily_loss(current_assets):
            return

        # 执行
        self.cash -= total_cost
        if signal.symbol not in self.positions:
            self.positions[signal.symbol] = {}

        # entry_price 包含佣金成本
        effective_entry = exec_price * (1 + commission_rate)

        if strategy.name in self.positions[signal.symbol]:
            # 加仓：更新加权平均价
            pos = self.positions[signal.symbol][strategy.name]
            total_cost_basis = pos.cost + total_cost
            total_shares = pos.shares + shares
            pos.entry_price = total_cost_basis / total_shares
            pos.shares = total_shares
            pos.units += 1
            pos.stop_loss = max(pos.stop_loss, signal.stop_loss)
            pos.highest_since_entry = max(pos.highest_since_entry, exec_price)
        else:
            self.positions[signal.symbol][strategy.name] = Position(
                symbol=signal.symbol,
                strategy_name=strategy.name,
                shares=shares,
                entry_price=effective_entry,
                entry_date=date_str,
                stop_loss=signal.stop_loss,
                highest_since_entry=exec_price,
                units=1,
            )

        # 更新策略持仓引用
        strategy.position = self.positions[signal.symbol][strategy.name]

        # 记录交易（含完整现金流明细）
        slippage_cost = shares * signal.price * slippage
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
            stamp_duty=0,
            slippage_cost=slippage_cost,
            net_cash_flow=-total_cost,
            cash_after=self.cash,
        ))

    def _execute_sell(self, signal: Signal, strategy: BaseStrategy, date_str: str):
        """执行卖出（含滑点+佣金+印花税）"""
        if signal.target_shares <= 0:
            return
        # 检查策略持仓是否存在（不能用 symbol in positions 判断，因为可能被同批次先执行的卖出删除了标的键）
        pos = None
        if signal.symbol in self.positions:
            pos = self.positions[signal.symbol].get(strategy.name)
        if pos is None:
            # 标的键已被同批次删除，但该策略可能仍有持仓引用 — 检查策略实例
            if strategy.position is not None:
                pos = strategy.position
            else:
                return

        cfg = self.cfg
        slippage = cfg.get("slippage", 0.001)
        commission_rate = cfg.get("commission_rate", 0.00025)
        stamp_duty = cfg.get("stamp_duty", 0.0005)

        # 滑点：卖出价下浮
        exec_price = signal.price * (1 - slippage)

        sell_shares = min(signal.target_shares, pos.shares)
        sell_value = sell_shares * exec_price
        commission = sell_value * commission_rate
        stamp_duty_cost = sell_value * stamp_duty
        net_proceeds = sell_value - commission - stamp_duty_cost

        # PnL = 净收入 - 成本基准
        cost_basis = sell_shares * pos.entry_price
        pnl = net_proceeds - cost_basis
        pnl_pct = pnl / cost_basis if cost_basis > 0 else 0

        self.cash += net_proceeds

        pos.shares -= sell_shares
        if pos.shares <= 0:
            # 从引擎持仓字典中删除（如果还存在的话）
            if signal.symbol in self.positions and strategy.name in self.positions[signal.symbol]:
                del self.positions[signal.symbol][strategy.name]
                # 清理空字典：某标的所有策略持仓都已卖出时，删除该标的键
                if not self.positions[signal.symbol]:
                    del self.positions[signal.symbol]
            strategy.position = None
        else:
            strategy.position = pos

        # 记录交易（含完整现金流明细）
        slippage_cost = sell_shares * signal.price * slippage
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
            stamp_duty=stamp_duty_cost,
            slippage_cost=slippage_cost,
            net_cash_flow=net_proceeds,
            cash_after=self.cash,
        ))

    def _generate_liquidation_signals(self, date_str: str) -> list[tuple[Signal, BaseStrategy]]:
        """生成全仓清仓信号 — 从引擎持仓字典生成（而非策略实例引用），避免不同步"""
        signals = []
        for code, sym_positions in self.positions.items():
            for strat_name, pos in sym_positions.items():
                # 找到对应的策略实例
                strategy = None
                if code in self.strategy_instances:
                    for s in self.strategy_instances[code]:
                        if s.name == strat_name:
                            strategy = s
                            break
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

    def _liquidate_all(self, data_map: dict, date: pd.Timestamp, reason: str = "熔断清仓"):
        """全仓清仓 — 以当日收盘价成交，并清除所有策略持仓引用"""
        cfg = self.cfg
        slippage = cfg.get("slippage", 0.001)
        commission_rate = cfg.get("commission_rate", 0.00025)
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
                commission = sell_value * commission_rate
                stamp_duty_cost = sell_value * stamp_duty
                net_proceeds = sell_value - commission - stamp_duty_cost

                pnl = net_proceeds - pos.cost
                pnl_pct = pnl / pos.cost if pos.cost > 0 else 0

                self.cash += net_proceeds
                self.trades.append(TradeRecord(
                    symbol=code, strategy_name=strat_name, direction="sell",
                    shares=pos.shares, price=exec_price, date=date_str,
                    reason=reason, pnl=pnl, pnl_pct=pnl_pct,
                    signal_date=date_str,
                    gross_value=sell_value, commission=commission,
                    stamp_duty=stamp_duty_cost,
                    slippage_cost=pos.shares * close_price * slippage,
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
        annual_return = (1 + total_return) ** (252 / max(n_trading_days, 1)) - 1

        # 最大回撤
        peak = eq["assets"].cummax()
        drawdown = (eq["assets"] - peak) / peak
        max_drawdown = drawdown.min()

        # 日收益率
        daily_returns = eq["assets"].pct_change().dropna()
        sharpe = 0
        if daily_returns.std() > 0:
            sharpe = daily_returns.mean() / daily_returns.std() * math.sqrt(252)

        # 胜率与盈亏比 — 用总盈利/总亏损计算profit_factor
        sell_trades = [t for t in self.trades if t.direction == "sell"]
        wins = [t for t in sell_trades if t.pnl > 0]
        losses = [t for t in sell_trades if t.pnl < 0]
        # pnl=0 的交易（平价卖出）既不算胜也不算负
        win_rate = len(wins) / len(sell_trades) if sell_trades else 0
        total_win = sum(t.pnl for t in wins) if wins else 0
        total_loss = abs(sum(t.pnl for t in losses)) if losses else 0
        profit_factor = total_win / total_loss if total_loss > 0 else float('inf')
        if profit_factor == float('inf'):
            profit_factor = 999.99  # 避免CSV导出显示inf

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
        print(f"  盈亏比:     {result['profit_factor']:>15.2f}")
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

    @staticmethod
    def save_result(result: dict, output_dir: str = "."):
        """导出回测结果到4个CSV文件：交易记录、权益曲线、回撤序列、绩效摘要"""
        os.makedirs(output_dir, exist_ok=True)

        # 1. 交易记录
        trades = result.get("trades", [])
        if trades:
            trades_data = []
            for t in trades:
                trades_data.append({
                    "执行日期": t.date,
                    "信号日期": t.signal_date,
                    "标的": t.symbol,
                    "策略": t.strategy_name,
                    "方向": t.direction,
                    "股数": t.shares,
                    "执行价格": round(t.price, 4),
                    "成交金额": round(t.gross_value, 2),
                    "佣金": round(t.commission, 2),
                    "印花税": round(t.stamp_duty, 2),
                    "滑点成本": round(t.slippage_cost, 2),
                    "净现金流": round(t.net_cash_flow, 2),
                    "交易后现金": round(t.cash_after, 2),
                    "盈亏": round(t.pnl, 2) if t.direction == "sell" else "",
                    "盈亏比例": f"{t.pnl_pct:.2%}" if t.direction == "sell" else "",
                    "原因": t.reason,
                })
            trades_df = pd.DataFrame(trades_data)
            trades_path = os.path.join(output_dir, "trades.csv")
            trades_df.to_csv(trades_path, index=False, encoding="utf-8-sig")
            print(f"  交易记录已保存: {trades_path} ({len(trades_data)}条)")

        # 2. 权益曲线
        eq = result.get("equity_curve")
        if eq is not None and not eq.empty:
            eq_path = os.path.join(output_dir, "equity_curve.csv")
            eq.to_csv(eq_path, encoding="utf-8-sig")
            print(f"  权益曲线已保存: {eq_path} ({len(eq)}条)")

        # 3. 回撤序列
        dd = result.get("drawdown_series")
        if dd is not None and not dd.empty:
            dd_path = os.path.join(output_dir, "drawdown.csv")
            dd.to_csv(dd_path, encoding="utf-8-sig")
            print(f"  回撤序列已保存: {dd_path} ({len(dd)}条)")

        # 4. 绩效摘要
        summary = {
            "初始资金": result["initial_capital"],
            "终值": result["final_assets"],
            "总收益率": f"{result['total_return']:.2%}",
            "年化收益率": f"{result['annual_return']:.2%}",
            "最大回撤": f"{result['max_drawdown']:.2%}",
            "夏普比率": f"{result['sharpe']:.2f}",
            "胜率": f"{result['win_rate']:.2%}",
            "盈亏比": f"{result['profit_factor']:.2f}",
            "总交易次数": result["total_trades"],
            "卖出次数": result["sell_trades"],
        }
        summary_path = os.path.join(output_dir, "summary.csv")
        pd.DataFrame([summary]).to_csv(summary_path, index=False, encoding="utf-8-sig")
        print(f"  绩效摘要已保存: {summary_path}")


# ═══════════════════════════════════════════════════════════════════════
#  CLI入口
# ═══════════════════════════════════════════════════════════════════════

DEFAULT_SYMBOLS = {
    # 通信/光模块标的池
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
    # 半导体设备/材料标的池
    "688249": "晶合集成",
    "688347": "华虹公司",
    "300666": "江丰电子",
    "600206": "有研新材",
    "688409": "富创精密",
    "688361": "中科飞测",
    "300604": "长川科技",
    "688120": "华海清科",
    "688082": "盛美上海",
    "688981": "中芯国际",
    "002371": "北方华创",
    "688012": "中微公司",
}

DEFAULT_SYMBOL_NAMES = {v: k for k, v in DEFAULT_SYMBOLS.items()}

# A股代码格式：6位数字
_SYMBOL_RE = re.compile(r"^\d{6}$")


def parse_symbols(symbols_str: str) -> dict[str, str]:
    """解析命令行标的参数: '300308,300502' 或 '中际旭创,新易盛'"""
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
            result[s] = s  # 代码作为名称
        else:
            raise ValueError(f"无效的股票代码或名称: '{s}'（需6位数字代码或预设名称）")
    return result


def main():
    parser = argparse.ArgumentParser(description="AQuant — A股自动量化交易系统")
    sub = parser.add_subparsers(dest="command")

    bt = sub.add_parser("backtest", help="运行回测")
    bt.add_argument("--symbol", "-s", default="300308", help="标的代码(逗号分隔)")
    bt.add_argument("--start", default="2025-01-01", help="开始日期")
    bt.add_argument("--end", default="2026-06-30", help="结束日期")
    bt.add_argument("--capital", type=float, default=2_000_000, help="初始资金")
    bt.add_argument("--save-csv", default="", help="CSV导出目录路径（留空则不导出）")

    args = parser.parse_args()

    if args.command == "backtest":
        symbols_dict = parse_symbols(args.symbol)
        engine = BacktestEngine(initial_capital=args.capital)
        result = engine.run(symbols_dict, args.start, args.end)

        PerformanceReport.print_report(result, symbols_dict)

        if args.save_csv:
            PerformanceReport.save_result(result, args.save_csv)

        PerformanceReport.plot_equity_curve(result)

        return result


if __name__ == "__main__":
    main()
