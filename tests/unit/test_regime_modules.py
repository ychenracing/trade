"""Contracts for regime evidence, routing, weak strategy, and replay modules."""

from __future__ import annotations

import importlib
import unittest


class RegimeModuleContracts(unittest.TestCase):
    """The legacy regime facade must re-export canonical implementation objects."""

    def test_models_and_state_machine_are_canonical(self) -> None:
        legacy = importlib.import_module("regime_adaptive")
        models = importlib.import_module("quantfusion.regime.models")
        state = importlib.import_module("quantfusion.regime.state_machine")
        self.assertIs(legacy.RegimeRoute, models.RegimeRoute)
        self.assertIs(legacy.RegimeEvidence, models.RegimeEvidence)
        self.assertIs(legacy.simulate_route_sequence, state.simulate_route_sequence)

    def test_evidence_and_weak_strategy_are_canonical(self) -> None:
        legacy = importlib.import_module("regime_adaptive")
        evidence = importlib.import_module("quantfusion.regime.evidence")
        weak = importlib.import_module("quantfusion.strategy.weak")
        self.assertIs(legacy.detect_regime, evidence.detect_regime)
        self.assertIs(
            legacy.select_positive_momentum_leaders,
            evidence.select_positive_momentum_leaders,
        )
        self.assertIs(legacy.PositiveMomentumHoldStrategy, weak.PositiveMomentumHoldStrategy)

    def test_replay_engines_are_canonical(self) -> None:
        legacy = importlib.import_module("regime_adaptive")
        replay = importlib.import_module("quantfusion.engine.replay")
        self.assertIs(legacy.ProductionReplayEngine, replay.ProductionReplayEngine)
        self.assertIs(
            legacy.RegimeAdaptiveBacktestEngine,
            replay.RegimeAdaptiveBacktestEngine,
        )


if __name__ == "__main__":
    unittest.main()
