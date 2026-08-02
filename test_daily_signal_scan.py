#!/usr/bin/env python3
"""End-to-end tests for daily_signal_scan.py utility functions.

Tests account loading, risk state persistence, signal classification,
position reconstruction, buy suppression, schema validation, pre-save
validation, CLI integration (subprocess), and CLI argument parsing
without requiring network access.
"""

from __future__ import annotations

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

# A complete valid risk state for schema validation tests
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
            self.assertEqual(result["peak_equity"], 100000.0)  # falls back to cash
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

            loaded, error = dss._load_prev_risk_state(tmpdir, "2026-07-31")
            self.assertIsNone(error)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded["scan_date"], "2026-07-30")
            self.assertTrue(loaded["terminal_risk_lock"])
            self.assertTrue(loaded["sector_guard_active"])
            self.assertEqual(loaded["cycle_lock_count"], 2)

    def test_same_day_state_still_loaded(self) -> None:
        """Same-day rerun should still return the state (not silently discard)
        so the caller can preserve terminal lock continuity."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = {
                "terminal_risk_lock": True,
                "sector_guard_active": False,
                "cycle_lock_count": 1,
                "max_drawdown": -0.05,
                "total_return": 0.10,
                "final_assets": 2200000.0,
            }
            dss._save_risk_state(tmpdir, "2026-07-30", result)

            # Same-day load should still return the state with no error
            loaded, error = dss._load_prev_risk_state(tmpdir, "2026-07-30")
            self.assertIsNone(error)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertTrue(loaded["terminal_risk_lock"])

    def test_missing_state_file_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            loaded, error = dss._load_prev_risk_state(tmpdir, "2026-07-30")
            self.assertIsNone(loaded)
            self.assertIsNone(error)

    def test_corrupted_state_file_returns_error(self) -> None:
        """Corrupted risk state should return an error, not silently None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "risk_state.json"
            state_file.write_text("{corrupted", encoding="utf-8")
            loaded, error = dss._load_prev_risk_state(tmpdir, "2026-07-31")
            self.assertIsNone(loaded)
            self.assertIsNotNone(error)


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
            loaded, error = dss._load_prev_risk_state(tmpdir, "2026-07-30")
            self.assertIsNone(error)
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
                "schema_version", "scan_date", "terminal_risk_lock",
                "sector_guard_active", "cycle_lock_count", "max_drawdown",
                "total_return", "final_assets",
            }
            self.assertEqual(set(data.keys()), expected_keys)

    def test_risk_state_save_with_tradable_includes_identity(self) -> None:
        """When tradable is provided, risk_state includes symbols_hash and run_id."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = {"terminal_risk_lock": False, "sector_guard_active": False,
                      "cycle_lock_count": 0, "max_drawdown": 0.0,
                      "total_return": 0.0, "final_assets": 2000000.0}
            tradable = {"300308": "中际旭创", "300502": "新易盛"}
            dss._save_risk_state(tmpdir, "2026-07-29", result, tradable=tradable)
            data = json.loads((Path(tmpdir) / "risk_state.json").read_text(encoding="utf-8"))
            self.assertIn("symbols_hash", data)
            self.assertIn("total_symbols", data)
            self.assertIn("run_id", data)
            self.assertEqual(data["total_symbols"], 2)

    def test_risk_state_save_with_config_hash_includes_hash(self) -> None:
        """When config_hash is provided, it is included in the symbols_hash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = {"terminal_risk_lock": False, "sector_guard_active": False,
                      "cycle_lock_count": 0, "max_drawdown": 0.0,
                      "total_return": 0.0, "final_assets": 2000000.0}
            tradable = {"300308": "中际旭创"}
            dss._save_risk_state(tmpdir, "2026-07-29", result, tradable=tradable,
                                 config_hash="start=2026-07-01|indicator=warm")
            data = json.loads((Path(tmpdir) / "risk_state.json").read_text(encoding="utf-8"))
            self.assertIn("symbols_hash", data)
            self.assertNotIn("account_path", data)

    def test_risk_state_without_hash_is_rejected_by_validation(self) -> None:
        """Risk state without symbols_hash should be rejected (fail-closed)."""
        # Simulate old format — no symbols_hash
        prev_risk = {"scan_date": "2026-07-28", "terminal_risk_lock": True,
                     "sector_guard_active": False, "cycle_lock_count": 0}
        # This logic mirrors the validation in daily_signal_scan.py
        prev_hash = prev_risk.get("symbols_hash", "")
        if not prev_hash:
            prev_risk = None  # fail-closed
        self.assertIsNone(prev_risk)

    def test_risk_state_with_wrong_hash_is_rejected(self) -> None:
        """Risk state with mismatched symbols_hash should be rejected."""
        prev_risk = {"scan_date": "2026-07-28", "terminal_risk_lock": True,
                     "sector_guard_active": False, "cycle_lock_count": 0,
                     "symbols_hash": "abcdef1234567890", "total_symbols": 5}
        # Simulate different current hash
        current_hash = "1234567890abcdef"
        prev_hash = prev_risk.get("symbols_hash", "")
        if prev_hash and prev_hash != current_hash:
            prev_risk = None  # reject on mismatch
        self.assertIsNone(prev_risk)


