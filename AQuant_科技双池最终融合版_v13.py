#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AQuant A股科技双池趋势回测系统（v13）。

系统将标的分为海外算力与国产半导体两个产业池，并在六个产业子类之间自动路由参数。
每个标的同时运行海龟突破、双均线和 ATR 通道三套策略；T 日收盘生成信号，T+1 开盘
按固定顺序执行。组合共享现金，默认最多持有 6 个标的。

主要约束：
    * 行情必须是前复权日线，字段至少包含日期、open、close、high、low。
    * 回测中的涨跌停判断是基于开盘价的可成交近似，不模拟盘口排队与成交量冲击。
    * 回撤熔断只决定何时生成 T+1 清仓信号，不保证成交回撤等于配置阈值。
    * 任何样本内收益与回撤均不是未来承诺，隔夜跳空、连续跌停和流动性会扩大损失。

命令行示例：
    # 使用 AKShare 下载单票前复权行情
    python AQuant_科技双池最终融合版.py backtest --symbol 300308 \
        --start 2025-01-01 --end 2026-06-30

    # 使用固定的本地 CSV 快照回测全部默认标的并保存审计文件
    python AQuant_科技双池最终融合版.py backtest \
        --start 2025-01-01 --end 2026-06-30 \
        --data-dir data_qfq_reconstructed --save-dir results --no-plot

Python API 示例：
    >>> engine = BacktestEngine(initial_capital=2_000_000)
    >>> result = engine.run(
    ...     {"300308": "中际旭创", "688072": "拓荆科技"},
    ...     "2025-01-01", "2026-06-30",
    ...     data_dir="data_qfq_reconstructed",
    ... )
    >>> round(result["max_drawdown"], 4)  # 每次运行都应从固定快照重新计算
"""

import argparse
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
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
EXECUTION_PRIORITY = {
    code: rank for rank, code in enumerate((
        "300308", "300502", "300394", "688008", "603986", "002409",
        "688072", "688300", "300054", "688205", "920045", "300776",
        "688535", "688249", "688347", "300666", "600206", "688409",
        "688361", "300604", "688120", "688082",
    ))
}


def _is_finite_number(value: Any) -> bool:
    """判断输入能否转换为有限浮点数。

    Args:
        value: 待检查对象；整数、浮点数和数值字符串均可。

    Returns:
        可转换且不是 ``NaN``/``inf`` 时返回 ``True``。

    Example:
        >>> _is_finite_number("12.5")
        True
        >>> _is_finite_number(float("nan"))
        False
    """
    try:
        return bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


def _require_finite(name: str, value: Any, *, min_value: float | None = None,
                    max_value: float | None = None, inclusive_max: bool = True) -> float:
    """把配置值转换为有限浮点数并校验上下界。

    Args:
        name: 出错时显示的配置字段名。
        value: 待转换的配置值。
        min_value: 可选下界，包含该边界。
        max_value: 可选上界。
        inclusive_max: ``True`` 表示允许等于 ``max_value``。

    Returns:
        校验后的 ``float``。

    Raises:
        ValueError: 值不可转换、非有限或超出边界。

    Example:
        >>> _require_finite("slippage", "0.001", min_value=0, max_value=1)
        0.001
    """
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
    """校验严格大于零的有限浮点配置。

    Args:
        name: 配置字段名。
        value: 待校验值。
        max_value: 可选上界。
        inclusive_max: 是否允许等于上界。

    Returns:
        校验后的正浮点数。

    Raises:
        ValueError: 值不大于零、非有限或超过上界。

    Example:
        >>> _require_positive("risk_pct", 0.03, max_value=1, inclusive_max=False)
        0.03
    """
    value = _require_finite(name, value, max_value=max_value, inclusive_max=inclusive_max)
    if value <= 0:
        raise ValueError(f"配置 {name} 必须 > 0，当前为 {value}")
    return value


def _require_bool(name: str, value: Any) -> bool:
    """要求配置值必须是真正的布尔类型。

    Args:
        name: 配置字段名。
        value: 待校验值；字符串 ``"False"`` 不会被接受。

    Returns:
        原布尔值。

    Raises:
        ValueError: 值不是 ``bool``。

    Example:
        >>> _require_bool("force_close_on_end", False)
        False
    """
    if not isinstance(value, bool):
        raise ValueError(f"配置 {name} 必须是 bool，当前为 {value!r}")
    return value


def _require_int(name: str, value: Any, *, min_value: int = 0) -> int:
    """校验整数配置并排除 Python 中属于整数子类的布尔值。

    Args:
        name: 配置字段名。
        value: Python 整数或 NumPy 整数。
        min_value: 包含式下界。

    Returns:
        标准 Python ``int``。

    Raises:
        ValueError: 值不是整数或小于下界。

    Example:
        >>> _require_int("max_positions", np.int64(6), min_value=1)
        6
    """
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise ValueError(f"配置 {name} 必须是整数，当前为 {value!r}")
    value = int(value)
    if value < min_value:
        raise ValueError(f"配置 {name} 必须 >= {min_value}，当前为 {value}")
    return value


def _floor_to_lot(shares: float, lot_size: int = A_SHARE_LOT_SIZE) -> int:
    """把目标股数向下取整为指定交易单位。

    Args:
        shares: 原始目标股数。
        lot_size: 每手股数；A股普通买入默认 100 股。

    Returns:
        非负整手股数；无效或非正输入返回 0。

    Raises:
        ValueError: ``lot_size`` 不是正整数。

    Example:
        >>> _floor_to_lot(1288)
        1200
    """
    if isinstance(lot_size, bool) or not isinstance(lot_size, (int, np.integer)) or lot_size <= 0:
        raise ValueError(f"lot_size 必须是正整数，当前为 {lot_size!r}")
    if not _is_finite_number(shares) or float(shares) <= 0:
        return 0
    return int(float(shares) // lot_size) * lot_size


def _limit_pct_for_code(code: str, cfg: dict | None = None, name: str = "") -> float:
    """按代码、ST状态和覆盖表返回回测使用的涨跌停比例。

    Args:
        code: 六位 A 股代码。
        cfg: 可包含 ``per_symbol_limit_pct`` 与 ``st_symbols`` 的配置。
        name: 标的名称；名称包含 ``ST`` 时按 5% 处理。

    Returns:
        例如主板 0.10、创业板/科创板 0.20、北交所 0.30。

    Note:
        这是按当前板块规则的近似。历史制度变化或特殊首日规则应通过
        ``per_symbol_limit_pct`` 显式覆盖。

    Example:
        >>> _limit_pct_for_code("300308")
        0.2
        >>> _limit_pct_for_code("600000", {"st_symbols": {"600000"}})
        0.05
    """
    code = str(code)
    if not _SYMBOL_RE.match(code):
        raise ValueError(f"股票代码必须是6位数字，当前为 {code!r}")
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
    """解析常见行情日期格式并保留原索引。

    Args:
        values: 日期序列，支持 ``YYYY-MM-DD``、``YYYY/MM/DD``、``YYYYMMDD``。

    Returns:
        ``datetime64[ns]`` 类型的 Series；无法解析的元素为 ``NaT``。

    Example:
        >>> _parse_dates(pd.Series(["20250102", "2025/01/03"])).dt.day.tolist()
        [2, 3]
    """
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
    """读取并标准化 A 股前复权日线。

    在线模式依次尝试东方财富和新浪，每轮失败后重试；固定回测应优先使用本地 CSV，
    以免数据供应商的历史修订导致结果漂移。
    """

    @staticmethod
    def fetch_stock_data(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        """通过 AKShare 获取前复权日线。

        Args:
            symbol: 六位股票代码，例如 ``"300308"``。
            start_date: 开始日期，``YYYY-MM-DD``。
            end_date: 结束日期，``YYYY-MM-DD``。

        Returns:
            日期升序、以日期为索引的标准 OHLCV DataFrame。

        Raises:
            ImportError: 环境没有安装 AKShare。
            ValueError: 代码或日期范围非法，或供应商返回非法行情。
            RuntimeError: 两个数据源经过三轮尝试仍不可用。

        Example:
            >>> df = DataFetcher.fetch_stock_data("300308", "2025-01-01", "2025-03-31")
            >>> {"open", "close", "high", "low"}.issubset(df.columns)
            True
        """
        symbol = str(symbol)
        if not _SYMBOL_RE.match(symbol):
            raise ValueError(f"symbol 必须是6位数字代码，当前为 {symbol!r}")
        start_ts, end_ts = pd.Timestamp(start_date), pd.Timestamp(end_date)
        if start_ts > end_ts:
            raise ValueError("start_date 不能晚于 end_date")
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
            # AKShare 会透传不同供应商/网络栈的多种异常；此边界统一记录后切换备用源。
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
            # 同上：仅在外部数据源边界捕获，业务层不吞掉异常。
            except Exception as e:
                errors.append(f"新浪(尝试{attempt+1}): {e}")

            # --- 数据源3：腾讯（HTTP直连兜底） ---
            try:
                if symbol.startswith(("0", "3")):
                    tc_symbol = f"sz{symbol}"
                elif symbol.startswith(("8", "4", "9")):
                    tc_symbol = f"bj{symbol}"
                else:
                    tc_symbol = f"sh{symbol}"
                url = (
                    f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                    f"?param={tc_symbol},day,"
                    f"{start_date.replace('-','')},{end_date.replace('-','')},"
                    f"1000,qfq"
                )
                import urllib.request
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                })
                with urllib.request.urlopen(req, timeout=15) as resp:
                    raw = json.loads(resp.read().decode())
                key = f"{tc_symbol}"
                if key in raw.get("data", {}):
                    klines = raw["data"][key].get("qfqday", [])
                    if klines:
                        rows = []
                        for k in klines:
                            rows.append({
                                "date": k[0],
                                "open": float(k[1]),
                                "close": float(k[2]),
                                "high": float(k[3]),
                                "low": float(k[4]),
                                "volume": float(k[5]),
                            })
                        df = pd.DataFrame(rows)
                        df = DataFetcher._normalize_columns(df)
                        print(f"  [数据] {symbol}: 腾讯源, {len(df)}条")
                        return df
            except Exception as e:
                errors.append(f"腾讯(尝试{attempt+1}): {e}")

            if attempt < 2:
                time.sleep(1)

        raise RuntimeError(f"获取{symbol}数据失败(3次重试): {'; '.join(errors)}")

    @staticmethod
    def load_stock_data(symbol: str, start_date: str, end_date: str,
                        data_dir: str | None = None) -> pd.DataFrame:
        """从本地快照或 AKShare 读取行情。

        Args:
            symbol: 六位股票代码。
            start_date: 请求开始日期；本地文件会在 ``BacktestEngine.run`` 中裁剪。
            end_date: 请求结束日期。
            data_dir: CSV 目录。传入时读取 ``<data_dir>/<symbol>.csv``；为空则联网。

        Returns:
            经 :meth:`_normalize_columns` 校验的 OHLCV DataFrame。

        Raises:
            ValueError: 股票代码或日期范围非法。
            FileNotFoundError: 指定目录中没有对应 CSV。

        Example:
            >>> df = DataFetcher.load_stock_data(
            ...     "300308", "2025-01-01", "2025-12-31", "data_qfq_reconstructed"
            ... )
        """
        symbol = str(symbol)
        if not _SYMBOL_RE.match(symbol):
            raise ValueError(f"symbol 必须是6位数字代码，当前为 {symbol!r}")
        if pd.Timestamp(start_date) > pd.Timestamp(end_date):
            raise ValueError("start_date 不能晚于 end_date")
        if data_dir:
            path = Path(data_dir).expanduser() / f"{symbol}.csv"
            if not path.is_file():
                raise FileNotFoundError(f"缺少本地行情文件: {path}")
            return DataFetcher._normalize_columns(pd.read_csv(path))
        return DataFetcher.fetch_stock_data(symbol, start_date, end_date)

    @staticmethod
    def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
        """统一列名与日期索引，并拒绝不合法的 OHLCV。

        Args:
            df: 含中英文日期及价格列的原始 DataFrame。

        Returns:
            只含 ``open/close/high/low/volume`` 的新 DataFrame。

        Raises:
            ValueError: 数据为空、字段冲突、日期重复、价格非正或 OHLC 关系非法。

        Example:
            >>> raw = pd.DataFrame({
            ...     "日期": ["2025-01-02"], "开盘": [10], "收盘": [11],
            ...     "最高": [12], "最低": [9]
            ... })
            >>> DataFetcher._normalize_columns(raw).columns.tolist()
            ['open', 'close', 'high', 'low', 'volume']
        """
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
    """计算策略需要的历史技术指标。

    所有通道先 ``shift(1)``，因此当日突破判断不会把当日最高/最低提前放入基准。
    """

    @staticmethod
    def _wilder_average(series: pd.Series, period: int) -> pd.Series:
        """用首个完整窗口的 SMA 初始化 Wilder 递推平均。

        Args:
            series: 待平滑序列，允许前部包含 ``NaN``。
            period: 正整数窗口。

        Returns:
            与输入同索引的浮点 Series；初始化前为 ``NaN``。

        Raises:
            ValueError: ``period`` 不是正整数。

        Example:
            >>> s = pd.Series([1.0, 2.0, 3.0, 4.0])
            >>> Indicators._wilder_average(s, 3).round(2).tolist()
            [nan, nan, 2.0, 2.67]
        """
        period = _require_int("period", period, min_value=1)
        values = pd.to_numeric(series, errors="coerce")
        out = pd.Series(np.nan, index=values.index, dtype="float64")
        if len(values) < period:
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
        """计算平均真实波幅 ATR。

        Args:
            df: 标准化 OHLCV DataFrame。
            period: ATR 窗口。
            method: ``"wilder"`` 或 ``"sma"``。

        Returns:
            与 ``df`` 同索引的 ATR Series。

        Raises:
            ValueError: 窗口非法或平滑方法未知。

        Example:
            >>> atr = Indicators.atr(df, period=10, method="wilder")
        """
        period = _require_int("period", period, min_value=1)
        method = str(method).lower()
        if method not in {"wilder", "sma"}:
            raise ValueError(f"method 必须是 'wilder' 或 'sma'，当前为 {method!r}")
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
        """计算 Wilder ADX 趋势强度。

        Args:
            df: 标准化 OHLCV DataFrame。
            period: DM、ATR 和 DX 的 Wilder 窗口。

        Returns:
            ADX Series；初始化完成前填 0。ADX 始终按 Wilder 定义计算，不受策略
            ``atr_method`` 配置影响。

        Example:
            >>> adx = Indicators.adx(df, period=14)
        """
        period = _require_int("period", period, min_value=1)
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
        """计算 Wilder RSI。

        Args:
            close: 收盘价序列。
            period: 正整数窗口。

        Returns:
            0–100 的 RSI；初始化前填 50，只有上涨且无下跌时填 100。

        Example:
            >>> rsi = Indicators.rsi(df["close"], period=14)
        """
        period = _require_int("period", period, min_value=1)
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
    def donchian(
        df: pd.DataFrame, entry_period: int = 20, exit_period: int = 10
    ) -> tuple[pd.Series, pd.Series]:
        """计算不含当日的唐奇安入场上轨和离场下轨。

        Args:
            df: 标准化 OHLCV DataFrame。
            entry_period: 上轨回看天数。
            exit_period: 下轨回看天数。

        Returns:
            ``(upper, lower)``，两者均已向后移动一天。

        Example:
            >>> upper, lower = Indicators.donchian(df, 20, 10)
        """
        entry_period = _require_int("entry_period", entry_period, min_value=1)
        exit_period = _require_int("exit_period", exit_period, min_value=1)
        upper = df["high"].rolling(entry_period).max().shift(1)
        lower = df["low"].rolling(exit_period).min().shift(1)
        return upper, lower

    @staticmethod
    def ma(series: pd.Series, period: int) -> pd.Series:
        """计算简单移动平均。

        Args:
            series: 输入序列。
            period: 正整数窗口。

        Returns:
            初始化前为 ``NaN`` 的滚动均值。

        Example:
            >>> Indicators.ma(pd.Series([1, 2, 3]), 2).tolist()
            [nan, 1.5, 2.5]
        """
        period = _require_int("period", period, min_value=1)
        return series.rolling(period).mean()

    @staticmethod
    def compute_all(df: pd.DataFrame, cfg: dict) -> dict[str, pd.Series]:
        """按单标的配置一次性计算三套策略所需指标。

        Args:
            df: 标准化 OHLCV DataFrame。
            cfg: 至少可读取 ``atr_period/adx_period/rsi_period/entry_period/exit_period/``
                ``ma_short/ma_long/atr_method`` 的完整配置。

        Returns:
            指标名到 Series 的映射，包含 ATR、ADX、RSI、唐奇安上下轨和长短均线。

        Example:
            >>> cfg = BacktestEngine._default_config()
            >>> indicators = Indicators.compute_all(df, cfg)
            >>> sorted(indicators)
            ['adx', 'atr', 'donchian_lower', 'donchian_upper', 'ma_long', 'ma_short', 'rsi']
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
    """某一标的、某一子策略的独立持仓。

    Attributes:
        symbol: 六位股票代码。
        strategy_name: 子策略唯一名称。
        shares: 当前股数。
        entry_price: 含买入佣金的加权单位成本。
        entry_date: 首次建仓执行日，``YYYY-MM-DD``。
        stop_loss: 只允许上移的当前保护价。
        highest_since_entry: 持仓期最高价，用于 ATR 追踪止损。
        highest_close_since_entry: 浮盈保护峰值；初值和加仓时纳入实际执行价，
            此后用每日收盘价只上移。
        units: 已执行的建仓/加仓次数。
        last_buy_date: 最近一次买入执行日。
        last_add_price: 最近一次买入成交价，用于计算下一次金字塔加仓间距。

    Example:
        >>> p = Position("300308", "turtle_breakout", 1000, 100.05, "2025-01-02")
        >>> round(p.cost, 2)
        100050.0
    """
    symbol: str
    strategy_name: str
    shares: int
    entry_price: float
    entry_date: str
    stop_loss: float = 0.0
    highest_since_entry: float = 0.0
    highest_close_since_entry: float = 0.0
    units: int = 1
    last_buy_date: str = ""
    last_add_price: float = 0.0

    @property
    def cost(self) -> float:
        """返回当前持仓总成本。

        Returns:
            ``shares * entry_price``。

        Example:
            >>> Position("300308", "dual_ma", 200, 10.0, "2025-01-02").cost
            2000.0
        """
        return self.shares * self.entry_price

    def market_value_at(self, price: float) -> float:
        """按指定价格计算持仓市值。

        Args:
            price: 用于估值的每股价格。

        Returns:
            ``shares * price``；调用者负责保证价格有效。

        Example:
            >>> Position("300308", "dual_ma", 200, 10.0, "2025-01-02").market_value_at(12)
            2400
        """
        return self.shares * price


