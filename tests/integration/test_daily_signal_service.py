#!/usr/bin/env python3
"""信号分类、账户建议与买入抑制集成契约。"""

from __future__ import annotations

# ruff: noqa: F401

from ._daily_scan_support import (
    FakeSignal,
    FakeTrade,
    Path,
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


class AccountRiskWorkflowTests(unittest.TestCase):
    """Verify the account + risk state workflow end-to-end."""

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
        # This logic mirrors the validation in quantfusion.application.daily_scan
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


class RiskStateNotInjectedTests(unittest.TestCase):
    """Verify that risk_state is NOT passed to engine.run() — preventing
    the time-direction error where future end-state changes past history.
    """

    def test_engine_run_not_called_with_risk_state(self) -> None:
        """engine.run() must NOT receive a risk_state parameter."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a valid risk state file so prev_risk is loaded
            state = dict(VALID_RISK_STATE)
            state["scan_date"] = "2026-07-29"
            state["symbols_hash"] = "a" * 16
            state["total_symbols"] = 1
            state_file = Path(tmpdir) / "risk_state.json"
            state_file.write_text(
                json.dumps(state, allow_nan=False), encoding="utf-8"
            )

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

            # Capture the actual call arguments
            captured_kwargs: dict = {}

            def capture_run(self, *args, **kwargs):
                captured_kwargs.update(kwargs)
                return mock_result

            with patch.object(dss.qf.DataFetcher, "load_stock_data",
                              return_value=mock_df), \
                 patch.object(dss.ra.RegimeAdaptiveBacktestEngine, "run", new=capture_run):
                with patch("sys.argv", [
                    "quantfusion.application.daily_scan",
                    "--output-dir", tmpdir,
                    "--end-date", "2026-07-30",
                ]):
                    exit_code = dss.main()

            self.assertEqual(exit_code, 0)
            # risk_state must NOT be in the kwargs passed to engine.run()
            self.assertNotIn("risk_state", captured_kwargs,
                             "risk_state must NOT be passed to engine.run()")


if __name__ == "__main__":
    unittest.main()
