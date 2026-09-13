"""Integration-only fixture identities for daily-scan transaction tests."""
from __future__ import annotations

import pytest

from tests.integration._daily_scan_support import synthetic_regime_dir


@pytest.fixture(scope="session", autouse=True)
def _transaction_test_regime_evidence(tmp_path_factory):
    """Give non-acquisition daily tests explicit target-session index evidence.

    The affected tests exercise artifact/state/report mechanics. They still
    pass through the production exchange-session coverage gate; only the old
    retained index fixture is replaced by explicit synthetic evidence for the
    test's 2026-07-30 request date.
    """
    regime_dir = synthetic_regime_dir(
        tmp_path_factory.mktemp("daily-transaction-regime"), "2026-07-30"
    )
    import tests.integration.test_daily_artifact_transactions as transactions
    import tests.integration.test_daily_signal_service as signal_service

    original_transactions = transactions.REGIME_DATA_DIR
    original_signal_service = signal_service.REGIME_DATA_DIR
    transactions.REGIME_DATA_DIR = regime_dir
    signal_service.REGIME_DATA_DIR = regime_dir
    try:
        yield
    finally:
        transactions.REGIME_DATA_DIR = original_transactions
        signal_service.REGIME_DATA_DIR = original_signal_service