@dataclass
class TradeRecord:
    """一笔实际成交记录。

    Attributes:
        symbol: 六位股票代码。
        strategy_name: 产生交易的子策略。
        direction: ``"buy"`` 或 ``"sell"``。
        shares: 实际成交股数。
        price: 含滑点、不含费用的成交价。
        date: 成交执行日。
        reason: 信号原因及融合标签。
        pnl: 卖出净收入减去对应成本；买入为 0。
        pnl_pct: ``pnl / cost_basis``；买入为 0。
        signal_date: 信号日，正常情况下早于 ``date`` 至少一个交易日。
        gross_value: ``shares * price``。
        commission: 本笔佣金。
        stamp_duty_cost: 本笔印花税；买入为 0。
        net_cash_flow: 买入为负、卖出为正。
        cash_after: 本笔成交后的组合现金。
        peak_close: 卖出前浮盈保护峰值，至少为含买入佣金的持仓成本。
        exit_from_peak_pct: 卖出价相对上述保护峰值的收益率，通常为非正数。

    Example:
        >>> TradeRecord("300308", "dual_ma", "buy", 100, 10.01, "2025-01-03")
    """
    symbol: str
    strategy_name: str
    direction: str
    shares: int
    price: float
    date: str
    reason: str = ""
    pnl: float = 0.0
    pnl_pct: float = 0.0
    signal_date: str = ""
    gross_value: float = 0.0
    commission: float = 0.0
    stamp_duty_cost: float = 0.0
    net_cash_flow: float = 0.0
    cash_after: float = 0.0
    peak_close: float = 0.0
    exit_from_peak_pct: float = 0.0


@dataclass
class Signal:
    """T日收盘生成、等待下一可交易日执行的指令。

    Attributes:
        symbol: 六位股票代码。
        strategy_name: 子策略名称。
        direction: 仅允许 ``"buy"`` 或 ``"sell"``。
        target_shares: 希望成交的股数；执行层仍会受资金和仓位上限约束。
        price: 生成时为 T 日收盘价；执行前替换为执行日开盘价。
        stop_loss: 信号日建议保护价。
        reason: 可审计的触发原因。
        signal_date: 生成日期。
        atr: 锁定的信号日 ATR，执行日不得用未来 ATR 重算。
        fusion_votes: 同一标的当日同向子策略票数。
        fusion_label: 单策略、双策略共振或冲突卖出等标签。

    Example:
        >>> Signal("300308", "dual_ma", "buy", 1000, 100.0, signal_date="2025-01-02")
    """
    symbol: str
    strategy_name: str
    direction: str
    target_shares: int = 0
    price: float = 0.0
    stop_loss: float = 0.0
    reason: str = ""
    signal_date: str = ""
    atr: float = 0.0
    fusion_votes: int = 1
    fusion_label: str = "单策略"


@dataclass
class BarContext:
    """传给 ``on_bar`` 的单标的当日只读上下文。

    Attributes:
        i: 当日数据在 ``df`` 中的整数位置。
        df: 当前标的完整历史行情；策略只能读取 ``iloc[:i+1]`` 范围。
        current_assets: 当日收盘估算的组合总资产。
        indicators: 与 ``df`` 同索引的指标映射。
        symbol: 当前标的代码。
        date: 当前信号日字符串。

    Example:
        >>> ctx = BarContext(10, df, 2_000_000, indicators, "300308", "2025-01-16")
    """
    i: int
    df: pd.DataFrame
    current_assets: float
    indicators: dict
    symbol: str
    date: str


# ═══════════════════════════════════════════════════════════════════════
#  策略层
# ═══════════════════════════════════════════════════════════════════════

