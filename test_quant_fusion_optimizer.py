#!/usr/bin/env python3
"""Fast tests for the v17 walk-forward selection layer."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

import quant_fusion_optimizer as optimizer


def metrics(
    window: optimizer.DateWindow,
    annual_return: float,
    drawdown: float,
    *,
    stressed: bool = False,
) -> optimizer.WindowMetrics:
    """Build minimal deterministic metrics for selection tests."""
    return optimizer.WindowMetrics(
        window=window.name,
        role=window.role,
        symbols=("300308",),
        stressed=stressed,
        total_return=annual_return / 2,
        annual_return=annual_return,
        max_drawdown=drawdown,
        sharpe=annual_return / max(abs(drawdown), 0.01),
        calmar=annual_return / max(abs(drawdown), 0.01),
        total_trades=10,
        final_assets=2_000_000 * (1 + annual_return / 2),
    )


class FakeRunner:
    """Return role-specific metrics and record when the holdout is revealed."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []

    def run(
        self,
        candidate: optimizer.Candidate,
        window: optimizer.DateWindow,
        *,
        stress: bool = False,
    ) -> optimizer.WindowMetrics:
        self.calls.append((candidate.candidate_id, window.role, stress))
        stronger = bool(candidate.symbol_multipliers)
        if window.role == "test":
            # The baseline wins the holdout, but that must not change selection.
            annual = 0.20 if stronger else 0.80
        elif window.role == "validation":
            annual = 0.70 if stronger else 0.30
        else:
            annual = 0.75 if stronger else 0.35
        return metrics(window, annual, -0.12 if not stress else -0.14, stressed=stress)


class CandidateTests(unittest.TestCase):
    def test_hard_portfolio_limits_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            optimizer.Candidate(engine_overrides={"max_positions": 7})
        with self.assertRaises(ValueError):
            optimizer.Candidate(engine_overrides={"max_positions": 5.5})
        with self.assertRaises(ValueError):
            optimizer.Candidate(engine_overrides={"max_symbol_weight": 0.61})
        with self.assertRaises(ValueError):
            optimizer.Candidate(engine_overrides={"max_total_weight": 1.01})

    def test_route_preserving_scaling_is_deterministic(self) -> None:
        candidate = optimizer.Candidate(
            symbol_multipliers={"entry_period": 1.15, "risk_pct": 0.8}
        )
        symbols = {"300308": "中际旭创", "688072": "拓荆科技"}
        first = candidate.per_symbol_config(symbols)
        second = candidate.per_symbol_config(symbols)
        self.assertEqual(first, second)
        self.assertNotEqual(
            first["300308"]["entry_period"], first["688072"]["entry_period"]
        )
        self.assertLessEqual(first["300308"]["max_symbol_weight"], 0.60)

    def test_parameter_sampling_is_reproducible_and_keeps_baseline(self) -> None:
        space = optimizer.ParameterSpace.default()
        first = [item.as_dict() for item in space.candidates(8, 17)]
        second = [item.as_dict() for item in space.candidates(8, 17)]
        self.assertEqual(first, second)
        self.assertEqual(first[0]["candidate_id"], "baseline")
        self.assertEqual(len({item["candidate_id"] for item in first}), len(first))

    def test_default_space_keeps_risk_thresholds_in_valid_bundles(self) -> None:
        candidates = optimizer.ParameterSpace.default().candidates(22, 17)
        risk_candidates = [
            item for item in candidates if "confirmed_drawdown" in item.policy_overrides
        ]
        self.assertEqual(len(risk_candidates), 2)
        for candidate in risk_candidates:
            policy = candidate.policy()
            self.assertLess(
                policy.drawdown_alert,
                policy.confirmed_drawdown,
            )
            self.assertLess(policy.confirmed_drawdown, policy.emergency_drawdown)
            self.assertGreaterEqual(policy.terminal_drawdown, policy.confirmed_drawdown)

    def test_pair_stage_prioritizes_supported_defensive_interactions(self) -> None:
        candidates = optimizer.ParameterSpace.default().candidates(32, 17)
        self.assertTrue(
            any(
                candidate.policy_overrides.get("confirmed_drawdown") == 0.18
                and candidate.symbol_multipliers.get("trail_atr_mult") == 0.85
                for candidate in candidates
            )
        )

    def test_local_triple_stage_has_two_axis_neighbor_support(self) -> None:
        candidates = optimizer.ParameterSpace.default().candidates(40, 17)
        target = next(
            candidate
            for candidate in candidates
            if candidate.policy_overrides.get("confirmed_drawdown") == 0.18
            and candidate.engine_overrides.get("max_positions") == 3
            and candidate.symbol_multipliers.get("risk_pct") == 0.8
        )
        self.assertTrue(
            any(
                candidate.policy_overrides.get("confirmed_drawdown") == 0.18
                and candidate.symbol_multipliers.get("risk_pct") == 0.8
                and "max_positions" not in candidate.engine_overrides
                for candidate in candidates
                if candidate.candidate_id != target.candidate_id
            )
        )


