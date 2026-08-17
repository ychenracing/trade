"""Behavior contracts for the canonical leaf modules."""

from __future__ import annotations

import importlib
import unittest

import quant_fusion as legacy


class DomainRuleTests(unittest.TestCase):
    """Shared A-share rules keep their legacy behavior and public names."""

    def test_lot_rounding_and_limit_rules_are_canonical(self) -> None:
        rules = importlib.import_module("quantfusion.domain.rules")
        self.assertEqual(rules.floor_to_lot(399.9), 300)
        self.assertEqual(rules.floor_to_lot(-1), 0)
        self.assertEqual(rules.limit_pct_for_code("300308"), 0.20)
        self.assertEqual(rules.limit_pct_for_code("600206"), 0.10)
        self.assertIs(legacy._floor_to_lot, rules.floor_to_lot)

    def test_numeric_validation_keeps_exception_contract(self) -> None:
        rules = importlib.import_module("quantfusion.domain.rules")
        with self.assertRaisesRegex(ValueError, "must be finite"):
            rules.require_finite("x", float("nan"))
        with self.assertRaisesRegex(ValueError, "must be bool"):
            rules.require_bool("enabled", 1)


class LeafIdentityTests(unittest.TestCase):
    """Legacy imports resolve to the one canonical implementation source."""

    def test_data_indicator_and_domain_types_are_reexported(self) -> None:
        providers = importlib.import_module("quantfusion.data.providers")
        technical = importlib.import_module("quantfusion.indicators.technical")
        models = importlib.import_module("quantfusion.domain.models")
        self.assertIs(legacy.DataFetcher, providers.DataFetcher)
        self.assertIs(legacy.Indicators, technical.Indicators)
        self.assertIs(legacy.Position, models.Position)
        self.assertIs(legacy.Signal, models.Signal)

    def test_market_data_contract_legacy_module_is_canonical(self) -> None:
        canonical = importlib.import_module("quantfusion.data.contracts")
        old = importlib.import_module("market_data_contracts")
        self.assertIs(old.refresh_regime_indices, canonical.refresh_regime_indices)

    def test_default_engine_config_is_a_single_source(self) -> None:
        config = importlib.import_module("quantfusion.config.engine")
        self.assertEqual(
            config.default_engine_config(),
            legacy._CoreBacktestEngine._default_config(),
        )
        first = config.default_engine_config()
        first["entry_period"] = 999
        self.assertEqual(config.default_engine_config()["entry_period"], 8)


if __name__ == "__main__":
    unittest.main()
