#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex Quant Fusion v14: an auditable A-share technology trend system.

The engine routes symbols to industry-specific parameter profiles and runs Turtle, dual-moving-average, and ATR-channel strategies with shared cash. Signals are generated after the close and executed at the next tradable open. A confirmed sector-breadth guard can liquidate correlated exposure and requires a multi-day recovery before new entries.

Inputs must be forward-adjusted daily OHLCV data. Limit checks are executable-price approximations and do not model queue priority, market impact, suspensions, or continuous limit-down events. Historical results are not a promise of future performance."""

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
REQUIRED_OHLC_COLUMNS = ("open", "close", "high", "low")
OPTIONAL_COLUMNS = ("volume",)
A_SHARE_LOT_SIZE = 100
_SYMBOL_RE = re.compile("^\\d{6}$")
EXECUTION_PRIORITY = {
    code: rank
    for rank, code in enumerate(
        (
            "300308",
            "300502",
            "300394",
            "688008",
            "603986",
            "002409",
            "688072",
            "688300",
            "300054",
            "688205",
            "920045",
            "300776",
            "688535",
            "688249",
            "688347",
            "300666",
            "600206",
            "688409",
            "688361",
            "300604",
            "688120",
            "688082",
        )
    )
}

# Execution order is stable by design. It makes shared-cash results independent of
# dictionary insertion order when several symbols have signals on the same open.


def _is_finite_number(value: Any) -> bool:
    """Handle is finite number for the quantitative backtest system."""
    try:
        return bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


def _require_finite(
    name: str,
    value: Any,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
    inclusive_max: bool = True,
) -> float:
    """Handle require finite for the quantitative backtest system."""
    if not _is_finite_number(value):
        raise ValueError(
            f"Configuration  {name} must be finite; current value is {value!r}"
        )
    value = float(value)
    if min_value is not None and value < min_value:
        raise ValueError(
            f"Configuration  {name} must be >= {min_value}; current value is {value}"
        )
    if max_value is not None:
        if inclusive_max and value > max_value:
            raise ValueError(
                f"Configuration  {name} must be <= {max_value}; current value is {value}"
            )
        if not inclusive_max and value >= max_value:
            raise ValueError(
                f"Configuration  {name} must be < {max_value}; current value is {value}"
            )
    return value


def _require_positive(
    name: str, value: Any, *, max_value: float | None = None, inclusive_max: bool = True
) -> float:
    """Handle require positive for the quantitative backtest system."""
    value = _require_finite(
        name, value, max_value=max_value, inclusive_max=inclusive_max
    )
    if value <= 0:
        raise ValueError(f"Configuration  {name} must be > 0; current value is {value}")
    return value


def _require_bool(name: str, value: Any) -> bool:
    """Handle require bool for the quantitative backtest system."""
    if not isinstance(value, bool):
        raise ValueError(
            f"Configuration  {name} must be bool; current value is {value!r}"
        )
    return value


def _require_int(name: str, value: Any, *, min_value: int = 0) -> int:
    """Handle require int for the quantitative backtest system."""
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise ValueError(
            f"Configuration  {name} must be an integer; current value is {value!r}"
        )
    value = int(value)
    if value < min_value:
        raise ValueError(
            f"Configuration  {name} must be >= {min_value}; current value is {value}"
        )
    return value


def _floor_to_lot(shares: float, lot_size: int = A_SHARE_LOT_SIZE) -> int:
    """Handle floor to lot for the quantitative backtest system."""
    if (
        isinstance(lot_size, bool)
        or not isinstance(lot_size, (int, np.integer))
        or lot_size <= 0
    ):
        raise ValueError(
            f"lot_size must be a positive integer; current value is {lot_size!r}"
        )
    if not _is_finite_number(shares) or float(shares) <= 0:
        return 0
    return int(float(shares) // lot_size) * lot_size


def _limit_pct_for_code(code: str, cfg: dict | None = None, name: str = "") -> float:
    """Handle limit pct for code for the quantitative backtest system."""
    code = str(code)
    if not _SYMBOL_RE.match(code):
        raise ValueError(
            f"Stock code must contain six digits; current value is {code!r}"
        )
    cfg = cfg or {}
    overrides = cfg.get("per_symbol_limit_pct", {}) or {}
    if code in overrides:
        return float(overrides[code])
    st_symbols = set(cfg.get("st_symbols", set()) or set())
    upper_name = str(name or "").upper()
    if code in st_symbols or "ST" in upper_name:
        return 0.05
    if code.startswith(("3", "68", "69")):
        return 0.2
    if code.startswith(("8", "4", "9")):
        return 0.3
    return 0.1


def _parse_dates(values: pd.Series | pd.Index) -> pd.Series:
    """Handle parse dates for the quantitative backtest system."""
    ser = pd.Series(values)
    as_str = ser.astype("string").str.strip()
    parsed = pd.Series(pd.NaT, index=ser.index, dtype="datetime64[ns]")
    yyyymmdd = as_str.str.fullmatch("\\d{8}", na=False)
    if yyyymmdd.any():
        parsed.loc[yyyymmdd] = pd.to_datetime(
            as_str.loc[yyyymmdd], format="%Y%m%d", errors="coerce"
        )
    rest = ~yyyymmdd
    if rest.any():
        try:
            parsed.loc[rest] = pd.to_datetime(
                as_str.loc[rest], errors="coerce", format="mixed"
            )
        except TypeError:
            parsed.loc[rest] = pd.to_datetime(as_str.loc[rest], errors="coerce")
    return parsed


class DataFetcher:
    """Load and validate forward-adjusted A-share daily market data."""

    _COLUMN_ALIASES = {
        # AKShare providers return localized headers. Unicode escapes keep the
        # source English-only while preserving compatibility with those frames.
        "\u65e5\u671f": "date",
        "date": "date",
        "datetime": "date",
        "time": "date",
        "trade_date": "date",
        "\u5f00\u76d8": "open",
        "\u5f00\u76d8\u4ef7": "open",
        "open": "open",
        "\u6536\u76d8": "close",
        "\u6536\u76d8\u4ef7": "close",
        "close": "close",
        "\u6700\u9ad8": "high",
        "\u6700\u9ad8\u4ef7": "high",
        "high": "high",
        "\u6700\u4f4e": "low",
        "\u6700\u4f4e\u4ef7": "low",
        "low": "low",
        "\u6210\u4ea4\u91cf": "volume",
        "volume": "volume",
        "vol": "volume",
    }

    @staticmethod
    def _exchange_symbol(symbol: str) -> str:
        """Add the exchange prefix required by Sina and Tencent endpoints."""
        if symbol.startswith(("0", "3")):
            return f"sz{symbol}"
        if symbol.startswith(("8", "4", "9")):
            return f"bj{symbol}"
        return f"sh{symbol}"

    @staticmethod
    def _fetch_eastmoney(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        """Fetch one forward-adjusted frame from Eastmoney through AKShare."""
        return ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
            adjust="qfq",
        )

    @staticmethod
    def _fetch_sina(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        """Fetch one forward-adjusted frame from Sina through AKShare."""
        return ak.stock_zh_a_daily(
            symbol=DataFetcher._exchange_symbol(symbol),
            start_date=start_date,
            end_date=end_date,
            adjust="qfq",
        )

    @staticmethod
    def _fetch_tencent(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        """Fetch one forward-adjusted frame from the fixed Tencent endpoint."""
        import urllib.request

        exchange_symbol = DataFetcher._exchange_symbol(symbol)
        compact_start = start_date.replace("-", "")
        compact_end = end_date.replace("-", "")
        url = (
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
            f"param={exchange_symbol},day,{compact_start},{compact_end},1000,qfq"
        )
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36"
            },
        )
        # The scheme and host are constants; only validated codes and dates are
        # interpolated, so this call cannot target an arbitrary host.
        with urllib.request.urlopen(request, timeout=15) as response:  # nosec B310
            payload = json.loads(response.read().decode())
        klines = payload.get("data", {}).get(exchange_symbol, {}).get("qfqday", [])
        rows = [
            {
                "date": item[0],
                "open": float(item[1]),
                "close": float(item[2]),
                "high": float(item[3]),
                "low": float(item[4]),
                "volume": float(item[5]),
            }
            for item in klines
        ]
        return pd.DataFrame(rows)

    @staticmethod
    def fetch_stock_data(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        """Fetch data with deterministic provider failover and strict validation."""
        symbol = str(symbol)
        if not _SYMBOL_RE.match(symbol):
            raise ValueError(
                f"symbol must be a six-digit code; current value is {symbol!r}"
            )
        start_ts, end_ts = (pd.Timestamp(start_date), pd.Timestamp(end_date))
        if start_ts > end_ts:
            raise ValueError("start_date must not be later than end_date")
        if ak is None:
            raise ImportError("AKShare is not installed")
        providers = (
            ("Eastmoney", DataFetcher._fetch_eastmoney),
            ("Sina", DataFetcher._fetch_sina),
            ("Tencent", DataFetcher._fetch_tencent),
        )
        errors: list[str] = []
        for attempt in range(3):
            # Provider failures are isolated at this external I/O boundary. Each
            # successful response still passes through the same strict validator.
            for provider_name, provider in providers:
                try:
                    frame = provider(symbol, start_date, end_date)
                    if frame is not None and not frame.empty:
                        frame = DataFetcher._normalize_columns(frame)
                        print(
                            f"  [Data] {symbol}: {provider_name} source, "
                            f"{len(frame)} rows"
                        )
                        return frame
                except Exception as error:
                    errors.append(f"{provider_name}(attempt {attempt + 1}): {error}")
            if attempt < 2:
                time.sleep(1)
        raise RuntimeError(
            f"Loading data for {symbol} failed after three attempts: {'; '.join(errors)}"
        )

    @staticmethod
    def load_stock_data(
        symbol: str, start_date: str, end_date: str, data_dir: str | None = None
    ) -> pd.DataFrame:
        """Handle load stock data for the quantitative backtest system."""
        symbol = str(symbol)
        if not _SYMBOL_RE.match(symbol):
            raise ValueError(
                f"symbol must be a six-digit code; current value is {symbol!r}"
            )
        if pd.Timestamp(start_date) > pd.Timestamp(end_date):
            raise ValueError("start_date must not be later than end_date")
        if data_dir:
            path = Path(data_dir).expanduser() / f"{symbol}.csv"
            if not path.is_file():
                raise FileNotFoundError(f"Missing local market-data file: {path}")
            return DataFetcher._normalize_columns(pd.read_csv(path))
        return DataFetcher.fetch_stock_data(symbol, start_date, end_date)

    @staticmethod
    def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
        """Normalize and validate an OHLCV frame without mutating the caller's object."""
        if df is None or df.empty:
            raise ValueError("Market data is empty")
        normalized_names = DataFetcher._normalized_column_names(df.columns)
        if len(normalized_names) != len(set(normalized_names)):
            duplicates = sorted(
                {c for c in normalized_names if normalized_names.count(c) > 1}
            )
            raise ValueError(
                f"Market data contains duplicate or conflicting columns: {duplicates}"
            )
        out = DataFetcher._with_datetime_index(df, normalized_names)
        DataFetcher._validate_ohlcv(out)
        return out[["open", "close", "high", "low", "volume"]].copy()

    @staticmethod
    def _normalized_column_names(columns: pd.Index) -> list[str]:
        """Map provider-specific column labels to the engine's canonical schema."""
        normalized: list[str] = []
        for column in columns:
            original = str(column).strip()
            lowered = original.lower()
            normalized.append(
                DataFetcher._COLUMN_ALIASES.get(
                    lowered, DataFetcher._COLUMN_ALIASES.get(original, lowered)
                )
            )
        return normalized

    @staticmethod
    def _with_datetime_index(
        df: pd.DataFrame, normalized_names: list[str]
    ) -> pd.DataFrame:
        """Return a sorted copy indexed by unique, parseable trading dates."""
        out = df.copy()
        out.columns = normalized_names
        if "date" in out.columns:
            out["date"] = _parse_dates(out["date"]).to_numpy()
            out = out.set_index("date")
        else:
            if not isinstance(out.index, pd.DatetimeIndex):
                raise ValueError("Market data has no date column and no DatetimeIndex")
            out.index = pd.to_datetime(out.index, errors="coerce")
        out = out.sort_index()
        out.index.name = "date"
        if out.index.isna().any():
            raise ValueError("Market data contains an unparseable date")
        if out.index.duplicated().any():
            dups = out.index[out.index.duplicated()].strftime("%Y-%m-%d").tolist()
            raise ValueError(f"Market data contains duplicate dates: {dups[:5]}")
        return out

    @staticmethod
    def _validate_ohlcv(out: pd.DataFrame) -> None:
        """Validate numeric values and the internal consistency of OHLCV bars."""
        missing = [c for c in REQUIRED_OHLC_COLUMNS if c not in out.columns]
        if missing:
            raise ValueError(f"Market data is missing required columns: {missing}")
        for col in (*REQUIRED_OHLC_COLUMNS, *OPTIONAL_COLUMNS):
            if col in out.columns:
                out[col] = pd.to_numeric(out[col], errors="coerce")
        if "volume" not in out.columns:
            out["volume"] = 0.0
        required = list(REQUIRED_OHLC_COLUMNS)
        if out[required].isna().any().any():
            bad = out[out[required].isna().any(axis=1)].head(3)
            raise ValueError(
                f"Market data contains an unparseable price; sample:\n{bad}"
            )
        if (out[required] <= 0).any().any():
            raise ValueError("Market data contains a non-positive price")
        if out["volume"].isna().any():
            out["volume"] = out["volume"].fillna(0.0)
        if (out["volume"] < 0).any():
            raise ValueError("Market data contains negative volume")
        if (out["high"] < out[["open", "close"]].max(axis=1)).any():
            raise ValueError(
                "Market data contains invalid OHLC: high < max(open, close)"
            )
        if (out["low"] > out[["open", "close"]].min(axis=1)).any():
            raise ValueError(
                "Market data contains invalid OHLC: low > min(open, close)"
            )


