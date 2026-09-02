"""Behavior contracts for the canonical leaf modules."""

from __future__ import annotations

import importlib
import unittest

class DomainRuleTests(unittest.TestCase):
    """Shared A-share rules keep their current behavior and public names."""

    def test_lot_rounding_and_limit_rules_are_canonical(self) -> None:
        rules = importlib.import_module("quantfusion.domain.rules")
        self.assertEqual(rules.floor_to_lot(399.9), 300)
        self.assertEqual(rules.floor_to_lot(-1), 0)
        self.assertEqual(rules.limit_pct_for_code("300308"), 0.20)
        self.assertEqual(rules.limit_pct_for_code("600206"), 0.10)

    def test_numeric_validation_keeps_exception_contract(self) -> None:
        rules = importlib.import_module("quantfusion.domain.rules")
        with self.assertRaisesRegex(ValueError, "must be finite"):
            rules.require_finite("x", float("nan"))
        with self.assertRaisesRegex(ValueError, "must be bool"):
            rules.require_bool("enabled", 1)


class LeafIdentityTests(unittest.TestCase):
    """Canonical public imports resolve to the implementation source."""

    def test_data_indicator_and_domain_types_are_reexported(self) -> None:
        public_data = importlib.import_module("quantfusion.data")
        providers = importlib.import_module("quantfusion.data.providers")
        public_domain = importlib.import_module("quantfusion.domain")
        models = importlib.import_module("quantfusion.domain.models")
        self.assertIs(public_data.DataFetcher, providers.DataFetcher)
        self.assertIs(public_domain.Position, models.Position)
        self.assertIs(public_domain.Signal, models.Signal)

    def test_default_engine_config_is_a_single_source(self) -> None:
        config = importlib.import_module("quantfusion.config.engine")
        core = importlib.import_module("quantfusion.engine.core")
        self.assertFalse(hasattr(core.CoreBacktestEngine, "_default_config"))
        self.assertEqual(core.CoreBacktestEngine().cfg, config.default_engine_config())
        first = config.default_engine_config()
        first["entry_period"] = 999
        self.assertEqual(config.default_engine_config()["entry_period"], 8)

    def test_misleading_account_order_count_alias_is_absent(self) -> None:
        models = importlib.import_module("quantfusion.domain.models")
        self.assertFalse(hasattr(models, "account_order_count"))


if __name__ == "__main__":
    unittest.main()