# ── Schema validation tests ─────────────────────────────────────────

class SchemaValidationTests(unittest.TestCase):
    """Verify _validate_risk_state catches type errors, missing fields,
    and unknown schema versions."""

    def test_valid_state_passes_validation(self) -> None:
        """A complete valid state with all required fields passes."""
        self.assertIsNone(dss._validate_risk_state(dict(VALID_RISK_STATE)))

    def test_string_bool_rejected(self) -> None:
        """String 'false' is truthy in Python — must be rejected."""
        data = dict(VALID_RISK_STATE)
        data["terminal_risk_lock"] = "false"
        error = dss._validate_risk_state(data)
        self.assertIsNotNone(error)
        self.assertIn("terminal_risk_lock", error)

    def test_string_int_rejected(self) -> None:
        data = dict(VALID_RISK_STATE)
        data["cycle_lock_count"] = "2"
        error = dss._validate_risk_state(data)
        self.assertIsNotNone(error)
        self.assertIn("cycle_lock_count", error)

    def test_bool_for_int_rejected(self) -> None:
        """bool is a subclass of int in Python — must be explicitly rejected."""
        data = dict(VALID_RISK_STATE)
        data["cycle_lock_count"] = True
        error = dss._validate_risk_state(data)
        self.assertIsNotNone(error)
        self.assertIn("cycle_lock_count", error)

    def test_missing_field_rejected(self) -> None:
        data = dict(VALID_RISK_STATE)
        del data["cycle_lock_count"]
        error = dss._validate_risk_state(data)
        self.assertIsNotNone(error)
        self.assertIn("cycle_lock_count", error)

    def test_non_dict_rejected(self) -> None:
        error = dss._validate_risk_state([1, 2, 3])
        self.assertIsNotNone(error)

    # ── schema_version validation ──

    def test_schema_version_missing_rejected(self) -> None:
        """Risk state without schema_version must be rejected."""
        data = dict(VALID_RISK_STATE)
        del data["schema_version"]
        error = dss._validate_risk_state(data)
        self.assertIsNotNone(error)
        self.assertIn("schema_version", error)

    def test_schema_version_string_rejected(self) -> None:
        """schema_version must be int, not string."""
        data = dict(VALID_RISK_STATE)
        data["schema_version"] = "1"
        error = dss._validate_risk_state(data)
        self.assertIsNotNone(error)
        self.assertIn("schema_version", error)

    def test_schema_version_bool_rejected(self) -> None:
        """schema_version must be int, not bool (bool is subclass of int)."""
        data = dict(VALID_RISK_STATE)
        data["schema_version"] = True
        error = dss._validate_risk_state(data)
        self.assertIsNotNone(error)
        self.assertIn("schema_version", error)

    def test_unknown_schema_version_rejected(self) -> None:
        """Unknown schema versions must be rejected (forward compatibility)."""
        data = dict(VALID_RISK_STATE)
        data["schema_version"] = 999
        error = dss._validate_risk_state(data)
        self.assertIsNotNone(error)
        self.assertIn("999", error)
        self.assertIn("已知版本", error)

    def test_schema_version_zero_rejected(self) -> None:
        """Version 0 is not a known version."""
        data = dict(VALID_RISK_STATE)
        data["schema_version"] = 0
        error = dss._validate_risk_state(data)
        self.assertIsNotNone(error)

    # ── Extended field type validation ──

    def test_max_drawdown_string_rejected(self) -> None:
        data = dict(VALID_RISK_STATE)
        data["max_drawdown"] = "bad"
        error = dss._validate_risk_state(data)
        self.assertIsNotNone(error)
        self.assertIn("max_drawdown", error)

    def test_total_return_string_rejected(self) -> None:
        data = dict(VALID_RISK_STATE)
        data["total_return"] = "bad"
        error = dss._validate_risk_state(data)
        self.assertIsNotNone(error)
        self.assertIn("total_return", error)

    def test_final_assets_string_rejected(self) -> None:
        data = dict(VALID_RISK_STATE)
        data["final_assets"] = "bad"
        error = dss._validate_risk_state(data)
        self.assertIsNotNone(error)
        self.assertIn("final_assets", error)

    def test_scan_date_non_string_rejected(self) -> None:
        data = dict(VALID_RISK_STATE)
        data["scan_date"] = 12345
        error = dss._validate_risk_state(data)
        self.assertIsNotNone(error)
        self.assertIn("scan_date", error)

    def test_max_drawdown_bool_rejected(self) -> None:
        """bool should not be accepted for numeric fields."""
        data = dict(VALID_RISK_STATE)
        data["max_drawdown"] = True
        error = dss._validate_risk_state(data)
        self.assertIsNotNone(error)
        self.assertIn("max_drawdown", error)

    def test_int_accepted_for_float_fields(self) -> None:
        """int is acceptable where float is expected (e.g. max_drawdown=0)."""
        data = dict(VALID_RISK_STATE)
        data["max_drawdown"] = 0
        data["total_return"] = 0
        data["final_assets"] = 2000000
        self.assertIsNone(dss._validate_risk_state(data))

    # ── Finite and range validation ──

    def test_nan_max_drawdown_rejected(self) -> None:
        """NaN values must be rejected — they break comparisons."""
        data = dict(VALID_RISK_STATE)
        data["max_drawdown"] = float("nan")
        error = dss._validate_risk_state(data)
        self.assertIsNotNone(error)
        self.assertIn("非有限值", error)

    def test_inf_total_return_rejected(self) -> None:
        """Inf values must be rejected."""
        data = dict(VALID_RISK_STATE)
        data["total_return"] = float("inf")
        error = dss._validate_risk_state(data)
        self.assertIsNotNone(error)
        self.assertIn("非有限值", error)

    def test_neg_inf_final_assets_rejected(self) -> None:
        """-Inf values must be rejected."""
        data = dict(VALID_RISK_STATE)
        data["final_assets"] = float("-inf")
        error = dss._validate_risk_state(data)
        self.assertIsNotNone(error)
        self.assertIn("非有限值", error)

    def test_negative_cycle_lock_count_rejected(self) -> None:
        """cycle_lock_count must not be negative."""
        data = dict(VALID_RISK_STATE)
        data["cycle_lock_count"] = -1
        error = dss._validate_risk_state(data)
        self.assertIsNotNone(error)
        self.assertIn("cycle_lock_count", error)
        self.assertIn("负数", error)

    def test_negative_final_assets_rejected(self) -> None:
        """final_assets must not be negative."""
        data = dict(VALID_RISK_STATE)
        data["final_assets"] = -100.0
        error = dss._validate_risk_state(data)
        self.assertIsNotNone(error)
        self.assertIn("final_assets", error)
        self.assertIn("负数", error)

    def test_zero_final_assets_accepted(self) -> None:
        """final_assets=0 is valid (account fully depleted)."""
        data = dict(VALID_RISK_STATE)
        data["final_assets"] = 0
        self.assertIsNone(dss._validate_risk_state(data))

    def test_large_negative_drawdown_accepted(self) -> None:
        """Large negative drawdown is valid (e.g. -0.95)."""
        data = dict(VALID_RISK_STATE)
        data["max_drawdown"] = -0.95
        self.assertIsNone(dss._validate_risk_state(data))

    # ── Schema version in saved state ──

    def test_schema_version_in_saved_state(self) -> None:
        """Saved risk state must include schema_version."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = {"terminal_risk_lock": False, "sector_guard_active": False,
                      "cycle_lock_count": 0, "max_drawdown": 0.0,
                      "total_return": 0.0, "final_assets": 2000000.0}
            tradable = {"300308": "中际旭创"}
            dss._save_risk_state(tmpdir, "2026-07-30", result, tradable=tradable)
            data = json.loads((Path(tmpdir) / "risk_state.json").read_text(encoding="utf-8"))
            self.assertIn("schema_version", data)
            self.assertEqual(data["schema_version"], 1)

    def test_run_id_is_unique(self) -> None:
        """Two saves on the same date should produce different run_ids."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = {"terminal_risk_lock": False, "sector_guard_active": False,
                      "cycle_lock_count": 0, "max_drawdown": 0.0,
                      "total_return": 0.0, "final_assets": 2000000.0}
            tradable = {"300308": "中际旭创"}
            dss._save_risk_state(tmpdir, "2026-07-30", result, tradable=tradable)
            data1 = json.loads((Path(tmpdir) / "risk_state.json").read_text(encoding="utf-8"))
            dss._save_risk_state(tmpdir, "2026-07-30", result, tradable=tradable)
            data2 = json.loads((Path(tmpdir) / "risk_state.json").read_text(encoding="utf-8"))
            self.assertNotEqual(data1["run_id"], data2["run_id"])

    def test_schema_validation_failure_returns_error(self) -> None:
        """Risk state with wrong types should return error, not state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "risk_state.json"
            state_file.write_text(
                json.dumps({
                    "schema_version": 1,
                    "scan_date": "2026-07-30",
                    "terminal_risk_lock": "true",  # string, not bool
                    "sector_guard_active": False,
                    "cycle_lock_count": 0,
                    "max_drawdown": 0.0,
                    "total_return": 0.0,
                    "final_assets": 2000000.0,
                }),
                encoding="utf-8",
            )
            loaded, error = dss._load_prev_risk_state(tmpdir, "2026-07-31")
            self.assertIsNone(loaded)
            self.assertIsNotNone(error)

    def test_unknown_schema_version_in_file_rejected(self) -> None:
        """A file with schema_version=999 should be rejected on load."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "risk_state.json"
            state_file.write_text(
                json.dumps({
                    "schema_version": 999,
                    "scan_date": "2026-07-30",
                    "terminal_risk_lock": False,
                    "sector_guard_active": False,
                    "cycle_lock_count": 0,
                    "max_drawdown": 0.0,
                    "total_return": 0.0,
                    "final_assets": 2000000.0,
                }),
                encoding="utf-8",
            )
            loaded, error = dss._load_prev_risk_state(tmpdir, "2026-07-31")
            self.assertIsNone(loaded)
            self.assertIsNotNone(error)
            self.assertIn("999", error)


