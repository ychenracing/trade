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


__all__ = [
    "BACKTEST_GOLDEN_METRICS",
    "DATA_ROOT",
    "EXAMPLES_DIR",
    "MARKET_DATA_DIR",
    "PROJECT_ROOT",
    "REGIME_DATA_DIR",
    "TEST_FIXTURES_DIR",
    "VALIDATION_ARTIFACT_DIR",
]
