#!/usr/bin/env python3
"""日扫输入、结果与严格 JSON schema 契约。"""

from __future__ import annotations

# ruff: noqa: F401

from ._daily_scan_support import (
    FakeSignal,
    FakeTrade,
    Path,
    VALID_ACCOUNT,
    VALID_RISK_STATE,
    dss,
    json,
    os,
    patch,
    subprocess,
    sys,
    tempfile,
    unittest,
)


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

    # ── Semantic range and format validation ──

    def test_total_return_below_negative_one_rejected(self) -> None:
        """total_return < -1.0 is impossible (cannot lose more than 100%)."""
        data = dict(VALID_RISK_STATE)
        data["total_return"] = -1.5
        error = dss._validate_risk_state(data)
        self.assertIsNotNone(error)
        self.assertIn("total_return", error)

    def test_total_return_exactly_negative_one_accepted(self) -> None:
        """total_return = -1.0 (total loss) is valid."""
        data = dict(VALID_RISK_STATE)
        data["total_return"] = -1.0
        self.assertIsNone(dss._validate_risk_state(data))

    def test_invalid_scan_date_rejected(self) -> None:
        """scan_date must be a valid YYYY-MM-DD date."""
        data = dict(VALID_RISK_STATE)
        data["scan_date"] = "2026-13-45"
        error = dss._validate_risk_state(data)
        self.assertIsNotNone(error)
        self.assertIn("scan_date", error)

    def test_scan_date_wrong_format_rejected(self) -> None:
        """scan_date must be YYYY-MM-DD, not DD/MM/YYYY."""
        data = dict(VALID_RISK_STATE)
        data["scan_date"] = "30/07/2026"
        error = dss._validate_risk_state(data)
        self.assertIsNotNone(error)
        self.assertIn("scan_date", error)

    def test_symbols_hash_wrong_length_rejected(self) -> None:
        """symbols_hash must be exactly 16 hex chars."""
        data = dict(VALID_RISK_STATE)
        data["symbols_hash"] = "abc123"
        error = dss._validate_risk_state(data)
        self.assertIsNotNone(error)
        self.assertIn("symbols_hash", error)

    def test_symbols_hash_non_hex_rejected(self) -> None:
        """symbols_hash must be valid hex."""
        data = dict(VALID_RISK_STATE)
        data["symbols_hash"] = "zzzzzzzzzzzzzzzz"
        error = dss._validate_risk_state(data)
        self.assertIsNotNone(error)
        self.assertIn("symbols_hash", error)

    def test_symbols_hash_valid_hex_accepted(self) -> None:
        """Valid 16-char hex symbols_hash is accepted."""
        data = dict(VALID_RISK_STATE)
        data["symbols_hash"] = "abc123def456abcd"
        data["total_symbols"] = 5
        self.assertIsNone(dss._validate_risk_state(data))

    def test_total_symbols_negative_rejected(self) -> None:
        """total_symbols must be non-negative."""
        data = dict(VALID_RISK_STATE)
        data["symbols_hash"] = "abc123def456abcd"
        data["total_symbols"] = -1
        error = dss._validate_risk_state(data)
        self.assertIsNotNone(error)
        self.assertIn("total_symbols", error)

    def test_saved_state_passes_full_validation(self) -> None:
        """A state saved by _save_risk_state must pass _validate_risk_state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = {
                "terminal_risk_lock": True,
                "sector_guard_active": True,
                "cycle_lock_count": 2,
                "max_drawdown": -0.1825,
                "total_return": 0.4532,
                "final_assets": 2906400.0,
            }
            tradable = {"300308": "中际旭创", "300502": "新易盛"}
            dss._save_risk_state(tmpdir, "2026-07-30", result,
                                 tradable=tradable,
                                 config_hash="start=2026-07-01|indicator=warm")
            loaded, error = dss._load_prev_risk_state(tmpdir, "2026-07-31")
            self.assertIsNone(error)
            self.assertIsNotNone(loaded)

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


class ArtifactStrictJSONTests(unittest.TestCase):
    """Verify artifact is strict JSON (ECMA-404) even with NaN/Inf results.

    Python's json.dumps() writes NaN/Infinity as non-standard tokens by
    default. The code must use allow_nan=False and produce an error-only
    artifact (in a separate .error.json file) when the result is invalid.
    The last successful artifact (signals_<date>.json) is never overwritten
    by an error artifact.
    """

    def _make_mock_result(self, **overrides) -> dict:
        base = {
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
        base.update(overrides)
        return base

    def _run_main_with_mock(self, tmpdir: str, mock_result: dict) -> int:
        """Run main() with mocked data and return exit code."""
        import pandas as pd
        mock_df = pd.DataFrame(
            {"open": [10.0], "high": [11.0], "low": [9.0],
             "close": [10.5], "volume": [1000000]},
            index=pd.DatetimeIndex(["2026-07-30"], name="date"),
        )
        with patch.object(dss.qf.DataFetcher, "load_stock_data",
                          return_value=mock_df), \
             patch.object(dss.ra.RegimeAdaptiveBacktestEngine, "run",
                          return_value=mock_result):
            with patch("sys.argv", [
                "daily_signal_scan.py",
                "--output-dir", tmpdir,
                "--end-date", "2026-07-30",
            ]):
                return dss.main()

    def test_nan_result_produces_error_artifact(self) -> None:
        """NaN in result must produce error artifact in .error.json, not .json."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_result = self._make_mock_result(
                max_drawdown=float("nan"),
            )
            exit_code = self._run_main_with_mock(tmpdir, mock_result)
            self.assertEqual(exit_code, 1)

            # Error artifact goes to .error.json — success file must NOT exist
            error_file = Path(tmpdir) / "signals_2026-07-30.error.json"
            success_file = Path(tmpdir) / "signals_2026-07-30.json"
            self.assertTrue(error_file.exists())
            self.assertFalse(success_file.exists())

            content = error_file.read_text(encoding="utf-8")
            data = json.loads(content)  # must parse as strict JSON
            self.assertEqual(data["status"], "error")
            self.assertIn("invalid_fields", data)
            self.assertFalse(data["risk_state_saved"])
            self.assertIn("run_id", data)

    def test_inf_result_produces_error_artifact(self) -> None:
        """Inf in result must produce error artifact in .error.json."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_result = self._make_mock_result(
                total_return=float("inf"),
            )
            exit_code = self._run_main_with_mock(tmpdir, mock_result)
            self.assertEqual(exit_code, 1)

            error_file = Path(tmpdir) / "signals_2026-07-30.error.json"
            success_file = Path(tmpdir) / "signals_2026-07-30.json"
            self.assertTrue(error_file.exists())
            self.assertFalse(success_file.exists())

            content = error_file.read_text(encoding="utf-8")
            data = json.loads(content)
            self.assertEqual(data["status"], "error")

    def test_nan_sharpe_produces_error_artifact(self) -> None:
        """NaN in sharpe must also trigger error artifact in .error.json."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_result = self._make_mock_result(
                sharpe=float("nan"),
            )
            exit_code = self._run_main_with_mock(tmpdir, mock_result)
            self.assertEqual(exit_code, 1)

            error_file = Path(tmpdir) / "signals_2026-07-30.error.json"
            success_file = Path(tmpdir) / "signals_2026-07-30.json"
            self.assertTrue(error_file.exists())
            self.assertFalse(success_file.exists())

            content = error_file.read_text(encoding="utf-8")
            data = json.loads(content)
            self.assertEqual(data["status"], "error")

    def test_valid_result_produces_ok_artifact(self) -> None:
        """Valid result must produce normal artifact with status=ok."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_result = self._make_mock_result()
            exit_code = self._run_main_with_mock(tmpdir, mock_result)
            self.assertEqual(exit_code, 0)

            artifact_file = Path(tmpdir) / "signals_2026-07-30.json"
            content = artifact_file.read_text(encoding="utf-8")
            data = json.loads(content)
            self.assertEqual(data["status"], "ok")
            self.assertIn("pending_signals", data)
            self.assertIn("portfolio", data)

    def test_artifact_is_strict_json_no_nan_tokens(self) -> None:
        """Error artifact file must never contain NaN/Infinity as JSON values.

        We verify by parsing with a strict constant handler that rejects
        NaN/Infinity tokens. String fields (like error messages) may contain
        the word "NaN" in prose — that's valid JSON.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_result = self._make_mock_result(
                max_drawdown=float("nan"),
                total_return=float("inf"),
            )
            self._run_main_with_mock(tmpdir, mock_result)

            # Error artifact goes to .error.json
            error_file = Path(tmpdir) / "signals_2026-07-30.error.json"
            content = error_file.read_text(encoding="utf-8")

            # Parse with a strict constant handler — this rejects
            # NaN, Infinity, -Infinity as JSON values (not in strings).
            def reject_constants(s):
                raise ValueError(f"Non-standard JSON token: {s}")
            json.loads(content, parse_constant=reject_constants)

    def test_risk_state_file_is_strict_json(self) -> None:
        """Saved risk_state.json must be strict JSON (no NaN tokens)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = {
                "terminal_risk_lock": False,
                "sector_guard_active": False,
                "cycle_lock_count": 0,
                "max_drawdown": -0.12,
                "total_return": 0.08,
                "final_assets": 2160000.0,
            }
            dss._save_risk_state(tmpdir, "2026-07-30", result,
                                 tradable={"300308": "test"})
            state_content = (Path(tmpdir) / "risk_state.json").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("NaN", state_content)
            self.assertNotIn("Infinity", state_content)
            # Must parse as strict JSON
            json.loads(state_content)


class StrictResultValidationTests(unittest.TestCase):
    """Verify _validate_result_fields uses strict type checking —
    strings (even float-convertible), bool, None, and non-dict results
    are all rejected.
    """

    def test_float_convertible_string_rejected(self) -> None:
        """String '1.23' must be rejected even though float('1.23') works."""
        result = {
            "final_assets": 2200000.0,
            "total_return": "1.23",  # float-convertible string
            "max_drawdown": -0.05,
            "sharpe": 2.5,
            "total_trades": 50,
        }
        errors = dss._validate_result_fields(result)
        self.assertTrue(any("total_return" in e and "str" in e for e in errors),
                        f"Expected str rejection for total_return, got: {errors}")

    def test_bool_for_numeric_field_rejected(self) -> None:
        """bool must be rejected for numeric fields."""
        result = {
            "final_assets": True,  # bool, not a number
            "total_return": 0.10,
            "max_drawdown": -0.05,
            "sharpe": 2.5,
            "total_trades": 50,
        }
        errors = dss._validate_result_fields(result)
        self.assertTrue(any("final_assets" in e and "bool" in e for e in errors),
                        f"Expected bool rejection for final_assets, got: {errors}")

    def test_none_result_rejected(self) -> None:
        """None result must return errors, not crash."""
        errors = dss._validate_result_fields(None)
        self.assertTrue(len(errors) > 0)
        self.assertIn("dict", errors[0])

    def test_list_result_rejected(self) -> None:
        """List result must return errors, not crash."""
        errors = dss._validate_result_fields([1, 2, 3])
        self.assertTrue(len(errors) > 0)
        self.assertIn("dict", errors[0])

    def test_total_trades_missing_rejected(self) -> None:
        """Missing total_trades must be flagged."""
        result = {
            "final_assets": 2200000.0,
            "total_return": 0.10,
            "max_drawdown": -0.05,
            "sharpe": 2.5,
            # total_trades missing
        }
        errors = dss._validate_result_fields(result)
        self.assertTrue(any("total_trades" in e for e in errors),
                        f"Expected total_trades missing error, got: {errors}")

    def test_total_trades_string_rejected(self) -> None:
        """String total_trades must be rejected."""
        result = {
            "final_assets": 2200000.0,
            "total_return": 0.10,
            "max_drawdown": -0.05,
            "sharpe": 2.5,
            "total_trades": "50",
        }
        errors = dss._validate_result_fields(result)
        self.assertTrue(any("total_trades" in e and "str" in e for e in errors),
                        f"Expected str rejection for total_trades, got: {errors}")

    def test_total_trades_bool_rejected(self) -> None:
        """bool total_trades must be rejected."""
        result = {
            "final_assets": 2200000.0,
            "total_return": 0.10,
            "max_drawdown": -0.05,
            "sharpe": 2.5,
            "total_trades": True,
        }
        errors = dss._validate_result_fields(result)
        self.assertTrue(any("total_trades" in e and "bool" in e for e in errors),
                        f"Expected bool rejection for total_trades, got: {errors}")

    def test_total_trades_negative_rejected(self) -> None:
        """Negative total_trades must be rejected."""
        result = {
            "final_assets": 2200000.0,
            "total_return": 0.10,
            "max_drawdown": -0.05,
            "sharpe": 2.5,
            "total_trades": -5,
        }
        errors = dss._validate_result_fields(result)
        self.assertTrue(any("total_trades" in e and "negative" in e for e in errors),
                        f"Expected negative rejection for total_trades, got: {errors}")

    def test_total_trades_float_rejected(self) -> None:
        """Float total_trades must be rejected (must be int)."""
        result = {
            "final_assets": 2200000.0,
            "total_return": 0.10,
            "max_drawdown": -0.05,
            "sharpe": 2.5,
            "total_trades": 50.0,
        }
        errors = dss._validate_result_fields(result)
        self.assertTrue(any("total_trades" in e for e in errors),
                        f"Expected float rejection for total_trades, got: {errors}")

    def test_valid_result_no_errors(self) -> None:
        """A fully valid result must produce no errors."""
        result = {
            "final_assets": 2200000.0,
            "total_return": 0.10,
            "max_drawdown": -0.05,
            "sharpe": 2.5,
            "total_trades": 50,
        }
        errors = dss._validate_result_fields(result)
        self.assertEqual(errors, [])

    def test_int_accepted_for_float_fields(self) -> None:
        """int must be accepted where float is expected."""
        result = {
            "final_assets": 2200000,  # int, not float
            "total_return": 0,  # int
            "max_drawdown": -5,  # int
            "sharpe": 2,  # int
            "total_trades": 50,
        }
        errors = dss._validate_result_fields(result)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
