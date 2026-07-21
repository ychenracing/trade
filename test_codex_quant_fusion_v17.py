#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Standard-library regression tests for universe-invariant version 17."""

from __future__ import annotations

import ast
import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import codex_quant_fusion_v17 as quant
from backtest_v17_universes import NAMES, UNIVERSES


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "market_data_qfq_22_20260720"


def run_quiet(codes: tuple[str, ...], state: str = "warm") -> dict:
    """Run one deterministic target-period scenario without console output."""
    with contextlib.redirect_stdout(io.StringIO()):
        return quant.BacktestEngine(2_000_000).run(
            {code: NAMES[code] for code in codes},
            "2025-04-01",
            "2026-07-20",
            data_dir=str(DATA_DIR),
            indicator_state=state,
        )


class PolicyTests(unittest.TestCase):
    """Verify v17 policy validation and smooth concentration scaling."""

    def test_default_policy_contract(self) -> None:
        policy = quant.V17Policy()
        self.assertEqual(policy.allocation_horizons[-1], (5, 20, 60))
        self.assertEqual(policy.candidate_lookbacks, (10, 20, 40))
        self.assertEqual(len(policy.candidate_horizons), 3)
        self.assertEqual(policy.candidate_reference_percentile, 0.50)
        self.assertLess(policy.drawdown_alert, policy.confirmed_drawdown)
        self.assertGreaterEqual(policy.terminal_drawdown, policy.confirmed_drawdown)
        self.assertEqual(len(policy.regime_symbols), 5)

    def test_invalid_regime_symbols_are_rejected(self) -> None:
        for symbols in ((), ("300308", "300308"), ("invalid",)):
            with self.subTest(symbols=symbols), self.assertRaises(ValueError):
                quant.V17Policy(regime_symbols=symbols)

    def test_effective_thresholds_tighten_smoothly_with_concentration(self) -> None:
        engine = quant.BacktestEngine()
        one = engine._effective_policy(1)
        five = engine._effective_policy(5)
        twenty_two = engine._effective_policy(22)
        self.assertAlmostEqual(one.confirmed_drawdown, 0.21)
        self.assertLess(one.confirmed_drawdown, five.confirmed_drawdown)
        self.assertLess(five.confirmed_drawdown, twenty_two.confirmed_drawdown)
        self.assertEqual(one.terminal_drawdown, engine.policy.terminal_drawdown)


class SymbolRoutingTests(unittest.TestCase):
    """Verify that explicit company metadata resolves every routing layer."""

    def test_cambricon_uses_the_domestic_design_semiconductor_route(self) -> None:
        code = "688256"
        name = "寒武纪"
        engine = quant.BacktestEngine

        self.assertEqual(engine.classify_symbol(code, name=name), "semiconductor")
        self.assertEqual(
            engine._SYMBOL_GROUP[code],
            "domestic_semiconductor",
        )
        self.assertEqual(engine._SYMBOL_PROFILE[code], "domestic_design")
        self.assertEqual(
            engine.config_for_symbol(code, name=name),
            engine.domestic_design_config(),
        )
        self.assertEqual(quant.parse_symbols(name), {code: name})
        self.assertIn(code, quant.EXECUTION_PRIORITY)
        self.assertEqual(
            set(engine._KNOWN_CLASSIFICATION),
            set(engine._SYMBOL_GROUP),
        )
        self.assertEqual(
            set(engine._KNOWN_CLASSIFICATION),
            set(engine._SYMBOL_PROFILE),
        )


