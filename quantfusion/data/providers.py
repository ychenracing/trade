"""Validated A-share market-data provider and cache access."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import ClassVar

import pandas as pd

from quantfusion.data.contracts import OPTIONAL_COLUMNS, REQUIRED_OHLC_COLUMNS
from quantfusion.domain.rules import A_SHARE_LOT_SIZE, SYMBOL_RE, parse_dates

try:
    import akshare as ak  # pyright: ignore[reportMissingImports]
except ImportError:
    ak = None

_SYMBOL_RE = SYMBOL_RE
_parse_dates = parse_dates

class DataFetcher:
    """Load and validate forward-adjusted A-share daily market data."""

    _COLUMN_ALIASES: ClassVar[dict[str, str]] = {
        # AKShare providers return localized headers. Unicode escapes keep the
        # source English-only while mapping the localized provider frames.
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
        assert ak is not None
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
        assert ak is not None
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
        symbol: str,
        start_date: str,
        end_date: str,
        data_dir: str | None = None,
        *,
        cache_dir: str | None = None,
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
        if cache_dir:
            return DataFetcher._load_with_cache(
                symbol, start_date, end_date, cache_dir
            )
        return DataFetcher.fetch_stock_data(symbol, start_date, end_date)

    @staticmethod
    def _load_with_cache(
        symbol: str, start_date: str, end_date: str, cache_dir: str
    ) -> pd.DataFrame:
        """Hybrid mode: load local cache, fetch only incremental data from network."""
        import sys

        def _log(msg: str) -> None:
            print(msg, file=sys.stderr, flush=True)

        cache_path = Path(cache_dir).expanduser() / f"{symbol}.csv"
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
                f"  [Cache] {symbol}: cache lacks a verified share-volume "
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
