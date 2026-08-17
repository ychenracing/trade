"""Explicit market-data cache context contracts."""

from __future__ import annotations

import inspect
import unittest
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
