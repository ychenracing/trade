"""Contracts for regime evidence, routing, weak strategy, and replay modules."""

from __future__ import annotations

import importlib
import unittest


class RegimeModuleContracts(unittest.TestCase):
    """The application namespace uses canonical regime implementation objects."""

    def test_application_regime_api_is_canonical(self) -> None:
        application = importlib.import_module("quantfusion.application.regime_api")
        evidence = importlib.import_module("quantfusion.regime.evidence")
        self.assertIs(
            application.select_positive_momentum_leaders,
            evidence.select_positive_momentum_leaders,
        )
        replay = importlib.import_module("quantfusion.engine.replay")
        self.assertIs(
            application.RegimeAdaptiveBacktestEngine,
            replay.RegimeAdaptiveBacktestEngine,
        )


if __name__ == "__main__":
    unittest.main()
