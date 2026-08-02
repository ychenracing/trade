"""Regression and causality tests for the regime-adaptive deployment layer."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import daily_signal_scan as daily
import regime_adaptive as ra


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "historical_data"
MARKET_DATA_DIR = ROOT / "market_data"
AI_SYMBOLS = (
    "002409",
    "300054",
    "300308",
    "300394",
    "300502",
    "300604",
    "300666",
    "603986",
    "688008",
    "688072",
    "688082",
    "688120",
    "688256",
    "688300",
    "688347",
    "688361",
)


class RegimeEvidenceTests(unittest.TestCase):
    def test_known_2024_boundary_is_choppy(self) -> None:
        evidence = ra.detect_regime(DATA_DIR, as_of="2023-12-29")
        self.assertEqual(evidence.regime, "choppy")
        self.assertEqual(len(evidence.observations), 2)
        self.assertTrue(all(item.observed_date <= evidence.as_of for item in evidence.observations))

    def test_known_bull_boundary_is_trending(self) -> None:
        evidence = ra.detect_regime(DATA_DIR, as_of="2025-03-31")
        self.assertEqual(evidence.regime, "trending")
        self.assertTrue(all(item.trending for item in evidence.observations))

    def test_stale_fixed_index_evidence_fails_closed(self) -> None:
        dates = pd.bdate_range("2023-01-02", periods=65)
        frame = pd.DataFrame(
            {
                "date": dates,
                "open": range(100, 165),
                "high": range(101, 166),
                "low": range(99, 164),
                "close": range(100, 165),
                "volume": 1_000_000,
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            for code in ra.REGIME_INDEX_FILES.values():
                frame.to_csv(Path(directory) / f"{code}.csv", index=False)
            evidence = ra.detect_regime(directory, as_of="2023-12-29")
        self.assertEqual(evidence.regime, "unknown")
        self.assertEqual(evidence.observations, ())


class LeaderSelectionTests(unittest.TestCase):
    def test_frozen_boundary_selects_three_positive_leaders(self) -> None:
        selection = ra.select_positive_momentum_leaders(
            AI_SYMBOLS, data_dir=DATA_DIR, as_of="2023-12-29"
        )
        self.assertEqual(
            selection.selected_symbols, ("300308", "300394", "300502")
        )
        self.assertTrue(all(value > 0 for value in selection.selected_returns))

    def test_future_rows_cannot_change_boundary_selection(self) -> None:
        before = ra.select_positive_momentum_leaders(
            ("300308", "300394"), data_dir=DATA_DIR, as_of="2023-12-29"
        )
        original = ra._local_frame

        def with_future(data_dir: str | Path, code: str, end_date: str) -> pd.DataFrame:
            frame = original(data_dir, code, "2024-12-31")
            frame.loc[frame.index > pd.Timestamp("2023-12-29"), "close"] *= 1000
            return frame.loc[frame.index <= pd.Timestamp(end_date)]

        with patch.object(ra, "_local_frame", side_effect=with_future):
            after = ra.select_positive_momentum_leaders(
                ("300308", "300394"), data_dir=DATA_DIR, as_of="2023-12-29"
            )
        self.assertEqual(before, after)

    def test_duplicate_symbols_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicates"):
            ra.select_positive_momentum_leaders(
                ("300308", "300308"), data_dir=DATA_DIR, as_of="2023-12-29"
            )

    def test_no_positive_observation_means_cash(self) -> None:
        engine = ra.RegimeAdaptiveBacktestEngine()
        decision = engine.decide(
            {"688008": "澜起科技"},
            start_date="2024-01-02",
            data_dir=DATA_DIR,
        )
        self.assertEqual(decision.name, "cash_preservation")
        self.assertEqual(decision.leaders.selected_symbols, ())


class AdaptiveEngineTests(unittest.TestCase):
    def test_daily_entrypoint_restores_global_cache_configuration(self) -> None:
        previous = daily.qf.DataFetcher._cache_dir

        def mutate_cache() -> int:
            daily.qf.DataFetcher._cache_dir = "temporary-test-cache"
            return 0

        try:
            daily.qf.DataFetcher._cache_dir = "existing-cache"
            with patch.object(daily, "_run_main", side_effect=mutate_cache):
                self.assertEqual(daily.main(), 0)
            self.assertEqual(daily.qf.DataFetcher._cache_dir, "existing-cache")
        finally:
            daily.qf.DataFetcher._cache_dir = previous

    def test_selection_boundary_must_precede_start(self) -> None:
        engine = ra.RegimeAdaptiveBacktestEngine()
        with self.assertRaisesRegex(ValueError, "before start_date"):
            engine.decide(
                {"300308": "中际旭创"},
                start_date="2024-01-02",
                selection_boundary="2024-01-02",
                data_dir=DATA_DIR,
            )

    def test_invalid_deployment_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "auto, trend, or weak"):
            ra.RegimeAdaptiveBacktestEngine().run(
                {"300308": "中际旭创"},
                "2024-01-02",
                "2024-01-31",
                data_dir=str(DATA_DIR),
                deployment_mode="oracle",
            )

    def test_late_listing_is_filtered_without_discarding_pool(self) -> None:
        available = ra.RegimeAdaptiveBacktestEngine._available_local_symbols(
            {"300308": "中际旭创", "688347": "华虹宏力"},
            data_dir=str(DATA_DIR),
            start_date="2023-01-03",
            end_date="2023-12-29",
            warmup_calendar_days=365,
        )
        self.assertEqual(available, {"300308": "中际旭创"})

    def test_weak_real_execution_matches_frozen_research_point(self) -> None:
        symbols = {
            "002409": "雅克科技",
            "300054": "鼎龙股份",
            "300308": "中际旭创",
            "300394": "天孚通信",
            "300502": "新易盛",
        }
        result = ra.RegimeAdaptiveBacktestEngine().run(
            symbols,
            "2024-01-02",
            "2024-12-31",
            data_dir=str(DATA_DIR),
            indicator_state="warm",
        )
        self.assertEqual(result["deployment_policy"], "positive_momentum_hold")
        self.assertEqual(result["selected_symbols"], ["300308", "300394", "300502"])
        self.assertAlmostEqual(result["total_return"], 0.5007309711617499, places=12)
        self.assertAlmostEqual(result["max_drawdown"], -0.17219488006814201, places=12)
        self.assertEqual(result["total_trades"], 6)
        self.assertLessEqual(len(result["selected_symbols"]), ra.MAX_LEADERS)
        json.dumps(result["deployment_decision"], allow_nan=False)
        for trade in result["trades"]:
            signal_date = getattr(trade, "signal_date", None)
            if signal_date:
                self.assertLess(signal_date, trade.date)

    def test_frozen_bull_single_symbol_golden_metric(self) -> None:
        result = ra.RegimeAdaptiveBacktestEngine().run(
            {"300308": "中际旭创"},
            "2025-04-01",
            "2026-06-30",
            data_dir=str(MARKET_DATA_DIR),
            regime_data_dir=str(DATA_DIR),
            indicator_state="warm",
        )
        self.assertEqual(result["deployment_policy"], "frozen_trend_engine")
        self.assertAlmostEqual(result["total_return"], 5.308949754885, places=12)
        self.assertAlmostEqual(result["max_drawdown"], -0.1834136674871038, places=12)
        self.assertEqual(result["total_trades"], 24)


if __name__ == "__main__":
    unittest.main()
