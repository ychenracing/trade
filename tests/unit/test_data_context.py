"""Explicit market-data cache context contracts."""

from __future__ import annotations

import contextlib
import inspect
import tempfile
import unittest
from unittest import mock

from quantfusion.data.contracts import refresh_regime_indices
from quantfusion.data.providers import DataFetcher


class DataContextContracts(unittest.TestCase):
    """Cache selection is request-scoped, never process-global."""

    def test_loader_accepts_explicit_cache_directory(self) -> None:
        self.assertIn("cache_dir", inspect.signature(DataFetcher.load_stock_data).parameters)

    def test_fetcher_has_no_mutable_cache_directory(self) -> None:
        self.assertFalse(hasattr(DataFetcher, "_cache_dir"))

    def test_explicit_cache_is_forwarded_to_cache_loader(self) -> None:
        with mock.patch.object(
            DataFetcher, "_load_with_cache", side_effect=RuntimeError("sentinel")
        ) as loader:
            with self.assertRaisesRegex(RuntimeError, "sentinel"):
                DataFetcher.load_stock_data(
                    "300308",
                    "2026-01-01",
                    "2026-01-02",
                    cache_dir="cache",
                )
        loader.assert_called_once_with(
            "300308", "2026-01-01", "2026-01-02", "cache"
        )

    def test_user_supplied_market_data_name_remains_a_literal_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory, contextlib.chdir(directory):
            with self.assertRaisesRegex(
                FileNotFoundError,
                r"Missing local market-data file: market_data/300308\.csv$",
            ):
                DataFetcher.load_stock_data(
                    "300308",
                    "2025-04-01",
                    "2025-04-10",
                    data_dir="market_data",
                )

    def test_user_supplied_historical_data_name_remains_a_literal_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory, contextlib.chdir(directory):
            with self.assertRaisesRegex(
                RuntimeError,
                r"missing frozen regime-index files: 000300, 000682$",
            ):
                refresh_regime_indices(
                    "historical_data", end_date="2020-01-01", strict=True
                )


if __name__ == "__main__":
    unittest.main()
