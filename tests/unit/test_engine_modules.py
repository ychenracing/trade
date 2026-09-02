"""Contracts for the canonical strategy, risk, portfolio, and engine modules."""

from __future__ import annotations

import importlib
import unittest
from dataclasses import fields
from inspect import signature

from quantfusion.config.engine import default_engine_config


class EngineModuleContracts(unittest.TestCase):
    """Current engine construction uses the canonical implementation."""

    def test_engine_constructs_with_frozen_defaults(self) -> None:
        universe = importlib.import_module("quantfusion.engine.universe")
        engine = universe.BacktestEngine(2_000_000)
        self.assertEqual(engine.initial_capital, 2_000_000)
        self.assertEqual(
            engine.cfg["entry_period"], default_engine_config()["entry_period"]
        )
        self.assertEqual(engine.cfg["max_drawdown"], engine.policy.confirmed_drawdown)

    def test_current_engine_and_replay_signatures_exclude_account_state(self) -> None:
        universe = importlib.import_module("quantfusion.engine.universe")
        replay = importlib.import_module("quantfusion.engine.replay")

        for engine_type in (
            universe.BacktestEngine,
            replay.ProductionReplayEngine,
            replay.RegimeAdaptiveBacktestEngine,
        ):
            with self.subTest(engine=engine_type.__name__):
                parameters = signature(engine_type.run).parameters
                self.assertNotIn("account_state", parameters)
                self.assertIn("risk_state", parameters)

    def test_ensemble_request_carries_risk_state_without_account_state(self) -> None:
        ensemble = importlib.import_module("quantfusion.engine.ensemble")

        request_fields = {field.name for field in fields(ensemble.RunRequest)}
        self.assertNotIn("account_state", request_fields)
        self.assertIn("risk_state", request_fields)


if __name__ == "__main__":
    unittest.main()
