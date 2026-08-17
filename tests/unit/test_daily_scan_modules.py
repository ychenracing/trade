"""Contracts for the daily-scan application and persistence boundaries."""

from __future__ import annotations

import importlib
import unittest


class DailyScanModuleContracts(unittest.TestCase):
    """The root CLI delegates to one canonical application implementation."""

    def test_import_resolves_to_canonical_application(self) -> None:
        legacy = importlib.import_module("daily_signal_scan")
        canonical = importlib.import_module("quantfusion.application.daily_scan")
        self.assertIs(legacy, canonical)

    def test_snapshot_and_state_helpers_are_canonical(self) -> None:
        daily = importlib.import_module("daily_signal_scan")
        snapshot = importlib.import_module("quantfusion.data.snapshot")
        state = importlib.import_module("quantfusion.io.state_store")
        self.assertIs(daily._verify_frozen_snapshot, snapshot.verify_frozen_snapshot)
        self.assertIs(daily._save_risk_state, state.save_risk_state)

    def test_daily_scan_uses_request_scoped_cache(self) -> None:
        daily = importlib.import_module("daily_signal_scan")
        self.assertFalse(hasattr(daily.qf.DataFetcher, "_cache_dir"))


if __name__ == "__main__":
    unittest.main()
