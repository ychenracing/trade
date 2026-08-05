#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Quant Fusion: a standalone A-share technology trend system.

This single module contains the complete data, indicator, signal, execution,
portfolio-allocation, and risk-control implementation. It has no runtime
or import dependency on any other module.

When ``data_dir`` is provided, the engine reads forward-adjusted daily CSV
files from that directory. Otherwise it fetches forward-adjusted data through
AKShare with deterministic Eastmoney, Sina, and Tencent provider failover.
Signals are formed after the close and execute no earlier than a later tradable
open. Historical simulations are not guarantees of future performance.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import re
import time
from dataclasses import dataclass, field, replace
from itertools import pairwise
from pathlib import Path
from typing import Any, ClassVar, cast

import numpy as np
import pandas as pd

try:
    import akshare as ak
except ImportError:
    ak = None

# Core data, signals, execution, and portfolio accounting.

REQUIRED_OHLC_COLUMNS = ("open", "close", "high", "low")
OPTIONAL_COLUMNS = ("volume",)
A_SHARE_LOT_SIZE = 100
_SYMBOL_RE = re.compile("^\\d{6}$")
EXECUTION_PRIORITY = {
    code: rank
    for rank, code in enumerate(
        (
            "300308",
            "688256",
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
    """Return whether value is a finite real number."""
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
    """Validate and normalize one bounded finite value."""
    if not _is_finite_number(value):
        raise ValueError(
            f"Configuration {name} must be finite; current value is {value!r}"
        )
    value = float(value)
    if min_value is not None and value < min_value:
        raise ValueError(
            f"Configuration {name} must be >= {min_value}; current value is {value}"
        )
    if max_value is not None:
        if inclusive_max and value > max_value:
            raise ValueError(
                f"Configuration {name} must be <= {max_value}; current value is {value}"
            )
        if not inclusive_max and value >= max_value:
            raise ValueError(
                f"Configuration {name} must be < {max_value}; current value is {value}"
            )
    return value


def _require_positive(
    name: str, value: Any, *, max_value: float | None = None, inclusive_max: bool = True
) -> float:
    """Validate a positive value with an optional upper bound."""
    value = _require_finite(
        name, value, max_value=max_value, inclusive_max=inclusive_max
    )
    if value <= 0:
        raise ValueError(f"Configuration {name} must be > 0; current value is {value}")
    return value


def _require_bool(name: str, value: Any) -> bool:
    """Reject truthy substitutes and return an actual Boolean."""
    if not isinstance(value, bool):
        raise ValueError(
            f"Configuration {name} must be bool; current value is {value!r}"
        )
    return value


def _require_int(name: str, value: Any, *, min_value: int = 0) -> int:
    """Validate an integer without accepting booleans or fractions."""
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise ValueError(
            f"Configuration {name} must be an integer; current value is {value!r}"
        )
    value = int(value)
    if value < min_value:
        raise ValueError(
            f"Configuration {name} must be >= {min_value}; current value is {value}"
        )
    return value


def _floor_to_lot(shares: float, lot_size: int = A_SHARE_LOT_SIZE) -> int:
    """Round a finite positive share count down to a board lot."""
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
    """Resolve the estimated daily board limit for a symbol."""
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
    """Parse exchange dates without interpreting YYYYMMDD as nanoseconds."""
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

    _COLUMN_ALIASES: ClassVar[dict[str, str]] = {
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


    _cache_dir: str | None = None
    _PROVIDER_VOLUME_UNITS: ClassVar[dict[str, str]] = {
        "Eastmoney": "lots",
        "Sina": "shares",
        "Tencent": "lots",
    }
    _CACHE_SCHEMA_VERSION = 1

    @staticmethod
    def _normalize_provider_volume(
        frame: pd.DataFrame, provider_name: str
    ) -> pd.DataFrame:
        """Convert every online provider's volume field to shares.

        Eastmoney and the Tencent k-line endpoint report A-share volume in
        board lots, while Sina reports shares. The execution engine's ADV
        participation limit is defined in shares, so provider output must be
        normalized before the common OHLCV validator runs.
        """
        if provider_name not in DataFetcher._PROVIDER_VOLUME_UNITS:
            raise ValueError(f"Unknown market-data provider: {provider_name}")
        out = frame.copy()
        normalized = DataFetcher._normalized_column_names(out.columns)
        volume_positions = [i for i, name in enumerate(normalized) if name == "volume"]
        if len(volume_positions) != 1:
            raise ValueError(
                f"{provider_name} response must contain exactly one volume column"
            )
        column = out.columns[volume_positions[0]]
        volume = pd.to_numeric(out[column], errors="coerce")
        if volume.isna().any() or (volume < 0).any():
            raise ValueError(f"{provider_name} returned invalid volume values")
        if DataFetcher._PROVIDER_VOLUME_UNITS[provider_name] == "lots":
            volume = volume * A_SHARE_LOT_SIZE
        out[column] = volume.astype(float)
        out.attrs["volume_unit"] = "shares"
        out.attrs["volume_provider"] = provider_name
        return out

    @staticmethod
    def _cache_contract_path(cache_path: Path) -> Path:
        return cache_path.with_suffix(cache_path.suffix + ".meta.json")

    @staticmethod
    def _cache_has_share_volume_contract(cache_path: Path) -> bool:
        """Accept only caches that explicitly declare share-based volume."""
        meta_path = DataFetcher._cache_contract_path(cache_path)
        if not meta_path.is_file():
            return False
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return (
            payload.get("schema_version") == DataFetcher._CACHE_SCHEMA_VERSION
            and payload.get("volume_unit") == "shares"
        )

    @staticmethod
    def _write_cache_contract(cache_path: Path) -> None:
        """Persist the unit contract next to an atomically replaceable cache."""
        meta_path = DataFetcher._cache_contract_path(cache_path)
        meta_path.write_text(
            json.dumps(
                {
                    "schema_version": DataFetcher._CACHE_SCHEMA_VERSION,
                    "volume_unit": "shares",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

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
        url = (
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
            f"param={exchange_symbol},day,{start_date},{end_date},1000,qfq"
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
                        frame = DataFetcher._normalize_provider_volume(
                            frame, provider_name
                        )
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
        """Load validated qfq OHLCV data from CSV or provider failover."""
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
        if DataFetcher._cache_dir:
            return DataFetcher._load_with_cache(symbol, start_date, end_date)
        return DataFetcher.fetch_stock_data(symbol, start_date, end_date)

    @staticmethod
    def _load_with_cache(
        symbol: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """Hybrid mode: load local cache, fetch only incremental data from network."""
        import sys

        def _log(msg: str) -> None:
            print(msg, file=sys.stderr, flush=True)

        cache_path = Path(DataFetcher._cache_dir).expanduser() / f"{symbol}.csv"  # type: ignore[arg-type]
        start_ts = pd.Timestamp(start_date)
        end_ts = pd.Timestamp(end_date)
        if cache_path.is_file() and DataFetcher._cache_has_share_volume_contract(cache_path):
            cached = DataFetcher._normalize_columns(pd.read_csv(cache_path))
            last_cached = cached.index[-1]
            fetch_start = (last_cached + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            if fetch_start > end_date:
                _log(
                    f"  [Cache] {symbol}: cache up to date "
                    f"({last_cached.strftime('%Y-%m-%d')}), no fetch needed"
                )
                combined = cached
            else:
                _log(
                    f"  [Cache] {symbol}: cache to {last_cached.strftime('%Y-%m-%d')}, "
                    f"fetching {fetch_start} ~ {end_date}"
                )
                try:
                    new_data = DataFetcher.fetch_stock_data(
                        symbol, fetch_start, end_date
                    )
                    combined = pd.concat([cached, new_data])
                    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
                except RuntimeError as exc:
                    _log(
                        f"  [Cache] {symbol}: incremental fetch failed ({exc}); "
                        f"using cached data only (latest day may be missing)"
                    )
                    combined = cached
                    # Mark the data as stale so callers can refuse to produce
                    # signals that look "latest" but are actually from cache.
                    combined.attrs["_stale"] = True
                    combined.attrs["_cache_last_date"] = str(last_cached.date())
                else:
                    combined.attrs["_stale"] = False
            combined.to_csv(cache_path)
            DataFetcher._write_cache_contract(cache_path)
            return combined[(combined.index >= start_ts) & (combined.index <= end_ts)].copy()
        if cache_path.is_file():
            _log(
                f"  [Cache] {symbol}: legacy cache lacks a verified share-volume "
                "contract; rebuilding from providers"
            )
        # No valid cache file: full fetch + save
        _log(f"  [Cache] {symbol}: no cache file, full fetch {start_date} ~ {end_date}")
        df = DataFetcher.fetch_stock_data(symbol, start_date, end_date)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(cache_path)
        DataFetcher._write_cache_contract(cache_path)
        return df

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
        normalized = out[["open", "close", "high", "low", "volume"]].copy()
        normalized.attrs.update(getattr(df, "attrs", {}))
        normalized.attrs.setdefault("volume_unit", "shares")
        return normalized

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
        """Compute Wilder smoothing after one complete non-missing seed."""
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
        """Compute average true range with Wilder or simple smoothing."""
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
        """Compute average directional index from OHLC data."""
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
        """Compute Wilder RSI, mapping unchanged windows to neutral."""
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
        """Return lagged Donchian bands without look-ahead."""
        entry_period = _require_int("entry_period", entry_period, min_value=1)
        exit_period = _require_int("exit_period", exit_period, min_value=1)
        # The one-bar shift is essential: today's high and low must not influence
        # a breakout decision made at today's close.
        upper = df["high"].rolling(entry_period).max().shift(1)
        lower = df["low"].rolling(exit_period).min().shift(1)
        return (upper, lower)

    @staticmethod
    def ma(series: pd.Series, period: int) -> pd.Series:
        """Return a full-window simple moving average."""
        period = _require_int("period", period, min_value=1)
        return series.rolling(period).mean()

    @staticmethod
    def hurst_rs(series: pd.Series, window: int = 100) -> float:
        """Estimate the Hurst exponent via rescaled-range (R/S) analysis.

        The function is a pure transformation: it consumes the most recent
        ``window`` observations of the supplied series (callers should pass a
        stationary transformation such as log returns for price data) and
        regresses ``log(R/S)`` on ``log(n)`` across several sub-period sizes.
        Values near 0.5 indicate a random walk, above 0.5 persistence
        (trending), and below 0.5 anti-persistence (mean-reversion). A finite
        result requires at least ``window`` non-missing observations.
        """
        period = _require_int("window", window, min_value=10)
        values = (
            pd.Series(pd.to_numeric(series, errors="coerce"))
            .dropna()
            .to_numpy(dtype=float)
        )
        if values.size < period:
            return float("nan")
        segment = values[-period:]
        n = segment.size
        # Build a stable set of sub-period sizes that divide the window so the
        # log-log regression has at least two support points.
        sizes: list[int] = []
        size = 4
        while size <= n // 2:
            sizes.append(size)
            size *= 2
        for divisor in (2, 3, 5):
            candidate = n // divisor
            if candidate >= 4 and candidate not in sizes:
                sizes.append(candidate)
        sizes = sorted(set(sizes))
        if len(sizes) < 2:
            return float("nan")
        log_sizes: list[float] = []
        log_rs: list[float] = []
        for size in sizes:
            blocks = n // size
            if blocks < 1:
                continue
            rs_values: list[float] = []
            for block in range(blocks):
                sub = segment[block * size : (block + 1) * size]
                mean = float(np.mean(sub))
                deviation = np.cumsum(sub - mean)
                r = float(np.max(deviation) - np.min(deviation))
                std = float(np.std(sub, ddof=0))
                if std > 0.0 and r > 0.0:
                    rs_values.append(r / std)
            if rs_values:
                log_sizes.append(math.log(size))
                log_rs.append(math.log(float(np.mean(rs_values))))
        if len(log_sizes) < 2:
            return float("nan")
        slope = float(np.polyfit(np.array(log_sizes), np.array(log_rs), 1)[0])
        if not math.isfinite(slope):
            return float("nan")
        return max(0.0, min(1.0, slope))

    @staticmethod
    def compute_all(df: pd.DataFrame, cfg: dict) -> dict[str, pd.Series]:
        """Precompute every indicator consumed by the strategies."""
        atr_period = cfg.get("atr_period", 20)
        adx_period = cfg.get("adx_period", 14)
        rsi_period = cfg.get("rsi_period", 14)
        entry_p = cfg.get("entry_period", 20)
        exit_p = cfg.get("exit_period", 10)
        ma_short = cfg.get("ma_short", 20)
        ma_long = cfg.get("ma_long", 60)
        reversal_exit_period = int(cfg.get("reversal_exit_period", 6))
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
            # The reversal floor is lagged: today's low cannot affect today's
            # close-generated exit signal.
            "reversal_low": (df["low"].rolling(reversal_exit_period).min().shift(1)),
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
        """Return the remaining position's fee-inclusive cost basis."""
        return self.shares * self.entry_price

    def market_value_at(self, price: float) -> float:
        """Mark the position at the supplied price."""
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


@dataclass(frozen=True)
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


@dataclass
class AccountState:
    """Real portfolio state from a live account snapshot.

    Used by the daily signal scanner to derive correct action labels
    without relying on a simulated replay from scratch.
    """
    cash: float
    position_value: float
    total_equity: float
    peak_equity: float
    positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    risk_state: dict[str, Any] = field(default_factory=dict)


@dataclass
class EngineState:
    """Cross-day state that survives between successive daily runs.

    Mirrors the fields that BacktestEngine.run() serialises into
    risk_state.json.
    """
    terminal_risk_lock: bool = False
    sector_guard_active: bool = False
    cycle_lock_count: int = 0
    persistent_risk_lock: bool = False
    run_id: str = ""


@dataclass(frozen=True)
class MarketRegimeObservation:
    """Snapshot the five basket-level regime indicators at one close.

    Each field is computed from the fixed ``regime_symbols`` basket using only
    data on or before the scored date. ``raw_score`` sums the per-indicator
    votes (+1 trend / -1 choppy / 0 neutral) and ``candidate_state`` is the
    unconfirmed regime implied by that score before the state machine applies
    its confirmation and minimum-hold gates.
    """

    ewi_slope: float
    breadth_above_ma: float
    adx_median: float
    hurst: float
    volatility_percentile: float
    raw_score: int
    candidate_state: str


class BaseStrategy:
    """Define shared sizing, signal construction, and reversal protection."""

    name: str = "base"

    def __init__(self, cfg: dict) -> None:
        """Bind one validated symbol configuration to the strategy."""
        self.cfg = cfg
        self.position: Position | None = None

    def on_bar(self, ctx: BarContext) -> Signal | None:
        """Generate a close-based signal for one bar."""
        raise NotImplementedError

    def _calc_shares(
        self, capital: float, price: float, atr_val: float, unit_number: int = 1
    ) -> int:
        """Convert a stop-distance risk budget into board-lot shares."""
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
        """Create an immutable buy instruction from current close data."""
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
        """Create a full-position sell instruction from current close data."""
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
                reversal_low = ctx.indicators.get("reversal_low")
                prior_low = reversal_low.iloc[i] if reversal_low is not None else np.nan
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
        """Generate Donchian breakout, pyramid, stop, or exit signals."""
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
        """Generate dual-moving-average trend signals."""
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
        """Generate ATR-channel breakout and risk-exit signals."""
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
        """Initialize shared daily-loss and exposure controls."""
        self.cfg = cfg
        self.peak_assets: float = 0.0
        self.daily_start_assets: float = 0.0
        self.symbol_groups: dict[str, str] = {}
        self.group_weight_limits: dict[str, float] = {}

    def configure_groups(self, symbol_groups: dict[str, str]) -> None:
        """Enable sector caps only when multiple groups are tradable."""
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
        """Require a concrete persistent or recoverable drawdown policy."""
        del current_assets, date_str, trading_dates, date_to_pos
        raise NotImplementedError("RiskManager requires a concrete drawdown policy")

    def check_daily_loss(self, current_assets: float) -> bool:
        """Return whether close-to-close portfolio loss reached its limit."""
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
        """Check symbol, sector, and total exposure before a buy."""
        if current_assets <= 0:
            return False
        if current_prices is not None:
            for sym in positions:
                price = current_prices.get(sym)
                if price is None or not _is_finite_number(price) or price <= 0:
                    return False

        def _mark(sym: str, pos: Position) -> float:
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
        return (total_position_value + buy_value) / current_assets <= self.cfg.get(
            "max_total_weight", 0.95
        )


class _CoreBacktestEngine:
    """Run a shared-cash, multi-symbol, multi-strategy T+1 backtest."""

    ENGINE_LABEL = "Quant Fusion"

    def _display_run_period(self, start_date: str, end_date: str) -> tuple[str, str]:
        """Return the user-facing trading period shown in the run header."""
        return start_date, end_date

    def __init__(
        self, initial_capital: float = 2000000, cfg: dict | None = None
    ) -> None:
        """Initialize a reusable engine with validated immutable inputs."""
        self.initial_capital = _require_finite(
            "initial_capital", initial_capital, min_value=0.01
        )
        self._user_cfg = dict(cfg or {})
        self.cfg = self._validate_config({**self._default_config(), **self._user_cfg})
        self.cash = self.initial_capital
        self.positions: dict[str, dict[str, Position]] = {}
        self._initial_positions: dict[str, dict[str, Position]] = {}
        self._initial_cash: float | None = None
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
        self._safe_mode_active: bool = False  # audit-only flag; no dynamic parameter changes
        self._sector_shock_positions: list[int] = []
        self._sector_recovery_streak = 0
        self.strategy_templates: list[type[BaseStrategy]] = [
            TurtleBreakoutStrategy,
            DualMAStrategy,
            ATRChannelStrategy,
        ]

    @staticmethod
    def _default_config() -> dict:
        """Return the complete auditable strategy and execution defaults."""
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
            # A sell is a symbol-level risk veto by default: it suppresses every
            # same-symbol buy, including another strategy's stale pending order.
            "symbol_level_sell_veto": True,
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
            "strict_unmapped": True,  # fail-closed: unmapped symbols raise instead of silently using default
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
            "risk_free_rate": 0.0,
            # Market regime recognition: a fixed-basket state machine that gates
            # new entries (CHOPPY) and scales sizes (TRANSITION) before signals.
            # Enabled by default with conservative parameters: the state machine
            # only intervenes after multi-day confirmation of a genuine regime
            # shift, so TREND periods are fully open (zero-cost).  The volatility
            # fast-path was removed because vol_pct=1.0 during V-shaped corrections
            # blocked trend entries and reduced returns.
            "market_regime_enabled": True,
            "regime_ewi_lookback": 20,
            "regime_breadth_ma_long": 20,
            "regime_adx_trend": 25,
            "regime_adx_choppy": 20,
            "regime_hurst_window": 100,
            "regime_hurst_trend": 0.55,
            "regime_hurst_choppy": 0.45,
            "regime_vol_lookback": 60,
            "regime_vol_extreme_pct": 0.9,
            "regime_ewi_slope_trend": 0.02,
            "regime_ewi_slope_choppy": -0.02,
            "regime_score_trend": 2,
            "regime_score_choppy": -3,
            "regime_choppy_confirmations": 2,
            "regime_trend_confirmations": 3,
            "regime_recovery_confirmations": 3,
            "regime_min_state_hold": 3,
            "regime_transition_scale": 1.0,
            "regime_trend_to_transition_confirmations": 3,
            "regime_choppy_exit_ratio": 0.3,
            "regime_transition_exit_ratio": 0.0,
            # 穿越牛熊 (cross-market-cycle) overlay: a bull-silent defensive layer
            # on top of the ensemble. Default ON; only fires on genuine risk so a
            # clean bull run is untouched. shock_trim is opt-in (the ensemble
            # already carries regime de-risking + drawdown circuit breakers).
            "enable_cm_overlay": True,
            "cm_overlay_shock_trim": False,
        }

    _PER_SYMBOL_OVERRIDE_KEYS: ClassVar[set[str]] = {
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
        """Return the high-turnover profile."""
        cfg = _CoreBacktestEngine._default_config()
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
                "momentum_lookback": 10,
                "max_positions": 2,
            }
        )
        return cfg

    @staticmethod
    def semiconductor_config() -> dict:
        """Return the broad semiconductor trend profile."""
        cfg = _CoreBacktestEngine._default_config()
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
        """Return the higher-risk semiconductor trend profile."""
        cfg = _CoreBacktestEngine.semiconductor_config()
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
        """Return the overseas-memory material profile."""
        cfg = _CoreBacktestEngine._default_config()
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
        """Return the domestic chip-design profile."""
        cfg = _CoreBacktestEngine.semiconductor_config()
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
        """Return the domestic semiconductor-material profile."""
        cfg = _CoreBacktestEngine.semiconductor_config()
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
        """Return the domestic foundry and equipment profile."""
        cfg = _CoreBacktestEngine.semiconductor_config()
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

    _KNOWN_CLASSIFICATION: ClassVar[dict[str, str]] = {
        "300308": "default",
        "300502": "default",
        "300394": "default",
        "688205": "default",
        "920045": "default",
        "688008": "default",
        "002409": "default",
        "688300": "default",
        "688498": "default",
        "002281": "default",
        "601869": "default",
        "688256": "semiconductor",
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
        "300223": "semiconductor",
        "688825": "semiconductor",
        "688041": "semiconductor",
        "002371": "semiconductor",
        "688012": "semiconductor",
        "688037": "semiconductor",
        "688019": "semiconductor",
        "688268": "semiconductor",
    }
    _SYMBOL_GROUP: ClassVar[dict[str, str]] = {
        "300308": "overseas_compute",
        "300502": "overseas_compute",
        "300394": "overseas_compute",
        "688205": "overseas_compute",
        "920045": "overseas_compute",
        "688008": "overseas_compute",
        "002409": "overseas_compute",
        "688300": "overseas_compute",
        "688498": "overseas_compute",
        "002281": "overseas_compute",
        "601869": "overseas_compute",
        "688256": "domestic_semiconductor",
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
        "300223": "domestic_semiconductor",
        "688825": "domestic_semiconductor",
        "688041": "domestic_semiconductor",
        "002371": "domestic_semiconductor",
        "688012": "domestic_semiconductor",
        "688037": "domestic_semiconductor",
        "688019": "domestic_semiconductor",
        "688268": "domestic_semiconductor",
    }
    _SYMBOL_PROFILE: ClassVar[dict[str, str]] = {
        "300308": "overseas_optical",
        "300502": "overseas_optical",
        "300394": "overseas_optical",
        "688205": "overseas_optical",
        "920045": "overseas_optical",
        "688008": "overseas_memory_material",
        "002409": "overseas_memory_material",
        "688300": "overseas_memory_material",
        "688498": "overseas_optical",
        "002281": "overseas_optical",
        "601869": "overseas_optical",
        "688256": "domestic_design",
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
        "300223": "domestic_design",
        "688825": "domestic_foundry",
        "688041": "domestic_design",
        "002371": "domestic_equipment",
        "688012": "domestic_equipment",
        "688037": "domestic_equipment",
        "688019": "domestic_material",
        "688268": "domestic_material",
    }

    @classmethod
    def get_symbol_classification(cls, code: str, default: str = "N/A") -> str:
        """Return the classification for a symbol (public accessor)."""
        return cls._KNOWN_CLASSIFICATION.get(code, default)

    @classmethod
    def get_symbol_group(cls, code: str, default: str = "N/A") -> str:
        """Return the industry group for a symbol (public accessor)."""
        return cls._SYMBOL_GROUP.get(code, default)

    @classmethod
    def get_symbol_profile(cls, code: str, default: str = "N/A") -> str:
        """Return the parameter profile for a symbol (public accessor)."""
        return cls._SYMBOL_PROFILE.get(code, default)

    @staticmethod
    def config_for_symbol(code: str, name: str = "") -> dict:
        """Resolve the built-in parameter profile for a symbol."""
        profile = _CoreBacktestEngine._SYMBOL_PROFILE.get(code)
        if profile == "overseas_memory_material":
            return _CoreBacktestEngine.overseas_memory_material_config()
        if profile == "domestic_design":
            return _CoreBacktestEngine.domestic_design_config()
        if profile == "domestic_material":
            return _CoreBacktestEngine.domestic_material_config()
        if profile == "domestic_foundry":
            return _CoreBacktestEngine.domestic_foundry_config()
        if profile == "domestic_equipment":
            cfg = _CoreBacktestEngine.semiconductor_config()
            cfg["max_symbol_weight"] = 0.45
            return cfg
        if profile == "overseas_optical":
            return _CoreBacktestEngine._default_config()
        return (
            _CoreBacktestEngine.semiconductor_config()
            if _CoreBacktestEngine.classify_symbol(code, name=name) == "semiconductor"
            else _CoreBacktestEngine._default_config()
        )

    _INDUSTRY_HINTS: ClassVar[dict[str, str]] = {
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
        """Infer a broad route from explicit code and name hints."""
        candidates = " ".join((str(x) for x in (code, name) if x))
        for key, cls in _CoreBacktestEngine._INDUSTRY_HINTS.items():
            if key in candidates:
                return cls
        return None

    @staticmethod
    def _uses_unmapped_auto_route(code: str, name: str = "") -> bool:
        """Return whether auto routing must fall back without explicit metadata."""
        return (
            code not in _CoreBacktestEngine._SYMBOL_PROFILE
            and _CoreBacktestEngine._classify_by_industry_hints(code, name) is None
        )

    @staticmethod
    def classify_symbol(
        code: str,
        df: pd.DataFrame | None = None,
        name: str = "",
        lookback_start: str = "",
        lookback_end: str | None = None,
    ) -> str:
        """Classify a symbol without network-dependent industry lookups."""
        known = _CoreBacktestEngine._KNOWN_CLASSIFICATION.get(code)
        if known:
            return known
        hint = _CoreBacktestEngine._classify_by_industry_hints(code, name)
        if hint:
            return hint
        return "default"

    @staticmethod
    def _validate_config(cfg: dict) -> dict:
        """Validate one complete engine configuration and normalize containers."""
        out = dict(cfg)
        allowed_keys = set(_CoreBacktestEngine._default_config().keys())
        unknown_keys = sorted(set(out) - allowed_keys)
        if unknown_keys:
            raise ValueError(
                f"Configuration contains unknown fields; check for typos: {unknown_keys}"
            )
        _CoreBacktestEngine._validate_integer_config(out)
        _CoreBacktestEngine._validate_numeric_config(out)
        _CoreBacktestEngine._validate_boolean_config(out)
        _CoreBacktestEngine._validate_container_config(out)
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
            "max_pending_buy_days": 1,
            "group_min_slots": 0,
            "reversal_exit_period": 2,
            "sector_shock_ma": 2,
            "sector_shock_window": 2,
            "sector_shock_confirmations": 1,
            "sector_recovery_ma": 2,
            "sector_recovery_confirmations": 1,
            "sector_guard_min_symbols": 1,
            "regime_ewi_lookback": 2,
            "regime_breadth_ma_long": 1,
            "regime_hurst_window": 10,
            "regime_vol_lookback": 2,
            "regime_score_trend": -10,
            "regime_score_choppy": -10,
            "regime_choppy_confirmations": 1,
            "regime_trend_confirmations": 1,
            "regime_recovery_confirmations": 1,
            "regime_min_state_hold": 1,
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
        # Market regime numeric thresholds: trend thresholds must sit above
        # their choppy counterparts so the scoring votes are well ordered.
        out["regime_adx_trend"] = _require_finite(
            "regime_adx_trend", out.get("regime_adx_trend"), min_value=0.0
        )
        out["regime_adx_choppy"] = _require_finite(
            "regime_adx_choppy", out.get("regime_adx_choppy"), min_value=0.0
        )
        out["regime_hurst_trend"] = _require_finite(
            "regime_hurst_trend",
            out.get("regime_hurst_trend"),
            min_value=0.0,
            max_value=1.0,
        )
        out["regime_hurst_choppy"] = _require_finite(
            "regime_hurst_choppy",
            out.get("regime_hurst_choppy"),
            min_value=0.0,
            max_value=1.0,
        )
        out["regime_vol_extreme_pct"] = _require_positive(
            "regime_vol_extreme_pct",
            out.get("regime_vol_extreme_pct"),
            max_value=1.0,
            inclusive_max=True,
        )
        out["regime_ewi_slope_trend"] = _require_finite(
            "regime_ewi_slope_trend",
            out.get("regime_ewi_slope_trend"),
            min_value=-1.0,
            max_value=1.0,
        )
        out["regime_ewi_slope_choppy"] = _require_finite(
            "regime_ewi_slope_choppy",
            out.get("regime_ewi_slope_choppy"),
            min_value=-1.0,
            max_value=1.0,
        )
        out["regime_transition_scale"] = _require_finite(
            "regime_transition_scale",
            out.get("regime_transition_scale"),
            min_value=0.0,
            max_value=1.0,
        )
        if out["regime_adx_trend"] <= out["regime_adx_choppy"]:
            raise ValueError("regime_adx_trend must be greater than regime_adx_choppy")
        if out["regime_hurst_trend"] <= out["regime_hurst_choppy"]:
            raise ValueError(
                "regime_hurst_trend must be greater than regime_hurst_choppy"
            )
        if out["regime_ewi_slope_trend"] <= out["regime_ewi_slope_choppy"]:
            raise ValueError(
                "regime_ewi_slope_trend must be greater than regime_ewi_slope_choppy"
            )

    @staticmethod
    def _validate_boolean_config(out: dict) -> None:
        """Reject truthy strings and integers for every Boolean option."""
        boolean_keys = (
            "liquidate_on_circuit_breaker",
            "sector_guard_enabled",
            "strict_unmapped",
            "symbol_level_sell_veto",
            "reversal_turtle_enabled",
            "reversal_dual_ma_enabled",
            "reversal_atr_channel_enabled",
            "market_regime_enabled",
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
        """Return the identity used to deduplicate pending instructions."""
        return (signal.symbol, signal.strategy_name, signal.direction)

    def _pending_has_buy(
        self, pending: list[tuple[Signal, BaseStrategy]], code: str, strategy_name: str
    ) -> bool:
        """Check for a pending buy from the same symbol and strategy."""
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
        """Check for a pending sell from the same symbol and strategy."""
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
        """Keep the newest instruction per symbol, strategy, and side."""
        result: dict[tuple[str, str, str], tuple[Signal, BaseStrategy]] = {}
        for signal, strategy in pending:
            if signal.direction not in {"buy", "sell"}:
                continue
            key = _CoreBacktestEngine._signal_key(signal)
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
                    label = (
                        "conflict: sell takes priority" if conflict else "exit signal"
                    )
                    reason = f"[{label}] {signal.reason}" if conflict else signal.reason
                    fused.append(
                        (
                            replace(
                                signal,
                                fusion_votes=len(sells),
                                fusion_label=label,
                                reason=reason,
                            ),
                            strategy,
                        )
                    )
                self.fusion_events.append(
                    {
                        "date": date_str,
                        "symbol": symbol,
                        "state": "conflict_sell_first" if conflict else "sell",
                        "buy_votes": len(buys),
                        "sell_votes": len(sells),
                    }
                )
                if bool(self.cfg["symbol_level_sell_veto"]):
                    continue
                selling_strategies = {signal.strategy_name for signal, _ in sells}
                buys = [
                    item
                    for item in buys
                    if item[0].strategy_name not in selling_strategies
                ]
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
                target_shares = _floor_to_lot(signal.target_shares * scale)
                if target_shares > 0:
                    fused.append(
                        (
                            replace(
                                signal,
                                fusion_votes=votes,
                                fusion_label=label,
                                target_shares=target_shares,
                                reason=f"[{label}] {signal.reason}",
                            ),
                            strategy,
                        )
                    )
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
        """Expire an unfilled buy after its trading-day lifetime."""
        if signal.direction != "buy" or not signal.signal_date:
            return False
        signal_ts = pd.Timestamp(signal.signal_date)
        if signal_ts in date_to_pos and date in date_to_pos:
            waited = date_to_pos[date] - date_to_pos[signal_ts]
            return waited > int(self.cfg.get("max_pending_buy_days", 5))
        return False

    @staticmethod
    def _has_pending_liquidation(pending: list[tuple[Signal, BaseStrategy]]) -> bool:
        """Detect risk-generated sells that must keep entries blocked."""
        return any(
            (
                sig.direction == "sell"
                and str(sig.reason)
                in {"circuit breaker liquidation", "sector breadth risk liquidation"}
                for sig, _ in pending
            )
        )

    def _validate_strategy_templates(self) -> None:
        """Reject unnamed or duplicate strategy templates before a run."""
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
        """Reset every mutable ledger and state machine for a fresh run.

        If account-state injection populated ``_initial_positions`` and
        ``_initial_cash`` before this method is called (single-sleeve mode),
        those values are restored after the reset so the engine replays from
        the real portfolio state instead of a zero-position, full-capital start.
        """
        self.cash = (
            self._initial_cash
            if getattr(self, "_initial_cash", None) is not None
            else self.initial_capital
        )
        self.positions = (
            dict(self._initial_positions)
            if getattr(self, "_initial_positions", None)
            else {}
        )
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
        self._safe_mode_active = False
        self._sector_shock_positions = []
        self._sector_recovery_streak = 0
        self.cfg = self._validate_config({**self._default_config(), **self._user_cfg})

    def _apply_global_profile(self, profile: str | None) -> None:
        """Layer an optional profile beneath explicit user overrides."""
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
        """Resolve and validate one effective strategy config per symbol."""
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
                    f"per_symbol_config[{code}] contains global-only or unknown keys: {ignored_keys}; set global values through _CoreBacktestEngine(cfg=...)"
                )
        self.risk = RiskManager(self.cfg)
        self.risk.configure_groups(
            {
                code: _CoreBacktestEngine._SYMBOL_GROUP.get(
                    code,
                    "domestic_semiconductor"
                    if _CoreBacktestEngine.classify_symbol(
                        code, name=symbols_dict.get(code, "")
                    )
                    == "semiconductor"
                    else "overseas_compute",
                )
                for code in symbols_dict
            }
        )

        def _base_for(code: str) -> dict:
            if config_route == "auto":
                return _CoreBacktestEngine.config_for_symbol(
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
        """Load data and precompute indicators for every symbol."""
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
                _CoreBacktestEngine._SYMBOL_PROFILE.get(
                    code, _CoreBacktestEngine.classify_symbol(code, name=name)
                )
                if config_route == "auto"
                else str(profile or "default")
            )
            if config_route == "auto" and self._uses_unmapped_auto_route(code, name):
                msg = (
                    f"  [Route warning] {name}({code}) has no explicit metadata; "
                    "using the default trend profile"
                )
                print(msg)
                if self.cfg.get("strict_unmapped", True):
                    raise RuntimeError(
                        f"strict_unmapped is enabled: {name}({code}) has no "
                        "explicit metadata or recognized name hint. Map it "
                        "explicitly or disable strict_unmapped."
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
        """Select at most max_positions candidates by lag-safe momentum."""
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
        max_positions = int(self.cfg.get("max_positions", 6))
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        min_slots = min(int(self.cfg.get("group_min_slots", 0)), max_positions // 2)
        selected: list[str] = []
        if min_slots > 0:
            for group in ("overseas_compute", "domestic_semiconductor"):
                group_ranked = [
                    code
                    for code, _ in ranked
                    if (
                        _CoreBacktestEngine._SYMBOL_GROUP.get(code)
                        or (
                            "domestic_semiconductor"
                            if _CoreBacktestEngine.classify_symbol(
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
        """Append one closing mark-to-market portfolio snapshot."""
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
        """Apply a concrete persistent or recoverable portfolio risk policy."""
        del current_assets, date_str, all_dates, date_to_pos, pending
        raise NotImplementedError

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
        required = int(self.cfg["sector_guard_min_symbols"])
        observed = observation.symbol_count if observation is not None else 0
        if observation is None or observed < required:
            # Missing one regime constituent must not erase earlier causal
            # confirmations or release an active defense. Old shocks still age
            # out normally; recovery simply pauses until quorum returns.
            self._trim_sector_shock_window(pos)
            self.risk_events.append(
                {
                    "date": date.strftime("%Y-%m-%d"),
                    "event": "sector_guard_data_insufficient",
                    "observed_symbols": observed,
                    "required_symbols": required,
                    "guard_active": bool(self.sector_guard_active),
                }
            )
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
        """Collect one close-generated instruction per eligible strategy."""
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
        display_start, display_end = self._display_run_period(start_date, end_date)
        print(f"\n{'=' * 60}")
        print(f"{self.ENGINE_LABEL} backtest")
        print(f"  Capital: {self.initial_capital:,.0f}")
        print(f"  Symbols: {symbols_dict}")
        print(f"  Period: {display_start} ~ {display_end}")
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
        sells = {
            (signal.symbol, signal.strategy_name)
            for signal, _ in fused_daily
            if signal.direction == "sell"
        }
        if sells:
            sell_symbols = {symbol for symbol, _ in sells}
            symbol_veto = bool(self.cfg["symbol_level_sell_veto"])
            pending = [
                item
                for item in pending
                if not (
                    item[0].direction == "buy"
                    and (
                        item[0].symbol in sell_symbols
                        if symbol_veto
                        else (item[0].symbol, item[0].strategy_name) in sells
                    )
                )
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
        """Deduplicate pending instructions and mark the closing portfolio equity."""
        pending = self._dedupe_pending_signals(pending)
        self._record_equity(data_map, date, date_str)
        return pending

    def _start_trading_day(self) -> None:
        """Freeze the prior-close equity used by the daily loss guard."""
        self.risk.daily_start_assets = (
            self.equity_curve[-1]["assets"]
            if self.equity_curve
            else self.initial_capital
        )

    def _evaluate_trading_day(
        self,
        symbols_dict: dict[str, str],
        data_map: dict[str, pd.DataFrame],
        indicator_map: dict[str, dict[str, pd.Series]],
        all_dates: list[pd.Timestamp],
        date_to_pos: dict[pd.Timestamp, int],
        date: pd.Timestamp,
        pending: list[tuple[Signal, BaseStrategy]],
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Evaluate close-based risk and signals after the opening execution phase."""
        date_str = date.strftime("%Y-%m-%d")
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
        """Execute prior-close orders, then evaluate today's close."""
        self._start_trading_day()
        if pending:
            pending = self._execute_pending_signals(
                pending, data_map, date, date_to_pos
            )
        return self._evaluate_trading_day(
            symbols_dict,
            data_map,
            indicator_map,
            all_dates,
            date_to_pos,
            date,
            pending,
        )

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
        # Apply initial risk state when explicitly provided by the caller.
        # Note: daily_signal_scan.py does NOT use this feature — it replays
        # the full history each time to avoid time-direction errors.
        initial_risk = self.cfg.get("_initial_risk_state")
        if initial_risk and initial_risk.get("sector_guard_active", False):
            self.sector_guard_active = True
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
        final_assets = self._total_assets(data_map, last_date)
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
        directions: frozenset[str] | None = None,
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Execute pending orders in a concrete causal execution model."""
        del pending, data_map, date, date_to_pos, directions
        raise NotImplementedError

    def _latest_close_on_or_before(self, df: pd.DataFrame, date: pd.Timestamp) -> float:
        """Return the latest valid closing mark known by date."""
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
        """Return the latest valid closing mark strictly before date."""
        mask = df.index < date
        if not mask.any():
            return 0.0
        closes = pd.to_numeric(df.loc[mask, "close"], errors="coerce")
        closes = closes[closes > 0]
        return float(closes.iloc[-1]) if not closes.empty else 0.0

    def _execution_mark_prices(
        self, data_map: dict[str, pd.DataFrame], date: pd.Timestamp
    ) -> dict[str, float]:
        """Build opening marks, falling back only to prior known closes."""
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
        """Mark cash and every position with an explicit price map."""
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
        """Mark total assets with the latest close known by date."""
        total = self.cash
        for code, positions in self.positions.items():
            if code not in data_map:
                continue
            price = self._latest_close_on_or_before(data_map[code], date)
            for pos in positions.values():
                mark = price if price > 0 else pos.entry_price
                total += pos.market_value_at(mark)
        return float(total)

    @staticmethod
    def _apply_account_state(
        account_state: AccountState | None, engine: _CoreBacktestEngine,
        set_cash: bool = True,
    ) -> dict[str, dict[str, Position]]:
        """Convert AccountState positions to engine Position objects and set them.

        Uses ``"external_account"`` as the strategy name for all injected
        positions so that portfolio-level risk controls (drawdown, daily loss,
        sector guard) can liquidate them.  Sets the engine's cash to the
        account's cash balance and populates both ``positions`` and
        ``_initial_positions`` so the engine can replay from the correct state
        without modifying the simulation-only path.

        Also seeds the engine's risk manager with the account's peak equity
        so drawdown calculations start from the real high-water mark.

        When ``set_cash`` is False (ensemble mode), cash is not overridden so
        each sleeve retains its proportional ``sleeve_capital`` and only the
        first sleeve receives the positions — preventing triple-counting.
        """
        if account_state is None:
            return {}
        if set_cash:
            engine.cash = float(account_state.cash) if account_state.cash is not None else engine.cash
            engine._initial_cash = engine.cash
        converted: dict[str, dict[str, Position]] = {}
        for code, pos_info in (account_state.positions or {}).items():
            if not isinstance(pos_info, dict):
                continue
            shares = int(pos_info.get("shares", 0))
            if shares <= 0:
                continue
            entry_price = float(pos_info.get("avg_cost", 0.0) or pos_info.get("price", 0.0))
            if entry_price <= 0:
                continue
            entry_date = str(pos_info.get("entry_date", ""))
            converted[code] = {
                "external_account": Position(
                    symbol=code,
                    strategy_name="external_account",
                    shares=shares,
                    entry_price=entry_price,
                    entry_date=entry_date,
                )
            }
        engine.positions = dict(converted)
        engine._initial_positions = dict(converted)
        # P0 fix: seed the sleeve-level risk manager with the account's
        # lifetime peak so drawdown calculations start from the real
        # high-water mark rather than building up from zero.
        peak = getattr(account_state, "peak_equity", None)
        if peak is not None and peak > 0 and hasattr(engine, "risk") and engine.risk is not None:
            if hasattr(engine.risk, "peak_assets"):
                engine.risk.peak_assets = float(peak)
            if hasattr(engine.risk, "lifetime_peak_assets"):
                engine.risk.lifetime_peak_assets = float(peak)
        return converted

    def _fit_buy_to_cash(
        self,
        requested_shares: float,
        exec_price: float,
        commission_rate: float,
        min_commission: float,
    ) -> tuple[int, float, float, float]:
        """Fit a board-lot buy to cash in constant time, including minimum fees."""
        requested = _floor_to_lot(requested_shares)
        if requested <= 0 or exec_price <= 0 or self.cash <= min_commission:
            return (0, 0.0, 0.0, 0.0)
        by_rate = _floor_to_lot(self.cash / (exec_price * (1.0 + commission_rate)))
        by_minimum_fee = _floor_to_lot(
            max(self.cash - min_commission, 0.0) / exec_price
        )
        shares = min(requested, by_rate, by_minimum_fee)
        if shares <= 0:
            return (0, 0.0, 0.0, 0.0)
        buy_value = shares * exec_price
        commission = max(buy_value * commission_rate, min_commission)
        total_cost = buy_value + commission
        return (shares, buy_value, commission, total_cost)

    def _apply_buy_to_position(
        self,
        signal: Signal,
        strategy: BaseStrategy,
        date_str: str,
        shares: int,
        exec_price: float,
        total_cost: float,
    ) -> None:
        """Apply one filled buy to its strategy sub-position."""
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

    def _record_buy_rejection(
        self,
        *,
        date: str,
        signal: Signal,
        event: str,
        **details: Any,
    ) -> None:
        """Allow audit-capable engines to explain a rejected buy."""
        del date, signal, event, details

    def _execute_buy(
        self,
        signal: Signal,
        strategy: BaseStrategy,
        date_str: str,
        data_map: dict[str, pd.DataFrame] | None = None,
        date: pd.Timestamp | None = None,
    ) -> bool:
        """Execute a buy after cash, risk, and exposure checks."""
        if signal.target_shares <= 0 or signal.price <= 0:
            self._record_buy_rejection(
                date=date_str,
                signal=signal,
                event="rejected_invalid_buy_order",
                requested_shares=int(signal.target_shares),
                signal_price=float(signal.price),
            )
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
            self._record_buy_rejection(
                date=date_str,
                signal=signal,
                event="rejected_insufficient_cash",
                requested_shares=int(signal.target_shares),
                execution_price=exec_price,
                available_cash=float(self.cash),
            )
            return False
        if data_map is not None and date is not None:
            current_prices = self._execution_mark_prices(data_map, date)
            current_assets = self._total_assets_at_prices(current_prices)
        else:
            current_assets = self.initial_capital
            current_prices = None
        if signal.symbol not in self.positions and len(self.positions) >= int(
            global_cfg.get("max_positions", 6)
        ):
            self._record_buy_rejection(
                date=date_str,
                signal=signal,
                event="rejected_max_positions",
                current_positions=len(self.positions),
                max_positions=int(global_cfg.get("max_positions", 6)),
            )
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
                self._record_buy_rejection(
                    date=date_str,
                    signal=signal,
                    event="rejected_risk_sizing_zero",
                    current_assets=float(current_assets),
                    signal_atr=float(signal.atr),
                )
                return False
            shares, buy_value, commission, total_cost = self._fit_buy_to_cash(
                shares, exec_price, commission_rate, min_commission
            )
            if shares <= 0:
                self._record_buy_rejection(
                    date=date_str,
                    signal=signal,
                    event="rejected_insufficient_cash_after_risk_sizing",
                    risk_limited_shares=int(risk_limited_shares),
                    available_cash=float(self.cash),
                )
                return False
        if not self.risk.check_position_limits(
            signal.symbol,
            self.positions,
            current_assets,
            buy_value,
            current_prices,
            position_cfg=strategy_cfg,
        ):
            self._record_buy_rejection(
                date=date_str,
                signal=signal,
                event="rejected_position_limit",
                proposed_buy_value=float(buy_value),
                current_assets=float(current_assets),
            )
            return False
        if self.risk.check_daily_loss(current_assets):
            self._record_buy_rejection(
                date=date_str,
                signal=signal,
                event="rejected_daily_loss_limit",
                current_assets=float(current_assets),
            )
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
        self, signal: Signal, strategy: BaseStrategy | None, date_str: str
    ) -> int:
        """Execute a sell and return the filled share count.

        When ``strategy`` is ``None`` (external-account positions), the
        signal's ``strategy_name`` field is used to look up the position
        instead of ``strategy.name``.
        """
        if signal.target_shares <= 0 or signal.price <= 0:
            return 0
        strat_name = strategy.name if strategy is not None else signal.strategy_name
        pos = None
        if signal.symbol in self.positions:
            pos = self.positions[signal.symbol].get(strat_name)
        if pos is None:
            if strategy is not None:
                strategy.position = None
            return 0
        cfg = self.cfg
        slippage = float(cfg.get("slippage", 0.001))
        commission_rate = float(cfg.get("commission_rate", 0.00025))
        min_commission = float(cfg.get("min_commission", 0.0))
        stamp_duty = float(cfg.get("stamp_duty", 0.0005))
        exec_price = float(signal.price) * (1 - slippage)
        sell_shares = _floor_to_lot(min(signal.target_shares, pos.shares))
        if sell_shares <= 0:
            return 0
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
            del self.positions[signal.symbol][strat_name]
            if not self.positions[signal.symbol]:
                del self.positions[signal.symbol]
            if strategy is not None:
                strategy.position = None
        else:
            if strategy is not None:
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
        return sell_shares

    def _generate_liquidation_signals(
        self, date_str: str, reason: str = "circuit breaker liquidation"
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Queue full-position sells for execution at a later tradable open.

        Covers both strategy-managed positions and externally injected
        (``external_account``) holdings so that portfolio-level risk controls
        (drawdown, daily loss, sector guard) can liquidate the entire book.
        External positions use ``None`` as the strategy placeholder; the
        execution path handles that case by looking up the position directly
        from ``self.positions`` using the signal's ``strategy_name``.
        """
        # These are ordinary pending sell signals. The placeholder price is always
        # replaced by a later tradable opening price before execution.
        signals = []
        for code, positions in self.positions.items():
            strategies = {s.name: s for s in self.strategy_instances.get(code, [])}
            for strat_name, pos in positions.items():
                strategy = strategies.get(strat_name)
                if strategy is None and strat_name != "external_account":
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

    def _generate_regime_reduction_signals(
        self, date_str: str, exit_ratio: float
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Queue partial-position sells when the regime enters CHOPPY.

        Unlike full liquidation, only *exit_ratio* of each position is sold,
        reducing exposure without abandoning all trend-following entries.
        Externally injected (``external_account``) positions are also trimmed
        so portfolio-level exposure reduction covers the entire book.
        """
        signals = []
        for code, positions in self.positions.items():
            strategies = {s.name: s for s in self.strategy_instances.get(code, [])}
            for strat_name, pos in positions.items():
                strategy = strategies.get(strat_name)
                if strategy is None and strat_name != "external_account":
                    continue
                sell_shares = max(0, int(pos.shares * exit_ratio))
                if sell_shares <= 0:
                    continue
                sig = Signal(
                    symbol=code,
                    strategy_name=strat_name,
                    direction="sell",
                    target_shares=sell_shares,
                    price=pos.entry_price,
                    reason="regime choppy reduction",
                    signal_date=date_str,
                )
                signals.append((sig, strategy))
        return signals

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
        calmar = annual_return / abs(max_drawdown) if max_drawdown < 0 else 0.0
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
            "calmar": calmar,
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
            "period_end_valuation": "mark_to_market",
            "equity_curve": eq,
            "trades": self.trades,
            "drawdown_series": drawdown,
            "pending_signals": [signal for signal, _ in self.pending_signals],
            "parameter_routes": {
                code: _CoreBacktestEngine._SYMBOL_PROFILE.get(
                    code,
                    _CoreBacktestEngine.classify_symbol(
                        code, name=self.symbol_names.get(code, "")
                    ),
                )
                for code in self.symbol_names
            },
            "unmapped_symbols": sorted(
                code
                for code, name in self.symbol_names.items()
                if _CoreBacktestEngine._uses_unmapped_auto_route(code, name)
            ),
            "fusion_events": list(self.fusion_events),
            "risk_events": list(self.risk_events),
            "sector_guard_active": bool(self.sector_guard_active),
            "safe_mode_active": bool(getattr(self, "_safe_mode_active", False)),
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
        """Print the standard human-readable performance summary."""
        if "error" in result:
            print(f"Backtest failed: {result['error']}")
            return
        print(f"\n{'═' * 60}")
        print("  Quant Fusion performance report")
        print(f"{'═' * 60}")
        print(f"  Symbols: {', '.join((f'{v}({k})' for k, v in symbols_dict.items()))}")
        print(f"  Initial capital:   {result['initial_capital']:>15,.0f}")
        print(f"  Final assets:       {result['final_assets']:>15,.0f}")
        print("  ────────────────────────────────")
        print(f"  Total return:   {result['total_return']:>15.2%}")
        print(f"  Annualized return: {result['annual_return']:>15.2%}")
        print(f"  Maximum drawdown:   {result['max_drawdown']:>15.2%}")
        print(f"  Sharpe ratio:   {result['sharpe']:>15.2f}")
        print(f"  Calmar ratio:   {result['calmar']:>15.2f}")
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
        """Persist audited tabular and JSON result artifacts."""
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
            "calmar",
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
        """Plot portfolio equity and drawdown in a deterministic layout."""
        if "error" in result:
            print(f"Backtest failed; cannot plot: {result['error']}")
            return
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        eq = result["equity_curve"]
        dd = result["drawdown_series"]
        _, axes = plt.subplots(
            2, 1, figsize=(14, 8), gridspec_kw={"height_ratios": [3, 1]}
        )
        axes[0].plot(eq.index, eq["assets"] / 10000, linewidth=1.5, color="#1a73e8")
        axes[0].set_title("Quant Fusion Portfolio Equity Curve", fontsize=14)
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
    "688256": "寒武纪",
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
    """Resolve comma-separated stock codes or supported Chinese names."""
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


# Causal execution and persistent-risk refinements.


class PersistentRiskManager(RiskManager):
    """Keep a breached portfolio locked until an explicit operator reset.

    A fixed backtest cannot model an investment committee or operator decision. The
    conservative default is therefore to stay in cash after the lifetime drawdown
    threshold is crossed. A new engine instance is the explicit reset boundary.
    """

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.persistent_lock = False
        self.lock_date: str | None = None
        self.lock_drawdown = 0.0

    def check_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        trading_dates: list[pd.Timestamp] | None = None,
        date_to_pos: dict[pd.Timestamp, int] | None = None,
    ) -> str | None:
        """Trigger once at the lifetime high-water drawdown and then block entries."""
        del trading_dates, date_to_pos
        self.peak_assets = max(self.peak_assets, float(current_assets))
        if self.persistent_lock:
            return "persistent portfolio risk lock"
        if self.peak_assets <= 0:
            return None
        drawdown = (self.peak_assets - current_assets) / self.peak_assets
        if drawdown < float(self.cfg.get("max_drawdown", 0.2)):
            return None
        self.persistent_lock = True
        self.lock_date = date_str
        self.lock_drawdown = float(drawdown)
        return "portfolio drawdown circuit breaker"


class _CausalBacktestEngine(_CoreBacktestEngine):
    """Run core signals with causal allocation, explicit state, and durable defense."""

    ENGINE_LABEL = "Quant Fusion"
    ALLOCATION_LOOKBACKS = (5, 10, 20)

    def __init__(
        self, initial_capital: float = 2_000_000, cfg: dict | None = None
    ) -> None:
        super().__init__(initial_capital=initial_capital, cfg=cfg)
        self.order_events: list[dict[str, Any]] = []
        self._profile_strategy_overrides: dict[str, Any] = {}
        self._indicator_state = "cold"
        self._warmup_calendar_days = 365
        self._requested_start_date = ""
        self._requested_end_date = ""
        self._risk_lock_logged = False

    def _display_run_period(self, start_date: str, end_date: str) -> tuple[str, str]:
        """Show the requested trading window, not the optional warmup window."""
        if self._requested_start_date and self._requested_end_date:
            return self._requested_start_date, self._requested_end_date
        return start_date, end_date

    def _reset_run_state(self, symbols_dict: dict[str, str]) -> None:
        """Reset causal audit and profile state with the inherited ledger."""
        super()._reset_run_state(symbols_dict)
        self.order_events = []
        self._profile_strategy_overrides = {}
        self._risk_lock_logged = False

    def _apply_global_profile(self, profile: str | None) -> None:
        """Apply a profile and remember its strategy-level routed overrides."""
        super()._apply_global_profile(profile)
        factories = {
            "semiconductor": self.semiconductor_config,
            "semiconductor_heavy": self.semiconductor_heavy_config,
            "aggressive": self.optimized_aggressive_config,
        }
        factory = factories.get(profile)
        if factory is None:
            self._profile_strategy_overrides = {}
            return
        defaults = self._default_config()
        profile_cfg = factory()
        self._profile_strategy_overrides = {
            key: profile_cfg[key]
            for key in self._PER_SYMBOL_OVERRIDE_KEYS
            if key in profile_cfg and profile_cfg[key] != defaults.get(key)
        }

    def _resolve_symbol_configs(
        self,
        symbols_dict: dict[str, str],
        per_symbol_config: dict[str, dict] | None,
        config_route: str,
    ) -> dict[str, dict]:
        """Resolve explicit precedence and install the persistent risk manager."""
        resolved = super()._resolve_symbol_configs(
            symbols_dict, per_symbol_config, config_route
        )
        symbol_groups = dict(self.risk.symbol_groups)
        self.risk = PersistentRiskManager(self.cfg)
        self.risk.configure_groups(symbol_groups)

        explicit_global = {
            key: value
            for key, value in self._user_cfg.items()
            if key in self._PER_SYMBOL_OVERRIDE_KEYS
        }
        route_overrides = {
            **self._profile_strategy_overrides,
            **explicit_global,
        }
        per_symbol = per_symbol_config or {}
        final: dict[str, dict] = {}
        for code, route_cfg in resolved.items():
            final[code] = self._validate_config(
                {
                    **route_cfg,
                    **route_overrides,
                    **per_symbol.get(code, {}),
                }
            )
        return final

    def _allocation_scores(
        self, data_map: dict[str, pd.DataFrame], date: pd.Timestamp
    ) -> dict[str, float]:
        """Average cross-sectional ranks of causal risk-adjusted momentum signals."""
        raw: dict[int, dict[str, float]] = {
            window: {} for window in self.ALLOCATION_LOOKBACKS
        }
        for code, frame in data_map.items():
            history = frame.loc[frame.index < date, "close"].dropna().astype(float)
            for window in self.ALLOCATION_LOOKBACKS:
                if len(history) <= window:
                    continue
                momentum = float(history.iloc[-1] / history.iloc[-1 - window] - 1.0)
                volatility = float(history.pct_change().tail(window).std())
                # A flat series has no risk-adjusted momentum evidence. Treating
                # its zero volatility as a divisor or as raw momentum would give
                # it an arbitrary ranking advantage.
                if not math.isfinite(volatility) or volatility <= 0:
                    continue
                score = momentum / volatility
                if np.isfinite(score):
                    raw[window][code] = score

        scores = {code: 0.0 for code in data_map}
        observations = {code: 0 for code in data_map}
        for values in raw.values():
            if not values:
                continue
            ranks = pd.Series(values, dtype="float64").rank(pct=True)
            for code, rank in ranks.items():
                scores[code] += float(rank)
                observations[code] += 1
        return {
            code: scores[code] / observations[code] if observations[code] else 0.0
            for code in scores
        }

    def _record_order_event(
        self,
        *,
        date: str,
        signal: Signal,
        event: str,
        **details: Any,
    ) -> None:
        """Append a compact, serializable order decision to the audit trail."""
        self.order_events.append(
            {
                "date": date,
                "symbol": signal.symbol,
                "strategy": signal.strategy_name,
                "direction": signal.direction,
                "event": event,
                "signal_date": signal.signal_date,
                **details,
            }
        )

    def _record_buy_rejection(
        self,
        *,
        date: str,
        signal: Signal,
        event: str,
        **details: Any,
    ) -> None:
        """Record the concrete execution check that rejected a buy."""
        self._record_order_event(
            date=date,
            signal=signal,
            event=event,
            **details,
        )

    def _remaining_buy_capacity(
        self,
        signal: Signal,
        strategy: BaseStrategy,
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
    ) -> tuple[float, float]:
        """Return execution-day equity and remaining currency exposure capacity."""
        prices = self._execution_mark_prices(data_map, date)
        current_assets = self._total_assets_at_prices(prices)
        if current_assets <= 0 or signal.symbol not in prices:
            return current_assets, 0.0

        def position_value(code: str) -> float:
            price = prices.get(code)
            if price is None or price <= 0:
                return 0.0
            return sum(
                position.shares * price
                for position in self.positions.get(code, {}).values()
            )

        symbol_value = position_value(signal.symbol)
        total_value = sum(position_value(code) for code in self.positions)
        symbol_cap = float(strategy.cfg.get("max_symbol_weight", 1.0))
        capacities = [
            current_assets * symbol_cap - symbol_value,
            current_assets * float(self.cfg.get("max_total_weight", 1.0)) - total_value,
        ]
        target_group = self.risk.symbol_groups.get(signal.symbol)
        if target_group:
            group_value = sum(
                position_value(code)
                for code in self.positions
                if self.risk.symbol_groups.get(code) == target_group
            )
            group_cap = float(self.risk.group_weight_limits.get(target_group, 1.0))
            capacities.append(current_assets * group_cap - group_value)
        return current_assets, max(min(capacities), 0.0)

    def _execute_buy(
        self,
        signal: Signal,
        strategy: BaseStrategy,
        date_str: str,
        data_map: dict[str, pd.DataFrame] | None = None,
        date: pd.Timestamp | None = None,
    ) -> bool:
        """Clip an oversized buy to available capacity before inherited checks."""
        adjusted_signal = signal
        if data_map is not None and date is not None and signal.price > 0:
            _, capacity = self._remaining_buy_capacity(signal, strategy, data_map, date)
            execution_price = float(signal.price) * (
                1.0 + float(self.cfg.get("slippage", 0.001))
            )
            capacity_shares = _floor_to_lot(capacity / execution_price)
            if capacity_shares < signal.target_shares:
                if capacity_shares <= 0:
                    self._record_order_event(
                        date=date_str,
                        signal=signal,
                        event="rejected_no_exposure_capacity",
                    )
                    return False
                adjusted_signal = replace(signal, target_shares=capacity_shares)
                self._record_order_event(
                    date=date_str,
                    signal=signal,
                    event="clipped_to_exposure_capacity",
                    requested_shares=int(signal.target_shares),
                    adjusted_shares=int(capacity_shares),
                )
        events_before = len(self.order_events)
        executed = super()._execute_buy(
            adjusted_signal, strategy, date_str, data_map, date
        )
        if not executed and len(self.order_events) == events_before:
            self._record_order_event(
                date=date_str,
                signal=adjusted_signal,
                event="rejected_by_execution_checks",
            )
        return executed

    @staticmethod
    def _allocate_lots_pro_rata(
        items: list[tuple[Signal, BaseStrategy]], capacity: int
    ) -> list[int]:
        """Split a same-symbol capacity proportionally, with at most one-lot skew."""
        targets = [_floor_to_lot(signal.target_shares) for signal, _ in items]
        total = sum(targets)
        capacity = min(_floor_to_lot(capacity), total)
        if capacity <= 0:
            return [0] * len(items)
        if capacity >= total:
            return targets

        exact = [capacity * target / total for target in targets]
        allocated = [_floor_to_lot(value) for value in exact]
        remaining_lots = (capacity - sum(allocated)) // A_SHARE_LOT_SIZE
        order = sorted(
            range(len(items)),
            key=lambda index: (
                -(exact[index] - allocated[index]),
                items[index][0].strategy_name,
            ),
        )
        while remaining_lots > 0:
            progressed = False
            for index in order:
                if allocated[index] >= targets[index]:
                    continue
                allocated[index] += A_SHARE_LOT_SIZE
                remaining_lots -= 1
                progressed = True
                if remaining_lots == 0:
                    break
            if not progressed:
                break
        return allocated

    def _remaining_adv_capacity(
        self,
        symbol: str,
        direction: str,
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
    ) -> int | None:
        """Return a liquidity cap when the concrete engine enables ADV limits."""
        del symbol, direction, data_map, date
        return None

    def _buy_batch_capacity(
        self,
        items: list[tuple[Signal, BaseStrategy]],
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
    ) -> int:
        """Return the shared cash, exposure, and liquidity capacity for one symbol."""
        if not items:
            return 0
        signal = items[0][0]
        if any(item[0].symbol != signal.symbol for item in items[1:]):
            raise ValueError("A buy batch must contain exactly one symbol")
        if any(
            not math.isclose(float(item[0].price), float(signal.price))
            for item in items[1:]
        ):
            raise ValueError("A buy batch must use one execution price")
        requested = sum(_floor_to_lot(item[0].target_shares) for item in items)
        execution_price = float(signal.price) * (
            1.0 + float(self.cfg.get("slippage", 0.001))
        )
        if requested <= 0 or execution_price <= 0:
            return 0
        if signal.symbol not in self.positions and len(self.positions) >= int(
            self.cfg.get("max_positions", 6)
        ):
            return 0
        # All production strategies for one symbol currently share a validated
        # config. Taking the minimum still preserves safety if a future caller
        # supplies heterogeneous strategy-level exposure limits.
        exposure_value = min(
            self._remaining_buy_capacity(item_signal, strategy, data_map, date)[1]
            for item_signal, strategy in items
        )
        exposure_shares = _floor_to_lot(exposure_value / execution_price)
        cash_shares = self._cash_affordable_batch_capacity(
            items, requested, execution_price
        )
        capacities = [requested, exposure_shares, cash_shares]
        adv_capacity = self._remaining_adv_capacity(
            signal.symbol, "buy", data_map, date
        )
        if adv_capacity is not None:
            capacities.append(adv_capacity)
        return max(min(capacities), 0)

    def _cash_affordable_batch_capacity(
        self,
        items: list[tuple[Signal, BaseStrategy]],
        requested: int,
        execution_price: float,
    ) -> int:
        """Find the largest proportional batch whose separate fees fit cash."""
        commission_rate = float(self.cfg.get("commission_rate", 0.00025))
        min_commission = float(self.cfg.get("min_commission", 0.0))

        def total_cost(capacity: int) -> float:
            allocations = self._allocate_lots_pro_rata(items, capacity)
            return sum(
                shares * execution_price
                + max(shares * execution_price * commission_rate, min_commission)
                for shares in allocations
                if shares > 0
            )

        # Largest-remainder allocation can exhibit the Alabama paradox: adding
        # one lot may remove a small strategy's allocation and one minimum fee.
        # Binary search is safe for normal A-share prices, where one extra lot
        # costs more than every possible fee-floor drop. Use an exact descending
        # scan only for pathological low adjusted prices where that proof fails.
        lot_cost = A_SHARE_LOT_SIZE * execution_price * (1.0 + commission_rate)
        if lot_cost < len(items) * min_commission:
            for capacity in range(_floor_to_lot(requested), -1, -A_SHARE_LOT_SIZE):
                if total_cost(capacity) <= self.cash:
                    return capacity
            return 0

        low = 0
        high = requested // A_SHARE_LOT_SIZE
        while low < high:
            midpoint = (low + high + 1) // 2
            capacity = midpoint * A_SHARE_LOT_SIZE
            if total_cost(capacity) <= self.cash:
                low = midpoint
            else:
                high = midpoint - 1
        return low * A_SHARE_LOT_SIZE

    def _execute_buy_batch(
        self,
        items: list[tuple[Signal, BaseStrategy]],
        date_str: str,
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
    ) -> None:
        """Execute same-symbol confirmations from one shared proportional budget."""
        capacity = self._buy_batch_capacity(items, data_map, date)
        allocations = self._allocate_lots_pro_rata(items, capacity)
        for (signal, strategy), allocated in zip(items, allocations, strict=True):
            if allocated <= 0:
                self._record_order_event(
                    date=date_str,
                    signal=signal,
                    event="rejected_no_shared_batch_capacity",
                )
                continue
            adjusted = replace(signal, target_shares=allocated)
            if allocated < signal.target_shares:
                self._record_order_event(
                    date=date_str,
                    signal=signal,
                    event="scaled_for_fair_batch_allocation",
                    requested_shares=int(signal.target_shares),
                    adjusted_shares=int(allocated),
                )
            self._execute_buy(adjusted, strategy, date_str, data_map, date)

    def _prepare_open_signal(
        self,
        signal: Signal,
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
        date_to_pos: dict[pd.Timestamp, int],
    ) -> tuple[Signal | None, bool]:
        """Return an executable open order and whether a blocked order must persist."""
        date_str = date.strftime("%Y-%m-%d")
        if self._buy_signal_expired(signal, date, date_to_pos):
            self._record_order_event(
                date=date_str, signal=signal, event="expired_pending_buy"
            )
            return None, False
        frame = data_map.get(signal.symbol)
        if frame is None or date not in frame.index:
            return None, True
        open_price = frame.loc[date, "open"]
        if pd.isna(open_price) or float(open_price) <= 0:
            return None, True
        executable = replace(signal, price=float(open_price))
        limit_state = self._opening_limit_state(
            executable, frame, date, float(open_price)
        )
        if limit_state == "buy_blocked":
            self._record_order_event(
                date=date_str, signal=signal, event="rejected_limit_up_open"
            )
            return None, False
        return (None, True) if limit_state == "sell_blocked" else (executable, False)

    def _execute_pending_signals(
        self,
        pending: list[tuple[Signal, BaseStrategy]],
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
        date_to_pos: dict[pd.Timestamp, int],
        directions: frozenset[str] | None = None,
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Execute selected sides, batching same-symbol buys before any fill."""
        date_str = date.strftime("%Y-%m-%d")
        strategy_rank = {"turtle_breakout": 0, "dual_ma": 1, "atr_channel": 2}
        allocation_scores = self._allocation_scores(data_map, date)
        sorted_pending = sorted(
            pending,
            key=lambda item: (
                0 if item[0].direction == "sell" else 1,
                -allocation_scores.get(item[0].symbol, 0.0),
                item[0].symbol,
                strategy_rank.get(item[0].strategy_name, 99),
            ),
        )
        allowed = directions or frozenset({"buy", "sell"})
        unexecuted: list[tuple[Signal, BaseStrategy]] = []
        buy_batches: dict[str, list[tuple[Signal, BaseStrategy]]] = {}
        for signal, strategy in sorted_pending:
            if signal.direction not in allowed:
                unexecuted.append((signal, strategy))
                continue
            executable_signal, keep_pending = self._prepare_open_signal(
                signal, data_map, date, date_to_pos
            )
            if keep_pending:
                unexecuted.append((signal, strategy))
            if executable_signal is None:
                continue
            code = executable_signal.symbol
            if executable_signal.direction == "buy":
                buy_batches.setdefault(code, []).append((executable_signal, strategy))
            elif executable_signal.direction == "sell":
                sold = self._execute_sell(executable_signal, strategy, date_str)
                remaining = max(executable_signal.target_shares - sold, 0)
                if remaining > 0 and (strategy is None or strategy.position is not None):
                    unexecuted.append(
                        (replace(signal, target_shares=remaining), strategy)
                    )
        for items in buy_batches.values():
            self._execute_buy_batch(items, date_str, data_map, date)
        return self._dedupe_pending_signals(unexecuted)

    def _opening_limit_state(
        self,
        signal: Signal,
        frame: pd.DataFrame,
        date: pd.Timestamp,
        open_price: float,
    ) -> str | None:
        """Classify an opening board limit without consuming a pending sell."""
        location = frame.index.get_loc(date)
        if location <= 0:
            return None
        previous_close = float(frame.iloc[location - 1]["close"])
        if previous_close <= 0:
            return None
        change = (open_price - previous_close) / previous_close
        limit_up = _limit_pct_for_code(
            signal.symbol, self.cfg, self.symbol_names.get(signal.symbol, "")
        )
        epsilon = float(self.cfg.get("limit_price_epsilon", 0.001))
        if signal.direction == "buy" and change >= limit_up - epsilon:
            return "buy_blocked"
        if signal.direction == "sell" and change <= -limit_up + epsilon:
            return "sell_blocked"
        return None

    def _apply_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        all_dates: list[pd.Timestamp],
        date_to_pos: dict[pd.Timestamp, int],
        pending: list[tuple[Signal, BaseStrategy]],
    ) -> tuple[list[tuple[Signal, BaseStrategy]], bool, bool]:
        """Apply T+1 liquidation and accurately report the durable risk lock."""
        risk_status = self.risk.check_portfolio_risk(
            current_assets,
            date_str,
            trading_dates=all_dates,
            date_to_pos=date_to_pos,
        )
        if risk_status is None and self.risk.check_daily_loss(current_assets):
            risk_status = "daily loss limit"
        risk_blocked = self._has_pending_liquidation(pending)
        if risk_blocked:
            risk_status = risk_status or "circuit breaker liquidation pending"
        if not risk_status:
            return pending, risk_blocked, False

        liquidate = False
        if risk_status == "portfolio drawdown circuit breaker":
            liquidate = bool(self.cfg.get("liquidate_on_circuit_breaker", True))
            if liquidate:
                print(
                    f"  WARNING [{date_str}] {risk_status}: generate T+1 "
                    "liquidation signals and enter a persistent risk lock"
                )
                liquidation_signals = self._generate_liquidation_signals(date_str)
                pending = self._dedupe_pending_signals(
                    [item for item in pending if item[0].direction == "sell"]
                    + liquidation_signals
                )
            else:
                print(
                    f"  WARNING [{date_str}] {risk_status}: block new entries "
                    "under a persistent risk lock"
                )
        if (
            isinstance(self.risk, PersistentRiskManager)
            and self.risk.persistent_lock
            and not self._risk_lock_logged
        ):
            self.risk_events.append(
                {
                    "date": self.risk.lock_date or date_str,
                    "event": "persistent_portfolio_risk_lock",
                    "drawdown": self.risk.lock_drawdown,
                }
            )
            self._risk_lock_logged = True
        return pending, True, liquidate

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
        """Optionally compute indicators on pre-start history while trading flat."""
        if self._indicator_state == "cold":
            return super()._prepare_run(
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
        warm_start = start_ts - pd.Timedelta(days=self._warmup_calendar_days)
        data_map, indicator_map, _, _ = super()._prepare_run(
            symbols_dict,
            warm_start.strftime("%Y-%m-%d"),
            end_date,
            warm_start,
            end_ts,
            per_symbol_config,
            profile,
            config_route,
            data_dir,
        )
        trading_dates = sorted(
            {
                date
                for frame in data_map.values()
                for date in frame.index
                if start_ts <= date <= end_ts
            }
        )
        date_to_pos = {
            pd.Timestamp(date): index for index, date in enumerate(trading_dates)
        }
        return data_map, indicator_map, trading_dates, date_to_pos

    def run(
        self,
        symbols_dict: dict[str, str],
        start_date: str,
        end_date: str,
        per_symbol_config: dict[str, dict] | None = None,
        profile: str | None = None,
        config_route: str = "auto",
        data_dir: str | None = None,
        *,
        indicator_state: str = "cold",
        warmup_calendar_days: int = 365,
    ) -> dict:
        """Run with an explicit cold or warm indicator-state contract."""
        indicator_state = str(indicator_state).lower()
        if indicator_state not in {"cold", "warm"}:
            raise ValueError("indicator_state must be either 'cold' or 'warm'")
        warmup_calendar_days = _require_int(
            "warmup_calendar_days", warmup_calendar_days, min_value=120
        )
        self._indicator_state = indicator_state
        self._warmup_calendar_days = warmup_calendar_days
        self._requested_start_date = start_date
        self._requested_end_date = end_date
        return super().run(
            symbols_dict,
            start_date,
            end_date,
            per_symbol_config=per_symbol_config,
            profile=profile,
            config_route=config_route,
            data_dir=data_dir,
        )

    def _build_result(self, final_assets: float, all_dates: list[pd.Timestamp]) -> dict:
        """Extend the inherited report with allocation and resolved-config audits."""
        result = super()._build_result(final_assets, all_dates)
        result.update(
            {
                "indicator_state": self._indicator_state,
                "allocation_lookbacks": list(self.ALLOCATION_LOOKBACKS),
                "order_events": list(self.order_events),
                "resolved_symbol_configs": {
                    code: dict(config) for code, config in self.symbol_configs.items()
                },
                "persistent_risk_lock": bool(
                    isinstance(self.risk, PersistentRiskManager)
                    and self.risk.persistent_lock
                ),
            }
        )
        return result


# Ensemble allocation, liquidity, and confirmed-risk controls.


def _require_positive_ratio(
    name: str, value: object, *, inclusive_max: bool = False
) -> float:
    """Reject booleans and return a validated ratio in the interval (0, 1]."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a numeric ratio, not a Boolean")
    return _require_positive(
        name,
        value,
        max_value=1.0,
        inclusive_max=inclusive_max,
    )


@dataclass(frozen=True)
class _PortfolioPolicyBase:
    """Define ensemble controls independently from the core signal parameters."""

    allocation_mode: str = "ensemble"
    single_lookbacks: tuple[int, ...] = (5, 10, 20)
    allocation_horizons: tuple[tuple[int, ...], ...] = (
        (3, 5, 10),
        (5, 10, 20),
        (10, 20, 40),
    )
    drawdown_alert: float = 0.14
    confirmed_drawdown: float = 0.15
    drawdown_confirmations: int = 2
    emergency_drawdown: float = 0.175
    adv_lookback: int = 20
    max_order_adv_ratio: float = 0.005

    def __post_init__(self) -> None:
        """Validate thresholds, horizons, and liquidity controls eagerly."""
        mode = str(self.allocation_mode).lower()
        if mode not in {"single", "ensemble"}:
            raise ValueError("allocation_mode must be 'single' or 'ensemble'")
        object.__setattr__(self, "allocation_mode", mode)
        alert = _require_positive_ratio("drawdown_alert", self.drawdown_alert)
        confirmed = _require_positive_ratio(
            "confirmed_drawdown", self.confirmed_drawdown
        )
        emergency = _require_positive_ratio(
            "emergency_drawdown", self.emergency_drawdown
        )
        if not alert < confirmed < emergency:
            raise ValueError(
                "drawdown thresholds must satisfy alert < confirmed < emergency"
            )
        object.__setattr__(self, "drawdown_alert", alert)
        object.__setattr__(self, "confirmed_drawdown", confirmed)
        object.__setattr__(self, "emergency_drawdown", emergency)
        confirmations = _require_int(
            "drawdown_confirmations", self.drawdown_confirmations, min_value=1
        )
        adv_lookback = _require_int("adv_lookback", self.adv_lookback, min_value=1)
        ratio = _require_positive_ratio(
            "max_order_adv_ratio",
            self.max_order_adv_ratio,
            inclusive_max=True,
        )
        object.__setattr__(self, "drawdown_confirmations", confirmations)
        object.__setattr__(self, "adv_lookback", adv_lookback)
        object.__setattr__(self, "max_order_adv_ratio", ratio)
        object.__setattr__(
            self,
            "single_lookbacks",
            self._validate_lookbacks("single_lookbacks", self.single_lookbacks),
        )
        horizons = tuple(
            self._validate_lookbacks(f"allocation_horizons[{index}]", values)
            for index, values in enumerate(self.allocation_horizons)
        )
        if not horizons:
            raise ValueError("allocation_horizons must contain at least one sleeve")
        if len(set(horizons)) != len(horizons):
            raise ValueError("allocation_horizons must not contain duplicate sleeves")
        object.__setattr__(self, "allocation_horizons", horizons)

    @staticmethod
    def _validate_lookbacks(name: str, values: object) -> tuple[int, ...]:
        """Return one strictly increasing tuple of positive integer lookbacks."""
        if isinstance(values, (str, bytes)) or not isinstance(values, (tuple, list)):
            raise ValueError(f"{name} must be a sequence of positive integers")
        normalized = tuple(
            _require_int(f"{name}[{index}]", value, min_value=1)
            for index, value in enumerate(values)
        )
        if not normalized or any(right <= left for left, right in pairwise(normalized)):
            raise ValueError(f"{name} must be strictly increasing and non-empty")
        return normalized

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly policy snapshot for result auditing."""
        return {
            "allocation_mode": self.allocation_mode,
            "single_lookbacks": list(self.single_lookbacks),
            "allocation_horizons": [
                list(values) for values in self.allocation_horizons
            ],
            "drawdown_alert": self.drawdown_alert,
            "confirmed_drawdown": self.confirmed_drawdown,
            "drawdown_confirmations": self.drawdown_confirmations,
            "emergency_drawdown": self.emergency_drawdown,
            "adv_lookback": self.adv_lookback,
            "max_order_adv_ratio": self.max_order_adv_ratio,
        }


class _ConfirmedDrawdownRiskManager(PersistentRiskManager):
    """Require sustained stress unless an emergency threshold is breached."""

    def __init__(self, cfg: dict, policy: _PortfolioPolicyBase) -> None:
        super().__init__(cfg)
        self.policy = policy
        self.breach_streak = 0
        self.alert_active = False
        self.audit_events: list[dict[str, Any]] = []

    def _record_alert_state(self, date_str: str, drawdown: float, active: bool) -> None:
        """Record threshold crossings without changing portfolio exposure."""
        event = (
            "portfolio_drawdown_alert_on" if active else "portfolio_drawdown_alert_off"
        )
        self.audit_events.append(
            {
                "date": date_str,
                "event": event,
                "drawdown": float(drawdown),
                "threshold": self.policy.drawdown_alert,
            }
        )

    def _activate_lock(self, date_str: str, drawdown: float, trigger: str) -> str:
        """Persist the hard lock and expose its exact trigger for the audit trail."""
        self.persistent_lock = True
        self.lock_date = date_str
        self.lock_drawdown = float(drawdown)
        self.audit_events.append(
            {
                "date": date_str,
                "event": trigger,
                "drawdown": float(drawdown),
                "breach_streak": int(self.breach_streak),
            }
        )
        return "portfolio drawdown circuit breaker"

    def check_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        trading_dates: list[pd.Timestamp] | None = None,
        date_to_pos: dict[pd.Timestamp, int] | None = None,
    ) -> str | None:
        """Apply shadow alert, sustained confirmation, and emergency lock rules."""
        del trading_dates, date_to_pos
        self.peak_assets = max(self.peak_assets, float(current_assets))
        if self.persistent_lock:
            return "persistent portfolio risk lock"
        if self.peak_assets <= 0:
            return None
        drawdown = (self.peak_assets - current_assets) / self.peak_assets
        above_alert = drawdown >= self.policy.drawdown_alert
        if above_alert != self.alert_active:
            self.alert_active = above_alert
            self._record_alert_state(date_str, drawdown, above_alert)
        if drawdown >= self.policy.emergency_drawdown:
            return self._activate_lock(
                date_str, drawdown, "emergency_portfolio_drawdown_lock"
            )
        self.breach_streak = (
            self.breach_streak + 1 if drawdown >= self.policy.confirmed_drawdown else 0
        )
        if self.breach_streak < self.policy.drawdown_confirmations:
            return None
        return self._activate_lock(
            date_str, drawdown, "confirmed_portfolio_drawdown_lock"
        )

    def drain_audit_events(self) -> list[dict[str, Any]]:
        """Move newly generated manager events into the engine-level audit log."""
        events = list(self.audit_events)
        self.audit_events = []
        return events


class _EnsembleSleeveBacktestEngine(_CausalBacktestEngine):
    """Run one independently funded horizon sleeve."""

    ENGINE_LABEL = "Quant Fusion"

    def __init__(
        self,
        initial_capital: float,
        *,
        cfg: dict | None,
        policy: _PortfolioPolicyBase,
        allocation_lookbacks: tuple[int, ...],
        sleeve_name: str,
    ) -> None:
        self.policy = policy
        self.sleeve_name = sleeve_name
        self.ALLOCATION_LOOKBACKS = tuple(allocation_lookbacks)
        self._execution_data_map: dict[str, pd.DataFrame] | None = None
        self._execution_date: pd.Timestamp | None = None
        self._adv_used: dict[tuple[str, str, str], int] = {}
        normalized_cfg = dict(cfg or {})
        normalized_cfg["max_drawdown"] = policy.confirmed_drawdown
        super().__init__(initial_capital=initial_capital, cfg=normalized_cfg)

    def _reset_run_state(self, symbols_dict: dict[str, str]) -> None:
        """Reset the ledger and all per-run liquidity reservations."""
        super()._reset_run_state(symbols_dict)
        self._adv_used = {}

    def _resolve_symbol_configs(
        self,
        symbols_dict: dict[str, str],
        per_symbol_config: dict[str, dict] | None,
        config_route: str,
    ) -> dict[str, dict]:
        """Install the confirmed manager after inherited route resolution."""
        resolved = super()._resolve_symbol_configs(
            symbols_dict, per_symbol_config, config_route
        )
        symbol_groups = dict(self.risk.symbol_groups)
        self.risk = _ConfirmedDrawdownRiskManager(self.cfg, self.policy)
        self.risk.configure_groups(symbol_groups)
        return resolved

    def _apply_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        all_dates: list[pd.Timestamp],
        date_to_pos: dict[pd.Timestamp, int],
        pending: list[tuple[Signal, BaseStrategy]],
    ) -> tuple[list[tuple[Signal, BaseStrategy]], bool, bool]:
        """Apply inherited liquidation handling and retain manager events."""
        outcome = super()._apply_portfolio_risk(
            current_assets, date_str, all_dates, date_to_pos, pending
        )
        if isinstance(self.risk, _ConfirmedDrawdownRiskManager):
            self.risk_events.extend(self.risk.drain_audit_events())
        return outcome

    def _adv_capacity(
        self,
        symbol: str,
        direction: str,
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
    ) -> tuple[int, float]:
        """Return a causal lot-rounded capacity from prior daily volumes only."""
        frame = data_map.get(symbol)
        if frame is None or "volume" not in frame.columns:
            return 0, 0.0
        raw_volume = cast(pd.Series, frame.loc[frame.index < date, "volume"])
        history = cast(pd.Series, pd.to_numeric(raw_volume, errors="coerce")).astype(
            "float64"
        )
        history = history.dropna()
        history = history.loc[history.gt(0)].tail(self.policy.adv_lookback)
        if history.empty:
            return 0, 0.0
        adv = float(history.mean())
        daily_capacity = _floor_to_lot(adv * self.policy.max_order_adv_ratio)
        key = (date.strftime("%Y-%m-%d"), symbol, direction)
        return max(daily_capacity - self._adv_used.get(key, 0), 0), adv

    def _remaining_adv_capacity(
        self,
        symbol: str,
        direction: str,
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
    ) -> int | None:
        """Return the unconsumed daily side-specific ADV budget."""
        capacity, _ = self._adv_capacity(symbol, direction, data_map, date)
        return capacity

    def _consume_adv(
        self, date_str: str, symbol: str, direction: str, shares: int
    ) -> None:
        """Reserve actual fills so later same-day orders cannot reuse capacity."""
        key = (date_str, symbol, direction)
        self._adv_used[key] = self._adv_used.get(key, 0) + int(shares)

    def _execute_buy(
        self,
        signal: Signal,
        strategy: BaseStrategy,
        date_str: str,
        data_map: dict[str, pd.DataFrame] | None = None,
        date: pd.Timestamp | None = None,
    ) -> bool:
        """Apply the causal ADV cap before exposure and cash checks."""
        adjusted_signal = signal
        if data_map is not None and date is not None:
            capacity, adv = self._adv_capacity(signal.symbol, "buy", data_map, date)
            if capacity <= 0:
                self._record_order_event(
                    date=date_str,
                    signal=signal,
                    event="rejected_no_prior_adv_capacity",
                )
                return False
            if capacity < signal.target_shares:
                adjusted_signal = replace(signal, target_shares=capacity)
                self._record_order_event(
                    date=date_str,
                    signal=signal,
                    event="clipped_to_adv_capacity",
                    requested_shares=int(signal.target_shares),
                    adjusted_shares=int(capacity),
                    prior_adv=adv,
                )
        before = len(self.trades)
        executed = super()._execute_buy(
            adjusted_signal, strategy, date_str, data_map, date
        )
        if executed and len(self.trades) > before:
            self._consume_adv(date_str, signal.symbol, "buy", self.trades[-1].shares)
        return executed

    def _execute_pending_signals(
        self,
        pending: list[tuple[Signal, BaseStrategy]],
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
        date_to_pos: dict[pd.Timestamp, int],
        directions: frozenset[str] | None = None,
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Expose execution context so inherited sell calls can use prior ADV."""
        self._execution_data_map = data_map
        self._execution_date = date
        try:
            return super()._execute_pending_signals(
                pending, data_map, date, date_to_pos, directions
            )
        finally:
            self._execution_data_map = None
            self._execution_date = None

    def _execute_sell(
        self, signal: Signal, strategy: BaseStrategy | None, date_str: str
    ) -> int:
        """Fill a sell from the remaining shared ADV budget."""
        data_map = self._execution_data_map
        date = self._execution_date
        if data_map is None or date is None:
            return super()._execute_sell(signal, strategy, date_str)
        capacity, adv = self._adv_capacity(signal.symbol, "sell", data_map, date)
        if capacity <= 0:
            self._record_order_event(
                date=date_str,
                signal=signal,
                event="deferred_sell_no_prior_adv_capacity",
            )
            return 0
        adjusted_signal = signal
        if capacity < signal.target_shares:
            adjusted_signal = replace(signal, target_shares=capacity)
            self._record_order_event(
                date=date_str,
                signal=signal,
                event="clipped_to_adv_capacity",
                requested_shares=int(signal.target_shares),
                adjusted_shares=int(capacity),
                prior_adv=adv,
            )
        shares_sold = super()._execute_sell(adjusted_signal, strategy, date_str)
        if shares_sold > 0:
            self._consume_adv(date_str, signal.symbol, "sell", shares_sold)
        return shares_sold

    def _build_result(self, final_assets: float, all_dates: list[pd.Timestamp]) -> dict:
        """Add sleeve, risk, and liquidity metadata to the inherited result."""
        result = super()._build_result(final_assets, all_dates)
        result.update(
            {
                "allocation_mode": "single",
                "sleeve_name": self.sleeve_name,
                "allocation_lookbacks": list(self.ALLOCATION_LOOKBACKS),
                "sleeve_policy": self._policy_snapshot("single"),
            }
        )
        return result

    def _policy_snapshot(self, allocation_mode: str) -> dict[str, Any]:
        """Report the effective runtime mode instead of only the policy default."""
        snapshot = self.policy.as_dict()
        snapshot["allocation_mode"] = allocation_mode
        return snapshot


@dataclass(frozen=True)
class _RunRequest:
    """Carry an inherited run contract through the ensemble coordinator."""

    symbols_dict: dict[str, str]
    start_date: str
    end_date: str
    per_symbol_config: dict[str, dict] | None
    profile: str | None
    config_route: str
    data_dir: str | None
    indicator_state: str
    warmup_calendar_days: int
    risk_state: dict | None = None
    # DEPRECATED: account_state is always None now — the public run() API
    # raises NotImplementedError before this field is ever set. Retained for
    # API compatibility until the account signal engine is built.
    account_state: AccountState | None = None


@dataclass
class _PreparedSleeveRun:
    """Hold one funded sleeve's state during synchronized portfolio replay."""

    sleeve: _EnsembleSleeveBacktestEngine
    data_map: dict[str, pd.DataFrame]
    indicator_map: dict[str, dict[str, pd.Series]]
    all_dates: list[pd.Timestamp]
    date_to_pos: dict[pd.Timestamp, int]
    pending: list[tuple[Signal, BaseStrategy]] = field(default_factory=list)


class _EnsembleBacktestEngine(_EnsembleSleeveBacktestEngine):
    """Coordinate one sleeve or an equal-capital ensemble of sleeves."""

    def __init__(
        self,
        initial_capital: float = 2_000_000,
        cfg: dict | None = None,
        policy: _PortfolioPolicyBase | None = None,
    ) -> None:
        supplied_policy = policy is not None
        resolved_policy = policy or _PortfolioPolicyBase()
        raw_cfg = dict(cfg or {})
        if "max_drawdown" in raw_cfg:
            configured_drawdown = _require_positive_ratio(
                "max_drawdown", raw_cfg["max_drawdown"]
            )
            if supplied_policy and not math.isclose(
                configured_drawdown, resolved_policy.confirmed_drawdown
            ):
                raise ValueError(
                    "cfg['max_drawdown'] conflicts with policy.confirmed_drawdown"
                )
            resolved_policy = replace(
                resolved_policy, confirmed_drawdown=configured_drawdown
            )
        raw_cfg["max_drawdown"] = resolved_policy.confirmed_drawdown
        self._ensemble_user_cfg = raw_cfg
        self.sleeves: list[_EnsembleSleeveBacktestEngine] = []
        self.last_result: dict | None = None
        super().__init__(
            initial_capital,
            cfg=raw_cfg,
            policy=resolved_policy,
            allocation_lookbacks=resolved_policy.single_lookbacks,
            sleeve_name="single",
        )

    @staticmethod
    def _sleeve_name(index: int, total: int) -> str:
        """Return stable human-readable names for the default three sleeves."""
        if total == 3:
            return ("fast", "base", "slow")[index]
        return f"sleeve_{index + 1}"

    def _run_ensemble(self, request: _RunRequest) -> dict:
        """Require the concrete portfolio coordinator to build its sleeves."""
        del request
        raise NotImplementedError

    @staticmethod
    def _decorate_trade(trade: TradeRecord, sleeve: str) -> TradeRecord:
        """Preserve the base trade schema while making sleeve ownership explicit."""
        return replace(trade, strategy_name=f"{sleeve}:{trade.strategy_name}")

    @staticmethod
    def _decorate_signal(signal: Signal, sleeve: str) -> Signal:
        """Preserve pending-signal compatibility while adding the sleeve prefix."""
        return replace(signal, strategy_name=f"{sleeve}:{signal.strategy_name}")

    @staticmethod
    def _decorate_events(events: list[dict], sleeve: str) -> list[dict]:
        """Copy audit events and attach their source sleeve."""
        return [{"sleeve": sleeve, **dict(event)} for event in events]

    @staticmethod
    def _sort_events(events: list[dict]) -> list[dict]:
        """Return a chronological deterministic audit trail across all sleeves."""
        return sorted(
            events,
            key=lambda event: (
                str(event.get("date", "")),
                str(event.get("sleeve", "")),
                str(event.get("event", "")),
            ),
        )

    def _aggregate_sleeve_results(self, results: list[dict]) -> dict:
        """Build standard portfolio metrics from independent sleeve ledgers."""
        if not results:
            raise RuntimeError("ensemble produced no sleeve results")
        reference_index = results[0]["equity_curve"].index
        if any(
            not result["equity_curve"].index.equals(reference_index)
            for result in results
        ):
            raise RuntimeError("ensemble sleeves produced different equity calendars")
        equity = pd.DataFrame(index=reference_index)
        for column in ("assets", "cash", "position_value"):
            equity[column] = sum(
                result["equity_curve"][column].astype(float) for result in results
            )
        final_assets = float(equity["assets"].iloc[-1])
        total_return = final_assets / self.initial_capital - 1.0
        trading_days = len(equity)
        annual_return = (
            (1.0 + total_return) ** (252 / max(trading_days, 1)) - 1.0
            if total_return > -1.0
            else -1.0
        )
        peak = equity["assets"].cummax()
        drawdown = (equity["assets"] - peak) / peak
        daily_returns = equity["assets"].pct_change().dropna()
        sharpe = 0.0
        if daily_returns.std() > 0:
            risk_free = float(self.cfg.get("risk_free_rate", 0.0))
            daily_rf = (1.0 + risk_free) ** (1 / 252) - 1.0
            sharpe = float(
                (daily_returns - daily_rf).mean() / daily_returns.std() * math.sqrt(252)
            )
        max_drawdown = float(drawdown.min())
        calmar = annual_return / abs(max_drawdown) if max_drawdown < 0 else 0.0
        names = [result["sleeve_name"] for result in results]
        trades = [
            self._decorate_trade(trade, name)
            for name, result in zip(names, results, strict=True)
            for trade in result["trades"]
        ]
        trades.sort(key=lambda trade: (trade.date, trade.symbol, trade.strategy_name))
        sell_trades = [trade for trade in trades if trade.direction == "sell"]
        wins = [trade for trade in sell_trades if trade.pnl > 0]
        losses = [trade for trade in sell_trades if trade.pnl < 0]
        decisive = len(wins) + len(losses)
        total_win = sum(trade.pnl for trade in wins)
        total_loss = abs(sum(trade.pnl for trade in losses))
        givebacks = [
            float(trade.exit_from_peak_pct)
            for trade in sell_trades
            if _is_finite_number(trade.exit_from_peak_pct)
        ]
        pending_signals = [
            self._decorate_signal(signal, name)
            for name, result in zip(names, results, strict=True)
            for signal in result.get("pending_signals", [])
        ]
        risk_events = [
            event
            for name, result in zip(names, results, strict=True)
            for event in self._decorate_events(result.get("risk_events", []), name)
        ]
        order_events = [
            event
            for name, result in zip(names, results, strict=True)
            for event in self._decorate_events(result.get("order_events", []), name)
        ]
        fusion_events = [
            event
            for name, result in zip(names, results, strict=True)
            for event in self._decorate_events(result.get("fusion_events", []), name)
        ]
        risk_events = self._sort_events(risk_events)
        order_events = self._sort_events(order_events)
        fusion_events = self._sort_events(fusion_events)
        locked_sleeves = [
            name
            for name, result in zip(names, results, strict=True)
            if result.get("persistent_risk_lock", False)
        ]
        return {
            "allocation_mode": "ensemble",
            "allocation_lookbacks": [
                list(values) for values in self.policy.allocation_horizons
            ],
            "sleeve_policy": self._policy_snapshot("ensemble"),
            "portfolio_max_order_adv_ratio": self.policy.max_order_adv_ratio,
            "per_sleeve_max_order_adv_ratio": (
                self.policy.max_order_adv_ratio / len(results)
            ),
            "indicator_state": results[0]["indicator_state"],
            "initial_capital": self.initial_capital,
            "final_assets": final_assets,
            "total_return": total_return,
            "annual_return": annual_return,
            "max_drawdown": max_drawdown,
            "sharpe": sharpe,
            "calmar": calmar,
            "win_rate": len(wins) / decisive if decisive else 0.0,
            "profit_factor": total_win / total_loss if total_loss > 0 else float("inf"),
            "total_trades": len(trades),
            "sell_trades": len(sell_trades),
            "avg_exit_from_peak": float(np.mean(givebacks)) if givebacks else 0.0,
            "worst_exit_from_peak": float(min(givebacks)) if givebacks else 0.0,
            "open_positions": sum(result["open_positions"] for result in results),
            "open_position_value": sum(
                float(result["open_position_value"]) for result in results
            ),
            "period_end_valuation": "mark_to_market",
            "equity_curve": equity,
            "drawdown_series": drawdown,
            "trades": trades,
            "pending_signals": pending_signals,
            "trade_cash_scope": "sleeve",
            "parameter_routes": dict(results[0].get("parameter_routes", {})),
            "unmapped_symbols": sorted(
                {
                    code
                    for result in results
                    for code in result.get("unmapped_symbols", [])
                }
            ),
            "fusion_events": fusion_events,
            "risk_events": risk_events,
            "order_events": order_events,
            "sector_guard_active": any(
                result.get("sector_guard_active", False) for result in results
            ),
            "safe_mode_active": any(
                result.get("safe_mode_active", False) for result in results
            ),
            "persistent_risk_lock": any(
                result.get("persistent_risk_lock", False) for result in results
            ),
            "all_sleeves_locked": len(locked_sleeves) == len(results),
            "locked_sleeves": locked_sleeves,
            "reversal_exit_trades": sum(
                int(result.get("reversal_exit_trades", 0)) for result in results
            ),
            "resolved_symbol_configs": {
                name: result.get("resolved_symbol_configs", {})
                for name, result in zip(names, results, strict=True)
            },
            "sleeve_summaries": [
                {
                    "name": name,
                    "allocation_lookbacks": result["allocation_lookbacks"],
                    "initial_capital": result["initial_capital"],
                    "final_assets": result["final_assets"],
                    "total_return": result["total_return"],
                    "max_drawdown": result["max_drawdown"],
                    "persistent_risk_lock": result["persistent_risk_lock"],
                    "max_order_adv_ratio": result["sleeve_policy"][
                        "max_order_adv_ratio"
                    ],
                }
                for name, result in zip(names, results, strict=True)
            ],
        }

    def run(  # noqa: PLR0913 - Keep the inherited public API compatible.
        self,
        symbols_dict: dict[str, str],
        start_date: str,
        end_date: str,
        per_symbol_config: dict[str, dict] | None = None,
        profile: str | None = None,
        config_route: str = "auto",
        data_dir: str | None = None,
        *,
        indicator_state: str = "cold",
        warmup_calendar_days: int = 365,
        allocation_mode: str | None = None,
        risk_state: dict | None = None,
        account_state: AccountState | None = None,
    ) -> dict:
        """Run the configured single sleeve or the default three-sleeve ensemble."""
        if account_state is not None:
            raise NotImplementedError(
                "Real-account mode (account_state) is disabled. "
                "The account injection logic has known architecture defects. "
                "Use simulation mode (account_state=None) instead. "
                "A separate account signal engine is planned."
            )
        mode = str(allocation_mode or self.policy.allocation_mode).lower()
        if mode not in {"single", "ensemble"}:
            raise ValueError("allocation_mode must be 'single' or 'ensemble'")
        if mode == "single":
            self.sleeves = [self]
            if risk_state:
                self.cfg = dict(self.cfg)
                self.cfg["_initial_risk_state"] = risk_state
            if account_state:
                self._initial_positions = self._apply_account_state(
                    account_state, self
                )
            result = super().run(
                symbols_dict,
                start_date,
                end_date,
                per_symbol_config=per_symbol_config,
                profile=profile,
                config_route=config_route,
                data_dir=data_dir,
                indicator_state=indicator_state,
                warmup_calendar_days=warmup_calendar_days,
            )
            result["allocation_mode"] = "single"
            result["sleeve_policy"] = self._policy_snapshot("single")
            self.last_result = result
            return result
        return self._run_ensemble(
            _RunRequest(
                symbols_dict=symbols_dict,
                start_date=start_date,
                end_date=end_date,
                per_symbol_config=per_symbol_config,
                profile=profile,
                config_route=config_route,
                data_dir=data_dir,
                indicator_state=indicator_state,
                warmup_calendar_days=warmup_calendar_days,
                risk_state=risk_state,
                account_state=account_state,
            )
        )


# Universe-invariant portfolio coordination.


@dataclass(frozen=True)
class PortfolioPolicy(_PortfolioPolicyBase):
    """Define recoverable cycle risk and a separate terminal loss boundary."""

    allocation_horizons: tuple[tuple[int, ...], ...] = (
        (3, 5, 10),
        (5, 10, 20),
        (5, 20, 60),
    )
    candidate_lookbacks: tuple[int, ...] = (10, 20, 40)
    candidate_horizons: tuple[tuple[int, ...], ...] = (
        (10, 20, 40),
        (10, 20, 40),
        (10, 40, 80),
    )
    drawdown_alert: float = 0.18
    confirmed_drawdown: float = 0.23
    emergency_drawdown: float = 0.27
    rearm_trading_days: int = 10
    terminal_drawdown: float = 0.28
    concentration_drawdown_adjustment: float = 0.02
    candidate_reference_percentile: float = 0.50
    regime_symbols: tuple[str, ...] = (
        "300308",
        "300502",
        "300394",
        "688008",
        "603986",
    )
    # Market regime recognition controls (propagated to cfg at runtime so the
    # mixin reads them via self.cfg.get(...); the policy snapshot stays auditable).
    # Enabled by default — see _default_config() for rationale.
    market_regime_enabled: bool = True
    regime_ewi_lookback: int = 20
    regime_breadth_ma_long: int = 20
    regime_adx_trend: float = 25
    regime_adx_choppy: float = 20
    regime_hurst_window: int = 100
    regime_hurst_trend: float = 0.55
    regime_hurst_choppy: float = 0.45
    regime_vol_lookback: int = 60
    regime_vol_extreme_pct: float = 0.9
    regime_ewi_slope_trend: float = 0.02
    regime_ewi_slope_choppy: float = -0.02
    regime_score_trend: int = 2
    regime_score_choppy: int = -3
    regime_choppy_confirmations: int = 2
    regime_trend_confirmations: int = 3
    regime_recovery_confirmations: int = 3
    regime_min_state_hold: int = 3
    regime_transition_scale: float = 1.0
    regime_trend_to_transition_confirmations: int = 3
    regime_choppy_exit_ratio: float = 0.3
    regime_transition_exit_ratio: float = 0.0

    def __post_init__(self) -> None:
        """Validate inherited controls and the portfolio recovery constraints."""
        super().__post_init__()
        rearm_days = _require_int(
            "rearm_trading_days", self.rearm_trading_days, min_value=1
        )
        terminal = _require_positive(
            "terminal_drawdown", self.terminal_drawdown, max_value=1.0
        )
        if terminal < self.confirmed_drawdown:
            raise ValueError("terminal_drawdown must not be below confirmed_drawdown")
        concentration_adjustment = _require_finite(
            "concentration_drawdown_adjustment",
            self.concentration_drawdown_adjustment,
            min_value=0.0,
            max_value=self.confirmed_drawdown,
        )
        if self.confirmed_drawdown - concentration_adjustment <= self.drawdown_alert:
            raise ValueError(
                "concentration_drawdown_adjustment leaves no room above "
                "drawdown_alert for a one-symbol portfolio"
            )
        reference_percentile = _require_finite(
            "candidate_reference_percentile",
            self.candidate_reference_percentile,
            min_value=0.0,
            max_value=1.0,
        )
        regime_symbols = tuple(str(symbol) for symbol in self.regime_symbols)
        if not regime_symbols:
            raise ValueError("regime_symbols must contain at least one symbol")
        if len(set(regime_symbols)) != len(regime_symbols):
            raise ValueError("regime_symbols must not contain duplicates")
        if any(re.fullmatch(r"\d{6}", symbol) is None for symbol in regime_symbols):
            raise ValueError("every regime symbol must be a six-digit code")
        object.__setattr__(self, "rearm_trading_days", rearm_days)
        object.__setattr__(self, "terminal_drawdown", terminal)
        object.__setattr__(
            self,
            "candidate_lookbacks",
            self._validate_lookbacks("candidate_lookbacks", self.candidate_lookbacks),
        )
        candidate_horizons = tuple(
            self._validate_lookbacks(f"candidate_horizons[{index}]", values)
            for index, values in enumerate(self.candidate_horizons)
        )
        if len(candidate_horizons) != len(self.allocation_horizons):
            raise ValueError(
                "candidate_horizons must align one-for-one with allocation_horizons"
            )
        object.__setattr__(self, "candidate_horizons", candidate_horizons)
        object.__setattr__(
            self, "concentration_drawdown_adjustment", concentration_adjustment
        )
        object.__setattr__(self, "candidate_reference_percentile", reference_percentile)
        object.__setattr__(self, "regime_symbols", regime_symbols)
        self._validate_market_regime_fields()

    def _validate_market_regime_fields(self) -> None:
        """Validate the market-regime controls and freeze normalized values."""
        market_regime_enabled = _require_bool(
            "market_regime_enabled", self.market_regime_enabled
        )
        object.__setattr__(self, "market_regime_enabled", market_regime_enabled)
        ewi_lookback = _require_int(
            "regime_ewi_lookback", self.regime_ewi_lookback, min_value=2
        )
        breadth_ma_long = _require_int(
            "regime_breadth_ma_long", self.regime_breadth_ma_long, min_value=1
        )
        hurst_window = _require_int(
            "regime_hurst_window", self.regime_hurst_window, min_value=10
        )
        vol_lookback = _require_int(
            "regime_vol_lookback", self.regime_vol_lookback, min_value=2
        )
        score_trend = _require_int(
            "regime_score_trend", self.regime_score_trend, min_value=-10
        )
        score_choppy = _require_int(
            "regime_score_choppy", self.regime_score_choppy, min_value=-10
        )
        choppy_confirmations = _require_int(
            "regime_choppy_confirmations",
            self.regime_choppy_confirmations,
            min_value=1,
        )
        trend_confirmations = _require_int(
            "regime_trend_confirmations",
            self.regime_trend_confirmations,
            min_value=1,
        )
        recovery_confirmations = _require_int(
            "regime_recovery_confirmations",
            self.regime_recovery_confirmations,
            min_value=1,
        )
        min_state_hold = _require_int(
            "regime_min_state_hold", self.regime_min_state_hold, min_value=1
        )
        adx_trend = _require_finite(
            "regime_adx_trend", self.regime_adx_trend, min_value=0.0
        )
        adx_choppy = _require_finite(
            "regime_adx_choppy", self.regime_adx_choppy, min_value=0.0
        )
        hurst_trend = _require_finite(
            "regime_hurst_trend",
            self.regime_hurst_trend,
            min_value=0.0,
            max_value=1.0,
        )
        hurst_choppy = _require_finite(
            "regime_hurst_choppy",
            self.regime_hurst_choppy,
            min_value=0.0,
            max_value=1.0,
        )
        vol_extreme_pct = _require_positive(
            "regime_vol_extreme_pct",
            self.regime_vol_extreme_pct,
            max_value=1.0,
            inclusive_max=True,
        )
        ewi_slope_trend = _require_finite(
            "regime_ewi_slope_trend",
            self.regime_ewi_slope_trend,
            min_value=-1.0,
            max_value=1.0,
        )
        ewi_slope_choppy = _require_finite(
            "regime_ewi_slope_choppy",
            self.regime_ewi_slope_choppy,
            min_value=-1.0,
            max_value=1.0,
        )
        transition_scale = _require_finite(
            "regime_transition_scale",
            self.regime_transition_scale,
            min_value=0.0,
            max_value=1.0,
        )
        trend_to_transition = _require_int(
            "regime_trend_to_transition_confirmations",
            self.regime_trend_to_transition_confirmations,
            min_value=1,
        )
        choppy_exit_ratio = _require_finite(
            "regime_choppy_exit_ratio",
            self.regime_choppy_exit_ratio,
            min_value=0.0,
            max_value=1.0,
        )
        transition_exit_ratio = _require_finite(
            "regime_transition_exit_ratio",
            self.regime_transition_exit_ratio,
            min_value=0.0,
            max_value=1.0,
        )
        if adx_trend <= adx_choppy:
            raise ValueError("regime_adx_trend must be greater than regime_adx_choppy")
        if hurst_trend <= hurst_choppy:
            raise ValueError(
                "regime_hurst_trend must be greater than regime_hurst_choppy"
            )
        if ewi_slope_trend <= ewi_slope_choppy:
            raise ValueError(
                "regime_ewi_slope_trend must be greater than regime_ewi_slope_choppy"
            )
        object.__setattr__(self, "regime_ewi_lookback", ewi_lookback)
        object.__setattr__(self, "regime_breadth_ma_long", breadth_ma_long)
        object.__setattr__(self, "regime_hurst_window", hurst_window)
        object.__setattr__(self, "regime_vol_lookback", vol_lookback)
        object.__setattr__(self, "regime_score_trend", score_trend)
        object.__setattr__(self, "regime_score_choppy", score_choppy)
        object.__setattr__(self, "regime_choppy_confirmations", choppy_confirmations)
        object.__setattr__(self, "regime_trend_confirmations", trend_confirmations)
        object.__setattr__(self, "regime_recovery_confirmations", recovery_confirmations)
        object.__setattr__(self, "regime_min_state_hold", min_state_hold)
        object.__setattr__(self, "regime_adx_trend", adx_trend)
        object.__setattr__(self, "regime_adx_choppy", adx_choppy)
        object.__setattr__(self, "regime_hurst_trend", hurst_trend)
        object.__setattr__(self, "regime_hurst_choppy", hurst_choppy)
        object.__setattr__(self, "regime_vol_extreme_pct", vol_extreme_pct)
        object.__setattr__(self, "regime_ewi_slope_trend", ewi_slope_trend)
        object.__setattr__(self, "regime_ewi_slope_choppy", ewi_slope_choppy)
        object.__setattr__(self, "regime_transition_scale", transition_scale)
        object.__setattr__(
            self, "regime_trend_to_transition_confirmations", trend_to_transition
        )
        object.__setattr__(self, "regime_choppy_exit_ratio", choppy_exit_ratio)
        object.__setattr__(
            self, "regime_transition_exit_ratio", transition_exit_ratio
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a complete JSON-friendly portfolio policy snapshot."""
        snapshot = super().as_dict()
        snapshot.update(
            {
                "rearm_trading_days": self.rearm_trading_days,
                "terminal_drawdown": self.terminal_drawdown,
                "candidate_lookbacks": list(self.candidate_lookbacks),
                "candidate_horizons": [
                    list(values) for values in self.candidate_horizons
                ],
                "concentration_drawdown_adjustment": (
                    self.concentration_drawdown_adjustment
                ),
                "candidate_reference_percentile": (self.candidate_reference_percentile),
                "regime_symbols": list(self.regime_symbols),
                "market_regime_enabled": self.market_regime_enabled,
                "regime_ewi_lookback": self.regime_ewi_lookback,
                "regime_breadth_ma_long": self.regime_breadth_ma_long,
                "regime_adx_trend": self.regime_adx_trend,
                "regime_adx_choppy": self.regime_adx_choppy,
                "regime_hurst_window": self.regime_hurst_window,
                "regime_hurst_trend": self.regime_hurst_trend,
                "regime_hurst_choppy": self.regime_hurst_choppy,
                "regime_vol_lookback": self.regime_vol_lookback,
                "regime_vol_extreme_pct": self.regime_vol_extreme_pct,
                "regime_ewi_slope_trend": self.regime_ewi_slope_trend,
                "regime_ewi_slope_choppy": self.regime_ewi_slope_choppy,
                "regime_score_trend": self.regime_score_trend,
                "regime_score_choppy": self.regime_score_choppy,
                "regime_choppy_confirmations": self.regime_choppy_confirmations,
                "regime_trend_confirmations": self.regime_trend_confirmations,
                "regime_recovery_confirmations": self.regime_recovery_confirmations,
                "regime_min_state_hold": self.regime_min_state_hold,
                "regime_transition_scale": self.regime_transition_scale,
                "regime_trend_to_transition_confirmations": (
                    self.regime_trend_to_transition_confirmations
                ),
                "regime_choppy_exit_ratio": self.regime_choppy_exit_ratio,
                "regime_transition_exit_ratio": self.regime_transition_exit_ratio,
            }
        )
        return snapshot


class RecoverableDrawdownRiskManager(_ConfirmedDrawdownRiskManager):
    """Rearm cycle locks after a cooldown but preserve a lifetime hard stop."""

    def __init__(self, cfg: dict, policy: PortfolioPolicy) -> None:
        super().__init__(cfg, policy)
        self.policy = policy
        self.lifetime_peak_assets = 0.0
        self.lock_start_position: int | None = None
        self.terminal_lock = False
        self.cycle_lock_count = 0

    @staticmethod
    def _date_position(
        date_str: str,
        trading_dates: list[pd.Timestamp] | None,
        date_to_pos: dict[pd.Timestamp, int] | None,
    ) -> int | None:
        """Resolve the current trading position without calendar-day assumptions."""
        try:
            timestamp = pd.Timestamp(date_str)
        except (TypeError, ValueError):
            return None
        if not isinstance(timestamp, pd.Timestamp):
            return None
        if date_to_pos is not None:
            return date_to_pos.get(timestamp)
        if trading_dates is None:
            return None
        try:
            return trading_dates.index(timestamp)
        except ValueError:
            return None

    def _activate_cycle_lock(
        self, date_str: str, drawdown: float, position: int | None, trigger: str
    ) -> str:
        """Enter a temporary cash lock and record its exact causal trigger."""
        self.persistent_lock = True
        self.terminal_lock = False
        self.lock_date = date_str
        self.lock_drawdown = float(drawdown)
        self.lock_start_position = position
        self.cycle_lock_count += 1
        self.audit_events.append(
            {
                "date": date_str,
                "event": trigger,
                "drawdown": float(drawdown),
                "breach_streak": int(self.breach_streak),
                "cycle_lock_count": int(self.cycle_lock_count),
            }
        )
        return "portfolio drawdown circuit breaker"

    def _activate_terminal_lock(
        self, date_str: str, drawdown: float, position: int | None
    ) -> str:
        """Enter the non-rearming lifetime safety lock."""
        self.persistent_lock = True
        self.terminal_lock = True
        self.lock_date = date_str
        self.lock_drawdown = float(drawdown)
        self.lock_start_position = position
        self.audit_events.append(
            {
                "date": date_str,
                "event": "terminal_portfolio_drawdown_lock",
                "drawdown": float(drawdown),
                "threshold": self.policy.terminal_drawdown,
            }
        )
        return "portfolio drawdown circuit breaker"

    def _try_rearm(
        self, date_str: str, current_assets: float, position: int | None
    ) -> bool:
        """Reset the cycle high-water mark after the required cash cooldown."""
        if self.terminal_lock or self.lock_start_position is None or position is None:
            return False
        elapsed = position - self.lock_start_position
        if elapsed < self.policy.rearm_trading_days:
            return False
        self.persistent_lock = False
        self.lock_date = None
        self.lock_drawdown = 0.0
        self.lock_start_position = None
        self.peak_assets = float(current_assets)
        self.breach_streak = 0
        self.alert_active = False
        self.audit_events.append(
            {
                "date": date_str,
                "event": "portfolio_drawdown_rearmed",
                "cooldown_trading_days": int(elapsed),
                "cycle_lock_count": int(self.cycle_lock_count),
            }
        )
        return True

    def check_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        trading_dates: list[pd.Timestamp] | None = None,
        date_to_pos: dict[pd.Timestamp, int] | None = None,
    ) -> str | None:
        """Apply temporary cycle defense before the lifetime terminal boundary."""
        assets = float(current_assets)
        self.lifetime_peak_assets = max(self.lifetime_peak_assets, assets)
        position = self._date_position(date_str, trading_dates, date_to_pos)
        if self.persistent_lock:
            if self._try_rearm(date_str, assets, position):
                return None
            return "persistent portfolio risk lock"

        self.peak_assets = max(self.peak_assets, assets)
        lifetime_drawdown = (
            (self.lifetime_peak_assets - assets) / self.lifetime_peak_assets
            if self.lifetime_peak_assets > 0
            else 0.0
        )
        if lifetime_drawdown >= self.policy.terminal_drawdown:
            return self._activate_terminal_lock(date_str, lifetime_drawdown, position)
        if self.peak_assets <= 0:
            return None
        cycle_drawdown = (self.peak_assets - assets) / self.peak_assets
        above_alert = cycle_drawdown >= self.policy.drawdown_alert
        if above_alert != self.alert_active:
            self.alert_active = above_alert
            self._record_alert_state(date_str, cycle_drawdown, above_alert)
        if cycle_drawdown >= self.policy.emergency_drawdown:
            return self._activate_cycle_lock(
                date_str,
                cycle_drawdown,
                position,
                "emergency_cycle_drawdown_lock",
            )
        self.breach_streak = (
            self.breach_streak + 1
            if cycle_drawdown >= self.policy.confirmed_drawdown
            else 0
        )
        if self.breach_streak < self.policy.drawdown_confirmations:
            return None
        return self._activate_cycle_lock(
            date_str,
            cycle_drawdown,
            position,
            "confirmed_cycle_drawdown_lock",
        )


class _UniverseInvariantSleeveMixin:
    """Share portfolio risk and breadth behavior across coordinator and child sleeves."""

    policy: PortfolioPolicy
    cfg: dict[str, Any]
    risk: RiskManager
    risk_events: list[dict[str, Any]]
    _risk_lock_logged: bool

    def _reset_run_state(self, symbols_dict: dict[str, str]) -> None:
        """Reset tradable and regime metadata at every independent run."""
        # Concrete classes place this cooperative mixin before the sleeve engine.
        super()._reset_run_state(  # pyright: ignore[reportAttributeAccessIssue]
            symbols_dict
        )
        self._tradable_symbol_codes: set[str] = set(symbols_dict)
        self._candidate_score_series: dict[str, dict[int, pd.Series]] = {}
        # Market regime state machine: start in TREND (full trading) and let the
        # basket indicators demote the state when conditions deteriorate.
        self._regime_state: str = "TREND"
        self._regime_state_series: list[dict[str, Any]] = []
        self._regime_indicator_series: dict[str, pd.Series] = {}
        self._regime_to_choppy_streak: int = 0
        self._regime_to_trend_streak: int = 0
        self._regime_non_choppy_streak: int = 0
        self._regime_to_transition_streak: int = 0
        self._regime_state_start_pos: int = 0
        self._regime_prev_state: str = "TREND"
        self._regime_latest_observation: MarketRegimeObservation | None = None
        # ── DEPRECATED: Account snapshot injection ─────────────────────
        # The public run() API now raises NotImplementedError when
        # account_state is passed. The injection logic below is dead code
        # retained for reference until the separate account signal engine
        # is built. Do NOT re-enable without reading the architecture
        # defects documented in BACKTEST_RESULTS.md.
        self._account_state_to_inject: AccountState | None = None
        self._account_state_injected: bool = False
        self._regime_effective_state: str = "TREND"

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
        """Inject the account snapshot on the as-of date before delegating.

        When ``_account_state_to_inject`` is set and the current date is the
        second-to-last trading day (the day before the simulation end / as-of
        date), the real account state replaces the simulated book before the
        day's opening execution.  Every date before the injection point runs
        as an unmodified simulation so historical signal generation is not
        affected by the snapshot.
        """
        if (
            self._account_state_to_inject is not None
            and not self._account_state_injected
        ):
            as_of_idx = max(0, len(all_dates) - 2)
            if date_to_pos.get(date, -1) >= as_of_idx:
                _CoreBacktestEngine._apply_account_state(
                    self._account_state_to_inject, self
                )
                self._account_state_injected = True
        return super()._process_trading_day(  # pyright: ignore[reportAttributeAccessIssue]
            symbols_dict,
            data_map,
            indicator_map,
            all_dates,
            date_to_pos,
            date,
            pending,
        )

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
        """Load a fixed signal-only regime basket beside tradable symbols."""
        self._tradable_symbol_codes = set(symbols_dict)
        combined = dict(symbols_dict)
        for code in self.policy.regime_symbols:
            combined.setdefault(code, code)
        prepared = super()._prepare_run(  # pyright: ignore[reportAttributeAccessIssue]
            combined,
            start_date,
            end_date,
            start_ts,
            end_ts,
            per_symbol_config,
            profile,
            config_route,
            data_dir,
        )
        self._tradable_symbol_codes = set(symbols_dict)
        self._candidate_score_series = self._build_candidate_score_series(prepared[0])
        self._regime_indicator_series = self._build_regime_indicator_series(prepared[0])
        return prepared

    def _build_candidate_score_series(
        self, data_map: dict[str, pd.DataFrame]
    ) -> dict[str, dict[int, pd.Series]]:
        """Precompute causal multi-horizon risk-adjusted momentum series."""
        cache: dict[str, dict[int, pd.Series]] = {}
        for code, frame in data_map.items():
            close = frame["close"].astype(float)
            daily_returns = close.pct_change()
            cache[code] = {}
            for window in self.policy.candidate_lookbacks:
                volatility = daily_returns.rolling(window, min_periods=window).std()
                valid_volatility = volatility.where(volatility > 0)
                cache[code][window] = close.pct_change(window) / valid_volatility
        return cache

    @staticmethod
    def _rolling_slope_pct(series: pd.Series, window: int) -> pd.Series:
        """Return the rolling linear-regression slope as a window percentage.

        The per-bar slope of a least-squares fit over ``window`` observations is
        scaled by ``window`` and divided by the window mean so the result is
        comparable to a percentage move over the window (e.g. +0.02 for a 2%
        uptrend). The output is lag-safe: each bar uses only itself and prior
        bars.
        """
        x = np.arange(window, dtype=float)
        x_mean = x.mean()
        denom = float(((x - x_mean) ** 2).sum())
        if denom <= 0.0:
            return pd.Series(np.nan, index=series.index, dtype="float64")

        def _slope(y: np.ndarray) -> float:
            y_arr = np.asarray(y, dtype=float)
            if y_arr.size != window or np.isnan(y_arr).any():
                return float("nan")
            y_mean = float(y_arr.mean())
            if y_mean <= 0.0:
                return float("nan")
            slope = float(((x - x_mean) * (y_arr - y_mean)).sum() / denom)
            return slope * window / y_mean

        return series.rolling(window, min_periods=window).apply(_slope, raw=True)

    def _build_regime_indicator_series(
        self, data_map: dict[str, pd.DataFrame]
    ) -> dict[str, pd.Series]:
        """Precompute the five causal market-regime indicator series.

        Every returned series is indexed by trading date and uses only data on
        or before that date (rolling windows end at the current bar), so the
        regime state machine never looks past the close it is scoring. The
        equal-weight basket index (EWI) cumulates the cross-sectional mean of
        daily returns across the fixed ``regime_symbols`` basket.
        """
        if not bool(self.cfg.get("market_regime_enabled", True)):
            return {}
        regime_codes = [code for code in self.policy.regime_symbols if code in data_map]
        if not regime_codes:
            return {}

        ewi_lookback = _require_int(
            "regime_ewi_lookback",
            int(self.cfg.get("regime_ewi_lookback", 20)),
            min_value=2,
        )
        breadth_ma = _require_int(
            "regime_breadth_ma_long",
            int(self.cfg.get("regime_breadth_ma_long", 20)),
            min_value=1,
        )
        adx_period = _require_int(
            "adx_period", int(self.cfg.get("adx_period", 14)), min_value=1
        )
        hurst_window = _require_int(
            "regime_hurst_window",
            int(self.cfg.get("regime_hurst_window", 100)),
            min_value=10,
        )
        vol_lookback = _require_int(
            "regime_vol_lookback",
            int(self.cfg.get("regime_vol_lookback", 60)),
            min_value=2,
        )

        close_frames: list[pd.Series] = []
        adx_frames: list[pd.Series] = []
        breadth_frames: list[pd.Series] = []
        for code in regime_codes:
            frame = data_map[code]
            close = pd.to_numeric(frame["close"], errors="coerce")
            close_frames.append(close)
            adx_frames.append(Indicators.adx(frame, adx_period))
            ma = close.rolling(breadth_ma, min_periods=breadth_ma).mean()
            breadth_frames.append((close > ma).astype(float))

        aligned_close = pd.concat(close_frames, axis=1, keys=regime_codes).sort_index()
        basket_return = aligned_close.pct_change().mean(axis=1, skipna=True)
        # Anchor the index at 1.0 and treat any fully-missing day as flat.
        basket_return = basket_return.fillna(0.0)
        ewi = (1.0 + basket_return).cumprod()

        ewi_slope = self._rolling_slope_pct(ewi, ewi_lookback)
        breadth = pd.concat(breadth_frames, axis=1, keys=regime_codes).mean(
            axis=1, skipna=True
        )
        adx_median = pd.concat(adx_frames, axis=1, keys=regime_codes).median(
            axis=1, skipna=True
        )

        ewi_log_returns = np.log(ewi / ewi.shift(1))
        hurst = ewi_log_returns.rolling(
            hurst_window, min_periods=hurst_window
        ).apply(lambda arr: Indicators.hurst_rs(arr, hurst_window), raw=True)

        volatility = ewi_log_returns.rolling(
            vol_lookback, min_periods=vol_lookback
        ).std()
        vol_percentile = volatility.rolling(
            vol_lookback, min_periods=vol_lookback
        ).rank(pct=True)

        return {
            "ewi_slope": ewi_slope,
            "breadth": breadth,
            "adx_median": adx_median,
            "hurst": hurst,
            "vol_percentile": vol_percentile,
        }

    def _score_regime_candidate(self, date: pd.Timestamp) -> MarketRegimeObservation:
        """Vote the five indicators at date into a raw score and candidate state."""
        series_map = self._regime_indicator_series

        def _value(key: str) -> float:
            series = series_map.get(key)
            if series is None or date not in series.index:
                return float("nan")
            value = float(series.loc[date])
            return value if math.isfinite(value) else float("nan")

        ewi_slope = _value("ewi_slope")
        breadth = _value("breadth")
        adx_median = _value("adx_median")
        hurst = _value("hurst")
        vol_percentile = _value("vol_percentile")

        score = 0
        if math.isfinite(ewi_slope):
            if ewi_slope > float(self.cfg.get("regime_ewi_slope_trend", 0.02)):
                score += 1
            elif ewi_slope < float(self.cfg.get("regime_ewi_slope_choppy", -0.02)):
                score -= 1
        if math.isfinite(breadth):
            if breadth > 0.6:
                score += 1
            elif breadth < 0.4:
                score -= 1
        if math.isfinite(adx_median):
            if adx_median > float(self.cfg.get("regime_adx_trend", 25)):
                score += 1
            elif adx_median < float(self.cfg.get("regime_adx_choppy", 20)):
                score -= 1
        if math.isfinite(hurst):
            if hurst > float(self.cfg.get("regime_hurst_trend", 0.55)):
                score += 1
            elif hurst < float(self.cfg.get("regime_hurst_choppy", 0.45)):
                score -= 1
        if math.isfinite(vol_percentile) and vol_percentile > float(
            self.cfg.get("regime_vol_extreme_pct", 0.9)
        ):
            score -= 1

        trend_threshold = int(self.cfg.get("regime_score_trend", 2))
        choppy_threshold = int(self.cfg.get("regime_score_choppy", -3))
        if score >= trend_threshold:
            candidate = "TREND"
        elif score <= choppy_threshold:
            candidate = "CHOPPY"
        else:
            candidate = "TRANSITION"

        return MarketRegimeObservation(
            ewi_slope=ewi_slope,
            breadth_above_ma=breadth,
            adx_median=adx_median,
            hurst=hurst,
            volatility_percentile=vol_percentile,
            raw_score=int(score),
            candidate_state=candidate,
        )

    def _advance_regime_state(
        self, candidate: str, pos: int, current: str
    ) -> str:
        """Apply confirmation gates and minimum hold to one candidate transition.

        Three-state machine with early-warning TRANSITION:
        - TREND → TRANSITION: when the composite score softens (candidate
          becomes TRANSITION) for *trend_to_transition_confirmations* days.
          This activates position-size scaling before full CHOPPY defense.
        - TREND/TRANSITION → CHOPPY: when the candidate is CHOPPY for
          *choppy_confirmations* day(s).  Fast defense — even one day of
          strong negative score triggers the block.
        - CHOPPY → TRANSITION/TREND: slow recovery via *recovery_confirmations*.
        """
        choppy_confirmations = int(self.cfg.get("regime_choppy_confirmations", 2))
        trend_confirmations = int(self.cfg.get("regime_trend_confirmations", 3))
        recovery_confirmations = int(self.cfg.get("regime_recovery_confirmations", 3))
        min_hold = int(self.cfg.get("regime_min_state_hold", 3))
        trend_to_transition = int(
            self.cfg.get("regime_trend_to_transition_confirmations", 3)
        )

        # Update confirmation streaks from the freshly scored candidate.
        if candidate == "CHOPPY":
            self._regime_to_choppy_streak += 1
        else:
            self._regime_to_choppy_streak = 0
        if candidate == "TREND":
            self._regime_to_trend_streak += 1
        else:
            self._regime_to_trend_streak = 0
        if candidate != "CHOPPY":
            self._regime_non_choppy_streak += 1
        else:
            self._regime_non_choppy_streak = 0
        if candidate == "TRANSITION":
            self._regime_to_transition_streak += 1
        else:
            self._regime_to_transition_streak = 0

        can_leave = (pos - self._regime_state_start_pos) >= min_hold

        if current == "CHOPPY":
            # Exit CHOPPY only after sustained non-choppy evidence (slow).
            if can_leave and self._regime_non_choppy_streak >= recovery_confirmations:
                new_state = (
                    "TREND"
                    if self._regime_to_trend_streak >= trend_confirmations
                    else "TRANSITION"
                )
            else:
                new_state = "CHOPPY"
        elif current == "TREND":
            # Early warning: demote to TRANSITION when momentum softens.
            if can_leave and self._regime_to_transition_streak >= trend_to_transition:
                new_state = "TRANSITION"
            # Fast defense: jump straight to CHOPPY when score confirms.
            elif can_leave and self._regime_to_choppy_streak >= choppy_confirmations:
                new_state = "CHOPPY"
            else:
                new_state = "TREND"
        else:  # TRANSITION
            if can_leave and self._regime_to_choppy_streak >= choppy_confirmations:
                new_state = "CHOPPY"
            elif can_leave and self._regime_to_trend_streak >= trend_confirmations:
                new_state = "TREND"
            else:
                new_state = "TRANSITION"

        if new_state != current:
            self._regime_state_start_pos = pos
            # Reset streaks after a committed transition so confirmation windows
            # restart cleanly from the new state.
            self._regime_to_choppy_streak = 0
            self._regime_to_trend_streak = 0
            self._regime_non_choppy_streak = 0
            self._regime_to_transition_streak = 0
        return new_state

    def _update_market_regime(
        self,
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
        all_dates: list[pd.Timestamp],
        date_to_pos: dict[pd.Timestamp, int],
    ) -> None:
        """Score the regime basket and advance the three-state machine by one day.

        Called after ``_update_sector_guard`` and before any new signal
        generation so entries respect the current regime. The EWI slope is the
        primary indicator: when it (or the indicator series) is unavailable the
        prior state is preserved without advancing confirmation streaks.
        """
        del all_dates
        # Guard: _regime_state is initialised in _reset_run_state; tests that
        # call _update_sector_guard directly (without a full run) skip it.
        if not hasattr(self, "_regime_state"):
            return
        previous_state = self._regime_state
        if not bool(self.cfg.get("market_regime_enabled", True)):
            self._regime_state = "TREND"
            return
        if not self._regime_indicator_series:
            self._regime_state = "TREND"
            return

        date = pd.Timestamp(date)
        pos = date_to_pos.get(date)
        if pos is None:
            return

        observation = self._score_regime_candidate(date)
        self._regime_latest_observation = observation
        # Insufficient causal history: preserve the prior state and record the
        # gap without advancing the confirmation streaks.
        if not math.isfinite(observation.ewi_slope):
            self._regime_state_series.append(
                {
                    "date": date.strftime("%Y-%m-%d"),
                    "state": previous_state,
                    "previous_state": previous_state,
                    "candidate": None,
                    "score": None,
                    "ewi_slope": None,
                    "breadth": None,
                    "adx_median": None,
                    "hurst": None,
                    "vol_percentile": None,
                }
            )
            return

        new_state = self._advance_regime_state(
            observation.candidate_state, pos, previous_state
        )
        self._regime_prev_state = previous_state
        self._regime_state = new_state
        self._safe_mode_active = (new_state == "CHOPPY")
        self._regime_state_series.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "state": new_state,
                "previous_state": previous_state,
                "candidate": observation.candidate_state,
                "score": observation.raw_score,
                "ewi_slope": observation.ewi_slope,
                "breadth": observation.breadth_above_ma,
                "adx_median": observation.adx_median,
                "hurst": observation.hurst,
                "vol_percentile": observation.volatility_percentile,
            }
        )

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
        """Rank candidates, fuse new signals, and remove conflicting pending buys.

        The market-regime state machine can veto new entries: when the current
        regime is CHOPPY, buys are suppressed (exits remain unaffected) by
        forwarding ``allow_buys=False`` to the strategy signal collector.

        On the *first day* the regime enters CHOPPY, a partial-position
        reduction is queued via ``_generate_regime_reduction_signals`` to
        actively cut exposure — not merely block new entries.

        The volatility fast-path was removed: in AI super-cycle markets,
        ``vol_percentile`` frequently hits 1.0 during V-shaped corrections
        (every new high sets the rank to 1.0), which blocked entries even in
        TREND state and reduced returns by ~20%.  The state machine's
        multi-day confirmation mechanism is sufficient to detect sustained
        choppy markets without blocking trend entries on temporary vol spikes.
        """
        regime_enabled = bool(self.cfg.get("market_regime_enabled", True))

        # --- Buy gate -------------------------------------------------------
        # Only the state machine gates entries: CHOPPY blocks buys, TRANSITION
        # scales them (handled in _fuse_daily_signals), TREND is fully open.
        allow_buys = self._regime_state != "CHOPPY"

        # --- Regime-mandated sells (state machine transitions only) --------
        # Forced sells fire ONLY on actual state machine transitions.
        # TRANSITION defaults to 0.0 (no trim) so only CHOPPY actively cuts
        # exposure.
        regime_sells: list[tuple[Signal, BaseStrategy]] = []
        if regime_enabled:
            if (
                self._regime_state == "TRANSITION"
                and self._regime_prev_state == "TREND"
            ):
                trim = float(self.cfg.get("regime_transition_exit_ratio", 0.0))
                if trim > 0:
                    regime_sells = self._generate_regime_reduction_signals(
                        date_str, trim
                    )
                    if regime_sells:
                        print(
                            f"  REGIME [{date_str}] TRANSITION entered — "
                            f"queue {len(regime_sells)} trim sells "
                            f"({trim:.0%} ratio)"
                        )
            elif (
                self._regime_state == "CHOPPY"
                and self._regime_prev_state != "CHOPPY"
            ):
                exit_ratio = float(self.cfg.get("regime_choppy_exit_ratio", 0.3))
                regime_sells = self._generate_regime_reduction_signals(
                    date_str, exit_ratio
                )
                if regime_sells:
                    print(
                        f"  REGIME [{date_str}] CHOPPY entered — "
                        f"queue {len(regime_sells)} partial sells "
                        f"({exit_ratio:.0%} ratio)"
                    )

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
            allow_buys=allow_buys,
            top_symbols=top_symbols,
        )
        fused_daily = self._fuse_daily_signals(daily_signals, date_str)
        # Append regime-mandated partial sells after fusion so they survive
        # the buy/sell conflict resolution pass.
        fused_daily.extend(regime_sells)
        sells = {
            (signal.symbol, signal.strategy_name)
            for signal, _ in fused_daily
            if signal.direction == "sell"
        }
        if sells:
            sell_symbols = {symbol for symbol, _ in sells}
            symbol_veto = bool(self.cfg["symbol_level_sell_veto"])
            pending = [
                item
                for item in pending
                if not (
                    item[0].direction == "buy"
                    and (
                        item[0].symbol in sell_symbols
                        if symbol_veto
                        else (item[0].symbol, item[0].strategy_name) in sells
                    )
                )
            ]
        pending.extend(fused_daily)
        return pending

    def _fuse_daily_signals(
        self, daily: list[tuple[Signal, BaseStrategy]], date_str: str
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Resolve buy/sell conflicts and scale same-symbol strategy confirmations.

        The market-regime state machine can throttle entries: when the current
        regime is TRANSITION, surviving buy signals have their target shares
        scaled by ``regime_transition_scale`` (default 1.0) on top of the
        fusion-vote scaling applied by the base implementation. CHOPPY already
        blocks buys upstream, so only TRANSITION needs a post-pass here.
        """
        fused = super()._fuse_daily_signals(  # pyright: ignore[reportAttributeAccessIssue]
            daily, date_str
        )
        if self._regime_state != "TRANSITION":
            return fused
        scale = float(self.cfg.get("regime_transition_scale", 1.0))
        if scale >= 1.0:
            return fused
        adjusted: list[tuple[Signal, BaseStrategy]] = []
        for signal, strategy in fused:
            if signal.direction != "buy":
                adjusted.append((signal, strategy))
                continue
            target_shares = _floor_to_lot(signal.target_shares * scale)
            if target_shares <= 0:
                # Regime throttle eliminated the entry; drop it but keep exits.
                continue
            adjusted.append(
                (
                    replace(
                        signal,
                        target_shares=target_shares,
                        reason=(
                            f"[regime transition x{scale:.2f}] {signal.reason}"
                        ),
                    ),
                    strategy,
                )
            )
        return adjusted

    def _resolve_symbol_configs(
        self,
        symbols_dict: dict[str, str],
        per_symbol_config: dict[str, dict] | None,
        config_route: str,
    ) -> dict[str, dict]:
        """Install the recoverable manager after inherited parameter routing."""
        resolved = super()._resolve_symbol_configs(  # pyright: ignore[reportAttributeAccessIssue]
            symbols_dict, per_symbol_config, config_route
        )
        symbol_groups = dict(self.risk.symbol_groups)
        self.risk = RecoverableDrawdownRiskManager(self.cfg, self.policy)
        self.risk.configure_groups(symbol_groups)
        return resolved

    def _select_momentum_candidates(
        self,
        data_map: dict[str, pd.DataFrame],
        symbols_dict: dict[str, str],
        date: pd.Timestamp,
    ) -> set[str]:
        """Rank tradable symbols with stable multi-horizon momentum evidence."""
        del data_map
        tradable = sorted(
            code for code in symbols_dict if code in self._tradable_symbol_codes
        )
        if not tradable:
            return set()
        maximum = int(self.cfg.get("max_positions", 6))
        if len(tradable) <= maximum:
            return set(tradable)

        totals = {code: 0.0 for code in tradable}
        observations = {code: 0 for code in tradable}
        for window in self.policy.candidate_lookbacks:
            reference_values: list[float] = []
            for code in self.policy.regime_symbols:
                series = self._candidate_score_series.get(code, {}).get(window)
                if series is None or date not in series.index:
                    continue
                value = float(series.loc[date])
                if math.isfinite(value):
                    reference_values.append(value)
            if not reference_values:
                continue

            for code in tradable:
                series = self._candidate_score_series.get(code, {}).get(window)
                if series is None or date not in series.index:
                    continue
                value = float(series.loc[date])
                if not math.isfinite(value):
                    continue
                percentile = sum(
                    reference <= value for reference in reference_values
                ) / len(reference_values)
                totals[code] += percentile
                observations[code] += 1

        def sort_key(code: str) -> tuple[bool, float, str]:
            count = observations[code]
            score = totals[code] / count if count else 0.0
            return count == 0, -score, code

        eligible = [
            code
            for code in tradable
            if observations[code]
            and totals[code] / observations[code]
            >= self.policy.candidate_reference_percentile
        ]
        ranked = sorted(eligible, key=sort_key)
        return set(ranked[:maximum])

    def _update_sector_guard(
        self,
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
        all_dates: list[pd.Timestamp],
        date_to_pos: dict[pd.Timestamp, int],
    ) -> str | None:
        """Update breadth risk, then advance the market-regime state machine.

        The regime update runs after the sector guard so entries respect the
        freshly scored regime, and before signal generation because
        ``_evaluate_trading_day`` continues only after this method returns.
        """
        scoped_data = {
            code: data_map[code]
            for code in self.policy.regime_symbols
            if code in data_map
        }
        guard_state = super()._update_sector_guard(  # pyright: ignore[reportAttributeAccessIssue]
            scoped_data,
            date,
            all_dates,
            date_to_pos,
        )
        self._update_market_regime(data_map, date, all_dates, date_to_pos)
        return guard_state

    def _apply_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        all_dates: list[pd.Timestamp],
        date_to_pos: dict[pd.Timestamp, int],
        pending: list[tuple[Signal, BaseStrategy]],
    ) -> tuple[list[tuple[Signal, BaseStrategy]], bool, bool]:
        """Reset inherited one-shot logging whenever a temporary lock rearms."""
        before = len(self.risk_events)
        outcome = super()._apply_portfolio_risk(  # pyright: ignore[reportAttributeAccessIssue]
            current_assets, date_str, all_dates, date_to_pos, pending
        )
        if any(
            event.get("event") == "portfolio_drawdown_rearmed"
            for event in self.risk_events[before:]
        ):
            self._risk_lock_logged = False
        return outcome

    def _build_result(self, final_assets: float, all_dates: list[pd.Timestamp]) -> dict:
        """Expose temporary and terminal lock state plus regime history."""
        result = super()._build_result(  # pyright: ignore[reportAttributeAccessIssue]
            final_assets,
            all_dates,
        )
        manager = self.risk
        result.update(
            {
                "portfolio_policy": self.policy.as_dict(),
                "safe_mode_active": bool(getattr(self, "_safe_mode_active", False)),
                "terminal_risk_lock": bool(
                    isinstance(manager, RecoverableDrawdownRiskManager)
                    and manager.terminal_lock
                ),
                "cycle_lock_count": int(
                    manager.cycle_lock_count
                    if isinstance(manager, RecoverableDrawdownRiskManager)
                    else 0
                ),
                "guard_scope_mode": "fixed_signal_only_regime_basket",
                "tradable_symbols": sorted(self._tradable_symbol_codes),
                "regime_state_series": list(self._regime_state_series),
                "regime_final_state": self._regime_state,
            }
        )
        return result


class SleeveBacktestEngine(
    _UniverseInvariantSleeveMixin, _EnsembleSleeveBacktestEngine
):
    """Run one portfolio sleeve with adaptive breadth and recoverable drawdown defense."""

    ENGINE_LABEL = "Quant Fusion"


class BacktestEngine(_UniverseInvariantSleeveMixin, _EnsembleBacktestEngine):
    """Coordinate equal-capital portfolio sleeves under one universe-invariant policy."""

    ENGINE_LABEL = "Quant Fusion"

    _SINGLE_ASSET_TREND_OVERRIDES: ClassVar[dict[str, Any]] = {
        "entry_period": 30,
        "exit_period": 20,
        "trail_atr_mult": 10.0,
        "profit_lock_giveback": 0.40,
        "reversal_break_giveback": 0.40,
        "reversal_exit_period": 20,
        "hard_stop": 0.25,
    }

    # Policy field names that are mirrored into cfg so a custom PortfolioPolicy can
    # drive the regime state machine read by the mixin (self.cfg.get(...)).
    _REGIME_CFG_KEYS: ClassVar[tuple[str, ...]] = (
        "market_regime_enabled",
        "regime_ewi_lookback",
        "regime_breadth_ma_long",
        "regime_adx_trend",
        "regime_adx_choppy",
        "regime_hurst_window",
        "regime_hurst_trend",
        "regime_hurst_choppy",
        "regime_vol_lookback",
        "regime_vol_extreme_pct",
        "regime_ewi_slope_trend",
        "regime_ewi_slope_choppy",
        "regime_score_trend",
        "regime_score_choppy",
        "regime_choppy_confirmations",
        "regime_trend_confirmations",
        "regime_recovery_confirmations",
        "regime_min_state_hold",
        "regime_transition_scale",
        "regime_trend_to_transition_confirmations",
        "regime_choppy_exit_ratio",
        "regime_transition_exit_ratio",
    )

    def __init__(
        self,
        initial_capital: float = 2_000_000,
        cfg: dict | None = None,
        policy: PortfolioPolicy | None = None,
    ) -> None:
        resolved_policy = policy or PortfolioPolicy()
        regime_cfg = {
            key: getattr(resolved_policy, key) for key in self._REGIME_CFG_KEYS
        }
        normalized_cfg = {
            "sector_guard_min_symbols": max(
                1, math.ceil(len(resolved_policy.regime_symbols) * 0.8)
            ),
            "group_min_slots": 0,
            # Policy regime values override _default_config defaults; explicit
            # user cfg still wins because it is spread last.
            **regime_cfg,
            **dict(cfg or {}),
        }
        super().__init__(
            initial_capital=initial_capital,
            cfg=normalized_cfg,
            policy=resolved_policy,
        )

    def _effective_policy(self, tradable_count: int) -> PortfolioPolicy:
        """Tighten drawdown gates smoothly as diversification approaches one."""
        count = _require_int("tradable_count", tradable_count, min_value=1)
        adjustment = self.policy.concentration_drawdown_adjustment / count
        return replace(
            self.policy,
            confirmed_drawdown=self.policy.confirmed_drawdown - adjustment,
            emergency_drawdown=self.policy.emergency_drawdown - adjustment,
        )

    def _runtime_sleeve_cfg(self, tradable_count: int) -> dict[str, Any]:
        """Return shared overrides with one fixed parameter set for all sizes.

        The per-sleeve ``max_positions`` is set to 10 so each sleeve has a
        wide candidate pool for momentum ranking. The portfolio-level
        ``self.cfg["max_positions"]`` (default 6) limits the total unique
        symbols held across all sleeves. These are two separate limits:
        per-sleeve=10 (candidate breadth), portfolio=6 (concentration).

        Small universes are naturally bounded by their own symbol count.
        Very small universes (<=2) still use the slower time-series
        trend contract, which is a strategy-logic switch (no cross-sectional
        information) rather than a parameter change.
        """
        sleeve_cfg = dict(self._ensemble_user_cfg)
        if tradable_count <= 2:
            sleeve_cfg.update(self._SINGLE_ASSET_TREND_OVERRIDES)
        sleeve_cfg["max_positions"] = 10
        return sleeve_cfg

    def run(  # noqa: PLR0913 - Preserve the inherited public API.
        self,
        symbols_dict: dict[str, str],
        start_date: str,
        end_date: str,
        per_symbol_config: dict[str, dict] | None = None,
        profile: str | None = None,
        config_route: str = "auto",
        data_dir: str | None = None,
        *,
        indicator_state: str = "cold",
        warmup_calendar_days: int = 365,
        allocation_mode: str | None = None,
        risk_state: dict | None = None,
        account_state: AccountState | None = None,
    ) -> dict:
        """Run one or several portfolio sleeves under the same effective policy formula."""
        if account_state is not None:
            raise NotImplementedError(
                "Real-account mode (account_state) is disabled. "
                "The account injection logic has known architecture defects. "
                "Use simulation mode (account_state=None) instead. "
                "A separate account signal engine is planned."
            )
        mode = str(allocation_mode or self.policy.allocation_mode).lower()
        if mode == "ensemble":
            return super().run(
                symbols_dict,
                start_date,
                end_date,
                per_symbol_config=per_symbol_config,
                profile=profile,
                config_route=config_route,
                data_dir=data_dir,
                indicator_state=indicator_state,
                warmup_calendar_days=warmup_calendar_days,
                allocation_mode="ensemble",
                risk_state=risk_state,
                account_state=account_state,
            )
        if mode != "single":
            raise ValueError("allocation_mode must be 'single' or 'ensemble'")
        count = len(symbols_dict)
        effective_policy = replace(
            self._effective_policy(count), allocation_mode="single"
        )
        sleeve = SleeveBacktestEngine(
            self.initial_capital,
            cfg=self._runtime_sleeve_cfg(count),
            policy=effective_policy,
            allocation_lookbacks=effective_policy.single_lookbacks,
            sleeve_name="single",
        )
        if risk_state:
            sleeve.cfg = dict(sleeve.cfg)
            sleeve.cfg["_initial_risk_state"] = risk_state
        if account_state:
            # DEPRECATED: This path is unreachable — the public run() API
            # raises NotImplementedError when account_state is not None.
            # Retained for reference until the account signal engine replaces
            # this injection mechanism.
            sleeve._account_state_to_inject = account_state
        result = sleeve.run(
            symbols_dict,
            start_date,
            end_date,
            per_symbol_config=per_symbol_config,
            profile=profile,
            config_route=config_route,
            data_dir=data_dir,
            indicator_state=indicator_state,
            warmup_calendar_days=warmup_calendar_days,
        )
        result["effective_portfolio_policy"] = effective_policy.as_dict()
        self.sleeves = [sleeve]
        self.last_result = result
        return result

    def _run_ensemble(self, request: _RunRequest) -> dict:
        """Replay fixed-capital sleeves on one synchronized portfolio calendar."""
        tradable_count = len(request.symbols_dict)
        effective_policy = self._effective_policy(tradable_count)
        states = self._prepare_ensemble_sleeves(request, effective_policy)
        reference_dates = states[0].all_dates
        if any(state.all_dates != reference_dates for state in states[1:]):
            raise RuntimeError("ensemble sleeves produced different trading calendars")

        portfolio_risk = RecoverableDrawdownRiskManager(
            {"max_drawdown": effective_policy.confirmed_drawdown}, effective_policy
        )
        # Restore previous risk state when explicitly provided by the caller.
        # Note: daily_signal_scan.py does NOT use this feature — it replays
        # the full history each time to avoid time-direction errors.
        if request.risk_state:
            if request.risk_state.get("terminal_risk_lock", False):
                portfolio_risk.terminal_lock = True
                portfolio_risk.persistent_lock = True
            portfolio_risk.cycle_lock_count = request.risk_state.get(
                "cycle_lock_count", 0
            )
            if request.risk_state.get("sector_guard_active", False):
                for state in states:
                    state.sleeve.sector_guard_active = True
        # DEPRECATED: account_state is always None — the public run() API
        # raises NotImplementedError before reaching this code. The entire
        # account injection block below is dead code, retained for reference.
        account_state = request.account_state
        as_of_idx = max(0, len(reference_dates) - 2) if account_state else -1
        # P0 fix: seed the portfolio-level risk manager with the account's
        # lifetime peak so drawdown calculations start from the real
        # high-water mark rather than building up from zero.
        if account_state is not None:
            peak = getattr(account_state, "peak_equity", None)
            if peak is not None and peak > 0:
                portfolio_risk.peak_assets = float(peak)
                portfolio_risk.lifetime_peak_assets = float(peak)
        portfolio_risk_events: list[dict[str, Any]] = []
        symbol_count_curve: list[dict[str, Any]] = []
        # 穿越牛熊 overlay: bull-silent defensive layer on top of the ensemble.
        # Default ON, only fires on genuine risk (catastrophe drop / structural
        # shock + drawdown), so a clean bull run is left untouched.
        from cross_market_overlay import CrossMarketOverlay
        cm_overlay = CrossMarketOverlay(
            enable_shock_trim=bool(self.cfg.get("cm_overlay_shock_trim", False))
        ) if self.cfg.get("enable_cm_overlay", True) else None
        cm_overlay_peak = 0.0
        for idx, date in enumerate(reference_dates):
            # Inject account snapshot at the open of the as-of date so that
            # everything from this day onward uses the real account state
            # while all prior dates ran as a clean simulation.
            if account_state is not None and idx == as_of_idx and states:
                self._apply_account_state(
                    account_state, states[0].sleeve, set_cash=False
                )
            self._execute_ensemble_open(states, date)
            for state in states:
                state.pending = state.sleeve._evaluate_trading_day(
                    request.symbols_dict,
                    state.data_map,
                    state.indicator_map,
                    state.all_dates,
                    state.date_to_pos,
                    date,
                    state.pending,
                )
            assets = sum(
                state.sleeve._total_assets(state.data_map, date) for state in states
            )
            status = portfolio_risk.check_portfolio_risk(
                assets,
                date.strftime("%Y-%m-%d"),
                trading_dates=reference_dates,
                date_to_pos=states[0].date_to_pos,
            )
            portfolio_risk_events.extend(portfolio_risk.drain_audit_events())
            if status:
                self._apply_global_risk_lock(states, date)
            # 穿越牛熊 overlay check (appends T+1 sell signals to pending).
            cm_overlay_peak = max(cm_overlay_peak, assets)
            if cm_overlay is not None and cm_overlay_peak > 0:
                cm_overlay.on_day(
                    states, date, idx, assets, cm_overlay_peak,
                    self._overlay_allocation_score(states, date),
                )
            held = self._held_portfolio_symbols(states)
            symbol_count_curve.append(
                {"date": date.strftime("%Y-%m-%d"), "symbol_count": len(held)}
            )

        results = self._finalize_ensemble_sleeves(states)
        combined = self._aggregate_sleeve_results(results)
        combined_risk_events = list(combined["risk_events"])
        combined_risk_events.extend(
            {"sleeve": "portfolio", **event} for event in portfolio_risk_events
        )
        if cm_overlay is not None:
            combined_risk_events.extend(
                {"sleeve": "overlay", **event} for event in cm_overlay.events
            )
        combined.update(
            {
                "portfolio_policy": self.policy.as_dict(),
                "effective_portfolio_policy": effective_policy.as_dict(),
                "terminal_risk_lock": bool(
                    portfolio_risk.terminal_lock
                    or any(
                        result.get("terminal_risk_lock", False) for result in results
                    )
                ),
                "cycle_lock_count": int(
                    portfolio_risk.cycle_lock_count
                    + sum(int(result.get("cycle_lock_count", 0)) for result in results)
                ),
                "portfolio_cycle_lock_count": int(portfolio_risk.cycle_lock_count),
                "persistent_risk_lock": bool(
                    portfolio_risk.persistent_lock or combined["persistent_risk_lock"]
                ),
                "all_sleeves_locked": bool(
                    portfolio_risk.persistent_lock or combined["all_sleeves_locked"]
                ),
                "locked_sleeves": (
                    [state.sleeve.sleeve_name for state in states]
                    if portfolio_risk.persistent_lock
                    else combined["locked_sleeves"]
                ),
                "guard_scope_mode": "fixed_signal_only_regime_basket",
                "portfolio_cash_model": "fixed_virtual_subaccounts",
                "portfolio_max_positions": int(self.cfg["max_positions"]),
                "max_concurrent_symbols": max(
                    item["symbol_count"] for item in symbol_count_curve
                ),
                "portfolio_symbol_count_curve": symbol_count_curve,
                "risk_events": self._sort_events(combined_risk_events),
                "regime_state_series": (
                    list(results[0].get("regime_state_series", []))
                    if results
                    else []
                ),
                "regime_final_state": (
                    results[0].get("regime_final_state", "TREND")
                    if results
                    else "TREND"
                ),
                "safe_mode_active": any(
                    result.get("safe_mode_active", False) for result in results
                ) if results else False,
            }
        )
        self.last_result = combined
        return combined

    def _prepare_ensemble_sleeves(
        self, request: _RunRequest, effective_policy: PortfolioPolicy
    ) -> list[_PreparedSleeveRun]:
        """Create funded sleeves and prepare their data without running ahead."""
        tradable_count = len(request.symbols_dict)
        indicator_state = str(request.indicator_state).lower()
        if indicator_state not in {"cold", "warm"}:
            raise ValueError("indicator_state must be either 'cold' or 'warm'")
        warmup_days = _require_int(
            "warmup_calendar_days", request.warmup_calendar_days, min_value=120
        )
        horizons = effective_policy.allocation_horizons
        sleeve_capital = self.initial_capital / len(horizons)
        self.sleeves = []
        states: list[_PreparedSleeveRun] = []
        base_sleeve_policy = replace(
            effective_policy,
            allocation_mode="single",
            max_order_adv_ratio=effective_policy.max_order_adv_ratio / len(horizons),
        )
        for index, lookbacks in enumerate(horizons):
            capital = (
                sleeve_capital
                if index < len(horizons) - 1
                else self.initial_capital - sleeve_capital * (len(horizons) - 1)
            )
            name = self._sleeve_name(index, len(horizons))
            sleeve_policy = replace(
                base_sleeve_policy,
                candidate_lookbacks=effective_policy.candidate_horizons[index],
            )
            # Cross-sectional ranks contain no information with one asset. The
            # fallback preserves the same 60% symbol exposure ceiling.
            sleeve_cfg = self._runtime_sleeve_cfg(tradable_count)
            sleeve = SleeveBacktestEngine(
                capital,
                cfg=sleeve_cfg,
                policy=sleeve_policy,
                allocation_lookbacks=lookbacks,
                sleeve_name=name,
            )
            sleeve._indicator_state = indicator_state
            sleeve._warmup_calendar_days = warmup_days
            sleeve._requested_start_date = request.start_date
            sleeve._requested_end_date = request.end_date
            profile, route, start_ts, end_ts = sleeve._validate_run_request(
                request.symbols_dict,
                request.start_date,
                request.end_date,
                request.profile,
                request.config_route,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                prepared = sleeve._prepare_run(
                    request.symbols_dict,
                    request.start_date,
                    request.end_date,
                    start_ts,
                    end_ts,
                    request.per_symbol_config,
                    profile,
                    route,
                    request.data_dir,
                )
            self.sleeves.append(sleeve)
            states.append(
                _PreparedSleeveRun(
                    sleeve=sleeve,
                    data_map=prepared[0],
                    indicator_map=prepared[1],
                    all_dates=prepared[2],
                    date_to_pos=prepared[3],
                )
            )
        return states

    @staticmethod
    def _held_portfolio_symbols(states: list[_PreparedSleeveRun]) -> set[str]:
        """Return the distinct symbols held by any virtual subaccount."""
        return {
            symbol
            for state in states
            for symbol, positions in state.sleeve.positions.items()
            if positions
        }

    @staticmethod
    def _overlay_allocation_score(states: list[_PreparedSleeveRun], date: pd.Timestamp):
        """Mean allocation score across sleeves, used to rank laggards for trim."""
        def _score(symbol: str) -> float:
            samples = []
            for state in states:
                try:
                    scores = state.sleeve._allocation_scores(state.data_map, date)
                except Exception:
                    scores = {}
                samples.append(float(scores.get(symbol, 0.0)))
            return float(np.mean(samples)) if samples else 0.0
        return _score

    def _authorize_portfolio_buys(
        self, states: list[_PreparedSleeveRun], date: pd.Timestamp
    ) -> None:
        """Admit symbols by the mean of comparable percentile ranks (Borda score)."""
        held = self._held_portfolio_symbols(states)
        maximum = int(self.cfg["max_positions"])
        if len(held) > maximum:
            raise RuntimeError("portfolio symbol limit was already exceeded")
        score_samples: dict[str, list[float]] = {}
        for state in states:
            scores = state.sleeve._allocation_scores(state.data_map, date)
            candidates = {
                signal.symbol
                for signal, _ in state.pending
                if signal.direction == "buy"
                and signal.symbol not in held
                and signal.symbol in state.data_map
                and date in state.data_map[signal.symbol].index
            }
            for symbol in candidates:
                score_samples.setdefault(symbol, []).append(scores.get(symbol, 0.0))
        ranked = sorted(
            score_samples,
            key=lambda symbol: (
                -float(np.mean(score_samples[symbol])),
                EXECUTION_PRIORITY.get(symbol, 9999),
                symbol,
            ),
        )
        allowed = held | set(ranked[: max(maximum - len(held), 0)])
        date_str = date.strftime("%Y-%m-%d")
        for state in states:
            retained: list[tuple[Signal, BaseStrategy]] = []
            for signal, strategy in state.pending:
                if signal.direction == "buy" and signal.symbol not in allowed:
                    state.sleeve._record_order_event(
                        date=date_str,
                        signal=signal,
                        event="rejected_portfolio_symbol_limit",
                        portfolio_max_positions=maximum,
                    )
                    continue
                retained.append((signal, strategy))
            state.pending = retained

    def _execute_ensemble_open(
        self, states: list[_PreparedSleeveRun], date: pd.Timestamp
    ) -> None:
        """Execute every sleeve's sells before globally admitting and filling buys."""
        for state in states:
            state.sleeve._start_trading_day()
            state.pending = state.sleeve._execute_pending_signals(
                state.pending,
                state.data_map,
                date,
                state.date_to_pos,
                frozenset({"sell"}),
            )
        self._authorize_portfolio_buys(states, date)
        for state in states:
            state.pending = state.sleeve._execute_pending_signals(
                state.pending,
                state.data_map,
                date,
                state.date_to_pos,
                frozenset({"buy"}),
            )
        if len(self._held_portfolio_symbols(states)) > int(self.cfg["max_positions"]):
            raise RuntimeError("portfolio symbol limit exceeded after buy execution")

    @staticmethod
    def _apply_global_risk_lock(
        states: list[_PreparedSleeveRun], date: pd.Timestamp
    ) -> None:
        """Cancel buys and queue T+1 liquidations in every funded sleeve."""
        date_str = date.strftime("%Y-%m-%d")
        for state in states:
            pending_sells = {
                state.sleeve._signal_key(signal): signal
                for signal, _ in state.pending
                if signal.direction == "sell"
            }
            liquidations = state.sleeve._generate_liquidation_signals(
                date_str, reason="portfolio-level drawdown liquidation"
            )
            for signal, _ in liquidations:
                previous = pending_sells.get(state.sleeve._signal_key(signal))
                if previous is None:
                    continue
                state.sleeve._record_order_event(
                    date=date_str,
                    signal=signal,
                    event="pending_sell_superseded_by_portfolio_liquidation",
                    previous_reason=previous.reason,
                    previous_target_shares=int(previous.target_shares),
                    liquidation_target_shares=int(signal.target_shares),
                )
            state.pending = state.sleeve._dedupe_pending_signals(
                [item for item in state.pending if item[0].direction == "sell"]
                + liquidations
            )

    @staticmethod
    def _finalize_ensemble_sleeves(
        states: list[_PreparedSleeveRun],
    ) -> list[dict]:
        """Mark open positions at the final close and build sleeve reports."""
        results: list[dict] = []
        for state in states:
            last_date = state.all_dates[-1]
            final_assets = state.sleeve._total_assets(state.data_map, last_date)
            state.sleeve.pending_signals = state.sleeve._dedupe_pending_signals(
                state.pending
            )
            results.append(state.sleeve._build_result(final_assets, state.all_dates))
        return results


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the standalone command-line interface."""
    parser = argparse.ArgumentParser(
        description="Quant Fusion standalone backtester"
    )
    parser.add_argument(
        "--symbol",
        "-s",
        default=",".join(DEFAULT_SYMBOLS),
        help="Comma-separated six-digit codes or preset stock names",
    )
    parser.add_argument("--start", default="2025-04-01")
    parser.add_argument("--end", default="2026-07-20")
    parser.add_argument("--capital", type=float, default=2_000_000)
    parser.add_argument(
        "--data-dir",
        default="",
        help=(
            "Local forward-adjusted CSV directory. Omit this option to fetch "
            "forward-adjusted data through AKShare provider failover."
        ),
    )
    parser.add_argument(
        "--cache-dir",
        default="",
        help=(
            "Local cache directory for incremental data fetching. On first run, "
            "fetches full history from AKShare and saves to cache. On subsequent "
            "runs, loads cached history and only fetches the latest days from "
            "AKShare, then merges and updates the cache. Combines the speed of "
            "local data with the freshness of online data."
        ),
    )
    parser.add_argument("--indicator-state", choices=["cold", "warm"], default="warm")
    parser.add_argument("--warmup-calendar-days", type=int, default=365)
    parser.add_argument("--save-dir", default="")
    parser.add_argument("--no-plot", action="store_true")
    return parser


def main() -> dict | None:
    """Run a standalone backtest from local CSV or online providers."""
    args = build_argument_parser().parse_args()
    symbols = parse_symbols(args.symbol)
    DataFetcher._cache_dir = args.cache_dir or None
    engine = BacktestEngine(args.capital)
    result = engine.run(
        symbols,
        args.start,
        args.end,
        data_dir=args.data_dir or None,
        indicator_state=args.indicator_state,
        warmup_calendar_days=args.warmup_calendar_days,
    )
    PerformanceReport.print_report(result, symbols)
    if args.save_dir:
        PerformanceReport.save_result(result, args.save_dir)
    if not args.no_plot:
        PerformanceReport.plot_equity_curve(
            result,
            f"equity_curve_{args.indicator_state}.png",
        )
    return result


if __name__ == "__main__":
    main()
