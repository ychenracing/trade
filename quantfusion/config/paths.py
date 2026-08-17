"""Canonical repository paths for bundled examples, data, and validation assets."""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = PROJECT_ROOT / "data"
MARKET_DATA_DIR = DATA_ROOT / "market"
REGIME_DATA_DIR = DATA_ROOT / "regime"
EXAMPLES_DIR = PROJECT_ROOT / "examples"
VALIDATION_ARTIFACT_DIR = PROJECT_ROOT / "artifacts" / "validation"
TEST_FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"
BACKTEST_GOLDEN_METRICS = TEST_FIXTURES_DIR / "backtest_golden_metrics.json"
_LEGACY_DATA_DIRECTORIES = {
    Path("market_data"): MARKET_DATA_DIR,
    Path("historical_data"): REGIME_DATA_DIR,
}


def resolve_repository_data_dir(value: str | Path) -> Path:
    """Resolve removed repository data names while preserving real user paths.

    Existing, absolute, and non-legacy paths are never rewritten. The fallback
    only applies to the two historical relative directory names when they do
    not exist in the caller's working directory.
    """
    path = Path(value).expanduser()
    if path.exists() or path.is_absolute():
        return path
    return _LEGACY_DATA_DIRECTORIES.get(path, path)


__all__ = [
    "BACKTEST_GOLDEN_METRICS",
    "DATA_ROOT",
    "EXAMPLES_DIR",
    "MARKET_DATA_DIR",
    "PROJECT_ROOT",
    "REGIME_DATA_DIR",
    "TEST_FIXTURES_DIR",
    "VALIDATION_ARTIFACT_DIR",
    "resolve_repository_data_dir",
]
