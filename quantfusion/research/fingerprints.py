"""Stable source-tree fingerprints for resumable research artifacts."""

from __future__ import annotations

import hashlib
from pathlib import Path


PACKAGE = Path(__file__).resolve().parents[1]
PRODUCTION_SOURCE_PACKAGES = (
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


def production_source_paths() -> tuple[Path, ...]:
    """Return every package that can affect production execution semantics."""
    return tuple(PACKAGE / name for name in PRODUCTION_SOURCE_PACKAGES)


def _source_sha(paths: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    files: set[Path] = set()
    for path in paths:
        files.update(path.rglob("*.py") if path.is_dir() else (path,))
    for path in sorted(files):
        digest.update(path.relative_to(PACKAGE).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def engine_source_sha() -> str:
    """Fingerprint the composed production engine and its policy sources."""
    return _source_sha(production_source_paths())


def replay_source_sha() -> str:
    """Fingerprint the full production tree used by replay evaluations."""
    return _source_sha(production_source_paths())


def optimizer_source_sha() -> str:
    """Fingerprint all research code plus its application entry point."""
    return _source_sha((PACKAGE / "research", PACKAGE / "application" / "optimizer.py"))