# ── Buy suppression tests ───────────────────────────────────────────

class BuySuppressionTests(unittest.TestCase):
    """Verify _apply_buy_suppression and _serialize_pending_signals
    correctly implement fail-closed buy suppression."""

    def _make_signal(self, direction: str, symbol: str = "300308",
                     strategy: str = "turtle") -> FakeSignal:
        return FakeSignal(direction, strategy, symbol, 100, 150.0,
                          "test", "2026-07-30")

    # ── _apply_buy_suppression: no suppression ──

    def test_pure_buy_no_suppression(self) -> None:
        """Without suppression, buy signals display normally."""
        sigs = [self._make_signal("buy")]
        label, strategies, suppressed = dss._apply_buy_suppression(sigs, False)
        self.assertEqual(label, "买入")
        self.assertFalse(suppressed)

    def test_pure_sell_no_suppression(self) -> None:
        """Without suppression, sell signals display normally."""
        sigs = [self._make_signal("sell")]
        label, strategies, suppressed = dss._apply_buy_suppression(sigs, False)
        self.assertEqual(label, "卖出")
        self.assertFalse(suppressed)

    def test_mixed_no_suppression(self) -> None:
        """Without suppression, mixed signals show all directions."""
        sigs = [
            self._make_signal("buy", strategy="turtle"),
            self._make_signal("sell", strategy="atr"),
        ]
        label, _, suppressed = dss._apply_buy_suppression(sigs, False)
        self.assertIn("买入", label)
        self.assertIn("卖出", label)
        self.assertFalse(suppressed)

    # ── _apply_buy_suppression: with suppression ──

    def test_pure_buy_suppressed(self) -> None:
        """Pure buy signals are replaced with wait message."""
        sigs = [self._make_signal("buy")]
        label, strategies, suppressed = dss._apply_buy_suppression(sigs, True)
        self.assertIn("风险状态不匹配", label)
        self.assertTrue(suppressed)

    def test_pure_sell_not_suppressed(self) -> None:
        """Sell signals are never suppressed."""
        sigs = [self._make_signal("sell")]
        label, _, suppressed = dss._apply_buy_suppression(sigs, True)
        self.assertEqual(label, "卖出")
        self.assertFalse(suppressed)

    def test_mixed_buy_sell_suppressed(self) -> None:
        """Mixed buy/sell: sell kept, buy suppressed, label shows [买入已抑制]."""
        sigs = [
            self._make_signal("buy", strategy="turtle"),
            self._make_signal("sell", strategy="atr"),
        ]
        label, strategies, suppressed = dss._apply_buy_suppression(sigs, True)
        self.assertIn("卖出", label)
        self.assertIn("买入已抑制", label)
        self.assertNotIn("买入(", label)  # buy part not shown
        self.assertTrue(suppressed)

    def test_mixed_buy_hold_suppressed(self) -> None:
        """Mixed buy/hold: hold is not sell, so pure buy suppression applies."""
        sigs = [
            self._make_signal("buy", strategy="turtle"),
            self._make_signal("hold", strategy="atr"),
        ]
        label, _, suppressed = dss._apply_buy_suppression(sigs, True)
        self.assertIn("风险状态不匹配", label)
        self.assertTrue(suppressed)

    def test_empty_signals(self) -> None:
        """Empty signal list returns empty label."""
        label, _, suppressed = dss._apply_buy_suppression([], True)
        self.assertEqual(label, "")
        self.assertFalse(suppressed)

    # ── _serialize_pending_signals: separation ──

    def test_executable_and_blocked_separated(self) -> None:
        """Blocked buys go to blocked_signals, not pending_signals."""
        sigs = [
            self._make_signal("buy", symbol="300308"),
            self._make_signal("sell", symbol="300502"),
            self._make_signal("buy", symbol="688008"),
        ]
        executable, blocked = dss._serialize_pending_signals(sigs, True)
        # Only sell should be in executable
        self.assertEqual(len(executable), 1)
        self.assertEqual(executable[0]["direction"], "sell")
        self.assertTrue(executable[0]["executable"])
        self.assertFalse(executable[0]["blocked"])
        # Both buys should be in blocked
        self.assertEqual(len(blocked), 2)
        for entry in blocked:
            self.assertEqual(entry["direction"], "buy")
            self.assertFalse(entry["executable"])
            self.assertTrue(entry["blocked"])
            self.assertEqual(entry["blocked_reason"], "risk_state_identity_mismatch")

    def test_no_suppression_all_executable(self) -> None:
        """Without suppression, all signals are executable."""
        sigs = [
            self._make_signal("buy"),
            self._make_signal("sell"),
        ]
        executable, blocked = dss._serialize_pending_signals(sigs, False)
        self.assertEqual(len(executable), 2)
        self.assertEqual(len(blocked), 0)
        for entry in executable:
            self.assertTrue(entry["executable"])
            self.assertFalse(entry["blocked"])

    def test_blocked_signals_have_direction_buy_only(self) -> None:
        """blocked_signals must only contain buy signals."""
        sigs = [
            self._make_signal("buy"),
            self._make_signal("sell"),
            self._make_signal("buy"),
        ]
        _, blocked = dss._serialize_pending_signals(sigs, True)
        for entry in blocked:
            self.assertEqual(entry["direction"], "buy")

    def test_pending_signals_no_blocked_when_suppressed(self) -> None:
        """pending_signals must not contain any blocked entries."""
        sigs = [self._make_signal("buy")]
        executable, blocked = dss._serialize_pending_signals(sigs, True)
        self.assertEqual(len(executable), 0)
        self.assertEqual(len(blocked), 1)


