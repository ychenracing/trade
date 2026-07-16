"""技术指标计算。

所有函数返回与输入等长、索引对齐的 pandas.Series。为避免前视偏差，
通道类指标均使用 shift(1)：即用「截至上一根」的 N 日极值作为当根信号阈值。
"""
import numpy as np
import pandas as pd


def true_range(high, low, close) -> pd.Series:
    """真实波幅 TR = max(H-L, |H-前收|, |L-前收|)。"""
    high = pd.Series(high, dtype="float64")
    low = pd.Series(low, dtype="float64")
    close = pd.Series(close, dtype="float64")
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr


def atr(high, low, close, period: int = 20) -> pd.Series:
    """平均真实波幅（Wilder 指数平滑）。"""
    tr = true_range(high, low, close)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def donchian_upper(high, period: int) -> pd.Series:
    """入场突破上轨：前 period 日最高价（不含当根）。"""
    return pd.Series(high, dtype="float64").rolling(period).max().shift(1)


def donchian_lower(low, period: int) -> pd.Series:
    """离场跌破下轨：前 period 日最低价（不含当根）。"""
    return pd.Series(low, dtype="float64").rolling(period).min().shift(1)


def moving_average(close, period: int) -> pd.Series:
    """简单移动平均线。"""
    return pd.Series(close, dtype="float64").rolling(period).mean()


def add_indicators(df: pd.DataFrame, cfg, donchian_periods: set = None) -> pd.DataFrame:
    """在行情 DataFrame 上追加所有回测所需指标列。

    donchian_periods：需计算的唐奇安通道窗口集合（入场上轨 / 离场下轨共用）。
    为 None 时默认取 cfg.turtle_entries 与 cfg.turtle_exits 的并集，保证自定义
    参数扫描时策略所需的任意周期通道列均存在。
    """
    df = df.copy()
    df["atr"] = atr(df["high"], df["low"], df["close"], cfg.atr_period)
    if donchian_periods is None:
        donchian_periods = set(cfg.turtle_entries) | set(cfg.turtle_exits)
    for p in sorted(donchian_periods):
        df[f"donchian_upper_{p}"] = donchian_upper(df["high"], p)
        df[f"donchian_lower_{p}"] = donchian_lower(df["low"], p)
    df["ma_fast"] = moving_average(df["close"], cfg.ma_fast)
    df["ma_slow"] = moving_average(df["close"], cfg.ma_slow)
    return df
