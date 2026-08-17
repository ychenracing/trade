"""Stable source-tree fingerprints for resumable research artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any


PACKAGE = Path(__file__).resolve().parents[1]
ECONOMIC_SOURCE_PATHS = tuple(
    PACKAGE / name
    for name in (
        "domain",
        "config",
        "data",
        "indicators",
        "strategy",
        "execution",
        "portfolio",
        "risk",
        "regime",
        "engine",
    )
)


def _source_sha(paths: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    files: set[Path] = set()
    for path in paths:
        files.update(path.rglob("*.py") if path.is_dir() else (path,))
    for path in sorted(files):
        digest.update(path.relative_to(PACKAGE).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _json_default(value: Any) -> Any:
    """Normalize domain records and scalar wrappers for canonical JSON."""
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    item = getattr(value, "item", None)
    if callable(item):
        return item()
    raise TypeError(f"Unsupported economic fingerprint value: {type(value).__name__}")


def _canonical_sequence_sha(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_json_default,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def economic_sequence_fingerprints(result: dict[str, Any]) -> dict[str, Any]:
    """Return exact behavioral hashes and counts for one golden replay."""
    sequence_keys = (
        "trades",
        "order_events",
        "risk_events",
        "fusion_events",
        "regime_state_series",
    )
    output: dict[str, Any] = {
        f"{key}_sha256": _canonical_sequence_sha(result.get(key, []))
        for key in sequence_keys
    }
    count_names = {
        "trades": "trade_fill_count",
        "order_events": "order_event_count",
        "risk_events": "risk_event_count",
        "fusion_events": "fusion_event_count",
        "regime_state_series": "regime_state_count",
        "pending_signals": "pending_signal_count",
    }
    for key, name in count_names.items():
        output[name] = len(result.get(key, []))
    return output


def engine_source_sha() -> str:
    """Fingerprint every economic layer reachable by the production engine."""
    return _source_sha(ECONOMIC_SOURCE_PATHS)


def replay_source_sha() -> str:
    """Fingerprint the complete economic graph used by production replay."""
    return _source_sha(ECONOMIC_SOURCE_PATHS)


def optimizer_source_sha() -> str:
    """Fingerprint all research code plus its application entry point."""
    return _source_sha((PACKAGE / "research", PACKAGE / "application" / "optimizer.py"))