class BaseStrategy:
    """三个子策略共用的仓位计算、信号构造和反转退出逻辑。

    子类只负责根据指标决定何时买卖；真实资金、成交成本和仓位上限由
    :class:`BacktestEngine` 统一处理。
    """

    name: str = "base"

    def __init__(self, cfg: dict) -> None:
        """创建一个无持仓的策略实例。

        Args:
            cfg: 已由 ``BacktestEngine._validate_config`` 校验的单标的完整配置。

        Example:
            >>> strategy = TurtleBreakoutStrategy(BacktestEngine._default_config())
            >>> strategy.position is None
            True
        """
        self.cfg = cfg
        self.position: Position | None = None

    def on_bar(self, ctx: BarContext) -> Signal | None:
        """根据当日上下文生成至多一个信号。

        Args:
            ctx: 只包含当日及此前可见信息的上下文。

        Returns:
            买卖 ``Signal``；没有动作时返回 ``None``。

        Raises:
            NotImplementedError: 基类不能直接用于交易。

        Example:
            >>> BaseStrategy({}).on_bar(ctx)
            Traceback (most recent call last):
            ...
            NotImplementedError
        """
        raise NotImplementedError

    def _calc_shares(self, capital: float, price: float, atr_val: float,
                     unit_number: int = 1) -> int:
        """按单单位 ATR 风险预算计算整手目标股数。

        公式为 ``capital * risk_pct * decay / (atr_val * atr_multiplier)``。``price``
        只用于合法性检查；资金与单票上限会在执行层再次约束。

        Args:
            capital: 用于风险预算的资本基数，通常为总资产乘 ``strategy_weight``。
            price: 信号日收盘价或执行检查使用的开盘价。
            atr_val: 锁定的信号日 ATR。
            unit_number: 1 表示首仓；后续单位应用 ``pyramid_risk_decay``。

        Returns:
            向下取整到 100 股的目标股数；无效输入返回 0。

        Example:
            >>> s = TurtleBreakoutStrategy(BacktestEngine._default_config())
            >>> s._calc_shares(1_000_000, 100, 5) % 100
            0
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
        """用当前收盘价构造买入信号。

        Args:
            ctx: 当前 K 线上下文。
            shares: 目标股数。
            stop_loss: 信号日建议止损价。
            reason: 人类可读的触发原因。
            atr_val: 信号日 ATR；执行层将据此重算风险上限。

        Returns:
            尚未成交的 ``Signal``。

        Example:
            >>> signal = strategy._make_buy_signal(ctx, 1000, 95.0, "突破", 3.0)
        """
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
        """构造卖出当前子策略全部持仓的信号。

        Args:
            ctx: 当前 K 线上下文。
            reason: 退出原因。

        Returns:
            ``target_shares`` 等于当前持仓股数的卖出信号；无仓时为 0。

        Example:
            >>> signal = strategy._make_sell_signal(ctx, "趋势破位")
        """
        return Signal(
            symbol=ctx.symbol,
            strategy_name=self.name,
            direction="sell",
            target_shares=self.position.shares if self.position else 0,
            price=float(ctx.df["close"].iloc[ctx.i]),
            reason=reason,
            signal_date=ctx.date,
        )

    def _fast_reversal_exit(self, ctx: BarContext) -> Signal | None:
        """检查浮盈回吐、短周期破位和失败仓软止损。

        Args:
            ctx: 当前 K 线上下文。

        Returns:
            满足任一反转条件时返回全仓卖出信号，否则返回 ``None``。即使当前子策略
            关闭快速退出，也会用当日收盘更新保护峰值以保证审计字段准确。

        Note:
            本方法只使用 T 日及此前数据，卖出仍在 T+1 开盘尝试，无法消除跳空或跌停。

        Example:
            >>> signal = strategy._fast_reversal_exit(ctx)
            >>> signal is None or signal.direction == "sell"
            True
        """
        pos = self.position
        if pos is None:
            return None
        i, df, cfg = ctx.i, ctx.df, self.cfg
        close = float(df["close"].iloc[i])
        if not _is_finite_number(close) or close <= 0:
            return None

        pos.highest_close_since_entry = max(pos.highest_close_since_entry, close)
        # 即使该子策略关闭快速退出，也持续维护峰值，确保卖出审计字段真实可比。
        strategy_switch = {
            "turtle_breakout": "reversal_turtle_enabled",
            "dual_ma": "reversal_dual_ma_enabled",
            "atr_channel": "reversal_atr_channel_enabled",
        }.get(self.name)
        if strategy_switch and not bool(cfg.get(strategy_switch, True)):
            return None
        peak_close = pos.highest_close_since_entry
        peak_gain = peak_close / pos.entry_price - 1 if pos.entry_price > 0 else 0.0

        activation = float(cfg.get("profit_lock_activation", 0.30))
        giveback = float(cfg.get("profit_lock_giveback", 0.18))
        if peak_gain >= activation:
            lock_stop = peak_close * (1 - giveback)
            # 与原追踪止损共享只上移的stop_loss，便于审计当前保护价。
            pos.stop_loss = max(pos.stop_loss, lock_stop)
            if close <= lock_stop:
                return self._make_sell_signal(
                    ctx, f"反转浮盈保护(保护峰值回吐{giveback:.0%})@{lock_stop:.2f}"
                )

            exit_period = int(cfg.get("reversal_exit_period", 6))
            break_giveback = float(cfg.get("reversal_break_giveback", 0.12))
            if i >= exit_period:
                prior_low = df["low"].rolling(exit_period).min().shift(1).iloc[i]
                ma_short = ctx.indicators.get("ma_short")
                ma_value = ma_short.iloc[i] if ma_short is not None else np.nan
                if (
                    _is_finite_number(prior_low)
                    and _is_finite_number(ma_value)
                    and close <= peak_close * (1 - break_giveback)
                    and close <= float(prior_low)
                    and close < float(ma_value)
                ):
                    return self._make_sell_signal(
                        ctx, f"反转短周期破位({break_giveback:.0%}回吐+{exit_period}日低点+短均线)"
                    )

        # 浮盈保护尚未激活时，只在亏损达到软阈值且短均线同步向下才提前认错。
        # 相比单纯固定止损，这个双条件能减少强趋势正常回踩造成的误杀。
        loss_cut = float(cfg.get("reversal_loss_cut", 0.10))
        ma_short = ctx.indicators.get("ma_short")
        if i >= 1 and ma_short is not None:
            ma_now, ma_prev = ma_short.iloc[i], ma_short.iloc[i - 1]
            if (
                _is_finite_number(ma_now)
                and _is_finite_number(ma_prev)
                and close <= pos.entry_price * (1 - loss_cut)
                and close < float(ma_now) < float(ma_prev)
            ):
                return self._make_sell_signal(
                    ctx, f"反转失败退出(亏损{loss_cut:.0%}+短均线转弱)"
                )
        return None


class TurtleBreakoutStrategy(BaseStrategy):
    """唐奇安突破入场、ATR金字塔加仓与多层退出策略。

    入场需要收盘价突破前 ``entry_period`` 日最高价且 ADX 达标；持仓后按最近成交价
    间隔 ``pyramid_add_atr * ATR`` 加仓。退出优先检查反转保护、ATR追踪、硬止损和
    唐奇安下轨。
    """

    name = "turtle_breakout"

    def on_bar(self, ctx: BarContext) -> Signal | None:
        """计算当日海龟突破信号。

        Args:
            ctx: 当前标的 K 线、指标和组合资产上下文。

        Returns:
            突破/加仓买入信号、退出卖出信号或 ``None``。

        Example:
            >>> signal = TurtleBreakoutStrategy(cfg).on_bar(ctx)
        """
        i, df, ind = ctx.i, ctx.df, ctx.indicators
        cfg = self.cfg

        entry_period = cfg.get("entry_period", 20)
        exit_period = cfg.get("exit_period", 10)
        adx_threshold = cfg.get("adx_threshold", 15)
        max_units = cfg.get("max_units", 8)
        atr_stop_mult = cfg.get("atr_multiplier", 2)
        trail_mult = cfg.get("trail_atr_mult", 2.5)

        # 先满足通道和 ADX 的基础预热；未完成二次 Wilder 平滑时 ADX 保持 0，不会误入场。
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

        if self.position is not None:
            pos = self.position
            pos.highest_since_entry = max(pos.highest_since_entry, high)
            reversal_signal = self._fast_reversal_exit(ctx)
            if reversal_signal is not None:
                return reversal_signal

            trail_stop = pos.highest_since_entry - trail_mult * atr_val
            initial_stop = pos.entry_price - atr_stop_mult * atr_val
            pos.stop_loss = max(pos.stop_loss, trail_stop, initial_stop)

            if close <= pos.stop_loss:
                return self._make_sell_signal(ctx, f"ATR追踪止损@{pos.stop_loss:.2f}")

            if close <= pos.entry_price * (1 - cfg.get("hard_stop", 0.15)):
                return self._make_sell_signal(ctx, f"硬止损{cfg.get('hard_stop', 0.15):.0%}")

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
        """计算双均线交叉与持仓退出信号。

        Args:
            ctx: 当前标的 K 线、指标和组合资产上下文。

        Returns:
            RSI 确认的金叉买入、死叉/止损卖出或 ``None``。

        Example:
            >>> signal = DualMAStrategy(cfg).on_bar(ctx)
        """
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
            reversal_signal = self._fast_reversal_exit(ctx)
            if reversal_signal is not None:
                return reversal_signal

            # 均线死叉离场
            if ma_s < ma_l:
                return self._make_sell_signal(
                    ctx, f"MA{cfg.get('ma_short', 20)}下穿MA{cfg.get('ma_long', 60)}"
                )

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
        """计算均线加减 ATR 通道的突破与退出信号。

        Args:
            ctx: 当前标的 K 线、指标和组合资产上下文。

        Returns:
            上轨突破买入、追踪止损/下轨/硬止损卖出或 ``None``。

        Example:
            >>> signal = ATRChannelStrategy(cfg).on_bar(ctx)
        """
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
            reversal_signal = self._fast_reversal_exit(ctx)
            if reversal_signal is not None:
                return reversal_signal

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
    """管理组合回撤、产业池、单票仓位和单日亏损限制。"""

    def __init__(self, cfg: dict) -> None:
        """初始化无历史状态的风控器。

        Args:
            cfg: 已校验的全局配置；读取回撤、冷却、仓位和产业池上限。

        Example:
            >>> risk = RiskManager(BacktestEngine._default_config())
            >>> risk.peak_assets
            0.0
        """
        self.cfg = cfg
        self.peak_assets: float = 0.0
        self.cooldown_until: str | None = None
        # 回测没有组合级开盘权益序列，这里用上一交易日收盘权益作为当日基准。
        self.daily_start_assets: float = 0.0
        self.symbol_groups: dict[str, str] = {}
        self.group_weight_limits: dict[str, float] = {}

    def configure_groups(self, symbol_groups: dict[str, str]) -> None:
        """配置标的产业池及混合组合的池级仓位上限。

        Args:
            symbol_groups: ``{股票代码: 产业池名称}``。

        Returns:
            ``None``。只有两个产业池同时出现时才使用
            ``combined_group_weight_limits``；单池自动放宽到 100%。

        Example:
            >>> risk.configure_groups({"300308": "overseas_compute"})
            >>> risk.group_weight_limits["overseas_compute"]
            1.0
        """
        self.symbol_groups = dict(symbol_groups)
        active = set(self.symbol_groups.values())
        if len(active) > 1:
            self.group_weight_limits = dict(self.cfg.get("combined_group_weight_limits", {}))
        else:
            self.group_weight_limits = {group: 1.0 for group in active}

    def check_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        trading_dates: list[pd.Timestamp] | None = None,
        date_to_pos: dict[pd.Timestamp, int] | None = None,
    ) -> str | None:
        """更新组合峰值并检查回撤熔断或冷却期。

        Args:
            current_assets: 当前收盘组合总资产。
            date_str: 当前交易日，``YYYY-MM-DD``。
            trading_dates: 可选完整交易日历，用于按交易日设置冷却结束日。
            date_to_pos: 可选日期到位置映射，避免线性查找。

        Returns:
            ``"组合回撤熔断"``、``"组合冷却期"`` 或 ``None``。

        Note:
            触发熔断后会把风险比较峰值重置为当前资产；绩效报告的最大回撤仍始终按
            全历史权益高点计算，两者统计口径不同。

        Example:
            >>> risk.check_portfolio_risk(1_000_000, "2025-01-02") is None
            True
        """
        max_drawdown_pct = self.cfg.get("max_drawdown", 0.20)
        cooldown_days = self.cfg.get("cooldown_days", 3)

        self.peak_assets = max(self.peak_assets, current_assets)

        if self.cooldown_until:
            cooldown_end = pd.Timestamp(self.cooldown_until)
            current_date = pd.Timestamp(date_str)
            if current_date < cooldown_end:
                return "组合冷却期"
            self.cooldown_until = None

        if self.peak_assets > 0:
            drawdown = (self.peak_assets - current_assets) / self.peak_assets
            if drawdown >= max_drawdown_pct:
                if trading_dates is not None:
                    current_date = pd.Timestamp(date_str)
                    idx = date_to_pos.get(current_date) if date_to_pos is not None else None
                    if idx is None:
                        try:
                            idx = trading_dates.index(current_date)
                        except ValueError:
                            idx = None
                    if idx is not None:
                        # 触发日之后完整跳过 cooldown_days 个交易日。
                        end_idx = min(idx + cooldown_days + 1, len(trading_dates) - 1)
                        self.cooldown_until = trading_dates[end_idx].strftime("%Y-%m-%d")
                    else:
                        self.cooldown_until = (pd.Timestamp(date_str) + timedelta(days=cooldown_days)).strftime("%Y-%m-%d")
                else:
                    self.cooldown_until = (pd.Timestamp(date_str) + timedelta(days=cooldown_days)).strftime("%Y-%m-%d")
                # 重新建立风险锚点，避免冷却结束后因尚未收复历史高点而立即重复熔断。
                self.peak_assets = current_assets
                return "组合回撤熔断"

        return None

    def check_daily_loss(self, current_assets: float) -> bool:
        """检查当前权益相对上一交易日收盘的跌幅。

        Args:
            current_assets: 当前时点组合权益。

        Returns:
            跌幅达到 ``daily_loss_limit`` 时返回 ``True``。该结果只阻止新买入，
            不会自动清仓。

        Example:
            >>> risk.daily_start_assets = 1_000_000
            >>> risk.check_daily_loss(930_000)
            True
        """
        if self.daily_start_assets > 0:
            daily_loss = (self.daily_start_assets - current_assets) / self.daily_start_assets
            return daily_loss >= self.cfg.get("daily_loss_limit", 0.06)
        return False

    def check_position_limits(self, symbol: str, positions: dict,
                              current_assets: float, buy_value: float,
                              current_prices: dict | None = None,
                              position_cfg: dict | None = None) -> bool:
        """检查新增买入后的单票、产业池和总仓位比例。

        Args:
            symbol: 拟买入股票代码。
            positions: ``{代码: {策略名: Position}}`` 当前持仓。
            current_assets: 使用执行时可见价格估算的总资产。
            buy_value: 拟成交金额，不含佣金。
            current_prices: 可选的执行时点价格映射；传入时所有持仓必须有有效价格。
            position_cfg: 当前标的配置，用于读取其 ``max_symbol_weight``。

        Returns:
            三项上限全部满足时返回 ``True``。

        Example:
            >>> risk.check_position_limits("300308", {}, 1_000_000, 100_000)
            True
        """
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
            """返回 ``sym`` 的风控估值价；未传市价时用 ``pos`` 成本价。

            Example:
                ``_mark("300308", position)``
            """
            if current_prices is None:
                return pos.entry_price
            return float(current_prices[sym])

        # 单标的上限 — 用当前价而非 entry_price。
        symbol_value = sum(
            p.shares * _mark(symbol, p)
            for p in positions.get(symbol, {}).values()
        )
        symbol_cap = (position_cfg or self.cfg).get(
            "max_symbol_weight", self.cfg.get("max_symbol_weight", 0.50)
        )
        if (symbol_value + buy_value) / current_assets > symbol_cap:
            return False

        # 两池同时运行时限制单一产业池占比；单池回测自动放宽为100%。
        target_group = self.symbol_groups.get(symbol)
        group_cap = self.group_weight_limits.get(target_group, 1.0)
        if target_group:
            group_value = sum(
                p.shares * _mark(sym, p)
                for sym, sym_positions in positions.items()
                if self.symbol_groups.get(sym) == target_group
                for p in sym_positions.values()
            )
            if (group_value + buy_value) / current_assets > group_cap:
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
    """共享资金的多标的、多策略 T+1 组合回测引擎。

    策略在 T 日收盘生成信号，引擎在下一可交易日的开盘阶段以开盘价加
    滑点执行。三个子策略各自管理仓位，但共享现金、标的上限和产业池风控。

    Args:
        initial_capital: 初始现金，单位为元，必须大于 0。
        cfg: 可选的全局配置覆盖。只允许 :meth:`_default_config` 已声明的键。

    Example:
        >>> engine = BacktestEngine(2_000_000, {"max_drawdown": 0.16})
        >>> result = engine.run(
        ...     {"300308": "中际旭创"}, "2025-01-01", "2026-06-30",
        ...     data_dir="data_qfq_reconstructed",
        ... )
    """

    def __init__(self, initial_capital: float = 2_000_000, cfg: dict | None = None) -> None:
        """初始化引擎；真正的回测状态会在每次 :meth:`run` 开始时重置。

        Args:
            initial_capital: 初始资金（元）。
            cfg: 全局参数覆盖；例如 ``{"commission_rate": 0.0002}``。

        Raises:
            ValueError: 资金非正数，或配置键/值不合法。

        Example:
            >>> engine = BacktestEngine(initial_capital=1_000_000)
            >>> engine.cash
            1000000.0
        """
        self.initial_capital = _require_finite("initial_capital", initial_capital, min_value=0.01)
        self._user_cfg = dict(cfg or {})
        self.cfg = self._validate_config({**self._default_config(), **self._user_cfg})
        self.cash = self.initial_capital
        self.positions: dict[str, dict[str, Position]] = {}  # {symbol: {strategy_name: Position}}
        self.trades: list[TradeRecord] = []
        self.equity_curve: list[dict] = []
        self.risk = RiskManager(self.cfg)
        self.strategy_instances: dict[str, list[BaseStrategy]] = {}  # 每个标的的策略实例
        self.symbol_names: dict[str, str] = {}
        self.symbol_last_dates: dict[str, pd.Timestamp] = {}
        self.global_last_date: pd.Timestamp | None = None
        self.symbol_configs: dict[str, dict] = {}
        self.pending_signals: list[tuple[Signal, BaseStrategy]] = []
        self.fusion_events: list[dict] = []

        # 策略模板（每个标的会复制独立实例）
        self.strategy_templates: list[type[BaseStrategy]] = [
            TurtleBreakoutStrategy,
            DualMAStrategy,
            ATRChannelStrategy,
        ]

    @staticmethod
    def _default_config() -> dict:
        """返回一份独立的默认配置。

        Returns:
            包含策略、仓位、风控、成本和执行参数的新字典。嵌套字典
            ``combined_group_weight_limits`` 也在每次调用时重建。

        Example:
            >>> cfg = BacktestEngine._default_config()
            >>> cfg["entry_period"], cfg["atr_method"]
            (8, 'wilder')
        """
        return {
            "entry_period": 8,       # 海龟入场唐奇安上轨回看日数。
            "exit_period": 3,        # 海龟离场唐奇安下轨回看日数。
            "adx_threshold": 12,    # 海龟和 ATR 通道入场的最低 ADX。
            "adx_period": 10,       # ADX 的 Wilder 平滑周期。
            "atr_period": 10,       # ATR 及 ATR 通道波动率周期。
            "rsi_period": 20,       # 双均线策略 RSI 入场过滤周期。
            "ma_short": 15,         # 短均线；也用作 ATR 通道中轨和反转趋势判定。
            "ma_long": 60,          # 双均线策略长均线周期。

            "atr_multiplier": 1.0, # 单位风险距离和初始 ATR 止损倍数。
            "trail_atr_mult": 4.0, # 持仓期间吊灯止损距离的 ATR 倍数。
            "channel_mult": 2.0,   # ATR 通道上轨偏移倍数。
            "channel_lower_mult": 3.0,  # ATR 通道下轨偏移倍数。

            "risk_pct": 0.03,          # 每个策略单位可承受亏损/资本基数。
            "hard_stop": 0.15,         # ATR 不可用时的固定百分比止损。
            "strategy_weight": 0.98,   # 计算目标股数时的资产基数系数。
            "max_symbol_weight": 0.60, # 同一标的所有子策略仓位合计上限。
            "max_total_weight": 1.00,  # 全组合持仓市值/总资产上限。
            "max_units": 20,           # 单标的单策略最多金字塔单位数。

            # 触发后下一交易日才能卖出，因此不是最大成交回撤保证。
            "max_drawdown": 0.165, # 相对风控锁定峰值的组合回撤触发比例。
            "cooldown_days": 10,   # 触发后禁止新开仓的完整交易日数。
            "daily_loss_limit": 0.06, # 当日收盘相对前收资产的亏损禁买阈值。

            "momentum_lookback": 5, # 候选轮动排名使用的收益回看日数。
            "max_positions": 6,     # 可入选的动量标的数，也是同时持有的标的数上限。
            "group_min_slots": 2,   # 每个产业池保留的动量候选名额，不强制成交。

            "fusion_single_scale": 0.90, # 仅一策略看多时的目标股数系数。
            "fusion_double_scale": 1.00, # 两策略同时看多时的目标股数系数。
            "fusion_triple_scale": 1.10, # 三策略同时看多时的目标股数系数。

            "profit_lock_activation": 0.20, # 收盘浮盈达到该值后激活峰值回吐保护。
            "profit_lock_giveback": 0.22,   # 激活后相对保护峰值的允许回吐。
            "reversal_break_giveback": 0.22,# 跌破短均线时的峰值回吐确认阈值。
            "reversal_exit_period": 6,      # 快速反转最低收盘通道的回看日数。
            "reversal_loss_cut": 0.10,      # 未激活浮盈保护时的亏损反转阈值。
            "reversal_turtle_enabled": True,     # 是否对海龟子仓启用快速反转。
            "reversal_dual_ma_enabled": True,    # 是否对双均线子仓启用快速反转。
            "reversal_atr_channel_enabled": True,# 是否对 ATR 通道子仓启用快速反转。

            "combined_group_weight_limits": {
                "overseas_compute": 1.00,       # 双池运行时海外算力池的市值上限。
                "domestic_semiconductor": 0.80, # 双池运行时国产半导体池的市值上限。
            },
            "liquidate_on_circuit_breaker": True, # 回撤熔断后是否生成全部 T+1 卖出单。

            "commission_rate": 0.00025, # 买卖佣金率。
            "stamp_duty": 0.0005,      # 仅卖出收取的印花税率。
            "slippage": 0.001,         # 买入价上浮/卖出价下浮的滑点率。
            "min_commission": 5.0,     # 每笔交易最低佣金（元）。
            "max_pending_buy_days": 5, # 买入信号最多等待的交易日数。
            "pyramid_add_atr": 1.0,    # 相对上次买价再上涨多少 ATR 才允许加仓。
            "pyramid_risk_decay": 1.0, # 第 n 单位风险按 ``decay ** (n-1)`` 递减。
            "atr_method": "wilder",   # ATR 平滑方法：``wilder`` 或 ``sma``。
            "limit_price_epsilon": 0.001, # 涨跌停比例判断容差。
            "per_symbol_limit_pct": {},   # 单标的涨跌停覆盖，例如 ``{"000001": 0.10}``。
            "st_symbols": set(),          # 按 5% 涨跌停处理的 ST/*ST 代码集合。
            "close_position_on_data_end": True, # 单票数据提前结束时按末日收盘结算。
            "force_close_on_end": False, # 全局末日是否人工强制平仓；默认只市值计价。
            "risk_free_rate": 0.0,       # 计算 Sharpe 的年化无风险利率。
        }

    # 只有确实在单标的指标、信号、仓位计算或单票上限中读取的键才允许按票覆盖。
    _PER_SYMBOL_OVERRIDE_KEYS = {
        "entry_period", "exit_period", "adx_threshold", "adx_period", "atr_period",
        "rsi_period", "ma_short", "ma_long", "atr_multiplier", "trail_atr_mult",
        "channel_mult", "channel_lower_mult", "risk_pct", "hard_stop", "strategy_weight",
        "max_symbol_weight", "max_units", "pyramid_add_atr", "pyramid_risk_decay",
        "atr_method", "profit_lock_activation", "profit_lock_giveback",
        "reversal_break_giveback", "reversal_exit_period", "reversal_loss_cut",
        "reversal_turtle_enabled", "reversal_dual_ma_enabled",
        "reversal_atr_channel_enabled",
    }

    @staticmethod
    def optimized_aggressive_config() -> dict:
        """返回高仓位、快速趋势的激进配置。

        Returns:
            以默认配置为底的新字典。该预设仅在显式选择
            ``profile="aggressive"`` 时使用。

        Example:
            >>> cfg = BacktestEngine.optimized_aggressive_config()
            >>> cfg["max_positions"]
            2
        """
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

        Returns:
            以默认配置为底的宽通道轻仓配置。

        Example:
            >>> BacktestEngine.semiconductor_config()["entry_period"]
            33
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
            "profit_lock_activation": 0.20,
            "profit_lock_giveback": 0.24,
            "reversal_break_giveback": 0.24,
            "reversal_exit_period": 8,
            "reversal_loss_cut": 0.08,
            # 宽通道已保护海龟/ATR 仓；只给更慢的双均线仓增加快速反转。
            "reversal_turtle_enabled": False,
            "reversal_dual_ma_enabled": True,
            "reversal_atr_channel_enabled": False,
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

        Returns:
            宽通道重仓配置的新字典。

        Example:
            >>> BacktestEngine.semiconductor_heavy_config()["max_units"]
            6
        """
        cfg = BacktestEngine.semiconductor_config()
        cfg.update({
            "risk_pct": 0.030,
            "strategy_weight": 0.90,
            "max_units": 6,
            "pyramid_add_atr": 1.5,
        })
        return cfg

    @staticmethod
    def overseas_memory_material_config() -> dict:
        """返回服务器内存接口/HBM 材料/高频填料配置。

        Returns:
            比光模块默认参数稍慢、稍宽的配置新字典。

        Example:
            >>> BacktestEngine.overseas_memory_material_config()["ma_long"]
            65
        """
        cfg = BacktestEngine._default_config()
        cfg.update({
            "entry_period": 9, "exit_period": 4, "atr_period": 12,
            "trail_atr_mult": 4.2, "hard_stop": 0.18,
            "risk_pct": 0.030, "max_units": 16, "pyramid_add_atr": 1.1,
            "ma_long": 65, "channel_mult": 2.1,
            "strategy_weight": 0.96, "max_symbol_weight": 0.58,
        })
        return cfg

    @staticmethod
    def domestic_design_config() -> dict:
        """返回国产存储/IC 设计配置。

        Returns:
            节奏介于光模块和半导体设备之间的配置新字典。

        Example:
            >>> BacktestEngine.domestic_design_config()["entry_period"]
            25
        """
        cfg = BacktestEngine.semiconductor_config()
        cfg.update({
            "entry_period": 25, "exit_period": 20, "ma_long": 80,
            "trail_atr_mult": 7.0, "risk_pct": 0.018,
            "max_units": 3, "pyramid_add_atr": 2.0,
            "strategy_weight": 0.82, "max_symbol_weight": 0.50,
        })
        return cfg

    @staticmethod
    def domestic_material_config() -> dict:
        """返回半导体材料/封装材料配置。

        Returns:
            中等速度、适度容忍假突破的配置新字典。

        Example:
            >>> BacktestEngine.domestic_material_config()["channel_mult"]
            3.0
        """
        cfg = BacktestEngine.semiconductor_config()
        cfg.update({
            "entry_period": 28, "exit_period": 22, "ma_long": 90,
            "trail_atr_mult": 7.0, "risk_pct": 0.018,
            "max_units": 3, "pyramid_add_atr": 2.0,
            "channel_mult": 3.0, "strategy_weight": 0.82,
            "max_symbol_weight": 0.45,
        })
        return cfg

    @staticmethod
    def domestic_foundry_config() -> dict:
        """返回晶圆制造配置。

        Returns:
            更宽通道、更长周期和较低单票上限的配置新字典。

        Example:
            >>> BacktestEngine.domestic_foundry_config()["ma_long"]
            120
        """
        cfg = BacktestEngine.semiconductor_config()
        cfg.update({
            "entry_period": 40, "exit_period": 30, "ma_long": 120,
            "trail_atr_mult": 9.0, "risk_pct": 0.012,
            "strategy_weight": 0.68, "max_symbol_weight": 0.35,
            # 晶圆制造持仓周期最长，使用略紧于设备/材料的浮盈回吐保护。
            "profit_lock_giveback": 0.22,
            "reversal_break_giveback": 0.22,
            "reversal_turtle_enabled": True,
        })
        return cfg

    # 参数路由不是“哪套样本内收益更高”的回看选择，而是事先固定的产业属性映射。
    # default: 海外算力链的快速趋势参数；semiconductor: 国产替代链的宽趋势参数。
    _KNOWN_CLASSIFICATION: dict[str, str] = {
        # === 海外算力产业链：default（快速趋势参数）===
        "300308": "default",  # 中际旭创
        "300502": "default",  # 新易盛
        "300394": "default",  # 天孚通信
        "688205": "default",  # 德科立
        "920045": "default",  # 蘅东光
        "688008": "default",  # 澜起科技
        "002409": "default",  # 雅克科技
        "688300": "default",  # 联瑞新材

        # === 国产半导体/国产科技设备链：semiconductor（宽趋势参数）===
        "603986": "semiconductor",  # 兆易创新
        "688072": "semiconductor",  # 拓荆科技
        "300054": "semiconductor",  # 鼎龙股份
        "688535": "semiconductor",  # 华海诚科
        # 帝尔激光本质是光伏设备，不是半导体；两池约束下归入“国产设备”宽参数组。
        "300776": "semiconductor",  # 帝尔激光
        "688249": "semiconductor",  # 晶合集成
        "688347": "semiconductor",  # 华虹宏力
        "300604": "semiconductor",  # 长川科技
        "688120": "semiconductor",  # 华海清科
        "688082": "semiconductor",  # 盛美上海
        "688361": "semiconductor",  # 中科飞测
        "688409": "semiconductor",  # 富创精密
        "300666": "semiconductor",  # 江丰电子
        "600206": "semiconductor",  # 有研新材
    }

    _SYMBOL_GROUP: dict[str, str] = {
        "300308": "overseas_compute", "300502": "overseas_compute",
        "300394": "overseas_compute", "688205": "overseas_compute",
        "920045": "overseas_compute", "688008": "overseas_compute",
        "002409": "overseas_compute", "688300": "overseas_compute",
        "603986": "domestic_semiconductor", "688072": "domestic_semiconductor",
        "300054": "domestic_semiconductor", "688535": "domestic_semiconductor",
        "300776": "domestic_semiconductor", "688249": "domestic_semiconductor",
        "688347": "domestic_semiconductor", "300604": "domestic_semiconductor",
        "688120": "domestic_semiconductor", "688082": "domestic_semiconductor",
        "688361": "domestic_semiconductor", "688409": "domestic_semiconductor",
        "300666": "domestic_semiconductor", "600206": "domestic_semiconductor",
    }

    # 两个资金池之下的参数子模板。分类依据产业属性预先固定，不读取回测期表现。
    _SYMBOL_PROFILE: dict[str, str] = {
        "300308": "overseas_optical", "300502": "overseas_optical",
        "300394": "overseas_optical", "688205": "overseas_optical",
        "920045": "overseas_optical",
        "688008": "overseas_memory_material", "002409": "overseas_memory_material",
        "688300": "overseas_memory_material",
        "603986": "domestic_design",
        "688072": "domestic_equipment", "300776": "domestic_equipment",
        "688361": "domestic_equipment", "688409": "domestic_equipment",
        "300604": "domestic_equipment", "688120": "domestic_equipment",
        "688082": "domestic_equipment",
        "300054": "domestic_material", "688535": "domestic_material",
        "300666": "domestic_material", "600206": "domestic_material",
        "688249": "domestic_foundry", "688347": "domestic_foundry",
    }

    @staticmethod
    def config_for_symbol(code: str, name: str = "") -> dict:
        """按事先固定的产业子类返回独立配置。

        Args:
            code: 6 位股票代码。
            name: 可选中文名称；未知代码时用于关键词兜底。

        Returns:
            完整配置的新字典。

        Example:
            >>> BacktestEngine.config_for_symbol("688072", "拓荆科技")["entry_period"]
            33
        """
        profile = BacktestEngine._SYMBOL_PROFILE.get(code)
        if profile == "overseas_memory_material":
            return BacktestEngine.overseas_memory_material_config()
        if profile == "domestic_design":
            return BacktestEngine.domestic_design_config()
        if profile == "domestic_material":
            return BacktestEngine.domestic_material_config()
        if profile == "domestic_foundry":
            return BacktestEngine.domestic_foundry_config()
        if profile == "domestic_equipment":
            cfg = BacktestEngine.semiconductor_config()
            cfg["max_symbol_weight"] = 0.45
            return cfg
        if profile == "overseas_optical":
            return BacktestEngine._default_config()
        return (BacktestEngine.semiconductor_config()
                if BacktestEngine.classify_symbol(code, name=name) == "semiconductor"
                else BacktestEngine._default_config())

    # 名称关键词是固定分类表之外的二级弱规则，不读取回测区间表现。
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
        """使用内置名称关键词尝试分类未知标的。

        Args:
            code: 股票代码；可与名称一起被关键词搜索。
            name: 股票名称或包含细分行业的描述。

        Returns:
            ``default``、``semiconductor`` 或未命中时的 ``None``。

        Example:
            >>> BacktestEngine._classify_by_industry_hints("999999", "某晶圆厂")
            'semiconductor'
        """
        candidates = " ".join(str(x) for x in (code, name) if x)
        for key, cls in BacktestEngine._INDUSTRY_HINTS.items():
            if key in candidates:
                return cls
        return None

    @staticmethod
    def classify_symbol(code: str, df: pd.DataFrame | None = None,
                        name: str = "", lookback_start: str = "",
                        lookback_end: str | None = None) -> str:
        """无前视参数分类：固定行业表 → 名称关键词 → 默认快速参数。

        决策层级：
          1. _KNOWN_CLASSIFICATION 硬编码主表（行业属性，最高优先级，零网络）
          2. _INDUSTRY_HINTS 细分行业关键词弱映射（零网络）
          3. 未知标的一律返回 default，并要求使用者通过 per_symbol_config 明确覆盖。

        禁止使用完整回测区间的收益、波动、ADX等结果来决定参数集；那会把未来行情
        偷渡进策略选择，形成样本内前视偏差。

        Args:
            code: 6 位股票代码。
            df: 已废弃的兼容参数；故意不读取，以防用价格表现选参。
            name: 可选股票名称/行业描述。
            lookback_start: 已废弃的兼容参数，不读取。
            lookback_end: 已废弃的兼容参数，不读取。

        Returns:
            ``default``（快速参数）或 ``semiconductor``（宽参数）。

        Example:
            >>> BacktestEngine.classify_symbol("688072", name="拓荆科技")
            'semiconductor'
        """
        # 第一层：硬编码主表
        known = BacktestEngine._KNOWN_CLASSIFICATION.get(code)
        if known:
            return known
        # 第二层：行业关键词弱映射
        hint = BacktestEngine._classify_by_industry_hints(code, name)
        if hint:
            return hint
        return "default"

    @staticmethod
    def _validate_config(cfg: dict) -> dict:
        """校验并规范化完整配置。

        Args:
            cfg: 配置字典；通常由默认配置与覆盖合并得到。

        Returns:
            类型和边界已规范化的浅拷贝。

        Raises:
            ValueError: 出现未知键、非有限数、越界值或互相矛盾的周期/权重。

        Example:
            >>> BacktestEngine._validate_config(BacktestEngine._default_config())["max_units"]
            20
        """
        out = dict(cfg)
        allowed_keys = set(BacktestEngine._default_config().keys())
        unknown_keys = sorted(set(out) - allowed_keys)
        if unknown_keys:
            raise ValueError(f"配置包含未知字段，疑似拼写错误: {unknown_keys}")
        for key, minimum in {
            "entry_period": 2, "exit_period": 1, "adx_period": 1, "atr_period": 1,
            "rsi_period": 1, "ma_short": 1, "ma_long": 2, "max_units": 1,
            "momentum_lookback": 1, "max_positions": 1, "cooldown_days": 0,
            "max_pending_buy_days": 1, "group_min_slots": 0,
            "reversal_exit_period": 2,
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
                    "max_drawdown", "daily_loss_limit", "profit_lock_activation",
                    "profit_lock_giveback", "reversal_break_giveback", "reversal_loss_cut"]:
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
        for key in ["reversal_turtle_enabled", "reversal_dual_ma_enabled",
                    "reversal_atr_channel_enabled"]:
            out[key] = _require_bool(key, out.get(key))
        out["risk_free_rate"] = _require_finite(
            "risk_free_rate", out.get("risk_free_rate", 0.0), min_value=-0.99, max_value=1.0
        )
        for key in ["fusion_single_scale", "fusion_double_scale", "fusion_triple_scale"]:
            out[key] = _require_positive(key, out.get(key), max_value=2.0, inclusive_max=True)
        group_limits = out.get("combined_group_weight_limits", {})
        if not isinstance(group_limits, dict):
            raise ValueError("combined_group_weight_limits 必须是 dict")
        allowed_groups = {"overseas_compute", "domestic_semiconductor"}
        unknown_groups = sorted(set(map(str, group_limits)) - allowed_groups)
        if unknown_groups:
            raise ValueError(
                f"combined_group_weight_limits 包含未知产业池: {unknown_groups}"
            )
        out["combined_group_weight_limits"] = {
            str(group): _require_positive(
                f"combined_group_weight_limits[{group}]", value,
                max_value=1.0, inclusive_max=True,
            )
            for group, value in group_limits.items()
        }

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
        """返回待执行信号的稳定去重键。

        Args:
            signal: 待编码的买卖信号。

        Returns:
            ``(股票代码, 策略名, 方向)``。

        Example:
            >>> s = Signal("300308", "dual_ma", "buy", 100, 10.0, "test")
            >>> BacktestEngine._signal_key(s)
            ('300308', 'dual_ma', 'buy')
        """
        return signal.symbol, signal.strategy_name, signal.direction

    def _pending_has_buy(self, pending: list[tuple[Signal, BaseStrategy]], code: str, strategy_name: str) -> bool:
        """检查同标的同策略是否已有待执行买单。

        Args:
            pending: ``(Signal, 策略实例)`` 列表。
            code: 股票代码。
            strategy_name: 策略唯一名。

        Returns:
            存在匹配买单时返回 ``True``。

        Example:
            >>> engine._pending_has_buy([], "300308", "dual_ma")
            False
        """
        return any(sig.symbol == code and sig.strategy_name == strategy_name and sig.direction == "buy" for sig, _ in pending)

    def _pending_has_sell(self, pending: list[tuple[Signal, BaseStrategy]], code: str, strategy_name: str) -> bool:
        """检查同标的同策略是否已有待执行卖单。

        Args:
            pending: ``(Signal, 策略实例)`` 列表。
            code: 股票代码。
            strategy_name: 策略唯一名。

        Returns:
            存在匹配卖单时返回 ``True``。

        Example:
            >>> engine._pending_has_sell([], "300308", "dual_ma")
            False
        """
        return any(sig.symbol == code and sig.strategy_name == strategy_name and sig.direction == "sell" for sig, _ in pending)

    @staticmethod
    def _dedupe_pending_signals(pending: list[tuple[Signal, BaseStrategy]]) -> list[tuple[Signal, BaseStrategy]]:
        """去重待执行信号，并在同策略冲突时保留卖出。

        Args:
            pending: 按时间顺序累积的待执行信号。

        Returns:
            同一 ``(标的, 策略, 方向)`` 仅保留最后一条的列表；若同策略
            同时有买卖单，买单被删除。

        Example:
            >>> BacktestEngine._dedupe_pending_signals([])
            []
        """
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

    def _fuse_daily_signals(
        self, daily: list[tuple[Signal, BaseStrategy]], date_str: str
    ) -> list[tuple[Signal, BaseStrategy]]:
        """融合相同标的的三策略信号。

        - 同一标的出现卖出与买入冲突：保留全部卖出、抑制新买入，避免同日反向打架。
        - 仅有买入：按1/2/3票标记单策略、双策略共振、三策略强共振，并调整目标股数。
        - 不合并策略持仓实体，便于各策略继续独立退出和审计。

        Args:
            daily: 当日各策略生成的 ``(Signal, 策略实例)``。
            date_str: 信号日，``YYYY-MM-DD``，用于融合审计日志。

        Returns:
            已解决冲突并按共振缩放目标股数的信号列表。

        Example:
            >>> engine._fuse_daily_signals([], "2026-01-05")
            []
        """
        by_symbol: dict[str, list[tuple[Signal, BaseStrategy]]] = {}
        for item in daily:
            by_symbol.setdefault(item[0].symbol, []).append(item)

        fused: list[tuple[Signal, BaseStrategy]] = []
        for symbol, items in by_symbol.items():
            sells = [item for item in items if item[0].direction == "sell"]
            buys = [item for item in items if item[0].direction == "buy"]
            if sells:
                conflict = bool(buys)
                for signal, strategy in sells:
                    signal.fusion_votes = len(sells)
                    signal.fusion_label = "冲突卖出优先" if conflict else "退出信号"
                    if conflict:
                        signal.reason = f"[冲突卖出优先] {signal.reason}"
                    fused.append((signal, strategy))
                self.fusion_events.append({
                    "date": date_str, "symbol": symbol,
                    "state": "conflict_sell_first" if conflict else "sell",
                    "buy_votes": len(buys), "sell_votes": len(sells),
                })
                continue
            if not buys:
                continue

            votes = len(buys)
            if votes >= 3:
                label, scale = "三策略强共振", float(self.cfg["fusion_triple_scale"])
            elif votes == 2:
                label, scale = "双策略共振", float(self.cfg["fusion_double_scale"])
            else:
                label, scale = "单策略试探", float(self.cfg["fusion_single_scale"])
            for signal, strategy in buys:
                signal.fusion_votes = votes
                signal.fusion_label = label
                signal.target_shares = _floor_to_lot(signal.target_shares * scale)
                signal.reason = f"[{label}] {signal.reason}"
                if signal.target_shares > 0:
                    fused.append((signal, strategy))
            self.fusion_events.append({
                "date": date_str, "symbol": symbol, "state": label,
                "buy_votes": votes, "sell_votes": 0, "scale": scale,
            })
        return fused

    def _buy_signal_expired(self, signal: Signal, date: pd.Timestamp,
                            date_to_pos: dict[pd.Timestamp, int]) -> bool:
        """判断买入信号是否超过允许等待交易日数。

        Args:
            signal: 待执行信号；卖出信号永不过期。
            date: 尝试执行日。
            date_to_pos: 统一交易日历中的日期到序号映射。

        Returns:
            等待日数严格大于 ``max_pending_buy_days`` 时返回 ``True``。

        Example:
            >>> engine._buy_signal_expired(Signal("300308", "dual_ma", "sell", 100, 10, "x"), pd.Timestamp("2026-01-05"), {})
            False
        """
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
        """检查是否存在因组合熔断产生的未成交卖单。

        Args:
            pending: 待执行信号。

        Returns:
            至少一条信号的原因精确为 ``熔断清仓`` 时返回 ``True``。

        Example:
            >>> BacktestEngine._has_pending_liquidation([])
            False
        """
        return any(sig.direction == "sell" and sig.reason == "熔断清仓" for sig, _ in pending)

    def _validate_strategy_templates(self) -> None:
        """校验策略模板。

        positions 使用 strategy.name 作为键；如果两个策略同名，会覆盖持仓、错记交易和错误清仓。
        因此运行前必须 fail fast。

        Raises:
            ValueError: 策略缺少非空 ``name``，或多个策略使用同名。

        Example:
            >>> engine._validate_strategy_templates()
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

    def _reset_run_state(self, symbols_dict: dict[str, str]) -> None:
        """为新的回测清空所有可变状态并重建用户基线配置。

        Args:
            symbols_dict: 本次回测的 ``{代码: 显示名称}``。

        Example:
            >>> engine._reset_run_state({"300308": "中际旭创"})
            >>> engine.cash == engine.initial_capital
            True
        """
        self.cash = self.initial_capital
        self.positions = {}
        self.trades = []
        self.equity_curve = []
        self.strategy_instances = {}
        self.symbol_names = dict(symbols_dict)
        self.symbol_last_dates = {}
        self.global_last_date = None
        self.symbol_configs = {}
        self.pending_signals = []
        self.fusion_events = []
        self.cfg = self._validate_config({**self._default_config(), **self._user_cfg})

    def _apply_global_profile(self, profile: str | None) -> None:
        """把显式预设应用到本次回测的全局基线。

        Args:
            profile: ``default``/``None`` 保留用户基线；其他值为已校验的
                ``semiconductor``、``semiconductor_heavy`` 或 ``aggressive``。构造器
                ``cfg`` 中的显式覆盖始终优先于预设。

        Example:
            >>> engine._apply_global_profile("semiconductor")
            >>> engine.cfg["entry_period"]
            33
        """
        factories = {
            "semiconductor": self.semiconductor_config,
            "semiconductor_heavy": self.semiconductor_heavy_config,
            "aggressive": self.optimized_aggressive_config,
        }
        factory = factories.get(profile)
        if factory is not None:
            self.cfg = self._validate_config({
                **self._default_config(), **factory(), **self._user_cfg,
            })

    def _resolve_symbol_configs(
        self,
        symbols_dict: dict[str, str],
        per_symbol_config: dict[str, dict] | None,
        config_route: str,
    ) -> dict[str, dict]:
        """解析产业路由、标的覆盖和产业池风控。

        Args:
            symbols_dict: 本次回测标的。
            per_symbol_config: ``{代码: {配置键: 值}}`` 最高优先级覆盖。
            config_route: ``auto`` 使用产业子模板，``none`` 使用全局基线。

        Returns:
            ``{代码: 已校验的完整配置}``，并同步配置 ``self.risk``。

        Raises:
            ValueError: 覆盖中出现本次未回测的代码或非法配置。

        Example:
            >>> configs = engine._resolve_symbol_configs({"300308": "中际旭创"}, None, "auto")
            >>> configs["300308"]["entry_period"]
            8
        """
        overrides = per_symbol_config or {}
        if not isinstance(overrides, dict):
            raise ValueError("per_symbol_config 必须是 dict")
        unknown_overrides = sorted(set(overrides) - set(symbols_dict))
        if unknown_overrides:
            raise ValueError(f"per_symbol_config包含未回测标的: {unknown_overrides}")
        for code, values in overrides.items():
            if not isinstance(values, dict):
                raise ValueError(f"per_symbol_config[{code}] 必须是 dict")
            ignored_keys = sorted(set(values) - self._PER_SYMBOL_OVERRIDE_KEYS)
            if ignored_keys:
                raise ValueError(
                    f"per_symbol_config[{code}] 包含仅支持全局设置或未知的键: {ignored_keys}; "
                    "请通过 BacktestEngine(cfg=...) 设置全局参数"
                )

        self.risk = RiskManager(self.cfg)
        self.risk.configure_groups({
            code: BacktestEngine._SYMBOL_GROUP.get(
                code,
                "domestic_semiconductor" if BacktestEngine.classify_symbol(
                    code, name=symbols_dict.get(code, "")) == "semiconductor"
                else "overseas_compute",
            )
            for code in symbols_dict
        })

        def _base_for(code: str) -> dict:
            """返回 ``code`` 的产业子模板或全局基线。

            Example:
                ``_base_for("300308")``
            """
            if config_route == "auto":
                return BacktestEngine.config_for_symbol(code, name=symbols_dict.get(code, ""))
            return self.cfg

        return {
            code: self._validate_config({**_base_for(code), **overrides.get(code, {})})
            for code in symbols_dict
        }

    def _load_market_data(
        self,
        symbols_dict: dict[str, str],
        symbol_configs: dict[str, dict],
        start_date: str,
        end_date: str,
        start_ts: pd.Timestamp,
        end_ts: pd.Timestamp,
        config_route: str,
        profile: str | None,
        data_dir: str | None,
    ) -> tuple[dict[str, pd.DataFrame], dict[str, dict[str, pd.Series]]]:
        """读取、二次裁剪行情并计算指标。

        Args:
            symbols_dict: 本次回测标的。
            symbol_configs: 每个标的的完整配置。
            start_date: 传给数据层的开始日字符串。
            end_date: 传给数据层的结束日字符串。
            start_ts: 已解析的包含开始日。
            end_ts: 已解析的包含结束日。
            config_route: 路由模式，仅用于打印实际路由。
            profile: 全局预设名，仅在无自动路由时显示。
            data_dir: 本地 CSV 目录；``None`` 表示在线读取。

        Returns:
            ``(data_map, indicator_map)``。

        Raises:
            RuntimeError: 任一标的在请求区间没有有效行情。

        Example:
            >>> data, indicators = engine._load_market_data(
            ...     {"300308": "中际旭创"}, {"300308": engine.cfg},
            ...     "2025-01-01", "2025-01-31", pd.Timestamp("2025-01-01"),
            ...     pd.Timestamp("2025-01-31"), "none", "default", "data_qfq_reconstructed",
            ... )
        """
        data_map: dict[str, pd.DataFrame] = {}
        ind_map: dict[str, dict[str, pd.Series]] = {}
        for code, name in symbols_dict.items():
            print(f"  获取 {name}({code}) 数据...")
            df = DataFetcher.load_stock_data(code, start_date, end_date, data_dir=data_dir)
            # 数据源可能返回超出请求范围的数据，引擎在信号层前再裁剪一次。
            df = df[(df.index >= start_ts) & (df.index <= end_ts)].copy()
            if df.empty:
                raise RuntimeError(f"{code} 在 {start_date} ~ {end_date} 内没有有效行情数据")
            data_map[code] = df
            ind_map[code] = Indicators.compute_all(df, symbol_configs[code])
            route = BacktestEngine._SYMBOL_PROFILE.get(
                code, BacktestEngine.classify_symbol(code, name=name)
            ) if config_route == "auto" else str(profile or "default")
            print(f"  [参数路由] {name}({code}) -> {route}")
            print(f"  {name}({code}): {len(df)}条数据, 区间 {df.index[0].date()} ~ {df.index[-1].date()}")
        return data_map, ind_map

    def _select_momentum_candidates(
        self,
        data_map: dict[str, pd.DataFrame],
        symbols_dict: dict[str, str],
        date: pd.Timestamp,
    ) -> set[str]:
        """按双池保留名额和全市场动量选出当日可新开仓标的。

        Args:
            data_map: 全部标的行情。
            symbols_dict: ``{代码: 名称}``，用于未知代码的产业分类。
            date: 信号日。

        Returns:
            可新开仓的代码集合。可计算动量的标的为空时返回全部代码。

        Example:
            >>> engine._select_momentum_candidates({}, {}, pd.Timestamp("2026-01-05"))
            set()
        """
        lookback = int(self.cfg.get("momentum_lookback", 20))
        scores: dict[str, float] = {}
        for code, df in data_map.items():
            if date not in df.index:
                continue
            i = df.index.get_loc(date)
            if i >= lookback:
                scores[code] = float(df["close"].iloc[i] / df["close"].iloc[i - lookback] - 1)
        if not scores:
            return set(symbols_dict)

        max_positions = int(self.cfg.get("max_positions", 2))
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        min_slots = min(int(self.cfg.get("group_min_slots", 0)), max_positions // 2)
        selected: list[str] = []
        if min_slots > 0:
            for group in ("overseas_compute", "domestic_semiconductor"):
                group_ranked = [
                    code for code, _ in ranked
                    if (BacktestEngine._SYMBOL_GROUP.get(code) or (
                        "domestic_semiconductor" if BacktestEngine.classify_symbol(
                            code, name=symbols_dict.get(code, "")) == "semiconductor"
                        else "overseas_compute"
                    )) == group
                ]
                selected.extend(group_ranked[:min_slots])
        for code, _ in ranked:
            if code not in selected:
                selected.append(code)
            if len(selected) >= max_positions:
                break
        return set(selected[:max_positions])

    def _record_equity(self, data_map: dict[str, pd.DataFrame], date: pd.Timestamp, date_str: str) -> None:
        """按当日收盘价追加一条权益记录。

        Args:
            data_map: 全部标的行情。
            date: 估值日。
            date_str: 同一日的 ``YYYY-MM-DD`` 文本。

        Example:
            >>> engine._record_equity({}, pd.Timestamp("2026-01-05"), "2026-01-05")
            >>> engine.equity_curve[-1]["cash"] == engine.cash
            True
        """
        assets = self._total_assets(data_map, date)
        self.equity_curve.append({
            "date": date_str,
            "assets": assets,
            "cash": self.cash,
            "position_value": assets - self.cash,
        })

    def _apply_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        all_dates: list[pd.Timestamp],
        date_to_pos: dict[pd.Timestamp, int],
        pending: list[tuple[Signal, BaseStrategy]],
    ) -> tuple[list[tuple[Signal, BaseStrategy]], bool, bool]:
        """应用回撤、冷却、单日亏损和未成交熔断卖单风控。

        Args:
            current_assets: 当日收盘资产。
            date_str: 同一日的 ``YYYY-MM-DD`` 文本。
            all_dates: 统一交易日历。
            date_to_pos: 交易日到序号的映射。
            pending: 当前待执行信号。

        Returns:
            ``(更新后的待执行信号, 是否禁止买入, 是否已生成全仓卖单)``。

        Example:
            >>> engine.risk.daily_start_assets = engine.initial_capital
            >>> _, blocked, liquidate = engine._apply_portfolio_risk(
            ...     engine.initial_capital, "2026-01-05",
            ...     [pd.Timestamp("2026-01-05")], {pd.Timestamp("2026-01-05"): 0}, [],
            ... )
            >>> (blocked, liquidate)
            (False, False)
        """
        risk_status = self.risk.check_portfolio_risk(
            current_assets, date_str, trading_dates=all_dates, date_to_pos=date_to_pos
        )
        if risk_status is None and self.risk.check_daily_loss(current_assets):
            risk_status = "单日亏损限制"

        risk_blocked = self._has_pending_liquidation(pending)
        liquidate = False
        if risk_blocked:
            risk_status = risk_status or "熔断清仓待成交"
        if not risk_status:
            return pending, risk_blocked, liquidate

        if risk_status == "组合回撤熔断":
            liquidate = bool(self.cfg.get("liquidate_on_circuit_breaker", True))
            if liquidate:
                print(
                    f"  ⚠ [{date_str}] {risk_status}! 生成清仓信号(T+1执行), "
                    f"冷却{self.cfg['cooldown_days']}日"
                )
                liquidation_signals = self._generate_liquidation_signals(date_str)
                pending = self._dedupe_pending_signals(
                    [(sig, strategy) for sig, strategy in pending if sig.direction == "sell"]
                    + liquidation_signals
                )
            else:
                print(
                    f"  ⚠ [{date_str}] {risk_status}! 禁止开新仓, "
                    f"冷却{self.cfg['cooldown_days']}日"
                )
        return pending, True, liquidate

    def _collect_strategy_signals(
        self,
        symbols_dict: dict[str, str],
        data_map: dict[str, pd.DataFrame],
        ind_map: dict[str, dict[str, pd.Series]],
        date: pd.Timestamp,
        date_str: str,
        current_assets: float,
        pending: list[tuple[Signal, BaseStrategy]],
        allow_buys: bool,
        top_symbols: set[str] | None = None,
    ) -> list[tuple[Signal, BaseStrategy]]:
        """遍历当日有行情的策略，收集不重复且通过动量闸门的信号。

        Args:
            symbols_dict: 本次回测标的。
            data_map: 标的行情。
            ind_map: 标的指标序列。
            date: 当前交易日。
            date_str: 同一日的 ``YYYY-MM-DD`` 文本。
            current_assets: 当日收盘资产。
            pending: 已在等待执行的信号，用于抑制重复单。
            allow_buys: ``False`` 时只收集卖出，用于熔断/冷却日。
            top_symbols: 允许新开仓的动量候选；``None`` 表示不施加该过滤。

        Returns:
            尚未做跨策略融合的 ``(Signal, 策略实例)`` 列表。

        Example:
            >>> engine._collect_strategy_signals({}, {}, {}, pd.Timestamp("2026-01-05"), "2026-01-05", 1_000_000, [], True)
            []
        """
        held_symbols = set(self.positions)
        daily: list[tuple[Signal, BaseStrategy]] = []
        for code in symbols_dict:
            df = data_map[code]
            if date not in df.index:
                continue
            i = df.index.get_loc(date)
            for strategy in self.strategy_instances[code]:
                ctx = BarContext(
                    i=i, df=df, current_assets=current_assets,
                    indicators=ind_map[code], symbol=code, date=date_str,
                )
                signal = strategy.on_bar(ctx)
                if signal is None:
                    continue
                if signal.direction == "buy":
                    if not allow_buys or self._pending_has_buy(pending, code, strategy.name):
                        continue
                    if top_symbols is not None and code not in top_symbols and code not in held_symbols:
                        continue
                elif signal.direction == "sell":
                    if self._pending_has_sell(pending, code, strategy.name):
                        continue
                else:
                    continue
                daily.append((signal, strategy))
        return daily

    def run(self, symbols_dict: dict[str, str], start_date: str, end_date: str,
            per_symbol_config: dict[str, dict] | None = None,
            profile: str | None = None,
            config_route: str = "auto",
            data_dir: str | None = None) -> dict:
        """运行一次多标的组合回测。

        ``config_route="auto"`` 会给每只股票套用固定的产业子模板；此时
        ``profile`` 只选择全局组合/执行基线（如回撤、轮动、融合和成本），
        不取代子模板的入场、退出和单票仓位参数。
        ``config_route="none"`` 则所有标的共用 ``profile`` 配置。两种模式中，
        ``per_symbol_config`` 都是标的策略与单票上限的最高优先级覆盖；
        佣金、总仓与轮动等全局键必须通过构造器 ``cfg`` 设置，
        若放入 ``per_symbol_config`` 会明确报错，避免静默忽略。

        Args:
            symbols_dict: ``{6位代码: 显示名称}``，例如 ``{"300308": "中际旭创"}``。
            start_date: 回测开始日（含），可被 :class:`pandas.Timestamp` 解析。
            end_date: 回测结束日（含）。
            per_symbol_config: ``{代码: {配置键: 值}}``；覆盖指定标的的指标、
                信号、仓位计算和 ``max_symbol_weight``，不覆盖全局成本/总仓。
            profile: ``default``、``semiconductor``、``semiconductor_heavy``、
                ``aggressive`` 或 ``None``。
            config_route: ``auto`` 按产业子模板分发，``none`` 共用全局配置。
            data_dir: 可选的本地前复权 CSV 目录；为 ``None`` 时使用 AKShare。

        Returns:
            包含收益、回撤、Sharpe、权益曲线、交易和未成交信号的字典。

        Raises:
            ValueError: 参数模式、股票代码、日期或配置覆盖不合法。
            RuntimeError: 某标的在请求区间没有数据。

        Example:
            >>> engine = BacktestEngine()
            >>> result = engine.run(
            ...     {"300308": "中际旭创"}, "2025-01-01", "2026-06-30",
            ...     config_route="auto", data_dir="data_qfq_reconstructed",
            ... )
            >>> "total_return" in result
            True
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
        bad_codes = [
            code for code in symbols_dict
            if not isinstance(code, str) or not _SYMBOL_RE.match(code)
        ]
        if bad_codes:
            raise ValueError(f"symbols_dict 包含非法股票代码: {bad_codes}")
        start_ts = pd.Timestamp(start_date)
        end_ts = pd.Timestamp(end_date)
        if start_ts > end_ts:
            raise ValueError("start_date 不能晚于 end_date")

        self._reset_run_state(symbols_dict)

        print(f"\n{'='*60}")
        print(f"AQuant 回测启动")
        print(f"  资金: {self.initial_capital:,.0f}")
        print(f"  标的: {symbols_dict}")
        print(f"  区间: {start_date} ~ {end_date}")
        print(f"{'='*60}\n")

        self._apply_global_profile(profile)
        symbol_configs = self._resolve_symbol_configs(
            symbols_dict, per_symbol_config, config_route
        )
        self.symbol_configs = symbol_configs

        data_map, ind_map = self._load_market_data(
            symbols_dict, symbol_configs, start_date, end_date, start_ts, end_ts,
            config_route, profile, data_dir,
        )

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

            # 开盘执行前锁定前收资产，供当日收盘亏损闸门使用。
            if self.equity_curve:
                self.risk.daily_start_assets = self.equity_curve[-1]["assets"]
            else:
                self.risk.daily_start_assets = self.initial_capital

            # ── Step 1: 执行昨日产生的待执行信号（以今日开盘价成交）──
            if pending_signals:
                # 停牌或跌停未执行卖单会留到下一交易日继续尝试。
                pending_signals = self._execute_pending_signals(pending_signals, data_map, date, date_to_pos)
            else:
                pending_signals = []

            # ── Step 2: 计算当前总资产（用今日收盘价）──
            current_assets = self._total_assets(data_map, date)

            pending_signals, risk_blocked, liquidate = self._apply_portfolio_risk(
                current_assets, date_str, all_dates, date_to_pos, pending_signals
            )

            if risk_blocked:
                # 已生成全仓卖单时不再重复调用策略；否则仍收集正常退出信号。
                if not liquidate:
                    pending_signals.extend(self._collect_strategy_signals(
                        symbols_dict, data_map, ind_map, date, date_str,
                        current_assets, pending_signals, allow_buys=False,
                    ))

                pending_signals = self._dedupe_pending_signals(pending_signals)
                closed_keys = self._close_positions_on_data_end(data_map, date)
                if closed_keys:
                    pending_signals = [(sig, strat) for sig, strat in pending_signals if (sig.symbol, sig.strategy_name) not in closed_keys]
                self._record_equity(data_map, date, date_str)
                continue

            # ── Step 4: 生成今日信号（基于今日收盘数据）+ 动量轮动过滤 ──
            top_symbols = self._select_momentum_candidates(data_map, symbols_dict, date)
            daily_signals = self._collect_strategy_signals(
                symbols_dict, data_map, ind_map, date, date_str,
                current_assets, pending_signals, allow_buys=True,
                top_symbols=top_symbols,
            )

            fused_daily = self._fuse_daily_signals(daily_signals, date_str)
            sell_symbols = {sig.symbol for sig, _ in fused_daily if sig.direction == "sell"}
            if sell_symbols:
                # 今日出现退出/冲突卖出时，撤销同标的此前尚未成交的旧买单。
                pending_signals = [
                    item for item in pending_signals
                    if not (item[0].direction == "buy" and item[0].symbol in sell_symbols)
                ]
            pending_signals.extend(fused_daily)

            pending_signals = self._dedupe_pending_signals(pending_signals)

            # ── Step 5: 标的数据提前结束时强制结算，避免持仓在无行情区间继续 stale 估值 ──
            closed_keys = self._close_positions_on_data_end(data_map, date)
            if closed_keys:
                pending_signals = [(sig, strat) for sig, strat in pending_signals if (sig.symbol, sig.strategy_name) not in closed_keys]

            self._record_equity(data_map, date, date_str)

        # 5. 末日估值 / 可选强制清算
        # 默认不强制平仓：最终权益按最后可用收盘价 mark-to-market，避免把人为末日卖出计入策略胜率。
        # 若用户需要所有交易闭合以统计已实现胜率/盈亏比，可设置 force_close_on_end=True。
        last_date = all_dates[-1]
        if self.cfg.get("force_close_on_end", False):
            self._liquidate_all(data_map, last_date, reason="末日强制清算")
            pending_signals = []
        final_assets = self._total_assets(data_map, last_date)
        if self.cfg.get("force_close_on_end", False) and self.equity_curve:
            # 强制卖出产生额外成本，末日权益必须与终值使用同一口径。
            self.equity_curve[-1].update({
                "assets": final_assets,
                "cash": self.cash,
                "position_value": final_assets - self.cash,
            })
        self.pending_signals = self._dedupe_pending_signals(pending_signals)
        print(f"\n  回测完成: 初始 {self.initial_capital:,.0f} → 终值 {final_assets:,.0f}")

        return self._build_result(final_assets, all_dates)

    def _execute_pending_signals(self, pending: list[tuple[Signal, BaseStrategy]],
                                  data_map: dict[str, pd.DataFrame], date: pd.Timestamp,
                                  date_to_pos: dict[pd.Timestamp, int]) -> list[tuple[Signal, BaseStrategy]]:
        """以当日开盘可见信息执行待处理信号，卖出优先。

        Args:
            pending: 之前收盘产生且尚未执行的信号。
            data_map: ``{代码: 前复权日线 DataFrame}``。
            date: 当前统一交易日。
            date_to_pos: 统一交易日历中的日期序号。

        Returns:
            停牌或跌停等原因未成交、需次日再试的信号。涨停未成交买单
            会丢弃，不会在入场条件可能已失效后追买。

        Example:
            >>> engine._execute_pending_signals([], {}, pd.Timestamp("2026-01-05"), {})
            []
        """
        date_str = date.strftime("%Y-%m-%d")
        # 卖出优先；同日买入按预先固定的产业龙头优先表执行，再用代码稳定打破并列。
        # 优先表不读取回测期收益，且保证 symbols_dict 只改变排列时结果不变。
        strategy_rank = {"turtle_breakout": 0, "dual_ma": 1, "atr_channel": 2}
        sorted_pending = sorted(
            pending,
            key=lambda x: (
                0 if x[0].direction == "sell" else 1,
                EXECUTION_PRIORITY.get(x[0].symbol, 9999),
                x[0].symbol,
                strategy_rank.get(x[0].strategy_name, 99),
            ),
        )
        unexecuted = []
        for signal, strategy in sorted_pending:
            code = signal.symbol
            if self._buy_signal_expired(signal, date, date_to_pos):
                # 买入信号等待过久后过期，避免停牌/长期无开盘后按旧突破信号买入。
                continue
            if code not in data_map or date not in data_map[code].index:
                # 该标的当日无行情（如停牌），信号保留到次日。
                unexecuted.append((signal, strategy))
                continue

            open_price = data_map[code].loc[date, "open"]
            if pd.isna(open_price) or open_price <= 0:
                unexecuted.append((signal, strategy))
                continue

            # 用前收和开盘价近似涨跌停可成交性。
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
                executed = self._execute_sell(signal, strategy, date_str)
                if not executed and strategy.position is not None:
                    unexecuted.append((signal, strategy))

        return self._dedupe_pending_signals(unexecuted)

    def _latest_close_on_or_before(self, df: pd.DataFrame, date: pd.Timestamp) -> float:
        """取指定日当日或之前最近的有效收盘价。

        Args:
            df: 以交易日为索引且含 ``close`` 的行情。
            date: 估值日。

        Returns:
            最近正收盘价；没有可用价时返回 ``0.0``。

        Example:
            >>> df = pd.DataFrame({"close": [10]}, index=pd.to_datetime(["2026-01-05"]))
            >>> engine._latest_close_on_or_before(df, pd.Timestamp("2026-01-05"))
            10.0
        """
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
        """取指定日之前最近的有效收盘价。

        Args:
            df: 以交易日为索引且含 ``close`` 的行情。
            date: 开盘执行日；本方法严格不使用该日收盘价。

        Returns:
            前一可用正收盘价；没有时返回 ``0.0``。

        Example:
            >>> df = pd.DataFrame({"close": [10]}, index=pd.to_datetime(["2026-01-05"]))
            >>> engine._latest_close_before(df, pd.Timestamp("2026-01-06"))
            10.0
        """
        mask = df.index < date
        if not mask.any():
            return 0.0
        closes = pd.to_numeric(df.loc[mask, "close"], errors="coerce")
        closes = closes[closes > 0]
        return float(closes.iloc[-1]) if not closes.empty else 0.0

    def _execution_mark_prices(self, data_map: dict[str, pd.DataFrame], date: pd.Timestamp) -> dict[str, float]:
        """开盘执行阶段可见价格：当日 open 可用则用 open，否则用前一可用 close。

        不能用执行日 close 做买入风控或仓位估值，否则会把 T+1 收盘价提前用于开盘交易。

        Args:
            data_map: 全部标的行情。
            date: 开盘执行日。

        Returns:
            ``{代码: 当日开盘价或前收兜底价}``。

        Example:
            >>> engine._execution_mark_prices({}, pd.Timestamp("2026-01-05"))
            {}
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
        """用显式价格表计算现金加持仓市值。

        Args:
            prices: ``{代码: 估值价}``；缺失代码使用持仓平均成本兜底。

        Returns:
            总资产（元）。

        Example:
            >>> BacktestEngine(1_000_000)._total_assets_at_prices({})
            1000000.0
        """
        total = self.cash
        for code, positions in self.positions.items():
            price = prices.get(code)
            for pos in positions.values():
                mark = price if price is not None and price > 0 else pos.entry_price
                total += pos.market_value_at(mark)
        return float(total)

    def _total_assets(self, data_map: dict[str, pd.DataFrame], date: pd.Timestamp) -> float:
        """按指定日收盘可见价计算总资产。

        Args:
            data_map: 全部标的行情。
            date: 收盘估值日；缺日数据时使用之前最近收盘价。

        Returns:
            现金加所有持仓市值（元）。

        Example:
            >>> BacktestEngine(1_000_000)._total_assets({}, pd.Timestamp("2026-01-05"))
            1000000.0
        """
        total = self.cash
        for code, positions in self.positions.items():
            if code not in data_map:
                continue
            price = self._latest_close_on_or_before(data_map[code], date)
            for pos in positions.values():
                mark = price if price > 0 else pos.entry_price
                total += pos.market_value_at(mark)
        return float(total)

    def _fit_buy_to_cash(
        self,
        requested_shares: float,
        exec_price: float,
        commission_rate: float,
        min_commission: float,
    ) -> tuple[int, float, float, float]:
        """把买入股数缩减到现金可覆盖的最大整手。

        Args:
            requested_shares: 风险层允许的最大股数。
            exec_price: 含滑点的每股成交价。
            commission_rate: 买入佣金率。
            min_commission: 单笔最低佣金（元）。

        Returns:
            ``(股数, 成交额, 佣金, 现金支出)``。无法买一手时四项都为 0。

        Example:
            >>> BacktestEngine(10_000)._fit_buy_to_cash(1_000, 10.0, 0.00025, 5.0)
            (900, 9000.0, 5.0, 9005.0)
        """
        shares = _floor_to_lot(requested_shares)
        if shares > 0:
            requested_value = shares * exec_price
            requested_commission = max(requested_value * commission_rate, min_commission)
            if requested_value + requested_commission > self.cash:
                shares = _floor_to_lot(self.cash / (exec_price * (1 + commission_rate)))
        while shares > 0:
            buy_value = shares * exec_price
            commission = max(buy_value * commission_rate, min_commission)
            total_cost = buy_value + commission
            if total_cost <= self.cash:
                return shares, buy_value, commission, total_cost
            shares -= A_SHARE_LOT_SIZE
        return 0, 0.0, 0.0, 0.0

    def _apply_buy_to_position(
        self,
        signal: Signal,
        strategy: BaseStrategy,
        date_str: str,
        shares: int,
        exec_price: float,
        total_cost: float,
    ) -> None:
        """新建或合并一个已成交买入到子策略持仓。

        Args:
            signal: 原始买入信号，提供 ATR 和建议止损。
            strategy: 被更新的策略实例。
            date_str: 成交日，``YYYY-MM-DD``。
            shares: 已通过资金和风控检查的整手股数。
            exec_price: 含滑点、不含佣金的成交价。
            total_cost: 成交额加佣金的现金支出。

        Example:
            >>> signal = Signal("300308", "dual_ma", "buy", 100, 10, "test", atr=1)
            >>> engine._apply_buy_to_position(signal, DualMAStrategy(engine.cfg), "2026-01-05", 100, 10, 1005)
        """
        strategy_cfg = strategy.cfg
        effective_entry = total_cost / shares
        exec_based_stop = (
            exec_price - float(strategy_cfg.get("atr_multiplier", 2.0)) * signal.atr
            if signal.atr > 0
            else signal.stop_loss
        )
        symbol_positions = self.positions.setdefault(signal.symbol, {})
        if strategy.name in symbol_positions:
            pos = symbol_positions[strategy.name]
            total_cost_basis = pos.cost + total_cost
            total_shares = pos.shares + shares
            pos.entry_price = total_cost_basis / total_shares
            pos.shares = total_shares
            pos.units += 1
            stop_candidates = [pos.stop_loss, exec_based_stop]
            if signal.stop_loss:
                stop_candidates.append(signal.stop_loss)
            pos.stop_loss = max(stop_candidates)
            pos.highest_since_entry = max(pos.highest_since_entry, exec_price)
            pos.highest_close_since_entry = max(pos.highest_close_since_entry, exec_price)
            pos.last_buy_date = date_str
            pos.last_add_price = exec_price
        else:
            symbol_positions[strategy.name] = Position(
                symbol=signal.symbol,
                strategy_name=strategy.name,
                shares=shares,
                entry_price=effective_entry,
                entry_date=date_str,
                stop_loss=exec_based_stop,
                highest_since_entry=exec_price,
                highest_close_since_entry=exec_price,
                units=1,
                last_buy_date=date_str,
                last_add_price=exec_price,
            )
        strategy.position = symbol_positions[strategy.name]

    def _execute_buy(self, signal: Signal, strategy: BaseStrategy, date_str: str,
                     data_map: dict[str, pd.DataFrame] | None = None,
                     date: pd.Timestamp | None = None) -> bool:
        """执行买入（含滑点+佣金）。

        关键：使用 ``signal.atr``（信号日锁定的ATR）计算风险仓位和止损，
        严禁读取执行日ATR，否则会引入前瞻偏差。

        Args:
            signal: 买入信号；``price`` 应已替换为执行日开盘价。
            strategy: 产生信号的标的级策略实例。
            date_str: 成交日，``YYYY-MM-DD``。
            data_map: 可选行情映射；正式回测应与 ``date`` 一起传入，供仓位风控估值。
            date: 可选执行日；正式回测应与 ``data_map`` 一起传入。

        Returns:
            有实际成交时返回 ``True``，风控拒绝或可买整手为 0 时返回 ``False``。

        Example:
            >>> signal = Signal("300308", "dual_ma", "buy", 0, 10, "test")
            >>> engine._execute_buy(signal, DualMAStrategy(engine.cfg), "2026-01-05")
            False
        """
        if signal.target_shares <= 0 or signal.price <= 0:
            return False

        global_cfg = self.cfg
        strategy_cfg = strategy.cfg
        slippage = float(global_cfg.get("slippage", 0.001))
        commission_rate = float(global_cfg.get("commission_rate", 0.00025))
        min_commission = float(global_cfg.get("min_commission", 0.0))

        exec_price = float(signal.price) * (1 + slippage)
        shares, buy_value, commission, total_cost = self._fit_buy_to_cash(
            signal.target_shares, exec_price, commission_rate, min_commission
        )
        if shares <= 0:
            return False

        if data_map is not None and date is not None:
            current_prices = self._execution_mark_prices(data_map, date)
            current_assets = self._total_assets_at_prices(current_prices)
        else:
            current_assets = self.initial_capital
            current_prices = None

        if signal.symbol not in self.positions and len(self.positions) >= int(global_cfg.get("max_positions", 1)):
            return False

        if (
            data_map is not None
            and date is not None
            and self.cfg.get("force_close_on_end", False)
            and self.global_last_date is not None
            and pd.Timestamp(date) == self.global_last_date
        ):
            # 末日开盘不再买入，避免收盘人工平仓违反 A 股 T+1。
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
                current_assets * float(strategy_cfg.get("strategy_weight", 1.0)), exec_price, signal.atr,
                unit_number=unit_num,
            )
            shares = min(shares, risk_limited_shares)
            if shares <= 0:
                return False
            shares, buy_value, commission, total_cost = self._fit_buy_to_cash(
                shares, exec_price, commission_rate, min_commission
            )
            if shares <= 0:
                return False

        if not self.risk.check_position_limits(
            signal.symbol, self.positions, current_assets, buy_value,
            current_prices, position_cfg=strategy_cfg,
        ):
            return False
        # 二次单日亏损检查：开盘执行阶段只能使用开盘/昨收可见估值，
        # 与收盘生成信号阶段的检查共同构成双闸门，避免隔夜跳空后继续买入。
        if self.risk.check_daily_loss(current_assets):
            return False

        self.cash -= total_cost
        self._apply_buy_to_position(signal, strategy, date_str, shares, exec_price, total_cost)
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

    def _execute_sell(self, signal: Signal, strategy: BaseStrategy, date_str: str) -> bool:
        """执行卖出并记录成本、盈亏和峰值回吐。

        Args:
            signal: 卖出信号；``price`` 应为执行日开盘价或明确的强制结算价。
            strategy: 持有相应子仓的策略实例。
            date_str: 成交日，``YYYY-MM-DD``。

        Returns:
            有实际成交时返回 ``True``；无持仓、价格无效或整手数为 0 时返回 ``False``。

        Example:
            >>> signal = Signal("300308", "dual_ma", "sell", 0, 10, "test")
            >>> engine._execute_sell(signal, DualMAStrategy(engine.cfg), "2026-01-05")
            False
        """
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
        peak_close = max(float(pos.highest_close_since_entry), float(pos.entry_price))
        exit_from_peak_pct = exec_price / peak_close - 1 if peak_close > 0 else 0.0

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
            peak_close=peak_close,
            exit_from_peak_pct=exit_from_peak_pct,
        ))
        return True

    def _generate_liquidation_signals(self, date_str: str) -> list[tuple[Signal, BaseStrategy]]:
        """生成全仓清仓信号（T+1执行，消除前瞻偏差）。

        以引擎持仓字典为唯一真实仓位来源，避免策略对象上的陈旧引用生成幽灵清仓单。

        Args:
            date_str: 熔断触发日，``YYYY-MM-DD``；实际成交日通常是下一交易日。

        Returns:
            每个现存子仓一条全量卖出信号，以及其策略实例。

        Example:
            >>> BacktestEngine()._generate_liquidation_signals("2026-01-05")
            []
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

    def _liquidate_all(
        self, data_map: dict[str, pd.DataFrame], date: pd.Timestamp,
        reason: str = "末日结算",
    ) -> None:
        """按指定日收盘价做强制结算。

        仅用于 force_close_on_end 或调试结算；熔断清仓必须走 T+1 pending sell，
        不能调用本方法绕过 A 股 T+1 执行模型。

        Args:
            data_map: 全部标的行情。
            date: 强制结算日；只有当日存在行情的标的可被结算。
            reason: 写入卖出交易记录的原因。

        Example:
            >>> engine._liquidate_all({}, pd.Timestamp("2026-06-30"))
        """
        cfg = self.cfg
        slippage = cfg.get("slippage", 0.001)
        commission_rate = cfg.get("commission_rate", 0.00025)
        min_commission = cfg.get("min_commission", 0.0)
        stamp_duty = cfg.get("stamp_duty", 0.0005)

        date_str = date.strftime("%Y-%m-%d")
        liquidated_codes: set[str] = set()
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
                peak_close = max(float(pos.highest_close_since_entry), float(pos.entry_price))
                exit_from_peak_pct = exec_price / peak_close - 1 if peak_close > 0 else 0.0

                self.cash += net_proceeds
                self.trades.append(TradeRecord(
                    symbol=code, strategy_name=strat_name, direction="sell",
                    shares=pos.shares, price=exec_price, date=date_str,
                    reason=reason, pnl=pnl, pnl_pct=pnl_pct, signal_date=date_str,
                    gross_value=sell_value, commission=commission, stamp_duty_cost=stamp_duty_cost,
                    net_cash_flow=net_proceeds, cash_after=self.cash,
                    peak_close=peak_close, exit_from_peak_pct=exit_from_peak_pct,
                ))
                del self.positions[code][strat_name]

            # 清理空字典：该标的所有策略持仓都已清仓时，删除该标的键
            if not self.positions[code]:
                del self.positions[code]
            liquidated_codes.add(code)

        # 只清理已成交标的的策略引用；停牌未结算标的保留持仓。
        for code in liquidated_codes:
            if code in self.strategy_instances:
                for strategy in self.strategy_instances[code]:
                    strategy.position = None

    def _close_positions_on_data_end(self, data_map: dict[str, pd.DataFrame], date: pd.Timestamp) -> set[tuple[str, str]]:
        """标的数据早于全局日历结束时，按该标的最后收盘价做强制结算。

        返回已结算的 (symbol, strategy_name)，调用方据此清理同标的同策略的 pending 信号，
        避免结算后仍保留永远无法执行的 stale sell/buy。

        Args:
            data_map: 全部标的行情。
            date: 当前统一交易日。

        Returns:
            本日已结算的 ``(代码, 策略名)`` 集合。

        Example:
            >>> BacktestEngine()._close_positions_on_data_end({}, pd.Timestamp("2026-06-30"))
            set()
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
                if self._execute_sell(signal, strategy, date.strftime("%Y-%m-%d")):
                    closed.add((code, strat_name))
        return closed

    def _build_result(self, final_assets: float, all_dates: list[pd.Timestamp]) -> dict:
        """由权益曲线和交易记录构建绩效结果。

        Args:
            final_assets: 末日现金加市值计价的总资产。
            all_dates: 本次回测的统一交易日历。

        Returns:
            绩效指标、权益/回撤序列、交易、待执行信号、参数路由与融合审计数据。
            权益曲线为空时返回 ``{"error": "无权益数据"}``。

        Example:
            >>> engine = BacktestEngine()
            >>> engine._build_result(engine.cash, [])
            {'error': '无权益数据'}
        """
        eq = pd.DataFrame(self.equity_curve)
        if eq.empty:
            return {"error": "无权益数据"}

        eq["date"] = pd.to_datetime(eq["date"])
        eq["assets"] = eq["assets"].astype(float)
        eq = eq.set_index("date")

        total_return = (final_assets - self.initial_capital) / self.initial_capital
        # 年化收益与 Sharpe 统一使用 252 交易日基准。
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
        exit_givebacks = [
            float(t.exit_from_peak_pct) for t in sell_trades
            if _is_finite_number(t.exit_from_peak_pct)
        ]
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
            "avg_exit_from_peak": float(np.mean(exit_givebacks)) if exit_givebacks else 0.0,
            "worst_exit_from_peak": float(min(exit_givebacks)) if exit_givebacks else 0.0,
            "open_positions": int(open_positions),
            "open_position_value": open_position_value,
            "force_close_on_end": bool(self.cfg.get("force_close_on_end", False)),
            "equity_curve": eq,
            "trades": self.trades,
            "drawdown_series": drawdown,
            "pending_signals": [signal for signal, _ in self.pending_signals],
            "parameter_routes": {
                code: BacktestEngine._SYMBOL_PROFILE.get(
                    code, BacktestEngine.classify_symbol(code, name=self.symbol_names.get(code, ""))
                )
                for code in self.symbol_names
            },
            "fusion_events": list(self.fusion_events),
            "reversal_exit_trades": sum(
                1 for trade in self.trades
                if trade.direction == "sell" and "反转" in str(trade.reason)
            ),
        }


# ═══════════════════════════════════════════════════════════════════════
#  绩效报告
# ═══════════════════════════════════════════════════════════════════════

class PerformanceReport:
    """将 :class:`BacktestEngine` 结果打印、导出或绘图的无状态工具。

    Example:
        >>> PerformanceReport.print_report({"error": "demo"}, {})
        回测失败: demo
    """

    @staticmethod
    def print_report(result: dict, symbols_dict: dict[str, str]) -> None:
        """向标准输出打印绩效摘要、待执行信号和最近 20 笔交易。

        Args:
            result: :meth:`BacktestEngine.run` 返回的结果字典。
            symbols_dict: ``{代码: 显示名称}``，用于报告标题和信号显示。

        Example:
            >>> PerformanceReport.print_report({"error": "无数据"}, {})
            回测失败: 无数据
        """
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
        print(f"  平均峰值回吐:{result.get('avg_exit_from_peak', 0.0):>15.2%}")
        print(f"  最差峰值回吐:{result.get('worst_exit_from_peak', 0.0):>15.2%}")
        print(f"{'═'*60}\n")

        pending = result.get("pending_signals", [])
        if pending:
            print("  下一交易日待执行信号（以届时可成交开盘价为准）:")
            for signal in pending:
                print(
                    f"  {signal.signal_date} {symbols_dict.get(signal.symbol, signal.symbol)}"
                    f"({signal.symbol}) {signal.strategy_name} {signal.direction.upper()} "
                    f"{signal.target_shares}股 | {signal.fusion_label} | {signal.reason}"
                )

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
    def save_result(result: dict, output_dir: str) -> None:
        """保存可审计的权益、交易、待执行信号和摘要 CSV。

        Args:
            result: 成功的 :meth:`BacktestEngine.run` 结果。
            output_dir: 输出目录；不存在时递归创建。

        Raises:
            ValueError: ``result`` 是失败结果，没有可导出的权益数据。
            OSError: 目录创建或 CSV 写入失败。

        Example:
            >>> PerformanceReport.save_result(result, "backtest_results")
        """
        if "error" in result:
            raise ValueError(f"无法保存失败的回测结果: {result['error']}")
        out = Path(output_dir).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        result["equity_curve"].to_csv(out / "equity_curve.csv", encoding="utf-8-sig")
        result["drawdown_series"].rename("drawdown").to_csv(
            out / "drawdown.csv", encoding="utf-8-sig"
        )
        pd.DataFrame([vars(t) for t in result.get("trades", [])]).to_csv(
            out / "trades.csv", index=False, encoding="utf-8-sig"
        )
        pd.DataFrame([vars(s) for s in result.get("pending_signals", [])]).to_csv(
            out / "latest_signals.csv", index=False, encoding="utf-8-sig"
        )
        summary_keys = [
            "initial_capital", "final_assets", "total_return", "annual_return",
            "max_drawdown", "sharpe", "win_rate", "profit_factor", "total_trades",
            "sell_trades", "open_positions",
            "reversal_exit_trades", "avg_exit_from_peak", "worst_exit_from_peak",
        ]
        pd.DataFrame([{k: result.get(k) for k in summary_keys}]).to_csv(
            out / "summary.csv", index=False, encoding="utf-8-sig"
        )

    @staticmethod
    def plot_equity_curve(result: dict, save_path: str = "equity_curve.png") -> None:
        """绘制权益和回撤子图并保存 PNG。

        Args:
            result: :meth:`BacktestEngine.run` 返回的结果。
            save_path: PNG 输出路径。

        Example:
            >>> PerformanceReport.plot_equity_curve({"error": "无数据"}, "ignored.png")
            回测失败，无法绘图: 无数据
        """
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
            except (OSError, subprocess.SubprocessError):
                # 系统没有 fontconfig 或执行失败时使用 Matplotlib 默认字体。
                zh_font_path = None

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
    "688249": "晶合集成",
    "688347": "华虹宏力",
    "300666": "江丰电子",
    "600206": "有研新材",
    "688409": "富创精密",
    "688361": "中科飞测",
    "300604": "长川科技",
    "688120": "华海清科",
    "688082": "盛美上海",
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
    """解析逗号分隔的股票代码或预设中文名。

    Args:
        symbols_str: 例如 ``300308,300502`` 或 ``中际旭创,新易盛``。空项会忽略。

    Returns:
        ``{代码: 名称}``。预设标的使用中文名；未知 6 位代码以代码作名称。

    Raises:
        ValueError: 任一非空项既不是 6 位代码，也不是预设名称。

    Example:
        >>> parse_symbols("300308,新易盛")
        {'300308': '中际旭创', '300502': '新易盛'}
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


