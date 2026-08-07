"""Universe stress-scenario generation and summary tests."""

from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