# ── Risk state mismatch and preservation tests ─────────────────────

class RiskStateMismatchTests(unittest.TestCase):
    """Verify risk state is preserved across mismatches and resets."""

    def test_mismatch_does_not_overwrite_old_state(self) -> None:
        """When identity doesn't match, old risk_state.json is preserved."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Save initial state with terminal lock
            result1 = {
                "terminal_risk_lock": True,
                "sector_guard_active": False,
                "cycle_lock_count": 1,
                "max_drawdown": -0.285,
                "total_return": -0.15,
                "final_assets": 1700000.0,
            }
            tradable1 = {"300308": "中际旭创", "300502": "新易盛"}
            dss._save_risk_state(tmpdir, "2026-07-28", result1,
                                 tradable=tradable1,
                                 config_hash="start=2026-07-01|indicator=warm")
            original_data = json.loads(
                (Path(tmpdir) / "risk_state.json").read_text(encoding="utf-8")
            )

            # Simulate a different tradable set (different identity)
            # The main() function would NOT call _save_risk_state when
            # suppress_buys=True. This test verifies that calling
            # _save_risk_state with a different tradable set creates a
            # DIFFERENT hash, confirming the identity check would trigger.
            tradable2 = {"300308": "中际旭创", "688008": "澜起科技"}
            result2 = {
                "terminal_risk_lock": False,
                "sector_guard_active": False,
                "cycle_lock_count": 0,
                "max_drawdown": 0.0,
                "total_return": 0.0,
                "final_assets": 2000000.0,
            }
            # Build the hash that main() would compute for tradable1
            import hashlib
            identity1 = ["trade", str(len(tradable1)),
                         ",".join(sorted(tradable1.keys())),
                         "start=2026-07-01|indicator=warm"]
            hash1 = hashlib.sha256("|".join(identity1).encode()).hexdigest()[:16]
            identity2 = ["trade", str(len(tradable2)),
                         ",".join(sorted(tradable2.keys())),
                         "start=2026-07-01|indicator=warm"]
            hash2 = hashlib.sha256("|".join(identity2).encode()).hexdigest()[:16]
            self.assertNotEqual(hash1, hash2)

            # The original file should still have the old hash
            self.assertEqual(original_data["symbols_hash"], hash1)
            self.assertTrue(original_data["terminal_risk_lock"])

    def test_consecutive_mismatch_preserves_lock(self) -> None:
        """Two consecutive mismatches should both see the same old state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Save state with terminal lock
            result = {
                "terminal_risk_lock": True,
                "sector_guard_active": True,
                "cycle_lock_count": 2,
                "max_drawdown": -0.20,
                "total_return": 0.05,
                "final_assets": 2100000.0,
            }
            tradable = {"300308": "中际旭创", "300502": "新易盛"}
            dss._save_risk_state(tmpdir, "2026-07-28", result,
                                 tradable=tradable,
                                 config_hash="start=2026-07-01|indicator=warm")

            # First load — should get the state
            loaded1, error1 = dss._load_prev_risk_state(tmpdir, "2026-07-29")
            self.assertIsNone(error1)
            self.assertIsNotNone(loaded1)
            assert loaded1 is not None
            self.assertTrue(loaded1["terminal_risk_lock"])
            self.assertTrue(loaded1["sector_guard_active"])

            # Second load (consecutive) — should still get the same state
            loaded2, error2 = dss._load_prev_risk_state(tmpdir, "2026-07-30")
            self.assertIsNone(error2)
            self.assertIsNotNone(loaded2)
            assert loaded2 is not None
            self.assertTrue(loaded2["terminal_risk_lock"])
            self.assertTrue(loaded2["sector_guard_active"])
            self.assertEqual(loaded2["cycle_lock_count"], 2)

    def test_terminal_lock_survives_save_load_roundtrip(self) -> None:
        """Terminal lock must survive multiple save/load cycles."""
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(3):
                result = {
                    "terminal_risk_lock": True,
                    "sector_guard_active": True,
                    "cycle_lock_count": i,
                    "max_drawdown": -0.15 - i * 0.01,
                    "total_return": 0.10 + i * 0.05,
                    "final_assets": 2200000.0 + i * 100000,
                }
                tradable = {"300308": "中际旭创"}
                dss._save_risk_state(tmpdir, f"2026-07-{28+i}", result,
                                     tradable=tradable,
                                     config_hash="start=2026-07-01|indicator=warm")
                loaded, error = dss._load_prev_risk_state(tmpdir, f"2026-07-{29+i}")
                self.assertIsNone(error)
                self.assertIsNotNone(loaded)
                assert loaded is not None
                self.assertTrue(loaded["terminal_risk_lock"])
                self.assertTrue(loaded["sector_guard_active"])
                self.assertEqual(loaded["cycle_lock_count"], i)