class Indicators:
    """Compute lag-safe technical indicators used by all strategies."""

    @staticmethod
    def _wilder_average(series: pd.Series, period: int) -> pd.Series:
        """Handle wilder average for the quantitative backtest system."""
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
        seed_window = values.iloc[seed_pos - period + 1 : seed_pos + 1]
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
        """Handle atr for the quantitative backtest system."""
        period = _require_int("period", period, min_value=1)
        method = str(method).lower()
        if method not in {"wilder", "sma"}:
            raise ValueError(
                f"method must be 'wilder' or 'sma'; current value is {method!r}"
            )
        high, low, close = (df["high"], df["low"], df["close"])
        prev_close = close.shift(1)
        tr = pd.concat(
            [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        if method == "sma":
            return tr.rolling(period, min_periods=period).mean()
        return Indicators._wilder_average(tr, period)

    @staticmethod
    def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """Handle adx for the quantitative backtest system."""
        period = _require_int("period", period, min_value=1)
        high, low = (df["high"], df["low"])
        up_move = high.diff()
        down_move = -low.diff()
        plus_dm = pd.Series(
            np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
            index=df.index,
        )
        minus_dm = pd.Series(
            np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
            index=df.index,
        )
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
        """Handle rsi for the quantitative backtest system."""
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
        """Handle donchian for the quantitative backtest system."""
        entry_period = _require_int("entry_period", entry_period, min_value=1)
        exit_period = _require_int("exit_period", exit_period, min_value=1)
        # The one-bar shift is essential: today's high and low must not influence
        # a breakout decision made at today's close.
        upper = df["high"].rolling(entry_period).max().shift(1)
        lower = df["low"].rolling(exit_period).min().shift(1)
        return (upper, lower)

    @staticmethod
    def ma(series: pd.Series, period: int) -> pd.Series:
        """Handle ma for the quantitative backtest system."""
        period = _require_int("period", period, min_value=1)
        return series.rolling(period).mean()

    @staticmethod
    def compute_all(df: pd.DataFrame, cfg: dict) -> dict[str, pd.Series]:
        """Handle compute all for the quantitative backtest system."""
        atr_period = cfg.get("atr_period", 20)
        adx_period = cfg.get("adx_period", 14)
        rsi_period = cfg.get("rsi_period", 14)
        entry_p = cfg.get("entry_period", 20)
        exit_p = cfg.get("exit_period", 10)
        ma_short = cfg.get("ma_short", 20)
        ma_long = cfg.get("ma_long", 60)
        donchian_upper, donchian_lower = Indicators.donchian(df, entry_p, exit_p)
        return {
            "atr": Indicators.atr(
                df, atr_period, method=str(cfg.get("atr_method", "wilder"))
            ),
            "adx": Indicators.adx(df, adx_period),
            "rsi": Indicators.rsi(df["close"], rsi_period),
            "donchian_upper": donchian_upper,
            "donchian_lower": donchian_lower,
            "ma_short": Indicators.ma(df["close"], ma_short),
            "ma_long": Indicators.ma(df["close"], ma_long),
        }


@dataclass
class Position:
    """Track one symbol and one strategy sub-position."""

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
        """Handle cost for the quantitative backtest system."""
        return self.shares * self.entry_price

    def market_value_at(self, price: float) -> float:
        """Handle market value at for the quantitative backtest system."""
        return self.shares * price


@dataclass
class TradeRecord:
    """Store one executed trade and its audited cash effects."""

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
    """Represent a close-generated instruction pending T+1 execution."""

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
    fusion_label: str = "single_strategy"


@dataclass
class BarContext:
    """Provide immutable per-bar inputs to a strategy."""

    i: int
    df: pd.DataFrame
    current_assets: float
    indicators: dict
    symbol: str
    date: str


@dataclass(frozen=True)
class SectorObservation:
    """Aggregate equal-weight return and breadth from fully observed symbols."""

    symbol_count: int
    equal_return: float
    shock_breadth: float
    recovery_breadth: float
    normalized_series: tuple[pd.Series, ...]


class BaseStrategy:
    """Define shared sizing, signal construction, and reversal protection."""

    name: str = "base"

    def __init__(self, cfg: dict) -> None:
        """Handle init for the quantitative backtest system."""
        self.cfg = cfg
        self.position: Position | None = None

    def on_bar(self, ctx: BarContext) -> Signal | None:
        """Handle on bar for the quantitative backtest system."""
        raise NotImplementedError

    def _calc_shares(
        self, capital: float, price: float, atr_val: float, unit_number: int = 1
    ) -> int:
        """Handle calc shares for the quantitative backtest system."""
        risk_pct = float(self.cfg.get("risk_pct", 0.01))
        atr_mult = float(self.cfg.get("atr_multiplier", 1.0))
        decay = float(self.cfg.get("pyramid_risk_decay", 1.0)) ** max(
            unit_number - 1, 0
        )
        if (
            not _is_finite_number(capital)
            or not _is_finite_number(price)
            or (not _is_finite_number(atr_val))
            or (not _is_finite_number(risk_pct))
            or (not _is_finite_number(atr_mult))
            or (not _is_finite_number(decay))
            or (capital <= 0)
            or (price <= 0)
            or (atr_val <= 0)
            or (risk_pct <= 0)
            or (atr_mult <= 0)
            or (decay <= 0)
        ):
            return 0
        # Risk-budget sizing: shares = cash risk budget / stop distance.
        # Later execution checks cap this theoretical size by cash and exposure.
        n = capital * risk_pct * decay / (atr_val * atr_mult)
        return _floor_to_lot(n)

    def _make_buy_signal(
        self,
        ctx: BarContext,
        shares: int,
        stop_loss: float,
        reason: str,
        atr_val: float = 0.0,
    ) -> Signal:
        """Handle make buy signal for the quantitative backtest system."""
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
        """Handle make sell signal for the quantitative backtest system."""
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
        """Return an early exit after a confirmed reversal or failed trend."""
        pos = self.position
        if pos is None:
            return None
        i, df, cfg = (ctx.i, ctx.df, self.cfg)
        close = float(df["close"].iloc[i])
        if not _is_finite_number(close) or close <= 0:
            return None
        pos.highest_close_since_entry = max(pos.highest_close_since_entry, close)
        strategy_switch = {
            "turtle_breakout": "reversal_turtle_enabled",
            "dual_ma": "reversal_dual_ma_enabled",
            "atr_channel": "reversal_atr_channel_enabled",
        }.get(self.name)
        if strategy_switch and (not bool(cfg.get(strategy_switch, True))):
            return None
        peak_close = pos.highest_close_since_entry
        peak_gain = peak_close / pos.entry_price - 1 if pos.entry_price > 0 else 0.0
        activation = float(cfg.get("profit_lock_activation", 0.3))
        giveback = float(cfg.get("profit_lock_giveback", 0.18))
        # Profit protection activates only after a meaningful gain. Before that,
        # a loss exit also requires a weakening short moving average.
        if peak_gain >= activation:
            lock_stop = peak_close * (1 - giveback)
            pos.stop_loss = max(pos.stop_loss, lock_stop)
            if close <= lock_stop:
                return self._make_sell_signal(
                    ctx,
                    f"reversal profit protection ({giveback:.0%} giveback) at {lock_stop:.2f}",
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
                    and (close <= peak_close * (1 - break_giveback))
                    and (close <= float(prior_low))
                    and (close < float(ma_value))
                ):
                    return self._make_sell_signal(
                        ctx,
                        f"short-term reversal breakdown({break_giveback:.0%}giveback+{exit_period}day low+short MA)",
                    )
        loss_cut = float(cfg.get("reversal_loss_cut", 0.1))
        ma_short = ctx.indicators.get("ma_short")
        if i >= 1 and ma_short is not None:
            ma_now, ma_prev = (ma_short.iloc[i], ma_short.iloc[i - 1])
            if (
                _is_finite_number(ma_now)
                and _is_finite_number(ma_prev)
                and (close <= pos.entry_price * (1 - loss_cut))
                and (close < float(ma_now) < float(ma_prev))
            ):
                return self._make_sell_signal(
                    ctx,
                    f"failed-trend reversal exit ({loss_cut:.0%} loss and weakening short MA)",
                )
        return None


class TurtleBreakoutStrategy(BaseStrategy):
    """Trade prior-window Donchian breakouts with ATR pyramiding and layered exits."""

    name = "turtle_breakout"

    def on_bar(self, ctx: BarContext) -> Signal | None:
        """Handle on bar for the quantitative backtest system."""
        i, df, ind = (ctx.i, ctx.df, ctx.indicators)
        cfg = self.cfg
        entry_period = cfg.get("entry_period", 20)
        exit_period = cfg.get("exit_period", 10)
        adx_threshold = cfg.get("adx_threshold", 15)
        max_units = cfg.get("max_units", 8)
        atr_stop_mult = cfg.get("atr_multiplier", 2)
        trail_mult = cfg.get("trail_atr_mult", 2.5)
        if i < max(entry_period, exit_period, cfg.get("adx_period", 14) + 5):
            return None
        close = df["close"].iloc[i]
        high = df["high"].iloc[i]
        atr_val = ind["atr"].iloc[i]
        adx_val = ind["adx"].iloc[i]
        upper = ind["donchian_upper"].iloc[i]
        lower = ind["donchian_lower"].iloc[i]
        if (
            pd.isna(atr_val)
            or pd.isna(adx_val)
            or pd.isna(upper)
            or pd.isna(lower)
            or (atr_val <= 0)
        ):
            return None
        if self.position is not None:
            pos = self.position
            pos.highest_since_entry = max(pos.highest_since_entry, high)
            reversal_signal = self._fast_reversal_exit(ctx)
            if reversal_signal is not None:
                return reversal_signal
            # Stops are monotonic: a later bar may tighten but never loosen them.
            trail_stop = pos.highest_since_entry - trail_mult * atr_val
            initial_stop = pos.entry_price - atr_stop_mult * atr_val
            pos.stop_loss = max(pos.stop_loss, trail_stop, initial_stop)
            if close <= pos.stop_loss:
                return self._make_sell_signal(
                    ctx, f"ATR trailing stop@{pos.stop_loss:.2f}"
                )
            if close <= pos.entry_price * (1 - cfg.get("hard_stop", 0.15)):
                return self._make_sell_signal(
                    ctx, f"hard stop{cfg.get('hard_stop', 0.15):.0%}"
                )
            if close <= lower:
                return self._make_sell_signal(ctx, f"Donchian exit@{lower:.2f}")
            if pos.units < max_units:
                add_gap = atr_val * float(cfg.get("pyramid_add_atr", 0.5))
                base_add_price = (
                    pos.last_add_price if pos.last_add_price > 0 else pos.entry_price
                )
                if close >= base_add_price + add_gap:
                    capital = ctx.current_assets * cfg.get("strategy_weight", 0.95)
                    shares = self._calc_shares(
                        capital, close, atr_val, unit_number=pos.units + 1
                    )
                    if shares > 0:
                        new_stop = high - trail_mult * atr_val
                        return Signal(
                            symbol=ctx.symbol,
                            strategy_name=self.name,
                            direction="buy",
                            target_shares=shares,
                            price=close,
                            stop_loss=max(pos.stop_loss, new_stop),
                            reason=f"Turtle pyramid add (unit {pos.units + 1})",
                            signal_date=ctx.date,
                            atr=float(atr_val),
                        )
            return None
        if adx_val > adx_threshold and close > upper:
            capital = ctx.current_assets * cfg.get("strategy_weight", 0.95)
            shares = self._calc_shares(capital, close, atr_val)
            if shares > 0:
                stop_loss = close - atr_stop_mult * atr_val
                return self._make_buy_signal(
                    ctx,
                    shares,
                    stop_loss,
                    f"Turtle breakout(ADX={adx_val:.1f})",
                    atr_val,
                )
        return None


class DualMAStrategy(BaseStrategy):
    """Trade RSI-confirmed moving-average crosses with ATR risk controls."""

    name = "dual_ma"

    def on_bar(self, ctx: BarContext) -> Signal | None:
        """Handle on bar for the quantitative backtest system."""
        i, df, ind = (ctx.i, ctx.df, ctx.indicators)
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
        if self.position is not None:
            pos = self.position
            pos.highest_since_entry = max(pos.highest_since_entry, high)
            reversal_signal = self._fast_reversal_exit(ctx)
            if reversal_signal is not None:
                return reversal_signal
            if ma_s < ma_l:
                return self._make_sell_signal(
                    ctx,
                    f"MA{cfg.get('ma_short', 20)}crossed below MA{cfg.get('ma_long', 60)}",
                )
            if not pd.isna(atr_val) and atr_val > 0:
                trail_stop = pos.highest_since_entry - trail_mult * atr_val
                initial_stop = pos.entry_price - cfg.get("atr_multiplier", 2) * atr_val
                pos.stop_loss = max(pos.stop_loss, trail_stop, initial_stop)
                if close <= pos.stop_loss:
                    return self._make_sell_signal(
                        ctx, f"ATR trailing stop@{pos.stop_loss:.2f}"
                    )
            if close <= pos.entry_price * (1 - cfg.get("hard_stop", 0.15)):
                return self._make_sell_signal(
                    ctx, f"hard stop{cfg.get('hard_stop', 0.15):.0%}"
                )
            return None
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
                return self._make_buy_signal(
                    ctx,
                    shares,
                    stop_loss,
                    f"MA golden cross(RSI={rsi_val:.0f})",
                    atr_fallback,
                )
        return None


class ATRChannelStrategy(BaseStrategy):
    """Trade ADX-confirmed ATR channel breakouts with channel and trailing exits."""

    name = "atr_channel"

    def on_bar(self, ctx: BarContext) -> Signal | None:
        """Handle on bar for the quantitative backtest system."""
        i, df, ind = (ctx.i, ctx.df, ctx.indicators)
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
        if pd.isna(atr_val) or pd.isna(adx_val) or pd.isna(ma) or (atr_val <= 0):
            return None
        upper_channel = ma + cfg.get("channel_mult", 2.5) * atr_val
        lower_channel = ma - cfg.get("channel_lower_mult", 2.0) * atr_val
        if self.position is not None:
            pos = self.position
            pos.highest_since_entry = max(pos.highest_since_entry, high)
            reversal_signal = self._fast_reversal_exit(ctx)
            if reversal_signal is not None:
                return reversal_signal
            trail_stop = pos.highest_since_entry - trail_mult * atr_val
            initial_stop = pos.entry_price - cfg.get("atr_multiplier", 2) * atr_val
            pos.stop_loss = max(pos.stop_loss, trail_stop, initial_stop)
            if close <= pos.stop_loss:
                return self._make_sell_signal(
                    ctx, f"ATR trailing stop@{pos.stop_loss:.2f}"
                )
            if close <= lower_channel:
                return self._make_sell_signal(
                    ctx, f"ATR lower-channel exit@{lower_channel:.2f}"
                )
            if close <= pos.entry_price * (1 - cfg.get("hard_stop", 0.15)):
                return self._make_sell_signal(
                    ctx, f"hard stop{cfg.get('hard_stop', 0.15):.0%}"
                )
            return None
        if adx_val > cfg.get("adx_threshold", 15) and close > upper_channel:
            capital = ctx.current_assets * cfg.get("strategy_weight", 0.95)
            shares = self._calc_shares(capital, close, atr_val)
            if shares > 0:
                stop_loss = close - cfg.get("atr_multiplier", 2) * atr_val
                return self._make_buy_signal(
                    ctx,
                    shares,
                    stop_loss,
                    f"ATR channel breakout(ADX={adx_val:.1f})",
                    atr_val,
                )
        return None


class RiskManager:
    """Enforce portfolio drawdown, daily-loss, sector, and position limits."""

    def __init__(self, cfg: dict) -> None:
        """Handle init for the quantitative backtest system."""
        self.cfg = cfg
        self.peak_assets: float = 0.0
        self.cooldown_until: str | None = None
        self.daily_start_assets: float = 0.0
        self.symbol_groups: dict[str, str] = {}
        self.group_weight_limits: dict[str, float] = {}

    def configure_groups(self, symbol_groups: dict[str, str]) -> None:
        """Handle configure groups for the quantitative backtest system."""
        self.symbol_groups = dict(symbol_groups)
        active = set(self.symbol_groups.values())
        if len(active) > 1:
            self.group_weight_limits = dict(
                self.cfg.get("combined_group_weight_limits", {})
            )
        else:
            self.group_weight_limits = {group: 1.0 for group in active}

    def check_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        trading_dates: list[pd.Timestamp] | None = None,
        date_to_pos: dict[pd.Timestamp, int] | None = None,
    ) -> str | None:
        """Handle check portfolio risk for the quantitative backtest system."""
        max_drawdown_pct = self.cfg.get("max_drawdown", 0.2)
        cooldown_days = self.cfg.get("cooldown_days", 3)
        self.peak_assets = max(self.peak_assets, current_assets)
        if self.cooldown_until:
            cooldown_end = pd.Timestamp(self.cooldown_until)
            current_date = pd.Timestamp(date_str)
            if current_date < cooldown_end:
                return "portfolio cooldown"
            self.cooldown_until = None
        if self.peak_assets > 0:
            drawdown = (self.peak_assets - current_assets) / self.peak_assets
            if drawdown >= max_drawdown_pct:
                if trading_dates is not None:
                    current_date = pd.Timestamp(date_str)
                    idx = (
                        date_to_pos.get(current_date)
                        if date_to_pos is not None
                        else None
                    )
                    if idx is None:
                        try:
                            idx = trading_dates.index(current_date)
                        except ValueError:
                            idx = None
                    if idx is not None:
                        end_idx = min(idx + cooldown_days + 1, len(trading_dates) - 1)
                        self.cooldown_until = trading_dates[end_idx].strftime(
                            "%Y-%m-%d"
                        )
                    else:
                        self.cooldown_until = (
                            pd.Timestamp(date_str) + timedelta(days=cooldown_days)
                        ).strftime("%Y-%m-%d")
                else:
                    self.cooldown_until = (
                        pd.Timestamp(date_str) + timedelta(days=cooldown_days)
                    ).strftime("%Y-%m-%d")
                self.peak_assets = current_assets
                return "portfolio drawdown circuit breaker"
        return None

    def check_daily_loss(self, current_assets: float) -> bool:
        """Handle check daily loss for the quantitative backtest system."""
        if self.daily_start_assets > 0:
            daily_loss = (
                self.daily_start_assets - current_assets
            ) / self.daily_start_assets
            return daily_loss >= self.cfg.get("daily_loss_limit", 0.06)
        return False

    def check_position_limits(
        self,
        symbol: str,
        positions: dict,
        current_assets: float,
        buy_value: float,
        current_prices: dict | None = None,
        position_cfg: dict | None = None,
    ) -> bool:
        """Handle check position limits for the quantitative backtest system."""
        if current_assets <= 0:
            return False
        if current_prices is not None:
            for sym in positions:
                price = current_prices.get(sym)
                if price is None or not _is_finite_number(price) or price <= 0:
                    return False

        def _mark(sym: str, pos: Position) -> float:
            """Handle mark for the quantitative backtest system."""
            if current_prices is None:
                return pos.entry_price
            return float(current_prices[sym])

        symbol_value = sum(
            (p.shares * _mark(symbol, p) for p in positions.get(symbol, {}).values())
        )
        symbol_cap = (position_cfg or self.cfg).get(
            "max_symbol_weight", self.cfg.get("max_symbol_weight", 0.5)
        )
        if (symbol_value + buy_value) / current_assets > symbol_cap:
            return False
        target_group = self.symbol_groups.get(symbol)
        group_cap = self.group_weight_limits.get(target_group, 1.0)
        if target_group:
            group_value = sum(
                (
                    p.shares * _mark(sym, p)
                    for sym, sym_positions in positions.items()
                    if self.symbol_groups.get(sym) == target_group
                    for p in sym_positions.values()
                )
            )
            if (group_value + buy_value) / current_assets > group_cap:
                return False
        total_position_value = sum(
            (
                p.shares * _mark(sym, p)
                for sym, sym_positions in positions.items()
                for p in sym_positions.values()
            )
        )
        # All three strategy sub-positions for a symbol share one symbol cap.
        if (total_position_value + buy_value) / current_assets > self.cfg.get(
            "max_total_weight", 0.95
        ):
            return False
        return True


class BacktestEngine:
    """Run a shared-cash, multi-symbol, multi-strategy T+1 backtest."""

    def __init__(
        self, initial_capital: float = 2000000, cfg: dict | None = None
    ) -> None:
        """Handle init for the quantitative backtest system."""
        self.initial_capital = _require_finite(
            "initial_capital", initial_capital, min_value=0.01
        )
        self._user_cfg = dict(cfg or {})
        self.cfg = self._validate_config({**self._default_config(), **self._user_cfg})
        self.cash = self.initial_capital
        self.positions: dict[str, dict[str, Position]] = {}
        self.trades: list[TradeRecord] = []
        self.equity_curve: list[dict] = []
        self.risk = RiskManager(self.cfg)
        self.strategy_instances: dict[str, list[BaseStrategy]] = {}
        self.symbol_names: dict[str, str] = {}
        self.symbol_last_dates: dict[str, pd.Timestamp] = {}
        self.global_last_date: pd.Timestamp | None = None
        self.symbol_configs: dict[str, dict] = {}
        self.pending_signals: list[tuple[Signal, BaseStrategy]] = []
        self.fusion_events: list[dict] = []
        self.risk_events: list[dict] = []
        self.sector_guard_active = False
        self._sector_shock_positions: list[int] = []
        self._sector_recovery_streak = 0
        self.strategy_templates: list[type[BaseStrategy]] = [
            TurtleBreakoutStrategy,
            DualMAStrategy,
            ATRChannelStrategy,
        ]

    @staticmethod
    def _default_config() -> dict:
        """Handle default config for the quantitative backtest system."""
        # Values are explicit to keep every historical run auditable. Industry
        # profiles below copy this dictionary and override only declared fields.
        return {
            "entry_period": 8,
            "exit_period": 3,
            "adx_threshold": 12,
            "adx_period": 10,
            "atr_period": 10,
            "rsi_period": 20,
            "ma_short": 15,
            "ma_long": 60,
            "atr_multiplier": 1.0,
            "trail_atr_mult": 4.0,
            "channel_mult": 2.0,
            "channel_lower_mult": 3.0,
            "risk_pct": 0.03,
            "hard_stop": 0.15,
            "strategy_weight": 0.98,
            "max_symbol_weight": 0.6,
            "max_total_weight": 1.0,
            "max_units": 20,
            "max_drawdown": 0.165,
            "cooldown_days": 10,
            "daily_loss_limit": 0.06,
            "sector_guard_enabled": True,
            "sector_guard_min_symbols": 5,
            "sector_shock_return": -0.05,
            "sector_shock_breadth": 0.2,
            "sector_shock_ma": 5,
            "sector_shock_window": 4,
            "sector_shock_confirmations": 2,
            "sector_recovery_ma": 5,
            "sector_recovery_breadth": 0.8,
            "sector_recovery_confirmations": 2,
            "momentum_lookback": 5,
            "max_positions": 6,
            "group_min_slots": 2,
            "fusion_single_scale": 0.9,
            "fusion_double_scale": 1.0,
            "fusion_triple_scale": 1.1,
            "profit_lock_activation": 0.2,
            "profit_lock_giveback": 0.22,
            "reversal_break_giveback": 0.22,
            "reversal_exit_period": 6,
            "reversal_loss_cut": 0.1,
            "reversal_turtle_enabled": True,
            "reversal_dual_ma_enabled": True,
            "reversal_atr_channel_enabled": True,
            "combined_group_weight_limits": {
                "overseas_compute": 1.0,
                "domestic_semiconductor": 0.8,
            },
            "liquidate_on_circuit_breaker": True,
            "commission_rate": 0.00025,
            "stamp_duty": 0.0005,
            "slippage": 0.001,
            "min_commission": 5.0,
            "max_pending_buy_days": 5,
            "pyramid_add_atr": 1.0,
            "pyramid_risk_decay": 1.0,
            "atr_method": "wilder",
            "limit_price_epsilon": 0.001,
            "per_symbol_limit_pct": {},
            "st_symbols": set(),
            "close_position_on_data_end": True,
            "force_close_on_end": False,
            "risk_free_rate": 0.0,
        }

    _PER_SYMBOL_OVERRIDE_KEYS = {
        "entry_period",
        "exit_period",
        "adx_threshold",
        "adx_period",
        "atr_period",
        "rsi_period",
        "ma_short",
        "ma_long",
        "atr_multiplier",
        "trail_atr_mult",
        "channel_mult",
        "channel_lower_mult",
        "risk_pct",
        "hard_stop",
        "strategy_weight",
        "max_symbol_weight",
        "max_units",
        "pyramid_add_atr",
        "pyramid_risk_decay",
        "atr_method",
        "profit_lock_activation",
        "profit_lock_giveback",
        "reversal_break_giveback",
        "reversal_exit_period",
        "reversal_loss_cut",
        "reversal_turtle_enabled",
        "reversal_dual_ma_enabled",
        "reversal_atr_channel_enabled",
    }

    @staticmethod
    def optimized_aggressive_config() -> dict:
        """Handle optimized aggressive config for the quantitative backtest system."""
        cfg = BacktestEngine._default_config()
        cfg.update(
            {
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
                "strategy_weight": 0.9,
                "max_symbol_weight": 0.98,
                "max_total_weight": 0.98,
                "max_units": 10,
                "max_drawdown": 0.15,
                "cooldown_days": 5,
                "momentum_lookback": 10,
                "max_positions": 2,
            }
        )
        return cfg

    @staticmethod
    def semiconductor_config() -> dict:
        """Handle semiconductor config for the quantitative backtest system."""
        cfg = BacktestEngine._default_config()
        cfg.update(
            {
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
                "profit_lock_activation": 0.2,
                "profit_lock_giveback": 0.24,
                "reversal_break_giveback": 0.24,
                "reversal_exit_period": 8,
                "reversal_loss_cut": 0.08,
                "reversal_turtle_enabled": False,
                "reversal_dual_ma_enabled": True,
                "reversal_atr_channel_enabled": False,
            }
        )
        return cfg

    @staticmethod
    def semiconductor_heavy_config() -> dict:
        """Handle semiconductor heavy config for the quantitative backtest system."""
        cfg = BacktestEngine.semiconductor_config()
        cfg.update(
            {
                "risk_pct": 0.03,
                "strategy_weight": 0.9,
                "max_units": 6,
                "pyramid_add_atr": 1.5,
            }
        )
        return cfg

    @staticmethod
    def overseas_memory_material_config() -> dict:
        """Handle overseas memory material config for the quantitative backtest system."""
        cfg = BacktestEngine._default_config()
        cfg.update(
            {
                "entry_period": 9,
                "exit_period": 4,
                "atr_period": 12,
                "trail_atr_mult": 4.2,
                "hard_stop": 0.18,
                "risk_pct": 0.03,
                "max_units": 16,
                "pyramid_add_atr": 1.1,
                "ma_long": 65,
                "channel_mult": 2.1,
                "strategy_weight": 0.96,
                "max_symbol_weight": 0.58,
            }
        )
        return cfg

    @staticmethod
    def domestic_design_config() -> dict:
        """Handle domestic design config for the quantitative backtest system."""
        cfg = BacktestEngine.semiconductor_config()
        cfg.update(
            {
                "entry_period": 25,
                "exit_period": 20,
                "ma_long": 80,
                "trail_atr_mult": 7.0,
                "risk_pct": 0.018,
                "max_units": 3,
                "pyramid_add_atr": 2.0,
                "strategy_weight": 0.82,
                "max_symbol_weight": 0.5,
            }
        )
        return cfg

    @staticmethod
    def domestic_material_config() -> dict:
        """Handle domestic material config for the quantitative backtest system."""
        cfg = BacktestEngine.semiconductor_config()
        cfg.update(
            {
                "entry_period": 28,
                "exit_period": 22,
                "ma_long": 90,
                "trail_atr_mult": 7.0,
                "risk_pct": 0.018,
                "max_units": 3,
                "pyramid_add_atr": 2.0,
                "channel_mult": 3.0,
                "strategy_weight": 0.82,
                "max_symbol_weight": 0.45,
            }
        )
        return cfg

    @staticmethod
    def domestic_foundry_config() -> dict:
        """Handle domestic foundry config for the quantitative backtest system."""
        cfg = BacktestEngine.semiconductor_config()
        cfg.update(
            {
                "entry_period": 40,
                "exit_period": 30,
                "ma_long": 120,
                "trail_atr_mult": 9.0,
                "risk_pct": 0.012,
                "strategy_weight": 0.68,
                "max_symbol_weight": 0.35,
                "profit_lock_giveback": 0.22,
                "reversal_break_giveback": 0.22,
                "reversal_turtle_enabled": True,
            }
        )
        return cfg

    _KNOWN_CLASSIFICATION: dict[str, str] = {
        "300308": "default",
        "300502": "default",
        "300394": "default",
        "688205": "default",
        "920045": "default",
        "688008": "default",
        "002409": "default",
        "688300": "default",
        "603986": "semiconductor",
        "688072": "semiconductor",
        "300054": "semiconductor",
        "688535": "semiconductor",
        "300776": "semiconductor",
        "688249": "semiconductor",
        "688347": "semiconductor",
        "300604": "semiconductor",
        "688120": "semiconductor",
        "688082": "semiconductor",
        "688361": "semiconductor",
        "688409": "semiconductor",
        "300666": "semiconductor",
        "600206": "semiconductor",
    }
    _SYMBOL_GROUP: dict[str, str] = {
        "300308": "overseas_compute",
        "300502": "overseas_compute",
        "300394": "overseas_compute",
        "688205": "overseas_compute",
        "920045": "overseas_compute",
        "688008": "overseas_compute",
        "002409": "overseas_compute",
        "688300": "overseas_compute",
        "603986": "domestic_semiconductor",
        "688072": "domestic_semiconductor",
        "300054": "domestic_semiconductor",
        "688535": "domestic_semiconductor",
        "300776": "domestic_semiconductor",
        "688249": "domestic_semiconductor",
        "688347": "domestic_semiconductor",
        "300604": "domestic_semiconductor",
        "688120": "domestic_semiconductor",
        "688082": "domestic_semiconductor",
        "688361": "domestic_semiconductor",
        "688409": "domestic_semiconductor",
        "300666": "domestic_semiconductor",
        "600206": "domestic_semiconductor",
    }
    _SYMBOL_PROFILE: dict[str, str] = {
        "300308": "overseas_optical",
        "300502": "overseas_optical",
        "300394": "overseas_optical",
        "688205": "overseas_optical",
        "920045": "overseas_optical",
        "688008": "overseas_memory_material",
        "002409": "overseas_memory_material",
        "688300": "overseas_memory_material",
        "603986": "domestic_design",
        "688072": "domestic_equipment",
        "300776": "domestic_equipment",
        "688361": "domestic_equipment",
        "688409": "domestic_equipment",
        "300604": "domestic_equipment",
        "688120": "domestic_equipment",
        "688082": "domestic_equipment",
        "300054": "domestic_material",
        "688535": "domestic_material",
        "300666": "domestic_material",
        "600206": "domestic_material",
        "688249": "domestic_foundry",
        "688347": "domestic_foundry",
    }

    @staticmethod
    def config_for_symbol(code: str, name: str = "") -> dict:
        """Handle config for symbol for the quantitative backtest system."""
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
        return (
            BacktestEngine.semiconductor_config()
            if BacktestEngine.classify_symbol(code, name=name) == "semiconductor"
            else BacktestEngine._default_config()
        )

    _INDUSTRY_HINTS: dict[str, str] = {
        "foundry": "semiconductor",
        "Nexchip": "semiconductor",
        "Hua Hong": "semiconductor",
        "semiconductor equipment": "semiconductor",
        "etching": "semiconductor",
        "thin-film deposition": "semiconductor",
        "wafer cleaning": "semiconductor",
        "lithography track": "semiconductor",
        "CMP": "semiconductor",
        "assembly and testing": "semiconductor",
        "Changchuan": "semiconductor",
        "Huafeng": "semiconductor",
        "ACM": "semiconductor",
        "inspection": "semiconductor",
        "sputtering target": "semiconductor",
        "Jiangfeng": "semiconductor",
        "GRINM": "semiconductor",
        "electronic specialty gas": "semiconductor",
        "photoresist": "semiconductor",
        "polishing slurry": "semiconductor",
        "silicon wafer": "semiconductor",
        "compound semiconductor": "semiconductor",
        "optical module": "default",
        "optical communication": "default",
        "Zhongji": "default",
        "新易盛": "default",
        "TFC": "default",
        "德科立": "default",
        "Hengdong": "default",
        "PCB": "default",
        "WUS": "default",
        "SCC": "default",
        "memory": "default",
        "GigaDevice": "default",
        "Lance": "default",
        "memory interface": "default",
        "CIS": "default",
        "Will Semiconductor": "default",
        "radio frequency": "default",
        "Maxscend": "default",
    }

    @staticmethod
    def _classify_by_industry_hints(code: str, name: str = "") -> str | None:
        """Handle classify by industry hints for the quantitative backtest system."""
        candidates = " ".join((str(x) for x in (code, name) if x))
        for key, cls in BacktestEngine._INDUSTRY_HINTS.items():
            if key in candidates:
                return cls
        return None

    @staticmethod
    def classify_symbol(
        code: str,
        df: pd.DataFrame | None = None,
        name: str = "",
        lookback_start: str = "",
        lookback_end: str | None = None,
    ) -> str:
        """Handle classify symbol for the quantitative backtest system."""
        known = BacktestEngine._KNOWN_CLASSIFICATION.get(code)
        if known:
            return known
        hint = BacktestEngine._classify_by_industry_hints(code, name)
        if hint:
            return hint
        return "default"

    @staticmethod
    def _validate_config(cfg: dict) -> dict:
        """Validate one complete engine configuration and normalize containers."""
        out = dict(cfg)
        allowed_keys = set(BacktestEngine._default_config().keys())
        unknown_keys = sorted(set(out) - allowed_keys)
        if unknown_keys:
            raise ValueError(
                f"Configuration contains unknown fields; check for typos: {unknown_keys}"
            )
        BacktestEngine._validate_integer_config(out)
        BacktestEngine._validate_numeric_config(out)
        BacktestEngine._validate_boolean_config(out)
        BacktestEngine._validate_container_config(out)
        if out["entry_period"] <= out["exit_period"]:
            raise ValueError("entry_period must be greater than exit_period")
        if out["ma_short"] >= out["ma_long"]:
            raise ValueError("ma_short must be less than ma_long")
        if out["sector_shock_confirmations"] > out["sector_shock_window"]:
            raise ValueError(
                "sector_shock_confirmations must not exceed sector_shock_window"
            )
        if out["max_symbol_weight"] > out["max_total_weight"]:
            raise ValueError("max_symbol_weight must not exceed max_total_weight")
        if out["strategy_weight"] > out["max_total_weight"]:
            raise ValueError("strategy_weight must not exceed max_total_weight")
        return out

    @staticmethod
    def _validate_integer_config(out: dict) -> None:
        """Validate integer periods, counters, slots, and confirmation windows."""
        minimums = {
            "entry_period": 2,
            "exit_period": 1,
            "adx_period": 1,
            "atr_period": 1,
            "rsi_period": 1,
            "ma_short": 1,
            "ma_long": 2,
            "max_units": 1,
            "momentum_lookback": 1,
            "max_positions": 1,
            "cooldown_days": 0,
            "max_pending_buy_days": 1,
            "group_min_slots": 0,
            "reversal_exit_period": 2,
            "sector_shock_ma": 2,
            "sector_shock_window": 2,
            "sector_shock_confirmations": 1,
            "sector_recovery_ma": 2,
            "sector_recovery_confirmations": 1,
            "sector_guard_min_symbols": 2,
        }
        for key, minimum in minimums.items():
            out[key] = _require_int(key, out.get(key), min_value=minimum)

    @staticmethod
    def _validate_numeric_config(out: dict) -> None:
        """Validate scalar thresholds, weights, costs, and strategy scales."""
        out["adx_threshold"] = _require_finite(
            "adx_threshold", out.get("adx_threshold"), min_value=0.0
        )
        out["pyramid_risk_decay"] = _require_finite(
            "pyramid_risk_decay",
            out.get("pyramid_risk_decay", 1.0),
            min_value=0.01,
            max_value=1.0,
        )
        out["limit_price_epsilon"] = _require_finite(
            "limit_price_epsilon",
            out.get("limit_price_epsilon", 0.001),
            min_value=0.0,
            max_value=0.1,
        )
        out["sector_shock_return"] = _require_finite(
            "sector_shock_return",
            out.get("sector_shock_return"),
            min_value=-1.0,
            max_value=0.0,
        )
        out["sector_shock_breadth"] = _require_finite(
            "sector_shock_breadth",
            out.get("sector_shock_breadth"),
            min_value=0.0,
            max_value=1.0,
        )
        out["sector_recovery_breadth"] = _require_finite(
            "sector_recovery_breadth",
            out.get("sector_recovery_breadth"),
            min_value=0.0,
            max_value=1.0,
        )
        atr_method = str(out.get("atr_method", "wilder")).lower()
        if atr_method not in {"wilder", "sma"}:
            raise ValueError(
                f"atr_method must be 'wilder' or 'sma', got {atr_method!r}"
            )
        out["atr_method"] = atr_method
        for key in [
            "atr_multiplier",
            "trail_atr_mult",
            "channel_mult",
            "channel_lower_mult",
            "pyramid_add_atr",
        ]:
            out[key] = _require_positive(key, out.get(key))
        for key in [
            "risk_pct",
            "hard_stop",
            "strategy_weight",
            "max_symbol_weight",
            "max_drawdown",
            "daily_loss_limit",
            "profit_lock_activation",
            "profit_lock_giveback",
            "reversal_break_giveback",
            "reversal_loss_cut",
        ]:
            out[key] = _require_positive(
                key, out.get(key), max_value=1.0, inclusive_max=False
            )
        out["max_total_weight"] = _require_positive(
            "max_total_weight",
            out.get("max_total_weight"),
            max_value=1.0,
            inclusive_max=True,
        )
        for key in ["commission_rate", "stamp_duty", "slippage"]:
            out[key] = _require_finite(
                key, out.get(key), min_value=0.0, max_value=1.0, inclusive_max=False
            )
        out["min_commission"] = _require_finite(
            "min_commission", out.get("min_commission", 0.0), min_value=0.0
        )
        out["risk_free_rate"] = _require_finite(
            "risk_free_rate",
            out.get("risk_free_rate", 0.0),
            min_value=-0.99,
            max_value=1.0,
        )
        for key in [
            "fusion_single_scale",
            "fusion_double_scale",
            "fusion_triple_scale",
        ]:
            out[key] = _require_positive(
                key, out.get(key), max_value=2.0, inclusive_max=True
            )

    @staticmethod
    def _validate_boolean_config(out: dict) -> None:
        """Reject truthy strings and integers for every Boolean option."""
        boolean_keys = (
            "liquidate_on_circuit_breaker",
            "sector_guard_enabled",
            "close_position_on_data_end",
            "force_close_on_end",
            "reversal_turtle_enabled",
            "reversal_dual_ma_enabled",
            "reversal_atr_channel_enabled",
        )
        for key in boolean_keys:
            out[key] = _require_bool(key, out.get(key))

    @staticmethod
    def _validate_container_config(out: dict) -> None:
        """Normalize sector caps, symbol limit overrides, and ST symbol codes."""
        group_limits = out.get("combined_group_weight_limits", {})
        if not isinstance(group_limits, dict):
            raise ValueError("combined_group_weight_limits must be a dict")
        allowed_groups = {"overseas_compute", "domestic_semiconductor"}
        unknown_groups = sorted(set(map(str, group_limits)) - allowed_groups)
        if unknown_groups:
            raise ValueError(
                f"combined_group_weight_limits contains unknown sector pools: {unknown_groups}"
            )
        out["combined_group_weight_limits"] = {
            str(group): _require_positive(
                f"combined_group_weight_limits[{group}]",
                value,
                max_value=1.0,
                inclusive_max=True,
            )
            for group, value in group_limits.items()
        }
        per_symbol_limit_pct = out.get("per_symbol_limit_pct", {}) or {}
        if not isinstance(per_symbol_limit_pct, dict):
            raise ValueError("per_symbol_limit_pct must be a dict")
        normalized_limit_overrides: dict[str, float] = {}
        for code, pct in per_symbol_limit_pct.items():
            code_str = str(code)
            if not _SYMBOL_RE.match(code_str):
                raise ValueError(
                    f"per_symbol_limit_pct contains an invalid stock code: {code!r}"
                )
            normalized_limit_overrides[code_str] = _require_positive(
                f"per_symbol_limit_pct[{code_str}]",
                pct,
                max_value=1.0,
                inclusive_max=False,
            )
        out["per_symbol_limit_pct"] = normalized_limit_overrides
        st_symbols = out.get("st_symbols", set()) or set()
        if isinstance(st_symbols, str) or not isinstance(
            st_symbols, (set, list, tuple)
        ):
            raise ValueError("st_symbols must be a collection of stock codes")
        normalized_st_symbols = {str(code) for code in st_symbols}
        bad_st = [code for code in normalized_st_symbols if not _SYMBOL_RE.match(code)]
        if bad_st:
            raise ValueError(f"st_symbols contains an invalid stock code: {bad_st}")
        out["st_symbols"] = normalized_st_symbols

    @staticmethod
    def _signal_key(signal: Signal) -> tuple[str, str, str]:
        """Handle signal key for the quantitative backtest system."""
        return (signal.symbol, signal.strategy_name, signal.direction)

    def _pending_has_buy(
        self, pending: list[tuple[Signal, BaseStrategy]], code: str, strategy_name: str
    ) -> bool:
        """Handle pending has buy for the quantitative backtest system."""
        return any(
            (
                sig.symbol == code
                and sig.strategy_name == strategy_name
                and (sig.direction == "buy")
                for sig, _ in pending
            )
        )

    def _pending_has_sell(
        self, pending: list[tuple[Signal, BaseStrategy]], code: str, strategy_name: str
    ) -> bool:
        """Handle pending has sell for the quantitative backtest system."""
        return any(
            (
                sig.symbol == code
                and sig.strategy_name == strategy_name
                and (sig.direction == "sell")
                for sig, _ in pending
            )
        )

    @staticmethod
    def _dedupe_pending_signals(
        pending: list[tuple[Signal, BaseStrategy]],
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Handle dedupe pending signals for the quantitative backtest system."""
        result: dict[tuple[str, str, str], tuple[Signal, BaseStrategy]] = {}
        for signal, strategy in pending:
            if signal.direction not in {"buy", "sell"}:
                continue
            key = BacktestEngine._signal_key(signal)
            result[key] = (signal, strategy)
        sell_keys = {
            (s.symbol, s.strategy_name)
            for s, _ in result.values()
            if s.direction == "sell"
        }
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
        """Resolve buy/sell conflicts and scale same-symbol strategy confirmations."""
        # Strategy positions remain independent for exits and audit trails; only
        # same-day target sizes and conflicting directions are fused here.
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
                    signal.fusion_label = (
                        "conflict: sell takes priority" if conflict else "exit signal"
                    )
                    if conflict:
                        signal.reason = (
                            f"[conflict: sell takes priority] {signal.reason}"
                        )
                    fused.append((signal, strategy))
                self.fusion_events.append(
                    {
                        "date": date_str,
                        "symbol": symbol,
                        "state": "conflict_sell_first" if conflict else "sell",
                        "buy_votes": len(buys),
                        "sell_votes": len(sells),
                    }
                )
                continue
            if not buys:
                continue
            votes = len(buys)
            if votes >= 3:
                label, scale = (
                    "three-strategy confirmation",
                    float(self.cfg["fusion_triple_scale"]),
                )
            elif votes == 2:
                label, scale = (
                    "two-strategy confirmation",
                    float(self.cfg["fusion_double_scale"]),
                )
            else:
                label, scale = (
                    "single-strategy probe",
                    float(self.cfg["fusion_single_scale"]),
                )
            for signal, strategy in buys:
                signal.fusion_votes = votes
                signal.fusion_label = label
                signal.target_shares = _floor_to_lot(signal.target_shares * scale)
                signal.reason = f"[{label}] {signal.reason}"
                if signal.target_shares > 0:
                    fused.append((signal, strategy))
            self.fusion_events.append(
                {
                    "date": date_str,
                    "symbol": symbol,
                    "state": label,
                    "buy_votes": votes,
                    "sell_votes": 0,
                    "scale": scale,
                }
            )
        return fused

    def _buy_signal_expired(
        self, signal: Signal, date: pd.Timestamp, date_to_pos: dict[pd.Timestamp, int]
    ) -> bool:
        """Handle buy signal expired for the quantitative backtest system."""
        if signal.direction != "buy" or not signal.signal_date:
            return False
        signal_ts = pd.Timestamp(signal.signal_date)
        if signal_ts in date_to_pos and date in date_to_pos:
            waited = date_to_pos[date] - date_to_pos[signal_ts]
            return waited > int(self.cfg.get("max_pending_buy_days", 5))
        return False

    @staticmethod
    def _has_pending_liquidation(pending: list[tuple[Signal, BaseStrategy]]) -> bool:
        """Handle has pending liquidation for the quantitative backtest system."""
        return any(
            (
                sig.direction == "sell"
                and str(sig.reason)
                in {"circuit breaker liquidation", "sector breadth risk liquidation"}
                for sig, _ in pending
            )
        )

    def _validate_strategy_templates(self) -> None:
        """Handle validate strategy templates for the quantitative backtest system."""
        names: list[str] = []
        for cls in self.strategy_templates:
            name = getattr(cls, "name", "")
            if not isinstance(name, str) or not name:
                raise ValueError(f"Strategy {cls!r} Missing a valid name")
            names.append(name)
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(
                f"strategy_templates contains duplicate strategy names: {duplicates}"
            )

    def _reset_run_state(self, symbols_dict: dict[str, str]) -> None:
        """Handle reset run state for the quantitative backtest system."""
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
        self.risk_events = []
        self.sector_guard_active = False
        self._sector_shock_positions = []
        self._sector_recovery_streak = 0
        self.cfg = self._validate_config({**self._default_config(), **self._user_cfg})

    def _apply_global_profile(self, profile: str | None) -> None:
        """Handle apply global profile for the quantitative backtest system."""
        factories = {
            "semiconductor": self.semiconductor_config,
            "semiconductor_heavy": self.semiconductor_heavy_config,
            "aggressive": self.optimized_aggressive_config,
        }
        factory = factories.get(profile)
        if factory is not None:
            self.cfg = self._validate_config(
                {**self._default_config(), **factory(), **self._user_cfg}
            )

    def _resolve_symbol_configs(
        self,
        symbols_dict: dict[str, str],
        per_symbol_config: dict[str, dict] | None,
        config_route: str,
    ) -> dict[str, dict]:
        """Handle resolve symbol configs for the quantitative backtest system."""
        overrides = per_symbol_config or {}
        if not isinstance(overrides, dict):
            raise ValueError("per_symbol_config must be a dict")
        unknown_overrides = sorted(set(overrides) - set(symbols_dict))
        if unknown_overrides:
            raise ValueError(
                f"per_symbol_configcontains a symbol outside the backtest universe: {unknown_overrides}"
            )
        for code, values in overrides.items():
            if not isinstance(values, dict):
                raise ValueError(f"per_symbol_config[{code}] must be a dict")
            ignored_keys = sorted(set(values) - self._PER_SYMBOL_OVERRIDE_KEYS)
            if ignored_keys:
                raise ValueError(
                    f"per_symbol_config[{code}] contains global-only or unknown keys: {ignored_keys}; set global values through BacktestEngine(cfg=...)"
                )
        self.risk = RiskManager(self.cfg)
        self.risk.configure_groups(
            {
                code: BacktestEngine._SYMBOL_GROUP.get(
                    code,
                    "domestic_semiconductor"
                    if BacktestEngine.classify_symbol(
                        code, name=symbols_dict.get(code, "")
                    )
                    == "semiconductor"
                    else "overseas_compute",
                )
                for code in symbols_dict
            }
        )

        def _base_for(code: str) -> dict:
            """Handle base for for the quantitative backtest system."""
            if config_route == "auto":
                return BacktestEngine.config_for_symbol(
                    code, name=symbols_dict.get(code, "")
                )
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
        """Handle load market data for the quantitative backtest system."""
        data_map: dict[str, pd.DataFrame] = {}
        ind_map: dict[str, dict[str, pd.Series]] = {}
        for code, name in symbols_dict.items():
            print(f"  Loading data for {name} ({code})...")
            df = DataFetcher.load_stock_data(
                code, start_date, end_date, data_dir=data_dir
            )
            df = df[(df.index >= start_ts) & (df.index <= end_ts)].copy()
            if df.empty:
                raise RuntimeError(
                    f"{code} contains no valid market data between {start_date} and {end_date}"
                )
            data_map[code] = df
            ind_map[code] = Indicators.compute_all(df, symbol_configs[code])
            route = (
                BacktestEngine._SYMBOL_PROFILE.get(
                    code, BacktestEngine.classify_symbol(code, name=name)
                )
                if config_route == "auto"
                else str(profile or "default")
            )
            print(f"  [Parameter route] {name}({code}) -> {route}")
            print(
                f"  {name} ({code}): {len(df)} rows, "
                f"{df.index[0].date()} through {df.index[-1].date()}"
            )
        return (data_map, ind_map)

    def _select_momentum_candidates(
        self,
        data_map: dict[str, pd.DataFrame],
        symbols_dict: dict[str, str],
        date: pd.Timestamp,
    ) -> set[str]:
        """Handle select momentum candidates for the quantitative backtest system."""
        lookback = int(self.cfg.get("momentum_lookback", 20))
        scores: dict[str, float] = {}
        for code, df in data_map.items():
            if date not in df.index:
                continue
            i = df.index.get_loc(date)
            if i >= lookback:
                scores[code] = float(
                    df["close"].iloc[i] / df["close"].iloc[i - lookback] - 1
                )
        if not scores:
            return set(symbols_dict)
        max_positions = int(self.cfg.get("max_positions", 2))
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        min_slots = min(int(self.cfg.get("group_min_slots", 0)), max_positions // 2)
        selected: list[str] = []
        if min_slots > 0:
            for group in ("overseas_compute", "domestic_semiconductor"):
                group_ranked = [
                    code
                    for code, _ in ranked
                    if (
                        BacktestEngine._SYMBOL_GROUP.get(code)
                        or (
                            "domestic_semiconductor"
                            if BacktestEngine.classify_symbol(
                                code, name=symbols_dict.get(code, "")
                            )
                            == "semiconductor"
                            else "overseas_compute"
                        )
                    )
                    == group
                ]
                selected.extend(group_ranked[:min_slots])
        for code, _ in ranked:
            if code not in selected:
                selected.append(code)
            if len(selected) >= max_positions:
                break
        return set(selected[:max_positions])

    def _record_equity(
        self, data_map: dict[str, pd.DataFrame], date: pd.Timestamp, date_str: str
    ) -> None:
        """Handle record equity for the quantitative backtest system."""
        assets = self._total_assets(data_map, date)
        self.equity_curve.append(
            {
                "date": date_str,
                "assets": assets,
                "cash": self.cash,
                "position_value": assets - self.cash,
            }
        )

    def _apply_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        all_dates: list[pd.Timestamp],
        date_to_pos: dict[pd.Timestamp, int],
        pending: list[tuple[Signal, BaseStrategy]],
    ) -> tuple[list[tuple[Signal, BaseStrategy]], bool, bool]:
        """Handle apply portfolio risk for the quantitative backtest system."""
        risk_status = self.risk.check_portfolio_risk(
            current_assets, date_str, trading_dates=all_dates, date_to_pos=date_to_pos
        )
        if risk_status is None and self.risk.check_daily_loss(current_assets):
            risk_status = "daily loss limit"
        risk_blocked = self._has_pending_liquidation(pending)
        liquidate = False
        if risk_blocked:
            risk_status = risk_status or "circuit breaker liquidation pending"
        if not risk_status:
            return (pending, risk_blocked, liquidate)
        if risk_status == "portfolio drawdown circuit breaker":
            liquidate = bool(self.cfg.get("liquidate_on_circuit_breaker", True))
            if liquidate:
                print(
                    f"  WARNING [{date_str}] {risk_status}: generate T+1 liquidation signals and cool down for {self.cfg['cooldown_days']} days"
                )
                liquidation_signals = self._generate_liquidation_signals(date_str)
                pending = self._dedupe_pending_signals(
                    [
                        (sig, strategy)
                        for sig, strategy in pending
                        if sig.direction == "sell"
                    ]
                    + liquidation_signals
                )
            else:
                print(
                    f"  WARNING [{date_str}] {risk_status}: block new entries and cool down for {self.cfg['cooldown_days']} days"
                )
        return (pending, True, liquidate)

    def _update_sector_guard(
        self,
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
        all_dates: list[pd.Timestamp],
        date_to_pos: dict[pd.Timestamp, int],
    ) -> str | None:
        """Update the portfolio breadth risk state using only data visible at the current close."""
        if not bool(self.cfg.get("sector_guard_enabled", True)):
            self.sector_guard_active = False
            return None
        pos = date_to_pos[pd.Timestamp(date)]
        shock_ma = int(self.cfg["sector_shock_ma"])
        recovery_ma = int(self.cfg["sector_recovery_ma"])
        max_ma = max(shock_ma, recovery_ma)
        if pos < max_ma:
            return self._current_sector_guard_state()
        # The breadth guard is a portfolio signal, not a disguised single-stock
        # stop. It remains inactive unless enough symbols have complete data.
        observation = self._build_sector_observation(
            data_map, date, max_ma, shock_ma, recovery_ma
        )
        if observation is None:
            return self._current_sector_guard_state()
        if observation.symbol_count < int(self.cfg["sector_guard_min_symbols"]):
            self._sector_shock_positions = []
            self._sector_recovery_streak = 0
            return self._current_sector_guard_state()
        shock = self._is_sector_shock(observation)
        if shock:
            self._record_sector_shock(date, pos, observation)
        self._trim_sector_shock_window(pos)
        if not self.sector_guard_active:
            return self._try_activate_sector_guard(date, observation)
        recovery = self._is_sector_recovery(
            observation, shock, all_dates, pos, recovery_ma
        )
        self._sector_recovery_streak = (
            self._sector_recovery_streak + 1 if recovery else 0
        )
        if self._sector_recovery_streak < int(
            self.cfg["sector_recovery_confirmations"]
        ):
            return "active"
        self.sector_guard_active = False
        self._sector_recovery_streak = 0
        self._sector_shock_positions = []
        self.risk_events.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "event": "sector_guard_off",
                "equal_weight_return": observation.equal_return,
                "breadth": observation.recovery_breadth,
            }
        )
        return "recovered"

    def _current_sector_guard_state(self) -> str | None:
        """Translate the current guard flag into the run-loop state contract."""
        return "active" if self.sector_guard_active else None

    @staticmethod
    def _build_sector_observation(
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
        max_ma: int,
        shock_ma: int,
        recovery_ma: int,
    ) -> SectorObservation | None:
        """Build one equal-weight breadth snapshot without looking past date."""
        daily_returns: list[float] = []
        above_shock_ma: list[bool] = []
        above_recovery_ma: list[bool] = []
        normalized_series: list[pd.Series] = []
        for df in data_map.values():
            history = df.loc[df.index <= date, "close"].dropna().astype(float)
            if len(history) <= max_ma or date not in history.index:
                continue
            current = float(history.iloc[-1])
            previous = float(history.iloc[-2])
            if current <= 0 or previous <= 0:
                continue
            daily_returns.append(current / previous - 1.0)
            above_shock_ma.append(current > float(history.tail(shock_ma).mean()))
            above_recovery_ma.append(current > float(history.tail(recovery_ma).mean()))
            normalized_series.append(history / float(history.iloc[0]))
        if not daily_returns:
            return None
        return SectorObservation(
            symbol_count=len(daily_returns),
            equal_return=float(np.mean(daily_returns)),
            shock_breadth=float(np.mean(above_shock_ma)),
            recovery_breadth=float(np.mean(above_recovery_ma)),
            normalized_series=tuple(normalized_series),
        )

    def _is_sector_shock(self, observation: SectorObservation) -> bool:
        """Require both a severe equal-weight loss and collapsed breadth."""
        # A shock requires both a large equal-weight loss and collapsed breadth.
        return observation.equal_return <= float(
            self.cfg["sector_shock_return"]
        ) and observation.shock_breadth <= float(self.cfg["sector_shock_breadth"])

    def _record_sector_shock(
        self, date: pd.Timestamp, pos: int, observation: SectorObservation
    ) -> None:
        """Append one shock occurrence to the rolling window and audit log."""
        self._sector_shock_positions.append(pos)
        self.risk_events.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "event": "sector_shock",
                "equal_weight_return": observation.equal_return,
                "breadth": observation.shock_breadth,
            }
        )

    def _trim_sector_shock_window(self, pos: int) -> None:
        """Discard shock confirmations older than the configured trading window."""
        window = int(self.cfg["sector_shock_window"])
        self._sector_shock_positions = [
            p for p in self._sector_shock_positions if p >= pos - window + 1
        ]

    def _try_activate_sector_guard(
        self, date: pd.Timestamp, observation: SectorObservation
    ) -> str | None:
        """Activate defense only after the configured shock count is confirmed."""
        # Multiple shocks inside a rolling trading-day window reduce the chance
        # that an isolated correction forces a full portfolio liquidation.
        if len(self._sector_shock_positions) < int(
            self.cfg["sector_shock_confirmations"]
        ):
            return None
        self.sector_guard_active = True
        self._sector_recovery_streak = 0
        self.risk_events.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "event": "sector_guard_on",
                "shock_count": len(self._sector_shock_positions),
                "equal_weight_return": observation.equal_return,
                "breadth": observation.shock_breadth,
            }
        )
        return "triggered"

    def _is_sector_recovery(
        self,
        observation: SectorObservation,
        shock: bool,
        all_dates: list[pd.Timestamp],
        pos: int,
        recovery_ma: int,
    ) -> bool:
        """Require positive return, broad participation, and sector trend repair."""
        # Recovery is deliberately asymmetric and slower than entry into defense.
        # It requires a positive day, broad participation, and sector trend repair.
        recent_dates = all_dates[max(0, pos - recovery_ma + 1) : pos + 1]
        sector_levels: list[float] = []
        for d in recent_dates:
            values = [
                float(series.loc[d])
                for series in observation.normalized_series
                if d in series.index
            ]
            if values:
                sector_levels.append(float(np.mean(values)))
        sector_above_ma = len(sector_levels) >= recovery_ma and sector_levels[
            -1
        ] > float(np.mean(sector_levels))
        return (
            not shock
            and observation.equal_return > 0
            and (
                observation.recovery_breadth
                >= float(self.cfg["sector_recovery_breadth"])
            )
            and sector_above_ma
        )

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
        """Handle collect strategy signals for the quantitative backtest system."""
        held_symbols = set(self.positions)
        daily: list[tuple[Signal, BaseStrategy]] = []
        for code in symbols_dict:
            df = data_map[code]
            if date not in df.index:
                continue
            i = df.index.get_loc(date)
            for strategy in self.strategy_instances[code]:
                ctx = BarContext(
                    i=i,
                    df=df,
                    current_assets=current_assets,
                    indicators=ind_map[code],
                    symbol=code,
                    date=date_str,
                )
                signal = strategy.on_bar(ctx)
                if signal is None:
                    continue
                if signal.direction == "buy":
                    if not allow_buys or self._pending_has_buy(
                        pending, code, strategy.name
                    ):
                        continue
                    if (
                        top_symbols is not None
                        and code not in top_symbols
                        and (code not in held_symbols)
                    ):
                        continue
                elif signal.direction == "sell":
                    if self._pending_has_sell(pending, code, strategy.name):
                        continue
                else:
                    continue
                daily.append((signal, strategy))
        return daily

    @staticmethod
    def _validate_run_request(
        symbols_dict: dict[str, str],
        start_date: str,
        end_date: str,
        profile: str | None,
        config_route: str,
    ) -> tuple[str | None, str, pd.Timestamp, pd.Timestamp]:
        """Validate public run inputs before mutating any engine state."""
        if profile is not None:
            profile = str(profile).lower()
            allowed_profiles = {
                "default",
                "semiconductor",
                "semiconductor_heavy",
                "aggressive",
            }
            if profile not in allowed_profiles:
                raise ValueError(
                    "profile must be one of 'default', 'semiconductor', "
                    f"'semiconductor_heavy', or 'aggressive'; received {profile!r}"
                )
        config_route = str(config_route).lower()
        if config_route not in {"auto", "none"}:
            raise ValueError(
                "config_route must be either 'auto' or 'none'; "
                f"received {config_route!r}"
            )
        if not symbols_dict:
            raise ValueError("symbols_dict must not be empty")
        bad_codes = [
            code
            for code in symbols_dict
            if not isinstance(code, str) or not _SYMBOL_RE.match(code)
        ]
        if bad_codes:
            raise ValueError(
                f"symbols_dict contains an invalid stock code: {bad_codes}"
            )
        start_ts = pd.Timestamp(start_date)
        end_ts = pd.Timestamp(end_date)
        if start_ts > end_ts:
            raise ValueError("start_date must not be later than end_date")
        return profile, config_route, start_ts, end_ts

    def _prepare_run(
        self,
        symbols_dict: dict[str, str],
        start_date: str,
        end_date: str,
        start_ts: pd.Timestamp,
        end_ts: pd.Timestamp,
        per_symbol_config: dict[str, dict] | None,
        profile: str | None,
        config_route: str,
        data_dir: str | None,
    ) -> tuple[
        dict[str, pd.DataFrame],
        dict[str, dict[str, pd.Series]],
        list[pd.Timestamp],
        dict[pd.Timestamp, int],
    ]:
        """Reset state, load data, compute indicators, and instantiate strategies."""
        self._reset_run_state(symbols_dict)
        print(f"\n{'=' * 60}")
        print("Codex Quant v14 backtest")
        print(f"  Capital: {self.initial_capital:,.0f}")
        print(f"  Symbols: {symbols_dict}")
        print(f"  Period: {start_date} ~ {end_date}")
        print(f"{'=' * 60}\n")
        self._apply_global_profile(profile)
        symbol_configs = self._resolve_symbol_configs(
            symbols_dict, per_symbol_config, config_route
        )
        self.symbol_configs = symbol_configs
        data_map, indicator_map = self._load_market_data(
            symbols_dict,
            symbol_configs,
            start_date,
            end_date,
            start_ts,
            end_ts,
            config_route,
            profile,
            data_dir,
        )
        all_dates = sorted(
            {date for frame in data_map.values() for date in frame.index}
        )
        date_to_pos = {pd.Timestamp(date): i for i, date in enumerate(all_dates)}
        self.global_last_date = pd.Timestamp(all_dates[-1])
        self.symbol_last_dates = {
            code: pd.Timestamp(frame.index[-1]) for code, frame in data_map.items()
        }
        print(f"\n  Trading days: {len(all_dates)}")
        self._validate_strategy_templates()
        self.strategy_instances = {
            code: [cls(symbol_configs[code]) for cls in self.strategy_templates]
            for code in symbols_dict
        }
        return data_map, indicator_map, all_dates, date_to_pos

    def _apply_sector_guard_actions(
        self,
        pending: list[tuple[Signal, BaseStrategy]],
        guard_state: str | None,
        date_str: str,
        risk_blocked: bool,
        liquidate: bool,
    ) -> tuple[list[tuple[Signal, BaseStrategy]], bool, bool]:
        """Convert guard state into pending T+1 liquidations and entry blocking."""
        if self.sector_guard_active:
            if guard_state == "triggered":
                print(
                    f"  WARNING [{date_str}] sector breadth deteriorated repeatedly; "
                    "generate T+1 liquidation signals"
                )
            guard_liquidations = self._generate_liquidation_signals(
                date_str, reason="sector breadth risk liquidation"
            )
            pending = self._dedupe_pending_signals(
                [item for item in pending if item[0].direction == "sell"]
                + guard_liquidations
            )
            return pending, True, True
        if guard_state == "recovered":
            print(
                f"  RECOVERED [{date_str}] sector breadth recovered repeatedly; "
                "new entries are allowed from the next trading day"
            )
        return pending, risk_blocked, liquidate

    def _merge_unblocked_daily_signals(
        self,
        symbols_dict: dict[str, str],
        data_map: dict[str, pd.DataFrame],
        indicator_map: dict[str, dict[str, pd.Series]],
        date: pd.Timestamp,
        date_str: str,
        current_assets: float,
        pending: list[tuple[Signal, BaseStrategy]],
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Rank candidates, fuse new signals, and remove conflicting pending buys."""
        # Momentum ranks only allocate scarce slots; they do not create a buy
        # unless an underlying strategy independently emits an entry signal.
        top_symbols = self._select_momentum_candidates(data_map, symbols_dict, date)
        daily_signals = self._collect_strategy_signals(
            symbols_dict,
            data_map,
            indicator_map,
            date,
            date_str,
            current_assets,
            pending,
            allow_buys=True,
            top_symbols=top_symbols,
        )
        fused_daily = self._fuse_daily_signals(daily_signals, date_str)
        sell_symbols = {
            signal.symbol for signal, _ in fused_daily if signal.direction == "sell"
        }
        if sell_symbols:
            pending = [
                item
                for item in pending
                if not (item[0].direction == "buy" and item[0].symbol in sell_symbols)
            ]
        pending.extend(fused_daily)
        return pending

    def _finish_trading_day(
        self,
        pending: list[tuple[Signal, BaseStrategy]],
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
        date_str: str,
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Deduplicate instructions, close exhausted symbols, and mark equity."""
        pending = self._dedupe_pending_signals(pending)
        closed_keys = self._close_positions_on_data_end(data_map, date)
        if closed_keys:
            pending = [
                item
                for item in pending
                if (item[0].symbol, item[0].strategy_name) not in closed_keys
            ]
        self._record_equity(data_map, date, date_str)
        return pending

    def _process_trading_day(
        self,
        symbols_dict: dict[str, str],
        data_map: dict[str, pd.DataFrame],
        indicator_map: dict[str, dict[str, pd.Series]],
        all_dates: list[pd.Timestamp],
        date_to_pos: dict[pd.Timestamp, int],
        date: pd.Timestamp,
        pending: list[tuple[Signal, BaseStrategy]],
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Process one date while preserving close-signal and next-open execution order."""
        date_str = date.strftime("%Y-%m-%d")
        self.risk.daily_start_assets = (
            self.equity_curve[-1]["assets"]
            if self.equity_curve
            else self.initial_capital
        )
        # Execute yesterday's close-generated instructions before observing
        # today's close. This ordering enforces T+1 causality throughout.
        if pending:
            pending = self._execute_pending_signals(
                pending, data_map, date, date_to_pos
            )
        current_assets = self._total_assets(data_map, date)
        pending, risk_blocked, liquidate = self._apply_portfolio_risk(
            current_assets, date_str, all_dates, date_to_pos, pending
        )
        # Portfolio risk and breadth state use the current close, then create
        # orders that cannot execute until a later tradable open.
        guard_state = self._update_sector_guard(data_map, date, all_dates, date_to_pos)
        pending, risk_blocked, liquidate = self._apply_sector_guard_actions(
            pending, guard_state, date_str, risk_blocked, liquidate
        )
        if risk_blocked and not liquidate:
            pending.extend(
                self._collect_strategy_signals(
                    symbols_dict,
                    data_map,
                    indicator_map,
                    date,
                    date_str,
                    current_assets,
                    pending,
                    allow_buys=False,
                )
            )
        elif not risk_blocked:
            pending = self._merge_unblocked_daily_signals(
                symbols_dict,
                data_map,
                indicator_map,
                date,
                date_str,
                current_assets,
                pending,
            )
        return self._finish_trading_day(pending, data_map, date, date_str)

    def run(
        self,
        symbols_dict: dict[str, str],
        start_date: str,
        end_date: str,
        per_symbol_config: dict[str, dict] | None = None,
        profile: str | None = None,
        config_route: str = "auto",
        data_dir: str | None = None,
    ) -> dict:
        """Run a deterministic backtest over the requested inclusive date range."""
        profile, config_route, start_ts, end_ts = self._validate_run_request(
            symbols_dict, start_date, end_date, profile, config_route
        )
        data_map, indicator_map, all_dates, date_to_pos = self._prepare_run(
            symbols_dict,
            start_date,
            end_date,
            start_ts,
            end_ts,
            per_symbol_config,
            profile,
            config_route,
            data_dir,
        )
        pending_signals: list[tuple[Signal, BaseStrategy]] = []
        for date in all_dates:
            pending_signals = self._process_trading_day(
                symbols_dict,
                data_map,
                indicator_map,
                all_dates,
                date_to_pos,
                date,
                pending_signals,
            )
        last_date = all_dates[-1]
        if self.cfg.get("force_close_on_end", False):
            self._liquidate_all(
                data_map, last_date, reason="forced liquidation at period end"
            )
            pending_signals = []
        final_assets = self._total_assets(data_map, last_date)
        if self.cfg.get("force_close_on_end", False) and self.equity_curve:
            self.equity_curve[-1].update(
                {
                    "assets": final_assets,
                    "cash": self.cash,
                    "position_value": final_assets - self.cash,
                }
            )
        self.pending_signals = self._dedupe_pending_signals(pending_signals)
        print(
            f"\n  Backtest completed: initial {self.initial_capital:,.0f} -> final assets {final_assets:,.0f}"
        )
        return self._build_result(final_assets, all_dates)

    def _execute_pending_signals(
        self,
        pending: list[tuple[Signal, BaseStrategy]],
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
        date_to_pos: dict[pd.Timestamp, int],
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Execute prior-close signals at the next tradable open in deterministic priority order."""
        date_str = date.strftime("%Y-%m-%d")
        strategy_rank = {"turtle_breakout": 0, "dual_ma": 1, "atr_channel": 2}
        # Sells execute before buys, then a stable symbol/strategy priority breaks
        # ties. This avoids accidental dependence on caller dictionary order.
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
                continue
            if code not in data_map or date not in data_map[code].index:
                unexecuted.append((signal, strategy))
                continue
            open_price = data_map[code].loc[date, "open"]
            if pd.isna(open_price) or open_price <= 0:
                unexecuted.append((signal, strategy))
                continue
            df = data_map[code]
            loc = df.index.get_loc(date)
            if loc > 0:
                # Opening prices at or beyond the estimated board limit are treated
                # as untradable. Sell orders survive for a later trading day.
                prev_close = df.iloc[loc - 1]["close"]
                if prev_close > 0:
                    change_pct = (open_price - prev_close) / prev_close
                    limit_up = _limit_pct_for_code(
                        code, self.cfg, self.symbol_names.get(code, "")
                    )
                    eps = float(self.cfg.get("limit_price_epsilon", 0.001))
                    limit_down = -limit_up
                    if signal.direction == "buy" and change_pct >= limit_up - eps:
                        continue
                    if signal.direction == "sell" and change_pct <= limit_down + eps:
                        unexecuted.append((signal, strategy))
                        continue
            signal.price = open_price
            if signal.direction == "buy":
                self._execute_buy(signal, strategy, date_str, data_map, date)
            elif signal.direction == "sell":
                executed = self._execute_sell(signal, strategy, date_str)
                if not executed and strategy.position is not None:
                    unexecuted.append((signal, strategy))
        return self._dedupe_pending_signals(unexecuted)

    def _latest_close_on_or_before(self, df: pd.DataFrame, date: pd.Timestamp) -> float:
        """Handle latest close on or before for the quantitative backtest system."""
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
        """Handle latest close before for the quantitative backtest system."""
        mask = df.index < date
        if not mask.any():
            return 0.0
        closes = pd.to_numeric(df.loc[mask, "close"], errors="coerce")
        closes = closes[closes > 0]
        return float(closes.iloc[-1]) if not closes.empty else 0.0

    def _execution_mark_prices(
        self, data_map: dict[str, pd.DataFrame], date: pd.Timestamp
    ) -> dict[str, float]:
        """Handle execution mark prices for the quantitative backtest system."""
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
        """Handle total assets at prices for the quantitative backtest system."""
        total = self.cash
        for code, positions in self.positions.items():
            price = prices.get(code)
            for pos in positions.values():
                mark = price if price is not None and price > 0 else pos.entry_price
                total += pos.market_value_at(mark)
        return float(total)

    def _total_assets(
        self, data_map: dict[str, pd.DataFrame], date: pd.Timestamp
    ) -> float:
        """Handle total assets for the quantitative backtest system."""
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
        """Handle fit buy to cash for the quantitative backtest system."""
        shares = _floor_to_lot(requested_shares)
        if shares > 0:
            requested_value = shares * exec_price
            requested_commission = max(
                requested_value * commission_rate, min_commission
            )
            if requested_value + requested_commission > self.cash:
                shares = _floor_to_lot(self.cash / (exec_price * (1 + commission_rate)))
        # Minimum commission makes the closed-form estimate slightly optimistic;
        # reduce by one board lot until the exact cash debit fits.
        while shares > 0:
            buy_value = shares * exec_price
            commission = max(buy_value * commission_rate, min_commission)
            total_cost = buy_value + commission
            if total_cost <= self.cash:
                return (shares, buy_value, commission, total_cost)
            shares -= A_SHARE_LOT_SIZE
        return (0, 0.0, 0.0, 0.0)

    def _apply_buy_to_position(
        self,
        signal: Signal,
        strategy: BaseStrategy,
        date_str: str,
        shares: int,
        exec_price: float,
        total_cost: float,
    ) -> None:
        """Handle apply buy to position for the quantitative backtest system."""
        strategy_cfg = strategy.cfg
        effective_entry = total_cost / shares
        # Anchor the stop to the actual slipped execution price, while never
        # loosening a stricter stop already generated at the signal close.
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
            pos.highest_close_since_entry = max(
                pos.highest_close_since_entry, exec_price
            )
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

    def _execute_buy(
        self,
        signal: Signal,
        strategy: BaseStrategy,
        date_str: str,
        data_map: dict[str, pd.DataFrame] | None = None,
        date: pd.Timestamp | None = None,
    ) -> bool:
        """Handle execute buy for the quantitative backtest system."""
        if signal.target_shares <= 0 or signal.price <= 0:
            return False
        global_cfg = self.cfg
        strategy_cfg = strategy.cfg
        slippage = float(global_cfg.get("slippage", 0.001))
        commission_rate = float(global_cfg.get("commission_rate", 0.00025))
        min_commission = float(global_cfg.get("min_commission", 0.0))
        # Buy slippage worsens the opening price; sell slippage is applied in the
        # opposite direction by _execute_sell.
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
        if signal.symbol not in self.positions and len(self.positions) >= int(
            global_cfg.get("max_positions", 1)
        ):
            return False
        if (
            data_map is not None
            and date is not None
            and self.cfg.get("force_close_on_end", False)
            and (self.global_last_date is not None)
            and (pd.Timestamp(date) == self.global_last_date)
        ):
            return False
        if (
            data_map is not None
            and date is not None
            and self.cfg.get("close_position_on_data_end", True)
            and (self.global_last_date is not None)
            and (signal.symbol in self.symbol_last_dates)
            and (self.symbol_last_dates[signal.symbol] == pd.Timestamp(date))
            and (pd.Timestamp(date) < self.global_last_date)
        ):
            return False
        if signal.atr > 0:
            # Recompute only the size against execution-day equity. The ATR itself
            # is frozen on the signal date to avoid future-data leakage.
            existing_pos = self.positions.get(signal.symbol, {}).get(strategy.name)
            unit_num = existing_pos.units + 1 if existing_pos is not None else 1
            risk_limited_shares = strategy._calc_shares(
                current_assets * float(strategy_cfg.get("strategy_weight", 1.0)),
                exec_price,
                signal.atr,
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
            signal.symbol,
            self.positions,
            current_assets,
            buy_value,
            current_prices,
            position_cfg=strategy_cfg,
        ):
            return False
        if self.risk.check_daily_loss(current_assets):
            return False
        self.cash -= total_cost
        self._apply_buy_to_position(
            signal, strategy, date_str, shares, exec_price, total_cost
        )
        self.trades.append(
            TradeRecord(
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
            )
        )
        return True

    def _execute_sell(
        self, signal: Signal, strategy: BaseStrategy, date_str: str
    ) -> bool:
        """Handle execute sell for the quantitative backtest system."""
        if signal.target_shares <= 0 or signal.price <= 0:
            return False
        pos = None
        if signal.symbol in self.positions:
            pos = self.positions[signal.symbol].get(strategy.name)
        if pos is None:
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
        commission = (
            max(sell_value * commission_rate, min_commission) if sell_value > 0 else 0.0
        )
        stamp_duty_cost = sell_value * stamp_duty
        # PnL uses net proceeds after commission and sell-side stamp duty.
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
        self.trades.append(
            TradeRecord(
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
            )
        )
        return True

    def _generate_liquidation_signals(
        self, date_str: str, reason: str = "circuit breaker liquidation"
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Handle generate liquidation signals for the quantitative backtest system."""
        # These are ordinary pending sell signals. The placeholder price is always
        # replaced by a later tradable opening price before execution.
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
                    price=pos.entry_price,
                    reason=reason,
                    signal_date=date_str,
                )
                signals.append((sig, strategy))
        return signals

    def _liquidate_all(
        self,
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
        reason: str = "period-end settlement",
    ) -> None:
        """Handle liquidate all for the quantitative backtest system."""
        cfg = self.cfg
        slippage = cfg.get("slippage", 0.001)
        commission_rate = cfg.get("commission_rate", 0.00025)
        min_commission = cfg.get("min_commission", 0.0)
        stamp_duty = cfg.get("stamp_duty", 0.0005)
        date_str = date.strftime("%Y-%m-%d")
        liquidated_codes: set[str] = set()
        for code in list(self.positions.keys()):
            if code not in data_map or date not in data_map[code].index:
                continue
            close_price = data_map[code].loc[date, "close"]
            exec_price = close_price * (1 - slippage)
            for strat_name in list(self.positions[code].keys()):
                pos = self.positions[code][strat_name]
                sell_value = pos.shares * exec_price
                commission = (
                    max(sell_value * commission_rate, min_commission)
                    if sell_value > 0
                    else 0.0
                )
                stamp_duty_cost = sell_value * stamp_duty
                net_proceeds = sell_value - commission - stamp_duty_cost
                pnl = net_proceeds - pos.cost
                pnl_pct = pnl / pos.cost if pos.cost > 0 else 0
                peak_close = max(
                    float(pos.highest_close_since_entry), float(pos.entry_price)
                )
                exit_from_peak_pct = (
                    exec_price / peak_close - 1 if peak_close > 0 else 0.0
                )
                self.cash += net_proceeds
                self.trades.append(
                    TradeRecord(
                        symbol=code,
                        strategy_name=strat_name,
                        direction="sell",
                        shares=pos.shares,
                        price=exec_price,
                        date=date_str,
                        reason=reason,
                        pnl=pnl,
                        pnl_pct=pnl_pct,
                        signal_date=date_str,
                        gross_value=sell_value,
                        commission=commission,
                        stamp_duty_cost=stamp_duty_cost,
                        net_cash_flow=net_proceeds,
                        cash_after=self.cash,
                        peak_close=peak_close,
                        exit_from_peak_pct=exit_from_peak_pct,
                    )
                )
                del self.positions[code][strat_name]
            if not self.positions[code]:
                del self.positions[code]
            liquidated_codes.add(code)
        for code in liquidated_codes:
            if code in self.strategy_instances:
                for strategy in self.strategy_instances[code]:
                    strategy.position = None

    def _close_positions_on_data_end(
        self, data_map: dict[str, pd.DataFrame], date: pd.Timestamp
    ) -> set[tuple[str, str]]:
        """Handle close positions on data end for the quantitative backtest system."""
        closed: set[tuple[str, str]] = set()
        if (
            not self.cfg.get("close_position_on_data_end", True)
            or self.global_last_date is None
        ):
            return closed
        for code in list(self.positions.keys()):
            last_date = self.symbol_last_dates.get(code)
            if (
                last_date is None
                or pd.Timestamp(date) != last_date
                or last_date >= self.global_last_date
            ):
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
                    reason="forced settlement at data end",
                    signal_date=date.strftime("%Y-%m-%d"),
                )
                if self._execute_sell(signal, strategy, date.strftime("%Y-%m-%d")):
                    closed.add((code, strat_name))
        return closed

    def _build_result(self, final_assets: float, all_dates: list[pd.Timestamp]) -> dict:
        """Build performance metrics and audited output objects from the equity curve."""
        eq = pd.DataFrame(self.equity_curve)
        if eq.empty:
            return {"error": "No equity data"}
        eq["date"] = pd.to_datetime(eq["date"])
        eq["assets"] = eq["assets"].astype(float)
        eq = eq.set_index("date")
        total_return = (final_assets - self.initial_capital) / self.initial_capital
        n_trading_days = len(all_dates)
        # Annualization and Sharpe consistently use 252 trading days.
        annual_return = (
            (1 + total_return) ** (252 / max(n_trading_days, 1)) - 1
            if total_return > -1
            else -1.0
        )
        # Drawdown is computed from the marked-to-market portfolio equity curve.
        peak = eq["assets"].cummax()
        drawdown = (eq["assets"] - peak) / peak
        max_drawdown = drawdown.min()
        daily_returns = eq["assets"].pct_change().dropna()
        sharpe = 0.0
        if daily_returns.std() > 0:
            rf_annual = float(self.cfg.get("risk_free_rate", 0.0))
            rf_daily = (1 + rf_annual) ** (1 / 252) - 1 if rf_annual > -1 else 0.0
            sharpe = (
                (daily_returns - rf_daily).mean() / daily_returns.std() * math.sqrt(252)
            )
        sell_trades = [t for t in self.trades if t.direction == "sell"]
        exit_givebacks = [
            float(t.exit_from_peak_pct)
            for t in sell_trades
            if _is_finite_number(t.exit_from_peak_pct)
        ]
        wins = [t for t in sell_trades if t.pnl > 0]
        losses = [t for t in sell_trades if t.pnl < 0]
        decisive_trades = len(wins) + len(losses)
        win_rate = len(wins) / decisive_trades if decisive_trades else 0
        total_win = sum((t.pnl for t in wins)) if wins else 0
        total_loss = abs(sum((t.pnl for t in losses))) if losses else 0
        profit_factor = total_win / total_loss if total_loss > 0 else float("inf")
        open_positions = sum(
            (len(sym_positions) for sym_positions in self.positions.values())
        )
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
            "avg_exit_from_peak": float(np.mean(exit_givebacks))
            if exit_givebacks
            else 0.0,
            "worst_exit_from_peak": float(min(exit_givebacks))
            if exit_givebacks
            else 0.0,
            "open_positions": int(open_positions),
            "open_position_value": open_position_value,
            "force_close_on_end": bool(self.cfg.get("force_close_on_end", False)),
            "equity_curve": eq,
            "trades": self.trades,
            "drawdown_series": drawdown,
            "pending_signals": [signal for signal, _ in self.pending_signals],
            "parameter_routes": {
                code: BacktestEngine._SYMBOL_PROFILE.get(
                    code,
                    BacktestEngine.classify_symbol(
                        code, name=self.symbol_names.get(code, "")
                    ),
                )
                for code in self.symbol_names
            },
            "fusion_events": list(self.fusion_events),
            "risk_events": list(self.risk_events),
            "sector_guard_active": bool(self.sector_guard_active),
            "reversal_exit_trades": sum(
                (
                    1
                    for trade in self.trades
                    if trade.direction == "sell" and "reversal" in str(trade.reason)
                )
            ),
        }


class PerformanceReport:
    """Render and persist deterministic backtest results."""

    @staticmethod
    def print_report(result: dict, symbols_dict: dict[str, str]) -> None:
        """Handle print report for the quantitative backtest system."""
        if "error" in result:
            print(f"Backtest failed: {result['error']}")
            return
        print(f"\n{'═' * 60}")
        print("  Codex Quant v14 performance report")
        print(f"{'═' * 60}")
        print(f"  Symbols: {', '.join((f'{v}({k})' for k, v in symbols_dict.items()))}")
        print(f"  Initial capital:   {result['initial_capital']:>15,.0f}")
        print(f"  Final assets:       {result['final_assets']:>15,.0f}")
        print("  ────────────────────────────────")
        print(f"  Total return:   {result['total_return']:>15.2%}")
        print(f"  Annualized return: {result['annual_return']:>15.2%}")
        print(f"  Maximum drawdown:   {result['max_drawdown']:>15.2%}")
        print(f"  Sharpe ratio:   {result['sharpe']:>15.2f}")
        print(f"  Win rate:       {result['win_rate']:>15.2%}")
        pf = result.get("profit_factor")
        pf_str = (
            "N/A (no losing trades)" if math.isinf(float(pf)) else f"{float(pf):.2f}"
        )
        print(f"  Profit factor:     {pf_str:>15}")
        print(f"  Open positions:   {result.get('open_positions', 0):>15d}")
        print(f"  Total trades: {result['total_trades']:>15d}")
        print(f"  Sell trades:   {result['sell_trades']:>15d}")
        print(f"  Average exit giveback:{result.get('avg_exit_from_peak', 0.0):>15.2%}")
        print(f"  Worst exit giveback:{result.get('worst_exit_from_peak', 0.0):>15.2%}")
        print(f"{'═' * 60}\n")
        pending = result.get("pending_signals", [])
        if pending:
            print(
                "  Signals pending for the next trading day (subject to a tradable opening price):"
            )
            for signal in pending:
                print(
                    f"  {signal.signal_date} {symbols_dict.get(signal.symbol, signal.symbol)}({signal.symbol}) {signal.strategy_name} {signal.direction.upper()} {signal.target_shares} shares | {signal.fusion_label} | {signal.reason}"
                )
        trades = result.get("trades", [])
        if trades:
            print("  Trade details (latest 20):")
            print(
                f"  {'Date':<12} {'Symbol':<8} {'Strategy':<20} {'Side':<6} {'Shares':>8} {'Price':>10} {'PnL':>12} {'Reason'}"
            )
            print(f"  {'─' * 100}")
            for t in trades[-20:]:
                pnl_str = f"{t.pnl:>+10,.0f}" if t.direction == "sell" else ""
                print(
                    f"  {t.date:<12} {t.symbol:<8} {t.strategy_name:<20} {t.direction:<6} {t.shares:>8} {t.price:>10.2f} {pnl_str}   {t.reason}"
                )

    @staticmethod
    def save_result(result: dict, output_dir: str) -> None:
        """Handle save result for the quantitative backtest system."""
        if "error" in result:
            raise ValueError(f"Cannot save a failed backtest result: {result['error']}")
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
            "initial_capital",
            "final_assets",
            "total_return",
            "annual_return",
            "max_drawdown",
            "sharpe",
            "win_rate",
            "profit_factor",
            "total_trades",
            "sell_trades",
            "open_positions",
            "reversal_exit_trades",
            "avg_exit_from_peak",
            "worst_exit_from_peak",
        ]
        pd.DataFrame([{k: result.get(k) for k in summary_keys}]).to_csv(
            out / "summary.csv", index=False, encoding="utf-8-sig"
        )

    @staticmethod
    def plot_equity_curve(result: dict, save_path: str = "equity_curve.png") -> None:
        """Handle plot equity curve for the quantitative backtest system."""
        if "error" in result:
            print(f"Backtest failed; cannot plot: {result['error']}")
            return
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.font_manager import FontProperties, fontManager

        zh_font_path = None
        preferred_fonts = ("noto sans cjk", "droid sans fallback", "source han sans")
        for font in fontManager.ttflist:
            if any(name in font.name.lower() for name in preferred_fonts):
                zh_font_path = font.fname
                break
        if zh_font_path:
            fontManager.addfont(zh_font_path)
            zh_font_name = FontProperties(fname=zh_font_path).get_name()
            plt.rcParams["font.family"] = ["DejaVu Sans", zh_font_name]
            plt.rcParams["axes.unicode_minus"] = False
        eq = result["equity_curve"]
        dd = result["drawdown_series"]
        fig, axes = plt.subplots(
            2, 1, figsize=(14, 8), gridspec_kw={"height_ratios": [3, 1]}
        )
        axes[0].plot(eq.index, eq["assets"] / 10000, linewidth=1.5, color="#1a73e8")
        axes[0].set_title("AQuant Portfolio Equity Curve", fontsize=14)
        axes[0].set_ylabel("Assets (CNY 10k)")
        axes[0].grid(True, alpha=0.3)
        axes[0].axhline(
            y=result["initial_capital"] / 10000, color="gray", linestyle="--", alpha=0.5
        )
        axes[1].fill_between(dd.index, dd * 100, 0, color="#dc3545", alpha=0.4)
        axes[1].set_title("Drawdown (%)", fontsize=12)
        axes[1].set_ylabel("Drawdown %")
        axes[1].grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig(save_path, dpi=150, bbox_inches="tight")
        plt.close()
        print(f"\n  Equity curve saved: {save_path}")


