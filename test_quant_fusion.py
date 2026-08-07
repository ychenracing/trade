#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Standard-library regression tests for the Quant Fusion engine."""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock

import pandas as pd

import quant_fusion as quant
from backtest_universes import NAMES, UNIVERSES


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "market_data"


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
    """Verify portfolio policy validation and smooth concentration scaling."""

    def test_default_policy_contract(self) -> None:
        policy = quant.PortfolioPolicy()
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
                quant.PortfolioPolicy(regime_symbols=symbols)

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
        # Report 4.6: fine-grained AI sub-industry profiles. 688256 (寒武纪) is
        # a chip-design / domestic-compute name, so it resolves to the
        # fine-grained ``chip_design`` profile (previously ``domestic_design``).
        self.assertEqual(engine._SYMBOL_PROFILE[code], "chip_design")
        self.assertEqual(
            engine.config_for_symbol(code, name=name),
            engine.chip_design_config(),
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

    def test_unknown_auto_routes_are_explicitly_identifiable(self) -> None:
        engine = quant.BacktestEngine
        self.assertFalse(engine._uses_unmapped_auto_route("300308", "中际旭创"))
        self.assertFalse(engine._uses_unmapped_auto_route("000001", "optical module"))
        self.assertTrue(engine._uses_unmapped_auto_route("000001", "示例科技"))

        core = quant._CoreBacktestEngine(initial_capital=100.0)
        core.symbol_names = {"000001": "示例科技", "300308": "中际旭创"}
        core.equity_curve = [{"date": "2026-01-02", "assets": 100.0, "cash": 100.0}]
        result = core._build_result(100.0, [pd.Timestamp("2026-01-02")])
        self.assertEqual(result["unmapped_symbols"], ["000001"])


class SubindustryShrinkageTests(unittest.TestCase):
    """财报 P1-2：子行业参数以层级收缩拉向粗粒度父画像。"""

    ENGINE = quant._CoreBacktestEngine

    def test_shrinkage_is_enabled_by_default_and_validated(self) -> None:
        self.assertEqual(
            self.ENGINE._default_config()["subindustry_shrinkage"], 0.5
        )
        engine = quant.BacktestEngine()
        self.assertEqual(engine.cfg["subindustry_shrinkage"], 0.5)

    def test_chip_design_delta_zero_is_unchanged_under_default_shrinkage(self) -> None:
        # 688256 (寒武纪) -> chip_design == domestic_design (parent), so every
        # shrinkable delta is 0 and the golden-metric profile is preserved.
        shrunk = self.ENGINE.config_for_symbol("688256", "寒武纪")
        self.assertEqual(shrunk, self.ENGINE.chip_design_config())

    def test_optical_component_is_pulled_toward_its_coarse_parent(self) -> None:
        # optical_component overrides trail_atr_mult (3.6 vs parent 4.0),
        # risk_pct (0.028 vs 0.03) and max_symbol_weight (0.55 vs 0.6). At the
        # default 0.5 factor each override is pulled halfway toward the parent,
        # while non-shrinkable params (e.g. exit_period) stay verbatim.
        shrunk = self.ENGINE.config_for_symbol("688498", "")
        sub = self.ENGINE.optical_component_config()
        parent = self.ENGINE.optical_module_config()
        for key in self.ENGINE.SHRINKABLE_PARAMS:
            expected = parent[key] + 0.5 * (sub[key] - parent[key])
            self.assertAlmostEqual(shrunk[key], expected)
        self.assertEqual(shrunk["exit_period"], sub["exit_period"])

    def test_shrinkage_zero_converges_and_one_keeps_subindustry(self) -> None:
        sub = self.ENGINE.optical_component_config()
        parent = self.ENGINE.optical_module_config()
        shrunk_zero = self.ENGINE.config_for_symbol("688498", "", shrinkage=0.0)
        for key in self.ENGINE.SHRINKABLE_PARAMS:
            self.assertEqual(shrunk_zero[key], parent[key])
        self.assertEqual(
            self.ENGINE.config_for_symbol("688498", "", shrinkage=1.0), sub
        )

    def test_non_shrinkable_params_are_not_refined_by_shrinkage(self) -> None:
        # entry/exit periods, profit protection, pyramid and regime params are
        # shared through the hierarchy and must never be shrunk toward a raw
        # global default (report P1-2 "不建议独立调整").
        self.assertEqual(
            self.ENGINE.SHRINKABLE_PARAMS,
            frozenset({"max_symbol_weight", "atr_multiplier", "trail_atr_mult", "risk_pct"}),
        )


class StandaloneAndDataSourceTests(unittest.TestCase):
    """Verify standalone packaging and explicit online/local source selection."""

    def test_source_has_no_import_dependency_on_another_strategy_module(self) -> None:
        source_path = ROOT / "quant_fusion.py"
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
            any(module.startswith("quant_fusion_v") for module in imported_modules)
        )

    def test_script_starts_in_an_isolated_directory(self) -> None:
        source = ROOT / "quant_fusion.py"
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
        previous_cache = quant.DataFetcher._cache_dir
        quant.DataFetcher._cache_dir = None  # force the raw provider failover path
        try:
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
        finally:
            quant.DataFetcher._cache_dir = previous_cache

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
        policy = quant.PortfolioPolicy(
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


class ExecutionControlTests(unittest.TestCase):
    """Verify immutable orders, fair batches, ADV accounting, and guard quorum."""

    def test_signal_is_immutable(self) -> None:
        signal = quant.Signal("300308", "alpha", "buy", target_shares=100)
        with self.assertRaises(FrozenInstanceError):
            signal.target_shares = 200

    def test_fair_batch_allocation_is_order_independent(self) -> None:
        items = [
            (quant.Signal("300308", name, "buy", target_shares=6_000), mock.Mock())
            for name in ("alpha", "beta")
        ]

        def allocate(batch: list[tuple[quant.Signal, mock.Mock]]) -> dict[str, int]:
            shares = quant.SleeveBacktestEngine._allocate_lots_pro_rata(batch, 6_000)
            return {
                item[0].strategy_name: allocated
                for item, allocated in zip(batch, shares, strict=True)
            }

        self.assertEqual(allocate(items), {"alpha": 3_000, "beta": 3_000})
        self.assertEqual(allocate(list(reversed(items))), allocate(items))

    @staticmethod
    def _sleeve(capital: float = 1_000_000) -> quant.SleeveBacktestEngine:
        policy = quant.PortfolioPolicy(allocation_mode="single")
        return quant.SleeveBacktestEngine(
            capital,
            cfg=None,
            policy=policy,
            allocation_lookbacks=policy.single_lookbacks,
            sleeve_name="test",
        )

    def test_batch_capacity_uses_the_strictest_strategy_exposure(self) -> None:
        sleeve = self._sleeve()
        dates = pd.bdate_range("2026-01-02", periods=22)
        frame = pd.DataFrame(
            {
                "open": 10.0,
                "close": 10.0,
                "high": 10.0,
                "low": 10.0,
                "volume": 10_000_000.0,
            },
            index=dates,
        )
        loose_cfg = sleeve._default_config()
        strict_cfg = sleeve._default_config()
        loose_cfg["max_symbol_weight"] = 0.60
        strict_cfg["max_symbol_weight"] = 0.20
        items = [
            (
                quant.Signal("300308", "turtle_breakout", "buy", 50_000, price=10.0),
                quant.TurtleBreakoutStrategy(loose_cfg),
            ),
            (
                quant.Signal("300308", "dual_ma", "buy", 50_000, price=10.0),
                quant.DualMAStrategy(strict_cfg),
            ),
        ]

        capacity = sleeve._buy_batch_capacity(items, {"300308": frame}, dates[-1])

        self.assertEqual(capacity, 19_900)

    def test_batch_rejects_mixed_symbols_or_execution_prices(self) -> None:
        sleeve = self._sleeve()
        strategy = quant.TurtleBreakoutStrategy(sleeve._default_config())
        dates = pd.bdate_range("2026-01-02", periods=2)
        frame = pd.DataFrame(
            {
                "open": 10.0,
                "close": 10.0,
                "high": 10.0,
                "low": 10.0,
                "volume": 1_000_000.0,
            },
            index=dates,
        )
        data_map = {"300308": frame, "300502": frame}

        with self.assertRaisesRegex(ValueError, "exactly one symbol"):
            sleeve._buy_batch_capacity(
                [
                    (quant.Signal("300308", "a", "buy", 100, price=10.0), strategy),
                    (quant.Signal("300502", "b", "buy", 100, price=10.0), strategy),
                ],
                data_map,
                dates[-1],
            )
        with self.assertRaisesRegex(ValueError, "one execution price"):
            sleeve._buy_batch_capacity(
                [
                    (quant.Signal("300308", "a", "buy", 100, price=10.0), strategy),
                    (quant.Signal("300308", "b", "buy", 100, price=10.1), strategy),
                ],
                data_map,
                dates[-1],
            )

    def test_low_price_fee_floor_uses_exact_affordability_search(self) -> None:
        sleeve = self._sleeve(capital=14.5)
        items = [
            (quant.Signal("300308", name, "buy", target * 100), mock.Mock())
            for name, target in zip(("a", "b", "c"), (1, 3, 3), strict=True)
        ]

        capacity = sleeve._cash_affordable_batch_capacity(
            items,
            requested=700,
            execution_price=0.01,
        )

        self.assertEqual(capacity, 400)

    def test_buy_rejection_records_the_concrete_execution_reason(self) -> None:
        sleeve = self._sleeve(capital=1_000)
        sleeve.cash = 0.0
        strategy = quant.TurtleBreakoutStrategy(sleeve._default_config())
        signal = quant.Signal(
            "300308", strategy.name, "buy", target_shares=100, price=10.0
        )

        executed = sleeve._execute_buy(signal, strategy, "2026-01-02")

        self.assertFalse(executed)
        self.assertEqual(sleeve.order_events[-1]["event"], "rejected_insufficient_cash")
        self.assertNotIn(
            "rejected_by_execution_checks",
            {event["event"] for event in sleeve.order_events},
        )

    def test_portfolio_liquidation_audits_a_superseded_sell_reason(self) -> None:
        sleeve = self._sleeve()
        strategy = quant.TurtleBreakoutStrategy(sleeve._default_config())
        strategy.position = quant.Position(
            "300308", strategy.name, 1_000, 10.0, "2026-01-02"
        )
        sleeve.positions = {"300308": {strategy.name: strategy.position}}
        sleeve.strategy_instances = {"300308": [strategy]}
        previous = quant.Signal(
            "300308",
            strategy.name,
            "sell",
            target_shares=300,
            price=9.0,
            reason="strategy reversal",
            signal_date="2026-01-05",
        )
        state = quant._PreparedSleeveRun(
            sleeve=sleeve,
            data_map={},
            indicator_map={},
            all_dates=[pd.Timestamp("2026-01-06")],
            date_to_pos={pd.Timestamp("2026-01-06"): 0},
            pending=[(previous, strategy)],
        )

        quant.BacktestEngine._apply_global_risk_lock(
            [state], pd.Timestamp("2026-01-06")
        )

        self.assertEqual(len(state.pending), 1)
        liquidation = state.pending[0][0]
        self.assertEqual(liquidation.target_shares, 1_000)
        self.assertEqual(liquidation.reason, "portfolio-level drawdown liquidation")
        event = sleeve.order_events[-1]
        self.assertEqual(
            event["event"], "pending_sell_superseded_by_portfolio_liquidation"
        )
        self.assertEqual(event["previous_reason"], "strategy reversal")

    def test_adv_capacity_is_shared_by_date_symbol_and_side(self) -> None:
        policy = quant.PortfolioPolicy(allocation_mode="single")
        sleeve = quant.SleeveBacktestEngine(
            1_000_000,
            cfg=None,
            policy=policy,
            allocation_lookbacks=policy.single_lookbacks,
            sleeve_name="test",
        )
        dates = pd.bdate_range("2026-01-02", periods=22)
        frame = pd.DataFrame({"volume": 1_000_000.0}, index=dates)
        data_map = {"300308": frame}
        execution_date = dates[-1]

        capacity, _ = sleeve._adv_capacity("300308", "buy", data_map, execution_date)
        self.assertEqual(capacity, 5_000)
        sleeve._consume_adv(execution_date.strftime("%Y-%m-%d"), "300308", "buy", 3_000)
        remaining, _ = sleeve._adv_capacity("300308", "buy", data_map, execution_date)
        sell_capacity, _ = sleeve._adv_capacity(
            "300308", "sell", data_map, execution_date
        )
        self.assertEqual(remaining, 2_000)
        self.assertEqual(sell_capacity, 5_000)

    def test_missing_regime_symbol_preserves_guard_state(self) -> None:
        policy = quant.PortfolioPolicy(allocation_mode="single")
        sleeve = quant.SleeveBacktestEngine(
            1_000_000,
            cfg={
                "sector_guard_min_symbols": 5,
                "sector_shock_ma": 2,
                "sector_recovery_ma": 2,
            },
            policy=policy,
            allocation_lookbacks=policy.single_lookbacks,
            sleeve_name="test",
        )
        dates = list(pd.bdate_range("2026-01-02", periods=4))
        data_map = {
            symbol: pd.DataFrame({"close": [100.0, 99.0, 98.0, 97.0]}, index=dates)
            for symbol in policy.regime_symbols
        }
        missing = policy.regime_symbols[-1]
        data_map[missing] = data_map[missing].iloc[:-1]
        sleeve.sector_guard_active = True
        sleeve._sector_shock_positions = [2]
        sleeve._sector_recovery_streak = 1

        state = sleeve._update_sector_guard(
            data_map,
            dates[-1],
            dates,
            {date: index for index, date in enumerate(dates)},
        )

        self.assertEqual(state, "active")
        self.assertEqual(sleeve._sector_shock_positions, [2])
        self.assertEqual(sleeve._sector_recovery_streak, 1)
        self.assertEqual(sleeve.risk_events[-1]["observed_symbols"], 4)
        self.assertEqual(
            sleeve.risk_events[-1]["event"], "sector_guard_data_insufficient"
        )

    def test_synthetic_period_end_liquidation_is_not_configurable(self) -> None:
        with self.assertRaises(ValueError):
            quant.BacktestEngine(cfg={"force_close_on_end": True})


class IntegrationTests(unittest.TestCase):
    """Protect target-period performance and signal-only basket isolation."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.results = {name: run_quiet(codes) for name, codes in UNIVERSES.items()}

    def test_signal_only_regime_symbols_never_become_trades(self) -> None:
        result = self.results["1_symbol"]
        self.assertEqual(result["guard_scope_mode"], "fixed_signal_only_regime_basket")
        self.assertEqual(
            set(result["effective_portfolio_policy"]["regime_symbols"]),
            set(quant.PortfolioPolicy().regime_symbols),
        )
        self.assertTrue(result["trades"])
        self.assertEqual({trade.symbol for trade in result["trades"]}, {"300308"})

    def test_all_requested_universes_keep_positive_high_return_and_sub_20_drawdown(
        self,
    ) -> None:
        return_floors = {
            "1_symbol": 3.0,
            "3_symbols": 7.5,
            "5_symbols": 8.5,
            "13_symbols": 8.5,
            "22_symbols": 6.5,
        }
        for name, floor in return_floors.items():
            with self.subTest(universe=name):
                result = self.results[name]
                self.assertGreaterEqual(result["total_return"], floor)
                self.assertGreaterEqual(result["max_drawdown"], -0.20)
                self.assertFalse(result["terminal_risk_lock"])
                self.assertLessEqual(result["max_concurrent_symbols"], 6)
                self.assertEqual(
                    result["portfolio_cash_model"], "fixed_virtual_subaccounts"
                )
                self.assertGreater(result["calmar"], 0.0)

    def test_combined_same_day_fills_respect_portfolio_adv_budget(self) -> None:
        result = self.results["22_symbols"]
        frames = {
            code: quant.DataFetcher.load_stock_data(
                code,
                "2024-01-01",
                "2026-07-20",
                data_dir=str(DATA_DIR),
            )
            for code in UNIVERSES["22_symbols"]
        }
        fills: dict[tuple[str, str, str], int] = {}
        for trade in result["trades"]:
            key = (trade.date, trade.symbol, trade.direction)
            fills[key] = fills.get(key, 0) + trade.shares
        for (date_str, symbol, direction), shares in fills.items():
            date = pd.Timestamp(date_str)
            history = frames[symbol].loc[frames[symbol].index < date, "volume"]
            history = history.loc[history.gt(0)].tail(quant.PortfolioPolicy().adv_lookback)
            with self.subTest(date=date_str, symbol=symbol, direction=direction):
                self.assertFalse(history.empty)
                self.assertLessEqual(
                    shares / float(history.mean()),
                    quant.PortfolioPolicy().max_order_adv_ratio + 1e-12,
                )

    def test_canonical_market_data_matches_the_frozen_checksums(self) -> None:
        checksum_path = DATA_DIR / "SHA256SUMS"
        expected = {}
        for line in checksum_path.read_text(encoding="utf-8").splitlines():
            digest, filename = line.split(maxsplit=1)
            expected[filename] = digest
        actual_files = sorted(path.name for path in DATA_DIR.glob("*.csv"))
        self.assertEqual(sorted(expected), actual_files)
        for filename, digest in expected.items():
            with self.subTest(filename=filename):
                actual = hashlib.sha256((DATA_DIR / filename).read_bytes()).hexdigest()
                self.assertEqual(actual, digest)

    def test_multi_symbol_wealth_dispersion_is_bounded(self) -> None:
        names = ("3_symbols", "5_symbols", "13_symbols", "22_symbols")
        wealth = [1.0 + self.results[name]["total_return"] for name in names]
        self.assertGreaterEqual(min(wealth) / max(wealth), 0.75)

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
        path = ROOT / "prefix_stress.json"
        artifact = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(artifact["portfolio_policy"], quant.PortfolioPolicy().as_dict())
        results = artifact["results"]
        self.assertEqual([item["symbol_count"] for item in results], list(range(1, 23)))
        self.assertGreaterEqual(results[0]["total_return"], 3.0)
        for item in results[1:]:
            with self.subTest(symbol_count=item["symbol_count"]):
                self.assertGreaterEqual(item["total_return"], 5.0)
                self.assertGreaterEqual(item["max_drawdown"], -0.23)
                self.assertLessEqual(item["max_concurrent_symbols"], 10)
        worst = artifact["worst_adjacent_transition"]
        self.assertGreaterEqual(worst["wealth_change"], -0.30)


class CambriconArtifactTests(unittest.TestCase):
    """Verify the mapped nine-symbol artifact and its route metadata."""

    def test_cambricon_artifact_matches_the_reviewed_regression(self) -> None:
        path = ROOT / "cambricon_universe_backtest.json"
        artifact = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            artifact["cambricon_mapping"],
            {
                "classification": "semiconductor",
                "risk_group": "domestic_semiconductor",
                "parameter_profile": "chip_design",
            },
        )
        expected = {
            ("cold", "2026-06-30"): (11.869360365348875, -0.1600912542996164),
            ("cold", "2026-07-20"): (11.869360365348875, -0.1600912542996164),
            ("warm", "2026-06-30"): (12.26126676001925, -0.1544551360344086),
            ("warm", "2026-07-20"): (12.26126676001925, -0.1544551360344086),
        }
        results = artifact["results"]
        self.assertEqual(len(results), len(expected))
        for item in results:
            key = (item["indicator_state"], item["end_date"])
            with self.subTest(scenario=key):
                expected_return, expected_drawdown = expected[key]
                self.assertAlmostEqual(item["total_return"], expected_return)
                self.assertAlmostEqual(item["max_drawdown"], expected_drawdown)
                self.assertEqual(item["cambricon_parameter_route"], "chip_design")
                self.assertEqual(item["guard_on_dates"], ["2026-06-26"])
                self.assertFalse(item["terminal_risk_lock"])


class NewFeatureTests(unittest.TestCase):
    """Verify AccountState, get_symbol accessors, and other new features."""

    def test_account_state_dataclass(self) -> None:
        """AccountState fields are correctly populated."""
        state = quant.AccountState(
            cash=500000.0,
            position_value=1500000.0,
            total_equity=2000000.0,
            peak_equity=2500000.0,
            positions={"300308": {"shares": 900, "avg_cost": 980.50}},
            risk_state={"terminal_risk_lock": False},
        )
        self.assertEqual(state.cash, 500000.0)
        self.assertEqual(state.total_equity, 2000000.0)
        self.assertEqual(state.peak_equity, 2500000.0)
        self.assertIn("300308", state.positions)

    def test_engine_state_dataclass(self) -> None:
        """EngineState defaults are correct."""
        state = quant.EngineState()
        self.assertFalse(state.terminal_risk_lock)
        self.assertFalse(state.sector_guard_active)
        self.assertEqual(state.cycle_lock_count, 0)
        self.assertEqual(state.run_id, "")

    def test_get_symbol_group_returns_known_code(self) -> None:
        """get_symbol_group returns the correct group for a known symbol."""
        result = quant._CoreBacktestEngine.get_symbol_group("300308")
        self.assertEqual(result, "overseas_compute")

    def test_get_symbol_group_returns_default_for_unknown(self) -> None:
        """get_symbol_group returns the default for an unknown symbol."""
        result = quant._CoreBacktestEngine.get_symbol_group("999999", "UNKNOWN")
        self.assertEqual(result, "UNKNOWN")

    def test_get_symbol_profile_returns_known_code(self) -> None:
        """get_symbol_profile returns the correct profile for a known symbol.

        Report 4.6: fine-grained AI sub-industry profiles. 300308 (中际旭创) is
        an optical-module name, so it resolves to the fine-grained
        ``optical_module`` profile (previously the coarse ``overseas_optical``).
        """
        result = quant._CoreBacktestEngine.get_symbol_profile("300308")
        self.assertEqual(result, "optical_module")

    def test_get_symbol_profile_returns_default_for_unknown(self) -> None:
        """get_symbol_profile returns the default for an unknown symbol."""
        result = quant._CoreBacktestEngine.get_symbol_profile("999999", "UNKNOWN")
        self.assertEqual(result, "UNKNOWN")

    def test_get_symbol_classification_returns_known_code(self) -> None:
        """get_symbol_classification returns the correct value for a known symbol."""
        result = quant._CoreBacktestEngine.get_symbol_classification("300308")
        self.assertEqual(result, "default")

    def test_get_symbol_classification_returns_default_for_unknown(self) -> None:
        """get_symbol_classification returns the default for an unknown symbol."""
        result = quant._CoreBacktestEngine.get_symbol_classification("999999", "UNKNOWN")
        self.assertEqual(result, "UNKNOWN")

    def test_external_account_positions_use_correct_strategy_name(self) -> None:
        """Injected account positions use 'external_account' (not 'legacy') so
        portfolio-level risk controls can liquidate them."""
        account_state = quant.AccountState(
            cash=1_000_000.0,
            position_value=500_000.0,
            total_equity=1_500_000.0,
            peak_equity=2_000_000.0,
            positions={
                "300308": {"shares": 500, "avg_cost": 800.0, "entry_date": "2025-01-15"},
            },
        )
        engine = quant._CoreBacktestEngine(initial_capital=1_000_000)
        converted = quant._CoreBacktestEngine._apply_account_state(account_state, engine)
        self.assertIn("300308", converted)
        self.assertIn("external_account", converted["300308"])
        self.assertNotIn("legacy", converted["300308"])
        pos = converted["300308"]["external_account"]
        self.assertEqual(pos.strategy_name, "external_account")
        self.assertEqual(pos.shares, 500)
        self.assertAlmostEqual(pos.entry_price, 800.0)

    def test_apply_account_state_seeds_risk_manager_peak(self) -> None:
        """_apply_account_state seeds the risk manager's peak_assets from
        the account's peak_equity so drawdown starts from the real HWM."""
        account_state = quant.AccountState(
            cash=800_000.0,
            position_value=700_000.0,
            total_equity=1_500_000.0,
            peak_equity=2_200_000.0,
            positions={
                "300308": {"shares": 600, "avg_cost": 900.0},
            },
        )
        engine = quant._CausalBacktestEngine(initial_capital=1_500_000)
        # Create a risk manager manually since _prepare_run isn't called
        engine.risk = quant.PersistentRiskManager(engine.cfg)
        self.assertEqual(engine.risk.peak_assets, 0.0)
        quant._CoreBacktestEngine._apply_account_state(account_state, engine)
        self.assertAlmostEqual(engine.risk.peak_assets, 2_200_000.0)

    def test_liquidation_covers_external_account_positions(self) -> None:
        """_generate_liquidation_signals includes external_account positions
        so portfolio-level risk controls can liquidate the entire book."""
        engine = quant._CoreBacktestEngine(initial_capital=1_000_000)
        engine.positions = {
            "300308": {
                "external_account": quant.Position(
                    symbol="300308",
                    strategy_name="external_account",
                    shares=500,
                    entry_price=800.0,
                    entry_date="2025-01-15",
                ),
            },
        }
        engine.strategy_instances = {"300308": []}
        signals = engine._generate_liquidation_signals(
            "2026-01-01", reason="test liquidation"
        )
        self.assertEqual(len(signals), 1)
        sig, strat = signals[0]
        self.assertEqual(sig.symbol, "300308")
        self.assertEqual(sig.strategy_name, "external_account")
        self.assertEqual(sig.direction, "sell")
        self.assertEqual(sig.target_shares, 500)
        self.assertIsNone(strat)  # external positions use None strategy placeholder

    def test_execute_sell_with_none_strategy_does_not_crash(self) -> None:
        """_execute_sell must handle strategy=None (external_account) without
        crashing on strategy.name or strategy.position access."""
        engine = quant._CoreBacktestEngine(initial_capital=1_000_000)
        engine.positions = {
            "300308": {
                "external_account": quant.Position(
                    symbol="300308",
                    strategy_name="external_account",
                    shares=500,
                    entry_price=800.0,
                    entry_date="2025-01-15",
                ),
            },
        }
        # Build a sell signal targeting the external_account position
        sig = quant.Signal(
            symbol="300308",
            strategy_name="external_account",
            direction="sell",
            target_shares=500,
            price=850.0,
            reason="test liquidation",
            signal_date="2026-01-01",
        )
        # Execute with strategy=None — must not raise AttributeError
        sold = engine._execute_sell(sig, None, "2026-01-01")
        self.assertEqual(sold, 500)
        # Position should be removed after full sell
        self.assertNotIn("300308", engine.positions)
        # A trade record should be created
        self.assertEqual(len(engine.trades), 1)
        trade = engine.trades[0]
        self.assertEqual(trade.symbol, "300308")
        self.assertEqual(trade.direction, "sell")
        self.assertEqual(trade.shares, 500)

    def test_execute_sell_partial_with_none_strategy(self) -> None:
        """Partial sell with strategy=None should reduce shares and keep position."""
        engine = quant._CoreBacktestEngine(initial_capital=1_000_000)
        engine.positions = {
            "300308": {
                "external_account": quant.Position(
                    symbol="300308",
                    strategy_name="external_account",
                    shares=1000,
                    entry_price=800.0,
                    entry_date="2025-01-15",
                ),
            },
        }
        sig = quant.Signal(
            symbol="300308",
            strategy_name="external_account",
            direction="sell",
            target_shares=400,
            price=850.0,
            reason="partial reduction",
            signal_date="2026-01-01",
        )
        sold = engine._execute_sell(sig, None, "2026-01-01")
        self.assertEqual(sold, 400)
        # Position should remain with reduced shares
        remaining_pos = engine.positions["300308"]["external_account"]
        self.assertEqual(remaining_pos.shares, 600)

    def test_account_state_api_raises_not_implemented(self) -> None:
        """Core API must raise NotImplementedError when account_state is passed."""
        engine = quant.BacktestEngine(2_000_000)
        account_state = quant.AccountState(
            cash=500_000.0,
            position_value=1_500_000.0,
            total_equity=2_000_000.0,
            peak_equity=2_200_000.0,
            positions={},
            risk_state={},
        )
        with self.assertRaises(NotImplementedError):
            engine.run(
                {"300308": "中际旭创"},
                "2025-04-01",
                "2026-07-20",
                data_dir="market_data",
                indicator_state="warm",
                account_state=account_state,
            )


class CrossSleeveNettingTests(unittest.TestCase):
    """Verify the account-level share-count netting (report P0-6).

    The three virtual sleeves are signal sources, not independent books: a
    symbol being sold by any sleeve must not also be bought by another on the
    same day. Buys are absorbed CUMULATIVELY against the residual same-day sell
    pool, so multiple buys are netted correctly instead of each being compared
    to the full sell total (which would over-net legitimate net ADD exposure).
    """

    @staticmethod
    def _sleeve() -> quant.SleeveBacktestEngine:
        policy = quant.PortfolioPolicy(allocation_mode="single")
        return quant.SleeveBacktestEngine(
            1_000_000,
            cfg=None,
            policy=policy,
            allocation_lookbacks=policy.single_lookbacks,
            sleeve_name="test",
        )

    @staticmethod
    def _states(
        *sleeves: quant.SleeveBacktestEngine,
        pending: list[list[tuple[quant.Signal, object]]],
    ) -> list[quant._PreparedSleeveRun]:
        return [
            quant._PreparedSleeveRun(
                sleeve=sleeve,
                data_map={},
                indicator_map={},
                all_dates=[pd.Timestamp("2026-01-06")],
                date_to_pos={pd.Timestamp("2026-01-06"): 0},
                pending=list(orders),
            )
            for sleeve, orders in zip(sleeves, pending)
        ]

    def test_buy_fully_absorbed_by_sell_is_cancelled(self) -> None:
        sleeve = self._sleeve()
        strategy = quant.TurtleBreakoutStrategy(sleeve._default_config())
        sell = quant.Signal(
            "300308", strategy.name, "sell", target_shares=100, price=10.0
        )
        buy = quant.Signal(
            "300308", "dual_ma", "buy", target_shares=80, price=10.0
        )
        states = self._states(
            sleeve, sleeve,
            pending=[
                [(sell, strategy)],
                [(buy, strategy)],
            ],
        )
        quant.BacktestEngine._net_cross_sleeve_orders(
            states, pd.Timestamp("2026-01-06")
        )
        # Sell sleeve keeps its sell; the absorbed buy is fully cancelled.
        self.assertEqual(len(states[0].pending), 1)
        self.assertEqual(states[0].pending[0][0].direction, "sell")
        self.assertEqual(len(states[1].pending), 0)
        events = sleeve.order_events
        self.assertEqual(events[-1]["event"], "netted_cross_sleeve_buy")
        self.assertEqual(events[-1]["sell_shares"], 80)

    def test_multiple_buys_net_cumulatively_against_residual_sell(self) -> None:
        # Sell 100; buy 80 + buy 80. Naive per-buy comparison would cancel BOTH
        # buys (each <= 100) and over-sell. The fix nets them cumulatively
        # against the residual pool: first buy absorbs 80 (rem=20), second buy
        # is partially absorbed and keeps the NET-ADD tail (80 - rem 20 = 60),
        # so the cumulative net-add intent survives.
        sleeve = self._sleeve()
        strategy = quant.TurtleBreakoutStrategy(sleeve._default_config())
        order_events_before = len(sleeve.order_events)
        sell = quant.Signal(
            "300308", strategy.name, "sell", target_shares=100, price=10.0
        )
        buy1 = quant.Signal(
            "300308", "dual_ma", "buy", target_shares=80, price=10.0
        )
        buy2 = quant.Signal(
            "300308", "atr_channel", "buy", target_shares=80, price=10.0
        )
        states = self._states(
            sleeve, sleeve, sleeve,
            pending=[
                [(sell, strategy)],
                [(buy1, strategy)],
                [(buy2, strategy)],
            ],
        )
        quant.BacktestEngine._net_cross_sleeve_orders(
            states, pd.Timestamp("2026-01-06")
        )
        # Sell retained; buy1 fully cancelled; buy2 keeps the net-add tail 60.
        self.assertEqual(len(states[0].pending), 1)
        self.assertEqual(len(states[1].pending), 0)
        self.assertEqual(len(states[2].pending), 1)
        self.assertEqual(states[2].pending[0][0].target_shares, 60)
        events = sleeve.order_events[order_events_before:]
        self.assertEqual(events[0]["event"], "netted_cross_sleeve_buy")
        self.assertEqual(events[0]["sell_shares"], 80)
        self.assertEqual(events[1]["event"], "netted_cross_sleeve_buy")
        self.assertEqual(events[1]["sell_shares"], 20)
        self.assertEqual(
            events[1]["because"], "same_symbol_sell_pending_absorbs_buy_partial"
        )

    def test_buy_exceeding_sell_pool_is_genuine_net_add(self) -> None:
        # Sell 100, buy 250 -> the 100 overlap is churn, the net-ADD tail
        # (250 - 100 = 150) survives as the retained buy.
        sleeve = self._sleeve()
        strategy = quant.TurtleBreakoutStrategy(sleeve._default_config())
        sell = quant.Signal(
            "300308", strategy.name, "sell", target_shares=100, price=10.0
        )
        buy = quant.Signal(
            "300308", "dual_ma", "buy", target_shares=250, price=10.0
        )
        states = self._states(
            sleeve, sleeve,
            pending=[
                [(sell, strategy)],
                [(buy, strategy)],
            ],
        )
        quant.BacktestEngine._net_cross_sleeve_orders(
            states, pd.Timestamp("2026-01-06")
        )
        self.assertEqual(len(states[0].pending), 1)
        # Buy kept the net ADD portion (250 - 100 = 150).
        self.assertEqual(len(states[1].pending), 1)
        self.assertEqual(states[1].pending[0][0].target_shares, 150)

    def test_no_sell_pool_leaves_buy_untouched(self) -> None:
        sleeve = self._sleeve()
        strategy = quant.TurtleBreakoutStrategy(sleeve._default_config())
        buy = quant.Signal(
            "300308", "dual_ma", "buy", target_shares=80, price=10.0
        )
        states = self._states(
            sleeve,
            pending=[[(buy, strategy)]],
        )
        quant.BacktestEngine._net_cross_sleeve_orders(
            states, pd.Timestamp("2026-01-06")
        )
        self.assertEqual(len(states[0].pending), 1)
        self.assertEqual(states[0].pending[0][0].target_shares, 80)


if __name__ == "__main__":
    unittest.main(verbosity=2)