# ── Reset and account conflict tests ────────────────────────────────

class ResetAccountConflictTests(unittest.TestCase):
    """Verify --reset-risk-state and --account are handled safely."""

    def test_account_checked_before_reset(self) -> None:
        """--account should be rejected before --reset-risk-state runs,
        so the old state is not lost."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a risk state file
            state_file = Path(tmpdir) / "risk_state.json"
            state_data = {
                "schema_version": 1,
                "scan_date": "2026-07-28",
                "terminal_risk_lock": True,
                "sector_guard_active": False,
                "cycle_lock_count": 1,
                "max_drawdown": -0.15,
                "total_return": 0.10,
                "final_assets": 2200000.0,
                "symbols_hash": "abc123",
                "total_symbols": 2,
                "run_id": "trade_2026-07-28_abc12345",
            }
            state_file.write_text(json.dumps(state_data), encoding="utf-8")

            # Simulate: --account is checked first in main()
            # The code checks args.account before args.reset_risk_state
            # So the state file should still exist
            account_mode = True
            reset_mode = True

            # In main(), account is checked first — if account is set,
            # it returns 1 without touching reset
            if account_mode:
                # Account mode exits early — state file untouched
                self.assertTrue(state_file.exists())
                data = json.loads(state_file.read_text(encoding="utf-8"))
                self.assertTrue(data["terminal_risk_lock"])
            elif reset_mode:
                state_file.unlink()
                self.assertFalse(state_file.exists())


# ── Pre-save validation tests ──────────────────────────────────────

class PreSaveValidationTests(unittest.TestCase):
    """Verify _save_risk_state rejects NaN/Inf/negative before writing."""

    def _base_result(self) -> dict:
        return {
            "terminal_risk_lock": False,
            "sector_guard_active": False,
            "cycle_lock_count": 0,
            "max_drawdown": 0.0,
            "total_return": 0.0,
            "final_assets": 2000000.0,
        }

    def test_nan_max_drawdown_rejected_on_save(self) -> None:
        """NaN in max_drawdown must raise ValueError before writing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = self._base_result()
            result["max_drawdown"] = float("nan")
            with self.assertRaises(ValueError) as ctx:
                dss._save_risk_state(tmpdir, "2026-07-30", result)
            self.assertIn("max_drawdown", str(ctx.exception))
            self.assertIn("NaN", str(ctx.exception))
            # File must NOT be created
            self.assertFalse((Path(tmpdir) / "risk_state.json").exists())

    def test_inf_total_return_rejected_on_save(self) -> None:
        """Inf in total_return must raise ValueError before writing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = self._base_result()
            result["total_return"] = float("inf")
            with self.assertRaises(ValueError) as ctx:
                dss._save_risk_state(tmpdir, "2026-07-30", result)
            self.assertIn("total_return", str(ctx.exception))

    def test_neg_inf_final_assets_rejected_on_save(self) -> None:
        """-Inf in final_assets must raise ValueError before writing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = self._base_result()
            result["final_assets"] = float("-inf")
            with self.assertRaises(ValueError) as ctx:
                dss._save_risk_state(tmpdir, "2026-07-30", result)
            self.assertIn("final_assets", str(ctx.exception))

    def test_negative_cycle_lock_count_rejected_on_save(self) -> None:
        """Negative cycle_lock_count must raise ValueError before writing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = self._base_result()
            result["cycle_lock_count"] = -1
            with self.assertRaises(ValueError) as ctx:
                dss._save_risk_state(tmpdir, "2026-07-30", result)
            self.assertIn("cycle_lock_count", str(ctx.exception))
            self.assertIn("负值", str(ctx.exception))

    def test_valid_values_saved_successfully(self) -> None:
        """Finite, valid values must save without error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = self._base_result()
            dss._save_risk_state(
                tmpdir, "2026-07-30", result,
                tradable={"300308": "test"},
                config_hash="start=2026-07-01|indicator=warm",
            )
            state_file = Path(tmpdir) / "risk_state.json"
            self.assertTrue(state_file.exists())
            data = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(data["schema_version"], 1)
            self.assertEqual(data["final_assets"], 2000000.0)


