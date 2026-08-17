"""Contracts for the canonical strategy, risk, portfolio, and engine modules."""

from __future__ import annotations

import importlib
import unittest


class EngineModuleContracts(unittest.TestCase):
    """Legacy exports must be the canonical implementation objects."""

    def test_strategy_exports_are_canonical(self) -> None:
        legacy = importlib.import_module("quant_fusion")
        strategies = importlib.import_module("quantfusion.strategy.trend")
        for name in (
            "BaseStrategy",
            "TurtleBreakoutStrategy",
            "DualMAStrategy",
            "ATRChannelStrategy",
        ):
            self.assertIs(getattr(legacy, name), getattr(strategies, name))

    def test_risk_and_portfolio_exports_are_canonical(self) -> None:
        legacy = importlib.import_module("quant_fusion")
        managers = importlib.import_module("quantfusion.risk.managers")
        policy = importlib.import_module("quantfusion.portfolio.policy")
        for name in (
            "RiskManager",
            "PersistentRiskManager",
            "RecoverableDrawdownRiskManager",
        ):
            self.assertIs(getattr(legacy, name), getattr(managers, name))
        self.assertIs(legacy.PortfolioPolicy, policy.PortfolioPolicy)

    def test_engine_exports_are_canonical(self) -> None:
        legacy = importlib.import_module("quant_fusion")
        core = importlib.import_module("quantfusion.engine.core")
        universe = importlib.import_module("quantfusion.engine.universe")
        self.assertIs(legacy._CoreBacktestEngine, core.CoreBacktestEngine)
        self.assertIs(legacy.SleeveBacktestEngine, universe.SleeveBacktestEngine)
        self.assertIs(legacy.BacktestEngine, universe.BacktestEngine)

    def test_execution_and_portfolio_behaviors_live_in_their_packages(self) -> None:
        execution = importlib.import_module("quantfusion.execution.flow")
        execution_facade = importlib.import_module("quantfusion.engine.execution_flow")
        portfolio = importlib.import_module("quantfusion.portfolio.allocation")
        universe = importlib.import_module("quantfusion.engine.universe")
        self.assertIs(execution_facade.CoreExecutionMixin, execution.CoreExecutionMixin)
        self.assertIn(portfolio.PortfolioAllocationMixin, universe.BacktestEngine.mro())

    def test_cli_and_reporting_exports_are_canonical(self) -> None:
        legacy = importlib.import_module("quant_fusion")
        cli = importlib.import_module("quantfusion.application.backtest_cli")
        reporting = importlib.import_module("quantfusion.application.reporting")
        self.assertIs(legacy.parse_symbols, cli.parse_symbols)
        self.assertIs(legacy.build_argument_parser, cli.build_argument_parser)
        self.assertIs(legacy.PerformanceReport, reporting.PerformanceReport)

    def test_engine_constructs_with_frozen_defaults(self) -> None:
        universe = importlib.import_module("quantfusion.engine.universe")
        engine = universe.BacktestEngine(2_000_000)
        self.assertEqual(engine.initial_capital, 2_000_000)
        self.assertEqual(engine.cfg["entry_period"], engine._default_config()["entry_period"])
        self.assertEqual(engine.cfg["max_drawdown"], engine.policy.confirmed_drawdown)


if __name__ == "__main__":
    unittest.main()
