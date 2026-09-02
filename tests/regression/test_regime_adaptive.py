"""Regression and causality tests for the regime-adaptive deployment layer."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from quantfusion.application import daily_scan as daily
import quantfusion.regime.evidence as regime_evidence
from quantfusion.config.paths import MARKET_DATA_DIR, PROJECT_ROOT, REGIME_DATA_DIR
from quantfusion.config.regime import REGIME_INDEX_FILES
from quantfusion.domain.models import Position
from quantfusion.engine import replay as ra
from quantfusion.engine.core import CoreBacktestEngine


ROOT = PROJECT_ROOT
DATA_DIR = REGIME_DATA_DIR
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
            for code in REGIME_INDEX_FILES.values():
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

        with patch.object(regime_evidence, "_local_frame", side_effect=with_future):
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
    def test_daily_entrypoint_has_no_process_global_cache_configuration(self) -> None:
        self.assertFalse(hasattr(daily.qf.DataFetcher, "_cache_dir"))
        with patch.object(daily, "_run_main", return_value=0) as run:
            self.assertEqual(daily.main(), 0)
        run.assert_called_once_with()

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
        with self.assertRaisesRegex(ValueError, "auto, replay, trend, or weak"):
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
        self.assertEqual(result["deployment_policy"], "production_daily_replay")
        self.assertEqual(result["selected_symbols"], sorted(symbols))
        # Weak-route positions now participate in portfolio/sector liquidation;
        # keep the corrected execution path as an exact frozen regression.
        self.assertAlmostEqual(result["total_return"], 0.5222755503315, places=12)
        self.assertAlmostEqual(
            result["max_drawdown"], -0.17219488006814201, places=12
        )
        self.assertEqual(result["total_trades"], 18)
        self.assertEqual(result["sleeve_fill_count"], 18)
        replay = result["production_replay"]
        self.assertEqual(replay["engine"], "ProductionReplayEngine")
        self.assertGreater(len(replay["daily_journal"]), 200)
        self.assertEqual(replay["daily_journal"][0]["route"], "weak")
        json.dumps(replay, allow_nan=False)
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
        self.assertEqual(result["deployment_policy"], "production_daily_replay")
        self.assertAlmostEqual(result["total_return"], 5.308949754885, places=12)
        self.assertAlmostEqual(result["max_drawdown"], -0.1834136674871038, places=12)
        self.assertEqual(result["total_trades"], 24)
        self.assertEqual(result["sleeve_fill_count"], 24)
        self.assertEqual(result["date_symbol_side_count"], 5)


class DynamicRouteStateMachineTests(unittest.TestCase):
    """Report 3.3/3.4: the daily route state machine is low-frequency,
    causally consistent, and shared by the current-day decision and the
    audited ``route_sequence``."""

    def _transitions(self, steps) -> int:
        return sum(
            1 for i in range(1, len(steps)) if steps[i].route != steps[i - 1].route
        )

    def test_outer_strategy_is_liquidatable_without_double_registration(self) -> None:
        engine = CoreBacktestEngine(1_000_000)
        strategy = ra.PositiveMomentumHoldStrategy(engine._default_config())
        position = Position(
            symbol="300308",
            strategy_name=strategy.name,
            shares=100,
            entry_price=10.0,
            entry_date="2026-01-05",
        )
        strategy.position = position
        engine.positions = {"300308": {strategy.name: position}}
        engine.strategy_instances = {"300308": []}
        engine.external_strategy_instances = {"300308": [strategy]}
        signals = engine._generate_liquidation_signals("2026-01-06")
        self.assertEqual(len(signals), 1)
        self.assertIs(signals[0][1], strategy)
        self.assertNotIn(strategy, engine.strategy_instances["300308"])

    def test_bull_window_route_is_low_frequency_and_leads_trend(self) -> None:
        steps = ra.simulate_route_sequence(
            DATA_DIR, start_date="2025-04-01", end_date="2026-06-30"
        )
        self.assertGreater(len(steps), 200)
        # Anti-churn: a ~15-month bull window must not switch repeatedly.
        self.assertLessEqual(self._transitions(steps), 8)
        trend_days = sum(
            1
            for s in steps
            if s.route in ("trend", "transition_to_trend")
        )
        self.assertGreater(trend_days / len(steps), 0.5)

    def test_bear_window_leads_weak_or_cash(self) -> None:
        steps = ra.simulate_route_sequence(
            DATA_DIR, start_date="2024-01-02", end_date="2024-12-31"
        )
        trend_days = sum(
            1
            for s in steps
            if s.route in ("trend", "transition_to_trend")
        )
        self.assertLess(trend_days / len(steps), 0.35)

    def test_route_sequence_is_emitted_and_current_day_route_is_consistent(self) -> None:
        engine = ra.RegimeAdaptiveBacktestEngine()
        result = engine.run(
            {"300308": "中际旭创", "300394": "天孚通信"},
            "2025-04-01",
            "2026-06-30",
            data_dir=str(MARKET_DATA_DIR),
            regime_data_dir=str(DATA_DIR),
            indicator_state="warm",
        )
        route_seq = result.get("route_sequence", [])
        self.assertGreater(len(route_seq), 200)
        self.assertRegex(route_seq[0]["route"], r"^(trend|weak|cash|transition)")
        # The current-day route must be one of the enum values.
        current = engine.decide_current(
            {"300308": "中际旭创", "300394": "天孚通信"},
            as_of="2026-06-30",
            data_dir=DATA_DIR,
        )
        self.assertIn(current.name, {"frozen_trend_engine", "positive_momentum_hold", "cash_preservation"})


if __name__ == "__main__":
    unittest.main()
