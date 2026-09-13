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


def regime_evidence_dir(root: str, as_of: str = "2026-07-30") -> Path:
    """Synthetic, date-complete indices for mocked orchestration tests only.

    Frozen market files remain unchanged. Construct independent prices on
    sourced exchange sessions so transaction tests reach their injected faults
    without bypassing the production coverage or stale-data checks.
    """
    import pandas as pd

    from quantfusion.config.regime import REGIME_INDEX_FILES
    from quantfusion.data.sessions import load_calendar

    sessions = [day for day in load_calendar().sessions if day <= as_of][-160:]
    if not sessions or sessions[-1] != as_of:
        raise ValueError("synthetic fixture requires a covered exchange session")
    directory = Path(root) / "synthetic_regime"
    directory.mkdir(parents=True, exist_ok=True)
    close = pd.Series([100. + i * .01 for i in range(len(sessions))],
                      index=pd.to_datetime(sessions))
    frame = pd.DataFrame({"open": close, "close": close, "high": close + 1.,
                          "low": close - 1., "volume": 1_000_000.})
    frame.index.name = "date"
    for code in REGIME_INDEX_FILES.values():
        frame.to_csv(directory / f"{code}.csv")
    return directory
