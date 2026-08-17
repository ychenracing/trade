"""Universe stress-scenario generation and summary tests."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import stress_test_prefixes as stress


class StressScenarioTests(unittest.TestCase):
    def test_scenarios_cover_every_requested_family_deterministically(self) -> None:
        first = stress._scenarios(
            random_samples=2, permutation_samples=2, seed=20260807
        )
        second = stress._scenarios(
            random_samples=2, permutation_samples=2, seed=20260807
        )
        self.assertEqual(first, second)
        counts: dict[str, int] = {}
        for item in first:
            kind = str(item["scenario_type"])
            counts[kind] = counts.get(kind, 0) + 1
        self.assertEqual(counts["prefix"], len(stress.ORDERED_CODES))
        self.assertEqual(counts["leave_one_out"], len(stress.ORDERED_CODES))
        self.assertEqual(counts["add_one"], 39)
        self.assertEqual(counts["random_subset"], 10)
        self.assertEqual(counts["permutation"], 2)

    def test_summary_reports_return_drawdown_and_trade_tails(self) -> None:
        summary = stress._summary(
            [
                {"total_return": 1.0, "max_drawdown": -0.10, "total_trades": 10},
                {"total_return": 3.0, "max_drawdown": -0.20, "total_trades": 30},
            ]
        )
        self.assertEqual(summary["scenario_count"], 2)
        self.assertEqual(summary["return_median"], 2.0)
        self.assertEqual(summary["drawdown_worst"], -0.20)
        self.assertEqual(summary["trades_worst"], 30.0)

    def test_impossible_sample_count_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unique subset capacity"):
            stress._scenarios(
                random_samples=2_000,
                permutation_samples=1,
                seed=20260807,
            )

    def test_multi_seed_plan_keeps_fixed_scenarios_singleton(self) -> None:
        scenarios = stress._multi_seed_scenarios(
            random_samples=2,
            permutation_samples=2,
            seeds=(1, 2),
        )
        self.assertEqual(len(scenarios), 83 + 2 * (10 + 2))
        self.assertEqual(
            len([item for item in scenarios if item["scenario_type"] == "prefix"]),
            len(stress.ORDERED_CODES),
        )

    def test_multi_seed_plan_rejects_duplicate_seed_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "unique"):
            stress._multi_seed_scenarios(
                random_samples=1,
                permutation_samples=1,
                seeds=(7, 7),
            )

    def test_checkpoint_rejects_mismatched_scenario_definition(self) -> None:
        scenarios = stress._multi_seed_scenarios(
            random_samples=1,
            permutation_samples=1,
            seeds=(7,),
        )
        result = {
            **scenarios[0],
            "symbols": ["tampered"],
            "total_return": 1.0,
            "max_drawdown": -0.1,
            "sharpe": 1.0,
            "calmar": 1.0,
            "total_trades": 1,
            "sleeve_fill_count": 1,
            "deployment_policy": "production_daily_replay",
        }
        with self.assertRaisesRegex(ValueError, "definition changed"):
            stress._validated_checkpoint_results(
                {"results": [result]}, scenarios
            )

    def test_run_signature_fingerprints_canonical_package_sources(self) -> None:
        captured: list[list[Path]] = []

        def fingerprint(paths: list[Path]) -> str:
            captured.append(paths)
            return "fingerprint"

        with patch.object(stress, "_tree_fingerprint", side_effect=fingerprint):
            stress._run_signature([], stress.DATA_DIR, stress.REGIME_DATA_DIR)
        source_paths = {path.relative_to(stress.ROOT).as_posix() for path in captured[0]}
        self.assertIn("quantfusion/engine/core.py", source_paths)
        self.assertIn("quantfusion/risk/overlay/policy.py", source_paths)


if __name__ == "__main__":
    unittest.main()