DEFAULT_SYMBOLS = {
    "300308": "中际旭创",
    "300502": "新易盛",
    "300394": "天孚通信",
    "688008": "澜起科技",
    "603986": "兆易创新",
}
SYMBOL_NAME_TABLE: dict[str, str] = {
    **DEFAULT_SYMBOLS,
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
DEFAULT_SYMBOL_NAMES = {v: k for k, v in SYMBOL_NAME_TABLE.items()}


def parse_symbols(symbols_str: str) -> dict[str, str]:
    """Handle parse symbols for the quantitative backtest system."""
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
            result[s] = SYMBOL_NAME_TABLE.get(s, s)
        else:
            raise ValueError(
                f"Invalid stock code or name: '{s}' (use a six-digit code or a preset name)"
            )
    return result


def main() -> dict | None:
    """Parse command-line arguments and run the requested backtest."""
    parser = argparse.ArgumentParser(
        description="Codex Quant Fusion v14 A-share backtester"
    )
    sub = parser.add_subparsers(dest="command")
    bt = sub.add_parser("backtest", help="Run backtest")
    bt.add_argument(
        "--symbol",
        "-s",
        default=",".join(DEFAULT_SYMBOLS),
        help="Symbol code(comma-separated)",
    )
    bt.add_argument("--start", default="2025-01-01", help="Start date")
    bt.add_argument("--end", default="2026-06-30", help="End date")
    bt.add_argument("--capital", type=float, default=2000000, help="Initial capital")
    bt.add_argument(
        "--profile",
        default="default",
        choices=["default", "semiconductor", "semiconductor_heavy", "aggressive"],
        help="Global profile: default / semiconductor(wide parameters and light exposure) / semiconductor_heavy(wide parameters and heavy exposure) / aggressive(aggressive preset)",
    )
    bt.add_argument(
        "--config-route",
        default="auto",
        choices=["auto", "none"],
        help="Parameter routing: auto(automatic industry routing, default) / none(use the global profile for every symbol)",
    )
    bt.add_argument(
        "--data-dir",
        default="",
        help="Local forward-adjusted CSV directory; leave empty to use AKShare",
    )
    bt.add_argument(
        "--save-dir",
        default="",
        help="Save equity, trades, drawdown, and latest-signal CSV files",
    )
    bt.add_argument(
        "--no-plot", action="store_true", help="Do not generate an equity-curve PNG"
    )
    args = parser.parse_args()
    if args.command == "backtest":
        symbols_dict = parse_symbols(args.symbol)
        engine = BacktestEngine(initial_capital=args.capital)
        result = engine.run(
            symbols_dict,
            args.start,
            args.end,
            profile=args.profile,
            config_route=args.config_route,
            data_dir=args.data_dir or None,
        )
        profile_desc = (
            args.profile
            if args.config_route == "none"
            else f"auto-route({args.profile}baseline)"
        )
        print(f"  [Configuration ] Parameter mode: {profile_desc}")
        PerformanceReport.print_report(result, symbols_dict)
        if args.save_dir:
            PerformanceReport.save_result(result, args.save_dir)
        if not args.no_plot:
            PerformanceReport.plot_equity_curve(
                result, f"equity_curve_{args.profile}_{args.config_route}.png"
            )
        return result


if __name__ == "__main__":
    main()