class WindowTests(unittest.TestCase):
    def test_overlapping_validation_folds_are_rejected(self) -> None:
        calendar = pd.bdate_range("2024-01-02", "2026-07-20")
        with self.assertRaisesRegex(ValueError, "do not overlap"):
            optimizer.build_walk_forward_folds(
                calendar,
                start="2024-01-02",
                test_start="2026-01-05",
                end="2026-07-20",
                train_months=12,
                validation_months=6,
                step_months=3,
            )

    def test_folds_end_before_holdout_and_do_not_overlap_roles(self) -> None:
        calendar = pd.bdate_range("2024-01-02", "2026-07-20")
        folds, holdout = optimizer.build_walk_forward_folds(
            calendar,
            start="2024-01-02",
            test_start="2026-01-05",
            end="2026-07-20",
            train_months=12,
            validation_months=6,
            step_months=6,
        )
        self.assertGreaterEqual(len(folds), 2)
        self.assertTrue(all(fold.validation.end < holdout.start for fold in folds))
        self.assertTrue(all(fold.train.end < fold.validation.start for fold in folds))

    def test_catalog_excludes_a_stock_before_its_listing_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for code, dates in {
                "300308": [
                    "2024-01-02",
                    "2024-01-03",
                    "2024-01-04",
                    "2024-01-05",
                    "2024-01-08",
                ],
                "920045": [
                    "2025-12-31",
                    "2026-01-05",
                    "2026-01-06",
                    "2026-01-07",
                    "2026-01-08",
                ],
            }.items():
                pd.DataFrame({"date": dates}).to_csv(root / f"{code}.csv", index=False)
            catalog = optimizer.LocalDataCatalog(
                root,
                {"300308": "中际旭创", "920045": "蘅东光"},
                (),
                min_window_rows=2,
            )
            earlier = optimizer.DateWindow(
                "earlier", "2024-01-02", "2024-01-08", "train"
            )
            later = optimizer.DateWindow("later", "2025-12-31", "2026-01-08", "test")
            self.assertEqual(set(catalog.available_symbols(earlier)), {"300308"})
            self.assertEqual(set(catalog.available_symbols(later)), {"920045"})


class SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folds = [
            optimizer.WalkForwardFold(
                "fold_1",
                optimizer.DateWindow("train_1", "2024-01-02", "2024-06-28", "train"),
                optimizer.DateWindow(
                    "validation_1", "2024-07-01", "2024-09-30", "validation"
                ),
            ),
            optimizer.WalkForwardFold(
                "fold_2",
                optimizer.DateWindow("train_2", "2024-01-02", "2024-09-30", "train"),
                optimizer.DateWindow(
                    "validation_2", "2024-10-01", "2024-12-31", "validation"
                ),
            ),
        ]
        self.holdout = optimizer.DateWindow(
            "holdout", "2025-01-02", "2025-06-30", "test"
        )

    def test_holdout_cannot_change_the_selected_candidate(self) -> None:
        runner = FakeRunner()
        process = optimizer.WalkForwardOptimizer(runner, self.folds, self.holdout)
        stronger = optimizer.Candidate(symbol_multipliers={"risk_pct": 0.8})
        report = process.optimize([optimizer.Candidate.baseline(), stronger])
        self.assertEqual(
            report["selected_candidate"]["candidate_id"], stronger.candidate_id
        )
        self.assertEqual(report["recommended_candidate"]["candidate_id"], "baseline")
        self.assertEqual(report["status"], "candidate_rejected_on_holdout")
        first_test_call = next(
            index for index, call in enumerate(runner.calls) if call[1] == "test"
        )
        self.assertTrue(
            all(call[1] != "test" for call in runner.calls[:first_test_call])
        )
        self.assertFalse(
            report["selection_protocol"]["test_data_used_for_parameter_selection"]
        )
        self.assertLess(
            report["holdout_comparison"]["selected"]["total_return"],
            report["holdout_comparison"]["baseline"]["total_return"],
        )

    def test_drawdown_limit_rejects_an_otherwise_high_return_candidate(self) -> None:
        window = self.folds[0].validation
        candidate = optimizer.Candidate(symbol_multipliers={"risk_pct": 1.2})
        evaluation = optimizer.CandidateEvaluation(
            candidate,
            [metrics(self.folds[0].train, 0.9, -0.19)],
            [metrics(window, 0.9, -0.21)],
            [metrics(window, 0.8, -0.22, stressed=True)],
            0.20,
        )
        self.assertFalse(evaluation.feasible)
        self.assertEqual(optimizer.pareto_frontier([evaluation]), [])

    def test_isolated_two_axis_candidate_cannot_win(self) -> None:
        runner = FakeRunner()
        process = optimizer.WalkForwardOptimizer(runner, self.folds, self.holdout)
        isolated = optimizer.Candidate(
            engine_overrides={"max_positions": 5},
            symbol_multipliers={"risk_pct": 0.8},
        )
        report = process.optimize([optimizer.Candidate.baseline(), isolated])
        self.assertEqual(report["selected_candidate"]["candidate_id"], "baseline")
        isolated_result = next(
            item
            for item in report["evaluations"]
            if item["candidate"]["candidate_id"] == isolated.candidate_id
        )
        self.assertFalse(isolated_result["summary"]["parameter_supported"])

    def test_completed_candidate_evaluations_are_reused_from_cache(self) -> None:
        candidate = optimizer.Candidate(symbol_multipliers={"risk_pct": 0.8})
        with tempfile.TemporaryDirectory() as directory:
            first_runner = FakeRunner()
            first = optimizer.WalkForwardOptimizer(
                first_runner, self.folds, self.holdout
            )
            first.optimize(
                [optimizer.Candidate.baseline(), candidate], cache_dir=directory
            )
            second_runner = FakeRunner()
            second = optimizer.WalkForwardOptimizer(
                second_runner, self.folds, self.holdout
            )
            second.optimize(
                [optimizer.Candidate.baseline(), candidate], cache_dir=directory
            )
        self.assertTrue(second_runner.calls)
        self.assertTrue(all(call[1] == "test" for call in second_runner.calls))


if __name__ == "__main__":
    unittest.main()
