"""Regression tests for fail-closed adaptive routing and weak-market risk."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import account_signal_engine as account
import regime_adaptive as ra
from quantfusion.config.paths import MARKET_DATA_DIR, REGIME_DATA_DIR
from quantfusion.engine import replay as replay_module


class UnknownEvidenceTests(unittest.TestCase):
    """Incomplete fixed-index evidence must never create new exposure."""

    @staticmethod
    def _unknown() -> ra.RegimeEvidence:
        return ra.RegimeEvidence(
            as_of="2026-01-30",
            regime="unknown",
            observations=(),
        )

    def test_decision_holds_cash_without_leader_selection(self) -> None:
        engine = ra.RegimeAdaptiveBacktestEngine()
        with (
            patch.object(replay_module, "detect_regime", return_value=self._unknown()),
            patch.object(
                replay_module,
                "select_positive_momentum_leaders",
                side_effect=AssertionError(
                    "leader selection must not run without index evidence"
                ),
            ),
        ):
            decision = engine.decide(
                {"300308": "中际旭创"},
                start_date="2026-02-02",
                data_dir=Path("regime"),
            )
        self.assertEqual(decision.name, "cash_preservation")
        self.assertIsNone(decision.leaders)

    def test_account_mode_emits_no_buy_candidate(self) -> None:
        snapshot = account.AccountSnapshot(
            schema_version=3,
            account_id="main",
            snapshot_date="2026-01-30",
            cash=1_000_000.0,
            peak_equity=1_000_000.0,
            positions=(),
        )
        engine = account.AccountSignalEngine(
            cache_dir="cache",
            regime_data_dir="regime",
        )
        with (
            patch.object(
                account.market_data_contracts,
                "refresh_regime_indices",
                return_value={},
            ),
            patch.object(replay_module, "detect_regime", return_value=self._unknown()),
            patch.object(
                replay_module,
                "select_positive_momentum_leaders",
                side_effect=AssertionError(
                    "leader selection must not run without index evidence"
                ),
            ),
        ):
            result = engine.run(
                snapshot,
                {"300308": "中际旭创"},
                as_of="2026-01-30",
            )
        self.assertEqual(
            result["deployment_decision"]["name"],
            "cash_preservation",
        )
        self.assertFalse(
            any(item["action"] == "BUY_CANDIDATE" for item in result["actions"])
        )


class WeakRiskTests(unittest.TestCase):
    """The weak route must retain a real portfolio-level loss boundary."""

    def test_weak_policy_has_effective_drawdown_limits(self) -> None:
        policy = ra._weak_regime_policy()
        self.assertLessEqual(policy.drawdown_alert, 0.15)
        self.assertLessEqual(policy.confirmed_drawdown, 0.20)
        self.assertLessEqual(policy.emergency_drawdown, 0.23)
        self.assertLessEqual(policy.terminal_drawdown, 0.26)
        self.assertLessEqual(ra._weak_regime_config(3)["daily_loss_limit"], 0.12)


class UniverseIntegrityTests(unittest.TestCase):
    """Missing data must be explicit instead of silently changing the universe."""

    def test_trend_route_rejects_silent_universe_shrink(self) -> None:
        engine = ra.RegimeAdaptiveBacktestEngine()
        trending = ra.RegimeEvidence(
            as_of="2025-03-31",
            regime="trending",
            observations=(),
        )
        with (
            patch.object(replay_module, "detect_regime", return_value=trending),
            patch.object(
                engine,
                "_available_local_symbols",
                return_value={"300308": "中际旭创"},
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "300502"):
                engine.run(
                    {"300308": "中际旭创", "300502": "新易盛"},
                    "2025-04-01",
                    "2025-04-30",
                    data_dir=str(MARKET_DATA_DIR),
                    regime_data_dir=str(REGIME_DATA_DIR),
                )

    def test_unavailable_does_not_mean_unselected(self) -> None:
        selection = ra.LeaderSelection(
            as_of="2024-01-01",
            requested_symbols=("300308", "300502"),
            observed_symbols=2,
            selected_symbols=("300308",),
            selected_returns=(0.2,),
            unavailable_symbols=(),
        )
        self.assertEqual(selection.unavailable_symbols, ())
        self.assertNotIn("300502", selection.unavailable_symbols)

    def test_unavailable_override_requires_real_boolean(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be bool"):
            ra.RegimeAdaptiveBacktestEngine().run(
                {"300308": "中际旭创"},
                "2025-04-01",
                "2025-04-30",
                data_dir=str(MARKET_DATA_DIR),
                regime_data_dir=str(REGIME_DATA_DIR),
                allow_unavailable_symbols=1,  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
