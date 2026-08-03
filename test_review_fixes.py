"""Regression tests for the production review fixes."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import account_signal_engine as account
import quant_fusion as qf
import regime_adaptive as ra


class ProviderVolumeContractTests(unittest.TestCase):
    def test_eastmoney_lots_are_converted_to_shares(self) -> None:
        frame = pd.DataFrame(
            {"日期": ["2026-01-01"], "开盘": [10], "收盘": [10],
             "最高": [11], "最低": [9], "成交量": [123]}
        )
        normalized = qf.DataFetcher._normalize_provider_volume(frame, "Eastmoney")
        self.assertEqual(float(normalized["成交量"].iloc[0]), 12_300.0)
        self.assertEqual(normalized.attrs["volume_unit"], "shares")

    def test_sina_share_volume_is_not_scaled(self) -> None:
        frame = pd.DataFrame(
            {"date": ["2026-01-01"], "open": [10], "close": [10],
             "high": [11], "low": [9], "volume": [12_300]}
        )
        normalized = qf.DataFetcher._normalize_provider_volume(frame, "Sina")
        self.assertEqual(float(normalized["volume"].iloc[0]), 12_300.0)

    def test_legacy_cache_without_unit_contract_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "300308.csv"
            path.write_text("date,open,close,high,low,volume\n2026-01-01,1,1,1,1,10\n")
            self.assertFalse(qf.DataFetcher._cache_has_share_volume_contract(path))
            qf.DataFetcher._write_cache_contract(path)
            self.assertTrue(qf.DataFetcher._cache_has_share_volume_contract(path))


class RegimeFreshnessAndProtectionTests(unittest.TestCase):
    def test_stale_stock_is_not_selected_as_a_leader(self) -> None:
        dates = pd.bdate_range("2022-01-03", periods=260)
        frame = pd.DataFrame(
            {"date": dates, "open": 10.0, "close": range(10, 270),
             "high": range(11, 271), "low": 9.0, "volume": 1_000_000}
        )
        with tempfile.TemporaryDirectory() as directory:
            frame.to_csv(Path(directory) / "300308.csv", index=False)
            selection = ra.select_positive_momentum_leaders(
                ("300308",), data_dir=directory, as_of="2024-12-31"
            )
        self.assertEqual(selection.selected_symbols, ())

    def test_weak_entry_has_nonzero_disaster_stop(self) -> None:
        dates = pd.bdate_range("2026-01-01", periods=30)
        frame = pd.DataFrame(
            {"open": 100.0, "close": 100.0, "high": 101.0,
             "low": 99.0, "volume": 1_000_000}, index=dates
        )
        strategy = ra.PositiveMomentumHoldStrategy(
            {"strategy_weight": 0.5, "risk_pct": 0.03,
             "atr_multiplier": 2.0, "max_units": 1}
        )
        context = qf.BarContext(
            i=29, df=frame, current_assets=2_000_000,
            indicators={"atr": pd.Series(2.0, index=dates)},
            symbol="300308", date=str(dates[-1].date()),
        )
        signal = strategy.on_bar(context)
        self.assertIsNotNone(signal)
        self.assertGreater(signal.stop_loss, 0.0)
        self.assertLess(signal.stop_loss, signal.price)


class AccountEngineTests(unittest.TestCase):
    def test_account_parser_rejects_invalid_position(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "account.json"
            path.write_text(json.dumps({"cash": 1, "positions": {"300308": {"shares": 0, "avg_cost": 10}}}))
            with self.assertRaises(ValueError):
                account.load_account_snapshot(path)

    def test_account_parser_accepts_zero_cash_full_investment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "account.json"
            path.write_text(json.dumps({"cash": 0, "peak_equity": 2_000_000,
                                        "positions": {"300308": {"shares": 900, "avg_cost": 100}}}))
            snapshot = account.load_account_snapshot(path)
            self.assertEqual(snapshot.cash, 0.0)
            self.assertEqual(snapshot.positions[0].shares, 900)


if __name__ == "__main__":
    unittest.main()
