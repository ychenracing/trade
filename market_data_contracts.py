"""Provider and fixed-index data contracts used by daily decision support."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, cast

import pandas as pd

try:
    import akshare as ak
except ImportError:
    ak = None

INDEX_SYMBOLS = {"000300": "csi000300", "000682": "csi000682"}


def _atomic_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".index_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            frame.to_csv(handle, index=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def refresh_regime_indices(
    data_dir: str | Path,
    *,
    end_date: str,
    strict: bool = False,
) -> dict[str, Any]:
    """Refresh fixed-index evidence without destroying last-good files."""
    root = Path(data_dir)
    status: dict[str, Any] = {"end_date": end_date, "indices": {}}
    # Historical replays use the frozen local snapshot. Only near-current
    # daily scans contact the external provider, which keeps tests and
    # reproducible past runs network-independent.
    end_timestamp = pd.Timestamp(end_date)
    if pd.isna(end_timestamp):
        raise ValueError("end_date must resolve to a valid timestamp")
    end_timestamp = cast(pd.Timestamp, end_timestamp)
    if end_timestamp.normalize() < (
        pd.Timestamp.today().normalize() - pd.Timedelta(days=2)
    ):
        for code in INDEX_SYMBOLS:
            existing = root / f"{code}.csv"
            status["indices"][code] = {
                "status": "frozen_historical" if existing.is_file() else "unavailable"
            }
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
                end_date=end_date.replace("-", ""),
            )
            if frame is None or frame.empty:
                raise ValueError("empty index response")
            names = {str(column).strip().lower(): column for column in frame.columns}
            required = ("date", "open", "close", "high", "low")
            if not all(name in names for name in required):
                raise ValueError(f"unexpected index columns: {list(frame.columns)}")
            out = pd.DataFrame(
                {
                    "date": pd.to_datetime(frame[names["date"]], errors="coerce"),
                    "open": pd.to_numeric(frame[names["open"]], errors="coerce"),
                    "close": pd.to_numeric(frame[names["close"]], errors="coerce"),
                    "high": pd.to_numeric(frame[names["high"]], errors="coerce"),
                    "low": pd.to_numeric(frame[names["low"]], errors="coerce"),
                    "volume": pd.to_numeric(
                        frame[names.get("volume")], errors="coerce"
                    ) if "volume" in names else 0.0,
                }
            ).dropna(subset=["date", "open", "close", "high", "low"])
            out = out.loc[out["date"] <= pd.Timestamp(end_date)].copy()
            if len(out) < 60:
                raise ValueError("insufficient index history")
            out["date"] = out["date"].dt.strftime("%Y-%m-%d")
            _atomic_csv(out, root / f"{code}.csv")
            status["indices"][code] = {
                "status": "updated",
                "last_date": out["date"].iloc[-1],
                "rows": len(out),
            }
        except Exception as exc:  # external provider boundary
            existing = root / f"{code}.csv"
            status["indices"][code] = {
                "status": "preserved_last_good" if existing.is_file() else "unavailable",
                "error": str(exc),
            }
            if strict and not existing.is_file():
                raise RuntimeError(f"Unable to refresh index {code}: {exc}") from exc
    manifest = root / "live_refresh_manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return status
