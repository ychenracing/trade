"""Frozen point-in-time evidence snapshots with SHA-256 manifests."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

from quantfusion.config.regime import REGIME_INDEX_FILES
from quantfusion.data.sessions import index_coverage

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_frozen_snapshot(snapshot_dir: str | Path) -> dict[str, Any]:
    """Verify the hashed manifest, every CSV byte, and the exact file set."""
    root = Path(snapshot_dir)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("frozen snapshot root must be a real directory")
    manifest_path = root / "manifest.json"
    signature_path = root / "manifest.sha256"
    if not manifest_path.is_file() or not signature_path.is_file():
        raise ValueError("frozen snapshot is missing its manifest or signature")
    expected_manifest_hash = signature_path.read_text(encoding="ascii").strip()
    if _sha256_file(manifest_path) != expected_manifest_hash:
        raise ValueError("frozen snapshot manifest bytes were modified")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("frozen snapshot manifest must be an object")
    symbols = manifest.get("symbols")
    if (
        not isinstance(symbols, list) or not symbols
        or any(not isinstance(code, str) or re.fullmatch(r"[0-9]{6}", code) is None
               for code in symbols)
        or len(symbols) != len(set(symbols))
    ):
        raise ValueError("frozen snapshot symbol inventory is invalid")
    required_csvs = {f"market_data/{code}.csv" for code in symbols} | {
        f"regime_data/{code}.csv" for code in REGIME_INDEX_FILES.values()
    }
    evidence = manifest.get("evidence", [])
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("frozen snapshot manifest contains no evidence files")
    expected_files = {"manifest.json", "manifest.sha256"}
    for item in evidence:
        if not isinstance(item, dict):
            raise ValueError("frozen snapshot evidence entry must be an object")
        relative = str(item.get("path", ""))
        if relative not in required_csvs or relative in expected_files:
            raise ValueError("frozen snapshot manifest contains an unsafe path")
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"frozen snapshot evidence is missing: {relative}")
        if _sha256_file(path) != item.get("sha256"):
            raise ValueError(f"frozen snapshot evidence was modified: {relative}")
        if path.stat().st_size != int(item.get("bytes", -1)):
            raise ValueError(f"frozen snapshot evidence size changed: {relative}")
        expected_files.add(relative)
    if expected_files - {"manifest.json", "manifest.sha256"} != required_csvs:
        raise ValueError("frozen snapshot is missing required symbol or index evidence")
    actual_files = {
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    }
    extra = sorted(actual_files - expected_files)
    missing = sorted(expected_files - actual_files)
    if extra or missing:
        raise ValueError(
            f"frozen snapshot file set changed (extra={extra}, missing={missing})"
        )
    if manifest.get("deployment_policy") != "production_daily_replay":
        raise ValueError("frozen snapshot is not bound to production daily replay")
    return manifest


def _materialize_frozen_snapshot(
    *,
    snapshot_dir: str | Path,
    cache_dir: str | Path,
    regime_data_dir: str | Path,
    frames: dict[str, pd.DataFrame],
    end_date: str,
    scan_dates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create once or strictly reuse one same-day production evidence snapshot."""
    target = Path(snapshot_dir)
    if target.exists():
        manifest = _verify_frozen_snapshot(target)
        if manifest.get("end_date") != end_date:
            raise ValueError("frozen snapshot end date does not match this scan")
        if manifest.get("symbols") != sorted(frames):
            raise ValueError("frozen snapshot symbol universe does not match this scan")
        if scan_dates is not None and manifest.get("scan_dates") != scan_dates:
            raise ValueError("frozen snapshot calendar identity differs or was never certified")
        if scan_dates is not None:
            index_coverage(target / "regime_data", scan_dates)
        for code, frame in frames.items():
            dates = pd.read_csv(target / "market_data" / f"{code}.csv", usecols=["date"])
            frozen_dates = pd.DatetimeIndex(pd.to_datetime(dates["date"], errors="raise"))
            requested_dates = pd.DatetimeIndex(frame.index)
            if len(frozen_dates) != len(requested_dates) or not (frozen_dates == requested_dates).all():
                raise ValueError(f"frozen snapshot date coverage differs for {code}")
        return manifest

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(dir=str(target.parent), prefix=".snapshot-"))
    try:
        market_target = temporary / "market_data"
        regime_target = temporary / "regime_data"
        market_target.mkdir()
        regime_target.mkdir()
        for code, frame in sorted(frames.items()):
            # Freeze the checked values, not a mutable cache read after validation.
            destination = market_target / f"{code}.csv"
            persisted = frame.copy()
            persisted.index.name = "date"
            persisted.to_csv(destination, index=True)
        for code in sorted(REGIME_INDEX_FILES.values()):
            source = Path(regime_data_dir).expanduser() / f"{code}.csv"
            if not source.is_file():
                raise ValueError(f"missing frozen regime evidence for {code}")
            shutil.copyfile(source, regime_target / source.name)
        checked_indices = index_coverage(regime_target, scan_dates) if scan_dates is not None else None
        evidence = []
        for path in sorted(temporary.rglob("*.csv")):
            evidence.append(
                {
                    "path": path.relative_to(temporary).as_posix(),
                    "sha256": _sha256_file(path),
                    "bytes": path.stat().st_size,
                }
            )
        manifest = {
            "schema_version": 1,
            "end_date": end_date,
            "symbols": sorted(frames),
            "deployment_policy": "production_daily_replay",
            "evidence": evidence,
        }
        if scan_dates is not None:
            manifest["scan_dates"] = scan_dates
            manifest["index_evidence"] = checked_indices
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        ).encode("utf-8")
        (temporary / "manifest.json").write_bytes(manifest_bytes)
        (temporary / "manifest.sha256").write_text(
            hashlib.sha256(manifest_bytes).hexdigest() + "\n", encoding="ascii"
        )
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return _verify_frozen_snapshot(target)


sha256_file = _sha256_file
verify_frozen_snapshot = _verify_frozen_snapshot
materialize_frozen_snapshot = _materialize_frozen_snapshot
