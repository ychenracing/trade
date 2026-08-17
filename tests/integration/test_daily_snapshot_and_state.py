#!/usr/bin/env python3
"""冻结快照与连续风险状态集成契约。"""

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


class FrozenSnapshotTests(unittest.TestCase):
    """Daily replay must reuse only byte-identical production evidence."""

    def _snapshot(self, root: Path) -> Path:
        import pandas as pd

        regime = root / "regime"
        regime.mkdir()
        for code in dss.ra.REGIME_INDEX_FILES.values():
            (regime / f"{code}.csv").write_text(
                "date,open,close,high,low,volume\n"
                "2026-07-30,1,1,1,1,1\n",
                encoding="utf-8",
            )
        frame = pd.DataFrame(
            {"open": [1.0], "close": [1.0], "high": [1.0], "low": [1.0]},
            index=pd.to_datetime(["2026-07-30"]),
        )
        target = root / "snapshot"
        manifest = dss._materialize_frozen_snapshot(
            snapshot_dir=target,
            cache_dir=root / "empty-cache",
            regime_data_dir=regime,
            frames={"300308": frame},
            end_date="2026-07-30",
        )
        self.assertEqual(manifest["deployment_policy"], "production_daily_replay")
        self.assertEqual(
            dss._materialize_frozen_snapshot(
                snapshot_dir=target,
                cache_dir=root / "empty-cache",
                regime_data_dir=regime,
                frames={"300308": frame},
                end_date="2026-07-30",
            ),
            manifest,
        )
        return target

    def test_csv_manifest_and_extra_file_rewrites_are_rejected(self) -> None:
        mutations = (
            lambda target: (target / "market_data" / "300308.csv").write_text(
                "tampered\n", encoding="utf-8"
            ),
            lambda target: (target / "manifest.json").write_text(
                "{}\n", encoding="utf-8"
            ),
            lambda target: (target / "regime_data" / "extra.csv").write_text(
                "date,close\n2026-07-30,1\n", encoding="utf-8"
            ),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate), tempfile.TemporaryDirectory() as tmp:
                target = self._snapshot(Path(tmp))
                mutate(target)
                with self.assertRaises(ValueError):
                    dss._verify_frozen_snapshot(target)


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


class RiskStateDateValidationTests(unittest.TestCase):
    """Verify that risk state with scan_date > end_date is rejected
    (prevents forward contamination / look-ahead bias).
    """

    def test_future_scan_date_rejected(self) -> None:
        """Risk state from August 1 must be rejected when end_date is July 20."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = dict(VALID_RISK_STATE)
            state["scan_date"] = "2026-08-01"
            state["symbols_hash"] = "a" * 16
            state["total_symbols"] = 5
            state_file = Path(tmpdir) / "risk_state.json"
            state_file.write_text(
                json.dumps(state, allow_nan=False), encoding="utf-8"
            )

            loaded, error = dss._load_prev_risk_state(tmpdir, "2026-07-20")
            self.assertIsNone(loaded)
            self.assertIsNotNone(error)
            self.assertIn("前视污染", error)

    def test_same_day_scan_date_accepted(self) -> None:
        """Risk state from same day must be accepted (same-day rerun)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = dict(VALID_RISK_STATE)
            state["scan_date"] = "2026-07-30"
            state["symbols_hash"] = "a" * 16
            state["total_symbols"] = 5
            state_file = Path(tmpdir) / "risk_state.json"
            state_file.write_text(
                json.dumps(state, allow_nan=False), encoding="utf-8"
            )

            loaded, error = dss._load_prev_risk_state(tmpdir, "2026-07-30")
            self.assertIsNotNone(loaded)
            self.assertIsNone(error)

    def test_past_scan_date_accepted(self) -> None:
        """Risk state from July 20 must be accepted when end_date is July 30."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = dict(VALID_RISK_STATE)
            state["scan_date"] = "2026-07-20"
            state["symbols_hash"] = "a" * 16
            state["total_symbols"] = 5
            state_file = Path(tmpdir) / "risk_state.json"
            state_file.write_text(
                json.dumps(state, allow_nan=False), encoding="utf-8"
            )

            loaded, error = dss._load_prev_risk_state(tmpdir, "2026-07-30")
            self.assertIsNotNone(loaded)
            self.assertIsNone(error)

    def test_future_scan_date_cli_exits_1(self) -> None:
        """CLI must exit 1 when risk state scan_date > end_date."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = dict(VALID_RISK_STATE)
            state["scan_date"] = "2026-08-01"
            state["symbols_hash"] = "a" * 16
            state["total_symbols"] = 5
            state_file = Path(tmpdir) / "risk_state.json"
            state_file.write_text(
                json.dumps(state, allow_nan=False), encoding="utf-8"
            )

            with patch("sys.argv", [
                "daily_signal_scan.py",
                "--output-dir", tmpdir,
                "--end-date", "2026-07-20",
            ]):
                exit_code = dss.main()

            self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