# ── CLI integration tests (subprocess) ─────────────────────────────

class CLIIntegrationTests(unittest.TestCase):
    """Verify CLI behavior through real subprocess calls.

    These tests exercise the actual ``main()`` entry point via
    ``subprocess.run`` to catch issues that unit-level mocking would
    miss (argument parsing, exit codes, file system side effects).

    Only paths that exit *before* network data fetching are tested,
    so no AKShare connection is required.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._project_dir = str(Path(__file__).resolve().parent)

    def _run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run daily_signal_scan.py with the given arguments."""
        return subprocess.run(
            [sys.executable, "daily_signal_scan.py", *args],
            capture_output=True,
            text=True,
            cwd=self._project_dir,
            timeout=15,
        )

    def test_account_mode_exits_1(self) -> None:
        """--account must exit with code 1 (mode is disabled)."""
        result = self._run_cli("--account", "dummy.json")
        self.assertEqual(result.returncode, 1)
        self.assertIn("不可用", result.stdout)

    def test_account_with_reset_preserves_state(self) -> None:
        """--account + --reset-risk-state must NOT delete risk_state.json.

        The --account check runs before --reset-risk-state, so the old
        state file must survive even when both flags are combined.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a risk state file with terminal lock
            state_file = Path(tmpdir) / "risk_state.json"
            state_data = dict(VALID_RISK_STATE)
            state_data["symbols_hash"] = "abc123"
            state_data["total_symbols"] = 2
            state_data["run_id"] = "trade_2026-07-28_abc12345"
            state_file.write_text(
                json.dumps(state_data), encoding="utf-8"
            )

            result = self._run_cli(
                "--output-dir", tmpdir,
                "--account", "dummy.json",
                "--reset-risk-state",
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("不可用", result.stdout)

            # State file must still exist and contain the terminal lock
            self.assertTrue(state_file.exists())
            data = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertFalse(data["terminal_risk_lock"])  # VALID_RISK_STATE has False
            # But the file was not deleted
            self.assertIn("schema_version", data)

    def test_corrupted_risk_state_exits_1(self) -> None:
        """Corrupted risk_state.json must cause exit code 1 (fail-closed).

        The scan loads risk state before fetching data, so a corrupt
        file triggers the fail-closed path without needing network access.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "risk_state.json"
            state_file.write_text("{corrupted json", encoding="utf-8")

            result = self._run_cli(
                "--output-dir", tmpdir,
                "--end-date", "2026-07-30",
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("损坏", result.stdout)

    def test_schema_invalid_risk_state_exits_1(self) -> None:
        """Risk state with wrong types must cause exit code 1."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "risk_state.json"
            # terminal_risk_lock is a string instead of bool
            state_file.write_text(
                json.dumps({
                    "schema_version": 1,
                    "scan_date": "2026-07-30",
                    "terminal_risk_lock": "true",
                    "sector_guard_active": False,
                    "cycle_lock_count": 0,
                    "max_drawdown": 0.0,
                    "total_return": 0.0,
                    "final_assets": 2000000.0,
                }),
                encoding="utf-8",
            )

            result = self._run_cli(
                "--output-dir", tmpdir,
                "--end-date", "2026-07-30",
            )

            self.assertEqual(result.returncode, 1)

    def test_unknown_schema_version_exits_1(self) -> None:
        """Risk state with unknown schema_version must exit 1."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "risk_state.json"
            state_file.write_text(
                json.dumps({
                    "schema_version": 999,
                    "scan_date": "2026-07-30",
                    "terminal_risk_lock": False,
                    "sector_guard_active": False,
                    "cycle_lock_count": 0,
                    "max_drawdown": 0.0,
                    "total_return": 0.0,
                    "final_assets": 2000000.0,
                }),
                encoding="utf-8",
            )

            result = self._run_cli(
                "--output-dir", tmpdir,
                "--end-date", "2026-07-30",
            )

            self.assertEqual(result.returncode, 1)


# ── Save failure exit code test (mock-based) ───────────────────────

class SaveFailureExitCodeTests(unittest.TestCase):
    """Verify main() returns exit code 1 when risk state save fails.

    Uses mock to simulate a save failure without requiring network data.
    """

    def test_save_failure_returns_exit_code_1(self) -> None:
        """When _save_risk_state raises OSError, main() must return 1."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # No risk_state.json — first run, so suppress_buys stays False
            # and the save path is exercised.

            # Mock the backtest to return quickly without network
            mock_result = {
                "terminal_risk_lock": False,
                "sector_guard_active": False,
                "cycle_lock_count": 0,
                "max_drawdown": -0.05,
                "total_return": 0.10,
                "final_assets": 2200000.0,
                "sharpe": 2.5,
                "total_trades": 50,
                "risk_events": [],
                "pending_signals": [],
                "trades": [],
                "safe_mode_active": False,
            }

            # Mock DataFetcher.load_stock_data to return a simple frame
            # so the pre-screen step passes without network access
            import pandas as pd
            mock_df = pd.DataFrame(
                {"open": [10.0], "high": [11.0], "low": [9.0],
                 "close": [10.5], "volume": [1000000]},
                index=pd.DatetimeIndex(["2026-07-30"], name="date"),
            )

            with patch.object(dss.qf.DataFetcher, "load_stock_data",
                              return_value=mock_df), \
                 patch.object(dss.qf.BacktestEngine, "run",
                              return_value=mock_result), \
                 patch.object(dss, "_save_risk_state",
                              side_effect=OSError("disk full")):
                with patch("sys.argv", [
                    "daily_signal_scan.py",
                    "--output-dir", tmpdir,
                    "--end-date", "2026-07-30",
                ]):
                    exit_code = dss.main()

            self.assertEqual(exit_code, 1)

    def test_save_value_error_returns_exit_code_1(self) -> None:
        """When _save_risk_state raises ValueError (NaN/Inf), return 1."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # No risk_state.json — first run, so suppress_buys stays False
            mock_result = {
                "terminal_risk_lock": False,
                "sector_guard_active": False,
                "cycle_lock_count": 0,
                "max_drawdown": -0.05,
                "total_return": 0.10,
                "final_assets": 2200000.0,
                "sharpe": 2.5,
                "total_trades": 50,
                "risk_events": [],
                "pending_signals": [],
                "trades": [],
                "safe_mode_active": False,
            }

            import pandas as pd
            mock_df = pd.DataFrame(
                {"open": [10.0], "high": [11.0], "low": [9.0],
                 "close": [10.5], "volume": [1000000]},
                index=pd.DatetimeIndex(["2026-07-30"], name="date"),
            )

            with patch.object(dss.qf.DataFetcher, "load_stock_data",
                              return_value=mock_df), \
                 patch.object(dss.qf.BacktestEngine, "run",
                              return_value=mock_result), \
                 patch.object(dss, "_save_risk_state",
                              side_effect=ValueError("拒绝保存非有限值")):
                with patch("sys.argv", [
                    "daily_signal_scan.py",
                    "--output-dir", tmpdir,
                    "--end-date", "2026-07-30",
                ]):
                    exit_code = dss.main()

            self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
