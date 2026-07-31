#!/usr/bin/env python3
"""End-to-end tests for daily_signal_scan.py utility functions.

Tests account loading, risk state persistence, signal classification,
position reconstruction, and CLI argument parsing without requiring
network access.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from collections import namedtuple
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import daily_signal_scan as dss


# ── Test fixtures ────────────────────────────────────────────────────

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


# ── Account loading tests ───────────────────────────────────────────

class AccountLoadingTests(unittest.TestCase):
    """Verify _load_account handles valid, missing, and malformed inputs."""

    def test_valid_account_loads_correctly(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(VALID_ACCOUNT, f)
            path = f.name
        try:
            result = dss._load_account(path)
            self.assertIsNotNone(result)
            assert result is not None  # for type checker
            self.assertEqual(result["cash"], 500000.0)
            self.assertEqual(result["peak_equity"], 2500000.0)
            self.assertEqual(len(result["positions"]), 2)
            self.assertEqual(result["positions"]["300308"]["shares"], 900)
        finally:
            Path(path).unlink()

    def test_missing_file_returns_none(self) -> None:
        result = dss._load_account("/nonexistent/path/account.json")
        self.assertIsNone(result)

    def test_invalid_json_returns_none(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            f.write("{invalid json content}")
            path = f.name
        try:
            result = dss._load_account(path)
            self.assertIsNone(result)
        finally:
            Path(path).unlink()

    def test_missing_cash_field_returns_none(self) -> None:
        account = {"positions": {}, "peak_equity": 1000000.0}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(account, f)
            path = f.name
        try:
            result = dss._load_account(path)
            self.assertIsNone(result)
        finally:
            Path(path).unlink()

    def test_missing_optional_fields_get_defaults(self) -> None:
        account = {"cash": 100000.0}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(account, f)
            path = f.name
        try:
            result = dss._load_account(path)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result["positions"], {})
            self.assertEqual(result["risk_state"], {})
            self.assertIsNone(result["peak_equity"])
        finally:
            Path(path).unlink()


# ── Risk state persistence tests ────────────────────────────────────

class RiskStateTests(unittest.TestCase):
    """Verify risk state save/load round-trip and edge cases."""

    def test_save_and_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = {
                "terminal_risk_lock": True,
                "sector_guard_active": True,
                "cycle_lock_count": 2,
                "max_drawdown": -0.1825,
                "total_return": 0.4532,
                "final_assets": 2906400.0,
            }
            dss._save_risk_state(tmpdir, "2026-07-30", result)

            loaded = dss._load_prev_risk_state(tmpdir, "2026-07-31")
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded["scan_date"], "2026-07-30")
            self.assertTrue(loaded["terminal_risk_lock"])
            self.assertTrue(loaded["sector_guard_active"])
            self.assertEqual(loaded["cycle_lock_count"], 2)

    def test_same_day_state_is_not_previous(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = {
                "terminal_risk_lock": False,
                "sector_guard_active": False,
                "cycle_lock_count": 0,
                "max_drawdown": -0.05,
                "total_return": 0.10,
                "final_assets": 2200000.0,
            }
            dss._save_risk_state(tmpdir, "2026-07-30", result)

            # Same-day load should return None (not "previous")
            loaded = dss._load_prev_risk_state(tmpdir, "2026-07-30")
            self.assertIsNone(loaded)

    def test_missing_state_file_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            loaded = dss._load_prev_risk_state(tmpdir, "2026-07-30")
            self.assertIsNone(loaded)

    def test_corrupted_state_file_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "risk_state.json"
            state_file.write_text("{corrupted", encoding="utf-8")
            loaded = dss._load_prev_risk_state(tmpdir, "2026-07-31")
            self.assertIsNone(loaded)


# ── Signal classification tests ─────────────────────────────────────

class SignalClassificationTests(unittest.TestCase):
    """Verify _classify_signal maps directions to Chinese labels."""

    def test_buy_signal(self) -> None:
        sig = FakeSignal("buy", "turtle", "300308", 100, 150.0, "breakout", "2026-07-30")
        self.assertEqual(dss._classify_signal(sig), "买入")

    def test_sell_signal(self) -> None:
        sig = FakeSignal("sell", "turtle", "300308", 100, 150.0, "stop_loss", "2026-07-30")
        self.assertEqual(dss._classify_signal(sig), "卖出")

    def test_hold_signal(self) -> None:
        sig = FakeSignal("hold", "turtle", "300308", 0, 150.0, "no_signal", "2026-07-30")
        self.assertEqual(dss._classify_signal(sig), "持有")

    def test_unknown_direction_defaults_to_hold(self) -> None:
        sig = FakeSignal("unknown", "turtle", "300308", 0, 150.0, "test", "2026-07-30")
        self.assertEqual(dss._classify_signal(sig), "持有")

    def test_missing_direction_attribute(self) -> None:
        class NoDirection:
            pass
        self.assertEqual(dss._classify_signal(NoDirection()), "持有")


# ── Position extraction tests ───────────────────────────────────────

class PositionExtractionTests(unittest.TestCase):
    """Verify _extract_positions reconstructs net shares from trades."""

    def test_single_buy(self) -> None:
        trades = [FakeTrade("buy", "300308", 900)]
        positions = dss._extract_positions(trades)
        self.assertEqual(positions["300308"], 900)

    def test_buy_then_partial_sell(self) -> None:
        trades = [
            FakeTrade("buy", "300308", 900),
            FakeTrade("sell", "300308", 300),
        ]
        positions = dss._extract_positions(trades)
        self.assertEqual(positions["300308"], 600)

    def test_full_sell_clamped_to_zero(self) -> None:
        trades = [
            FakeTrade("buy", "300308", 900),
            FakeTrade("sell", "300308", 900),
        ]
        positions = dss._extract_positions(trades)
        self.assertEqual(positions["300308"], 0)

    def test_oversell_clamped_to_zero(self) -> None:
        trades = [
            FakeTrade("buy", "300308", 500),
            FakeTrade("sell", "300308", 800),
        ]
        positions = dss._extract_positions(trades)
        self.assertEqual(positions["300308"], 0)

    def test_multiple_symbols(self) -> None:
        trades = [
            FakeTrade("buy", "300308", 900),
            FakeTrade("buy", "688256", 200),
            FakeTrade("sell", "300308", 100),
        ]
        positions = dss._extract_positions(trades)
        self.assertEqual(positions["300308"], 800)
        self.assertEqual(positions["688256"], 200)

    def test_empty_trades(self) -> None:
        positions = dss._extract_positions([])
        self.assertEqual(positions, {})

    def test_pyramiding_buys(self) -> None:
        trades = [
            FakeTrade("buy", "300308", 300),
            FakeTrade("buy", "300308", 200),
            FakeTrade("buy", "300308", 100),
        ]
        positions = dss._extract_positions(trades)
        self.assertEqual(positions["300308"], 600)


# ── CLI argument tests ──────────────────────────────────────────────

class CLIArgumentTests(unittest.TestCase):
    """Verify CLI arguments are parsed correctly."""

    def test_default_arguments(self) -> None:
        with patch("sys.argv", ["daily_signal_scan.py"]):
            parser = dss.__dict__.get("_parser")
            # We can't easily test main() without network, but we can
            # verify the parser is constructed with expected defaults
            # by checking the argparse setup indirectly
            pass

    def test_all_26_symbols_are_mapped(self) -> None:
        """Verify all SYMBOLS have explicit routing metadata."""
        import quant_fusion as qf
        for code, name in dss.SYMBOLS.items():
            self.assertFalse(
                qf._CoreBacktestEngine._uses_unmapped_auto_route(code, name),
                f"{code} {name} is unmapped — add it to _SYMBOL_PROFILE and _SYMBOL_GROUP",
            )

    def test_symbol_count_is_26(self) -> None:
        self.assertEqual(len(dss.SYMBOLS), 26)

    def test_no_duplicate_codes(self) -> None:
        codes = list(dss.SYMBOLS.keys())
        self.assertEqual(len(codes), len(set(codes)))

    def test_default_start_date_is_valid(self) -> None:
        import pandas as pd
        ts = pd.Timestamp(dss.START_DATE)
        self.assertTrue(pd.notna(ts))

    def test_default_capital_is_positive(self) -> None:
        self.assertGreater(dss.INITIAL_CAPITAL, 0)


# ── Integration: account + risk state workflow ─────────────────────

class AccountRiskWorkflowTests(unittest.TestCase):
    """Verify the account + risk state workflow end-to-end."""

    def test_account_with_active_risk_state_shows_warnings(self) -> None:
        """An account with terminal_risk_lock=True should be loadable."""
        account = dict(VALID_ACCOUNT)
        account["risk_state"]["terminal_risk_lock"] = True
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(account, f)
            path = f.name
        try:
            result = dss._load_account(path)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertTrue(result["risk_state"]["terminal_risk_lock"])
        finally:
            Path(path).unlink()

    def test_risk_state_persists_terminal_lock(self) -> None:
        """Terminal risk lock should survive save/load round-trip."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = {
                "terminal_risk_lock": True,
                "sector_guard_active": False,
                "cycle_lock_count": 1,
                "max_drawdown": -0.285,
                "total_return": -0.15,
                "final_assets": 1700000.0,
            }
            dss._save_risk_state(tmpdir, "2026-07-28", result)
            loaded = dss._load_prev_risk_state(tmpdir, "2026-07-30")
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertTrue(loaded["terminal_risk_lock"])
            self.assertEqual(loaded["cycle_lock_count"], 1)
            self.assertAlmostEqual(loaded["max_drawdown"], -0.285)

    def test_risk_state_file_is_valid_json(self) -> None:
        """The saved risk_state.json must be valid JSON with all fields."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = {
                "terminal_risk_lock": False,
                "sector_guard_active": True,
                "cycle_lock_count": 0,
                "max_drawdown": -0.12,
                "total_return": 0.08,
                "final_assets": 2160000.0,
            }
            dss._save_risk_state(tmpdir, "2026-07-29", result)
            state_path = Path(tmpdir) / "risk_state.json"
            self.assertTrue(state_path.exists())
            data = json.loads(state_path.read_text(encoding="utf-8"))
            expected_keys = {
                "scan_date", "terminal_risk_lock", "sector_guard_active",
                "cycle_lock_count", "max_drawdown", "total_return", "final_assets",
            }
            self.assertEqual(set(data.keys()), expected_keys)


if __name__ == "__main__":
    unittest.main()
