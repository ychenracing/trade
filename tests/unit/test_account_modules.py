"""Contracts for account models, snapshot validation, and scan service."""

from __future__ import annotations

import importlib
import unittest


class AccountModuleContracts(unittest.TestCase):
    """Account sizing uses canonical domain rules."""

    def test_target_sizing_uses_shared_domain_lot_rule(self) -> None:
        service = importlib.import_module("quantfusion.account.service")
        rules = importlib.import_module("quantfusion.domain.rules")
        self.assertIs(service.floor_to_lot, rules.floor_to_lot)


if __name__ == "__main__":
    unittest.main()
