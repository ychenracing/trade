"""Shared fixtures for daily-scan integration contracts."""

from __future__ import annotations

# ruff: noqa: F401

import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections import namedtuple
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from quantfusion.config.paths import REGIME_DATA_DIR
from quantfusion.config.regime import REGIME_INDEX_FILES
from unittest.mock import patch

from quantfusion.application import daily_scan as dss


@dataclass(frozen=True, slots=True)
class FakeSignal:
    direction: str
    strategy_name: str
    symbol: str
    target_shares: int
    price: float
    reason: str
    signal_date: str


FakeTrade = namedtuple("FakeTrade", ["direction", "symbol", "shares"])


VALID_RISK_STATE = {
    "schema_version": 1,
    "run_id": "test-run-20260730",
    "scan_date": "2026-07-30",
    "terminal_risk_lock": False,
    "sector_guard_active": False,
    "cycle_lock_count": 0,
    "max_drawdown": -0.12,
    "total_return": 0.08,
    "final_assets": 2160000.0,
}


def synthetic_regime_dir(root: str | Path, end_date: str = "2026-07-30") -> Path:
    """Create explicit synthetic index evidence for non-market-data tests.

    These integration tests exercise artifact/state mechanics rather than index
    acquisition. Keep the retained history and append only a synthetic target
    row so the production trading-session gate is still exercised instead of
    being patched out.
    """
    target = Path(root) / "_synthetic_regime"
    target.mkdir(parents=True, exist_ok=True)
    target_stamp = pd.Timestamp(end_date)
    for code in REGIME_INDEX_FILES.values():
        source = pd.read_csv(Path(REGIME_DATA_DIR) / f"{code}.csv")
        source["date"] = pd.to_datetime(source["date"], errors="raise")
        source = source.loc[source["date"] <= target_stamp].copy()
        if source.empty:
            raise ValueError(f"retained index fixture does not cover history for {end_date}")
        if not source["date"].eq(target_stamp).any():
            row = source.iloc[-1].copy()
            row["date"] = target_stamp
            source = pd.concat([source, row.to_frame().T], ignore_index=True)
        source["date"] = pd.to_datetime(source["date"]).dt.strftime("%Y-%m-%d")
        source.to_csv(target / f"{code}.csv", index=False)
    return target