def main() -> dict | None:
    """解析 CLI 并在 ``backtest`` 子命令下运行回测。

    Returns:
        运行 ``backtest`` 时返回回测结果字典；未提供子命令时返回 ``None``。

    Example:
        命令行用法::

            python AQuant_科技双池最终融合版.py backtest --symbol 300308 --no-plot
    """
    parser = argparse.ArgumentParser(description="AQuant — A股自动量化交易系统")
    sub = parser.add_subparsers(dest="command")

    bt = sub.add_parser("backtest", help="运行回测")
    bt.add_argument("--symbol", "-s", default=",".join(DEFAULT_SYMBOLS), help="标的代码(逗号分隔)")
    bt.add_argument("--start", default="2025-01-01", help="开始日期")
    bt.add_argument("--end", default="2026-06-30", help="结束日期")
    bt.add_argument("--capital", type=float, default=2_000_000, help="初始资金")
    bt.add_argument("--profile", default="default",
                    choices=["default", "semiconductor", "semiconductor_heavy", "aggressive"],
                    help="全局参数集: default / semiconductor(宽参数轻仓) / semiconductor_heavy(宽参数重仓) / aggressive(激进优化)")
    bt.add_argument("--config-route", default="auto", choices=["auto", "none"],
                    help="参数分发: auto(按行业分类自动套参数, 默认) / none(全部用全局profile参数)")
    bt.add_argument("--data-dir", default="", help="本地前复权CSV目录；留空则使用AKShare")
    bt.add_argument("--save-dir", default="", help="保存权益、交易、回撤和最新信号CSV")
    bt.add_argument("--no-plot", action="store_true", help="不生成权益曲线PNG")

    args = parser.parse_args()

    if args.command == "backtest":
        symbols_dict = parse_symbols(args.symbol)
        engine = BacktestEngine(initial_capital=args.capital)
        result = engine.run(
            symbols_dict, args.start, args.end,
            profile=args.profile, config_route=args.config_route,
            data_dir=args.data_dir or None,
        )

        profile_desc = args.profile if args.config_route == "none" else f"auto-route({args.profile}基线)"
        print(f"  [配置] 参数模式: {profile_desc}")
        PerformanceReport.print_report(result, symbols_dict)
        if args.save_dir:
            PerformanceReport.save_result(result, args.save_dir)
        if not args.no_plot:
            PerformanceReport.plot_equity_curve(result, f"equity_curve_{args.profile}_{args.config_route}.png")

        return result


if __name__ == "__main__":
    main()
