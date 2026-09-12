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


def synthetic_budget_fields() -> dict:
    """A genuinely evaluated synthetic close, not an enabled-only success stub."""
    from quantfusion.config.engine import default_engine_config
    from quantfusion.risk.account_budget import account_budget_plan, account_budget_status

    plan, _ = account_budget_plan(2_000_000., 2_000_000., default_engine_config(),
                                  0, [], [], sell_order=[])
    event = {"event": "account_budget_envelope", "date": "2026-07-30",
             "mechanism": "AB5", "planned_not_filled": True, **plan,
             "buy_shares_removed": 0, "new_reduction_orders": 0}
    return {"risk_events": [event], "account_risk_budget": account_budget_status(
        True, [event], hwm_source="merged_account.lifetime_peak_assets")}
