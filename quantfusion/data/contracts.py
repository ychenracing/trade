"""日常决策所需的行情提供方契约与固定指数刷新逻辑。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd

try:
    import akshare as ak  # pyright: ignore[reportMissingImports]
except ImportError:
    ak = None


INDEX_SYMBOLS = {"000300": "csi000300", "000682": "csi000682"}
REQUIRED_OHLC_COLUMNS = ("open", "close", "high", "low")
OPTIONAL_COLUMNS = ("volume",)
_REQUIRED_PRICE_COLUMNS = REQUIRED_OHLC_COLUMNS


def _atomic_csv(frame: pd.DataFrame, path: Path) -> None:
    """以临时文件、刷盘和原子替换方式写入 CSV。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=".index_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            frame.to_csv(handle, index=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_json(payload: dict[str, Any], path: Path) -> None:
    """原子写入严格 JSON，避免刷新中断留下半个清单文件。"""
    content = (
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=".manifest_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _normalize_index_frame(frame: pd.DataFrame, *, end_date: str) -> pd.DataFrame:
    """规范并验证指数 OHLCV，拒绝重复、未来和不可能的价格。"""
    if frame is None or frame.empty:
        raise ValueError("empty index response")

    names = {str(column).strip().lower(): column for column in frame.columns}
    required = ("date", *_REQUIRED_PRICE_COLUMNS)
    if not all(name in names for name in required):
        raise ValueError(f"unexpected index columns: {list(frame.columns)}")

    has_volume = "volume" in names
    volume = (
        pd.to_numeric(frame[names["volume"]], errors="coerce")
        if has_volume
        else pd.Series(0.0, index=frame.index)
    )
    out = pd.DataFrame(
        {
            "date": pd.to_datetime(frame[names["date"]], errors="coerce"),
            "open": pd.to_numeric(frame[names["open"]], errors="coerce"),
            "close": pd.to_numeric(frame[names["close"]], errors="coerce"),
            "high": pd.to_numeric(frame[names["high"]], errors="coerce"),
            "low": pd.to_numeric(frame[names["low"]], errors="coerce"),
            "volume": volume,
        }
    )
    if bool(out.loc[:, list(required)].isna().to_numpy().any()):
        raise ValueError("index response contains an unparseable date or OHLC value")
    if has_volume and bool(out.loc[:, "volume"].isna().to_numpy().any()):
        raise ValueError("index response contains an unparseable volume value")

    boundary = pd.Timestamp(end_date)
    if boundary is pd.NaT:
        raise ValueError("end_date must resolve to a valid timestamp")
    boundary = cast(pd.Timestamp, boundary).normalize()
    out = out.loc[out["date"] <= boundary].copy()
    out.sort_values("date", inplace=True)
    out.drop_duplicates(subset=["date"], keep="last", inplace=True)

    if len(out) < 60:
        raise ValueError("insufficient index history")
    prices = out[list(_REQUIRED_PRICE_COLUMNS)]
    if not np.isfinite(prices.to_numpy(dtype=float)).all():
        raise ValueError("index OHLC prices must be finite")
    if (prices <= 0).any().any():
        raise ValueError("index OHLC prices must be positive")
    volume_values = out["volume"].to_numpy(dtype=float)
    if not np.isfinite(volume_values).all():
        raise ValueError("index volume must be finite")
    if (out["volume"] < 0).any():
        raise ValueError("index volume must be non-negative")
    if (
        (out["high"] < out[["open", "close"]].max(axis=1)).any()
        or (out["low"] > out[["open", "close"]].min(axis=1)).any()
        or (out["high"] < out["low"]).any()
    ):
        raise ValueError("index OHLC relationships are invalid")

    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    return out.reset_index(drop=True)


def refresh_regime_indices(
    data_dir: str | Path,
    *,
    end_date: str,
    strict: bool = False,
) -> dict[str, Any]:
    """刷新两只固定指数；外部失败时保留最近一次完整文件。"""
    root = Path(data_dir)
    status: dict[str, Any] = {"end_date": end_date, "indices": {}}

    end_timestamp = pd.Timestamp(end_date)
    if end_timestamp is pd.NaT:
        raise ValueError("end_date must resolve to a valid timestamp")
    end_timestamp = cast(pd.Timestamp, end_timestamp).normalize()

    # 历史回放必须使用冻结快照，只有接近当前日期的日扫才访问外部接口。
    if end_timestamp < (
        pd.Timestamp.today().normalize() - pd.Timedelta(days=2)
    ):
        missing: list[str] = []
        for code in INDEX_SYMBOLS:
            existing = root / f"{code}.csv"
            available = existing.is_file()
            status["indices"][code] = {
                "status": "frozen_historical" if available else "unavailable"
            }
            if not available:
                missing.append(code)
        if strict and missing:
            raise RuntimeError(
                "missing frozen regime-index files: " + ", ".join(missing)
            )
        return status

    if ak is None:
        if strict:
            raise RuntimeError("AKShare is required to refresh regime indices")
        status["error"] = "AKShare is not installed"
        return status

    for code, provider_symbol in INDEX_SYMBOLS.items():
        try:
            frame = ak.stock_zh_index_daily_em(
                symbol=provider_symbol,
                start_date="20200101",
                end_date=end_timestamp.strftime("%Y%m%d"),
            )
            out = _normalize_index_frame(
                frame,
                end_date=end_timestamp.strftime("%Y-%m-%d"),
            )
            _atomic_csv(out, root / f"{code}.csv")
            status["indices"][code] = {
                "status": "updated",
                "last_date": out["date"].iloc[-1],
                "rows": len(out),
            }
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            existing = root / f"{code}.csv"
            status["indices"][code] = {
                "status": (
                    "preserved_last_good"
                    if existing.is_file()
                    else "unavailable"
                ),
                "error": str(exc),
            }
            if strict and not existing.is_file():
                raise RuntimeError(
                    f"Unable to refresh index {code}: {exc}"
                ) from exc

    _atomic_json(status, root / "live_refresh_manifest.json")
    return status
