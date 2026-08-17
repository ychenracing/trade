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

import daily_signal_scan as dss


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


VALID_ACCOUNT = {
    "cash": 500000.0,
    "peak_equity": 2500000.0,
    "positions": {
        "300308": {"shares": 900, "avg_cost": 980.50, "entry_date": "2026-03-18"},
        "688256": {"shares": 200, "avg_cost": 1250.00, "entry_date": "2026-04-15"},
    },
    "risk_state": {
        "terminal_risk_lock": False,
        "sector_guard_active": False,
        "cycle_lock_count": 0,
    },
}


VALID_RISK_STATE = {
    "schema_version": 1,
    "scan_date": "2026-07-30",
    "terminal_risk_lock": False,
    "sector_guard_active": False,
    "cycle_lock_count": 0,
    "max_drawdown": -0.12,
    "total_return": 0.08,
    "final_assets": 2160000.0,
}
