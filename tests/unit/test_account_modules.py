"""Contracts for account models, snapshot validation, and scan service."""

from __future__ import annotations

import importlib
import unittest


class AccountModuleContracts(unittest.TestCase):
    """Legacy account exports resolve to the canonical modules."""

    def test_models_and_snapshot_loader_are_canonical(self) -> None:
        legacy = importlib.import_module("account_signal_engine")
        models = importlib.import_module("quantfusion.account.models")
        snapshot = importlib.import_module("quantfusion.account.snapshot")
        self.assertIs(legacy.AccountPosition, models.AccountPosition)
        self.assertIs(legacy.AccountSnapshot, models.AccountSnapshot)
        self.assertIs(legacy.load_account_snapshot, snapshot.load_account_snapshot)

    def test_account_engine_is_an_application_service(self) -> None:
        legacy = importlib.import_module("account_signal_engine")
        application = importlib.import_module("quantfusion.application.account_scan")
        self.assertIs(legacy.AccountSignalEngine, application.AccountSignalEngine)
        self.assertIs(legacy.run_account_scan, application.run_account_scan)

    def test_target_sizing_uses_shared_domain_lot_rule(self) -> None:
        service = importlib.import_module("quantfusion.account.service")
        rules = importlib.import_module("quantfusion.domain.rules")
        self.assertIs(service.floor_to_lot, rules.floor_to_lot)


if __name__ == "__main__":
    unittest.main()