class StandaloneAndDataSourceTests(unittest.TestCase):
    """Verify standalone packaging and explicit online/local source selection."""

    def test_source_has_no_import_dependency_on_another_strategy_module(self) -> None:
        source_path = ROOT / "codex_quant_fusion_v17.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imported_modules = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_modules.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        self.assertFalse(
            any(module.startswith("codex_quant_fusion_") for module in imported_modules)
        )

    def test_script_starts_in_an_isolated_directory(self) -> None:
        source = ROOT / "codex_quant_fusion_v17.py"
        with tempfile.TemporaryDirectory() as directory:
            isolated = Path(directory) / source.name
            shutil.copy2(source, isolated)
            completed = subprocess.run(
                [sys.executable, "-I", str(isolated), "--help"],
                cwd=directory,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("standalone backtester", completed.stdout)

    def test_cli_defaults_to_online_data(self) -> None:
        args = quant.build_argument_parser().parse_args([])
        self.assertEqual(args.data_dir, "")

    def test_local_csv_mode_never_calls_the_online_loader(self) -> None:
        with mock.patch.object(
            quant.DataFetcher,
            "fetch_stock_data",
            side_effect=AssertionError("online loader must not be called"),
        ):
            frame = quant.DataFetcher.load_stock_data(
                "300308",
                "2025-04-01",
                "2025-04-10",
                data_dir=str(DATA_DIR),
            )
        self.assertFalse(frame.empty)

    def test_online_mode_uses_provider_failover(self) -> None:
        raw = pd.DataFrame(
            {
                "date": ["2026-01-02", "2026-01-05"],
                "open": [10.0, 10.2],
                "close": [10.1, 10.3],
                "high": [10.2, 10.4],
                "low": [9.9, 10.1],
                "volume": [1_000.0, 1_200.0],
            }
        )
        with (
            mock.patch.object(quant, "ak", object()),
            mock.patch.object(
                quant.DataFetcher,
                "_fetch_eastmoney",
                side_effect=RuntimeError("provider unavailable"),
            ) as eastmoney,
            mock.patch.object(
                quant.DataFetcher,
                "_fetch_sina",
                return_value=raw,
            ) as sina,
            mock.patch.object(quant.DataFetcher, "_fetch_tencent") as tencent,
        ):
            frame = quant.DataFetcher.load_stock_data(
                "300308",
                "2026-01-02",
                "2026-01-05",
                data_dir=None,
            )
        eastmoney.assert_called_once()
        sina.assert_called_once()
        tencent.assert_not_called()
        self.assertEqual(len(frame), 2)

    def test_eastmoney_provider_requests_forward_adjusted_akshare_data(self) -> None:
        provider = mock.Mock()
        provider.stock_zh_a_hist.return_value = pd.DataFrame()
        with mock.patch.object(quant, "ak", provider):
            quant.DataFetcher._fetch_eastmoney(
                "300308",
                "2025-04-01",
                "2026-07-20",
            )
        provider.stock_zh_a_hist.assert_called_once_with(
            symbol="300308",
            period="daily",
            start_date="20250401",
            end_date="20260720",
            adjust="qfq",
        )


class RiskManagerTests(unittest.TestCase):
    """Verify temporary rearming and the independent lifetime terminal lock."""

    def test_cycle_lock_rearms_then_lifetime_boundary_stays_terminal(self) -> None:
        policy = quant.V17Policy(
            allocation_mode="single",
            drawdown_alert=0.16,
            confirmed_drawdown=0.20,
            emergency_drawdown=0.24,
            terminal_drawdown=0.21,
            rearm_trading_days=3,
        )
        manager = quant.RecoverableDrawdownRiskManager(
            {"max_drawdown": policy.confirmed_drawdown}, policy
        )
        dates = list(pd.bdate_range("2026-01-02", periods=8))
        positions = {date: index for index, date in enumerate(dates)}

        def check(value: float, index: int) -> str | None:
            return manager.check_portfolio_risk(
                value,
                dates[index].strftime("%Y-%m-%d"),
                trading_dates=dates,
                date_to_pos=positions,
            )

        self.assertIsNone(check(100.0, 0))
        self.assertIsNone(check(80.0, 1))
        self.assertEqual(check(80.0, 2), "portfolio drawdown circuit breaker")
        self.assertEqual(check(80.0, 3), "persistent portfolio risk lock")
        self.assertEqual(check(80.0, 4), "persistent portfolio risk lock")
        self.assertIsNone(check(80.0, 5))
        self.assertFalse(manager.persistent_lock)
        self.assertEqual(check(78.0, 6), "portfolio drawdown circuit breaker")
        self.assertTrue(manager.terminal_lock)
        events = manager.drain_audit_events()
        self.assertTrue(
            any(event["event"] == "portfolio_drawdown_rearmed" for event in events)
        )
        self.assertTrue(
            any(
                event["event"] == "terminal_portfolio_drawdown_lock" for event in events
            )
        )

    def test_date_position_rejects_invalid_or_missing_dates(self) -> None:
        dates = list(pd.bdate_range("2026-01-02", periods=2))
        positions = {date: index for index, date in enumerate(dates)}

        self.assertIsNone(
            quant.RecoverableDrawdownRiskManager._date_position(
                "not-a-date",
                dates,
                positions,
            )
        )
        self.assertIsNone(
            quant.RecoverableDrawdownRiskManager._date_position(
                "NaT",
                dates,
                positions,
            )
        )


class IntegrationTests(unittest.TestCase):
    """Protect target-period performance and signal-only basket isolation."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.results = {name: run_quiet(codes) for name, codes in UNIVERSES.items()}

    def test_signal_only_regime_symbols_never_become_trades(self) -> None:
        result = self.results["1_symbol"]
        self.assertEqual(result["guard_scope_mode"], "fixed_signal_only_regime_basket")
        self.assertEqual(
            set(result["effective_v17_policy"]["regime_symbols"]),
            set(quant.V17Policy().regime_symbols),
        )
        self.assertTrue(result["trades"])
        self.assertEqual({trade.symbol for trade in result["trades"]}, {"300308"})

    def test_all_requested_universes_keep_positive_high_return_and_sub_20_drawdown(
        self,
    ) -> None:
        return_floors = {
            "1_symbol": 6.0,
            "3_symbols": 9.5,
            "5_symbols": 11.0,
            "13_symbols": 8.5,
            "22_symbols": 9.0,
        }
        for name, floor in return_floors.items():
            with self.subTest(universe=name):
                result = self.results[name]
                self.assertGreaterEqual(result["total_return"], floor)
                self.assertGreaterEqual(result["max_drawdown"], -0.20)
                self.assertFalse(result["terminal_risk_lock"])

    def test_multi_symbol_wealth_dispersion_is_bounded(self) -> None:
        names = ("3_symbols", "5_symbols", "13_symbols", "22_symbols")
        wealth = [1.0 + self.results[name]["total_return"] for name in names]
        self.assertGreaterEqual(min(wealth) / max(wealth), 0.80)

    def test_regime_gate_activates_before_the_july_selloff(self) -> None:
        for name, result in self.results.items():
            on_dates = {
                event["date"]
                for event in result["risk_events"]
                if event.get("event") == "sector_guard_on"
            }
            with self.subTest(universe=name):
                self.assertIn("2026-06-26", on_dates)


class PrefixStressArtifactTests(unittest.TestCase):
    """Verify the exhaustive one-through-22 prefix audit is current."""

    def test_all_prefix_counts_meet_bounded_regression_contract(self) -> None:
        path = ROOT / "v17_prefix_stress_20260720.json"
        artifact = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(artifact["v17_policy"], quant.V17Policy().as_dict())
        results = artifact["results"]
        self.assertEqual([item["symbol_count"] for item in results], list(range(1, 23)))
        self.assertGreaterEqual(results[0]["total_return"], 6.0)
        for item in results[1:]:
            with self.subTest(symbol_count=item["symbol_count"]):
                self.assertGreaterEqual(item["total_return"], 10.0)
                self.assertGreaterEqual(item["max_drawdown"], -0.23)
        worst = artifact["worst_adjacent_transition"]
        self.assertGreaterEqual(worst["wealth_change"], -0.12)


class CambriconArtifactTests(unittest.TestCase):
    """Verify the mapped nine-symbol artifact and its route metadata."""

    def test_cambricon_artifact_matches_the_reviewed_regression(self) -> None:
        path = ROOT / "v17_cambricon_universe_backtest_20260720.json"
        artifact = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            artifact["cambricon_mapping"],
            {
                "classification": "semiconductor",
                "risk_group": "domestic_semiconductor",
                "parameter_profile": "domestic_design",
            },
        )
        expected = {
            ("cold", "2026-06-30"): (11.919321500964255, -0.1494399295472653),
            ("cold", "2026-07-20"): (11.919321500964255, -0.1494399295472653),
            ("warm", "2026-06-30"): (14.180776358598129, -0.1511105646833452),
            ("warm", "2026-07-20"): (14.180776358598129, -0.1511105646833452),
        }
        results = artifact["results"]
        self.assertEqual(len(results), len(expected))
        for item in results:
            key = (item["indicator_state"], item["end_date"])
            with self.subTest(scenario=key):
                expected_return, expected_drawdown = expected[key]
                self.assertAlmostEqual(item["total_return"], expected_return)
                self.assertAlmostEqual(item["max_drawdown"], expected_drawdown)
                self.assertEqual(item["cambricon_parameter_route"], "domestic_design")
                self.assertEqual(item["guard_on_dates"], ["2026-06-26"])
                self.assertFalse(item["terminal_risk_lock"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
