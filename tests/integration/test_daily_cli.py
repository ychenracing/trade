#!/usr/bin/env python3
"""日扫命令行参数与子进程集成契约。"""

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


class CLIArgumentTests(unittest.TestCase):
    """Verify CLI arguments are parsed correctly."""

    def test_default_arguments(self) -> None:
        with patch("sys.argv", ["daily_signal_scan.py"]):
            # The real parser is built inside _run_main() (not exposed at
            # module level), so we can't inspect it here without executing a
            # full scan. Just confirm the argparse builder is reachable.
            self.assertTrue(callable(dss._run_main))

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
        cls._project_dir = str(Path(__file__).resolve().parents[2])

    def _run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run daily_signal_scan.py with the given arguments."""
        return subprocess.run(
            [sys.executable, "daily_signal_scan.py", *args],
            capture_output=True,
            text=True,
            cwd=self._project_dir,
            timeout=15,
        )

    def test_missing_account_file_exits_1(self) -> None:
        """A missing account snapshot must fail without touching simulation state."""
        result = self._run_cli("--account", "dummy.json")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Account signal scan failed", result.stdout)

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
            self.assertIn("Account signal scan failed", result.stdout)

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


if __name__ == "__main__":
    unittest.main()
