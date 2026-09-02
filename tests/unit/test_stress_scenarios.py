"""Universe stress-scenario generation and summary tests."""

# Existing nested unittest contexts are intentionally kept stable in this refactor.
# ruff: noqa: SIM117

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from quantfusion.application import (
    stress,
    stress_artifacts,
    stress_metrics,
    stress_scenarios,
)
from quantfusion.application.stress_scenarios import _scenarios, select_scenarios


class StressScenarioTests(unittest.TestCase):
    @staticmethod
    def _provenance(revision: str, scenario_count: int) -> dict[str, object]:
        return {
            "source_revision": revision,
            "source_fingerprint": "source",
            "data_fingerprint": "data",
            "scenario_signature": "scenario",
            "run_signature": "run",
            "scenario_count": scenario_count,
            "start_date": "2025-04-01",
            "end_date": "2026-07-20",
            "initial_capital": 2_000_000.0,
            "engine": "ProductionReplayEngine",
            "deployment_policy": "production_daily_replay",
        }

    @staticmethod
    def _complete_result(scenario: dict[str, object]) -> dict[str, object]:
        return {
            **scenario,
            "symbol_count": len(scenario["symbols"]),
            "total_return": 1.0,
            "max_drawdown": -0.1,
            "sharpe": 1.0,
            "calmar": 1.0,
            "total_trades": 0,
            "sleeve_fill_count": 0,
            "date_symbol_side_count": 0,
            "reason_attribution": {
                category: 0 for category in stress_metrics.ATTRIBUTION_CATEGORIES
            },
            "max_concurrent_symbols": 0,
            "terminal_risk_lock": False,
            "deployment_policy": "production_daily_replay",
        }

    @classmethod
    def _complete_small_plan(cls) -> list[dict[str, object]]:
        scenarios = stress_scenarios._multi_seed_scenarios(
            random_samples=1,
            permutation_samples=1,
            seeds=(7,),
        )
        return [cls._complete_result(item) for item in scenarios]

    @staticmethod
    def _accepted_current_incumbent(
        results: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "acceptance_status": "accepted",
            "canonical": True,
            "trade_count_semantics": "trade_records",
            "results": results,
        }

    def test_orchestrator_does_not_reexport_moved_implementation(self) -> None:
        for name in (
            "_multi_seed_scenarios",
            "_scenario_signature",
            "_summary",
            "_hard_gates",
            "_promotion_gates",
            "_build_provenance",
            "_validated_checkpoint_results",
            "_publish_formal_artifacts",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(stress, name))

    def test_scenarios_cover_every_requested_family_deterministically(self) -> None:
        first = _scenarios(
            random_samples=2, permutation_samples=2, seed=20260807
        )
        second = _scenarios(
            random_samples=2, permutation_samples=2, seed=20260807
        )
        self.assertEqual(first, second)
        counts: dict[str, int] = {}
        for item in first:
            kind = str(item["scenario_type"])
            counts[kind] = counts.get(kind, 0) + 1
        self.assertEqual(counts["prefix"], len(stress_scenarios.ORDERED_CODES))
        self.assertEqual(counts["leave_one_out"], len(stress_scenarios.ORDERED_CODES))
        self.assertEqual(counts["add_one"], 39)
        self.assertEqual(counts["random_subset"], 10)
        self.assertEqual(counts["permutation"], 2)

    def test_summary_reports_return_drawdown_and_trade_tails(self) -> None:
        summary = stress_metrics._summary(
            [
                {
                    "total_return": 1.0,
                    "max_drawdown": -0.10,
                    "total_trades": 10,
                    "date_symbol_side_count": 10,
                },
                {
                    "total_return": 3.0,
                    "max_drawdown": -0.20,
                    "total_trades": 30,
                    "date_symbol_side_count": 12,
                },
            ]
        )
        self.assertEqual(summary["scenario_count"], 2)
        self.assertEqual(summary["return_median"], 2.0)
        self.assertEqual(summary["drawdown_worst"], -0.20)
        self.assertEqual(summary["trades_worst"], 30.0)
        self.assertEqual(summary["date_symbol_side_buckets_worst"], 12.0)

    def test_summary_does_not_convert_trade_records_to_side_buckets(self) -> None:
        with self.assertRaisesRegex(KeyError, "date_symbol_side_count"):
            stress_metrics._summary(
                [
                    {
                        "total_return": 1.0,
                        "max_drawdown": -0.1,
                        "total_trades": 10,
                    }
                ]
            )

    def test_impossible_sample_count_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unique subset capacity"):
            _scenarios(
                random_samples=2_000,
                permutation_samples=1,
                seed=20260807,
            )

    def test_multi_seed_plan_keeps_fixed_scenarios_singleton(self) -> None:
        scenarios = stress_scenarios._multi_seed_scenarios(
            random_samples=2,
            permutation_samples=2,
            seeds=(1, 2),
        )
        self.assertEqual(len(scenarios), 83 + 2 * (10 + 2))
        self.assertEqual(
            len([item for item in scenarios if item["scenario_type"] == "prefix"]),
            len(stress_scenarios.ORDERED_CODES),
        )

    def test_multi_seed_plan_rejects_duplicate_seed_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "unique"):
            stress_scenarios._multi_seed_scenarios(
                random_samples=1,
                permutation_samples=1,
                seeds=(7, 7),
            )

    def test_formal_plan_has_983_unique_scenario_ids(self) -> None:
        scenarios = stress_scenarios._multi_seed_scenarios(
            random_samples=50,
            permutation_samples=50,
            seeds=stress_scenarios.DEFAULT_SEEDS,
        )

        self.assertEqual(len(scenarios), 983)
        self.assertEqual(
            len({str(item["scenario_id"]) for item in scenarios}),
            983,
        )

    def test_formal_plan_order_and_signature_are_frozen(self) -> None:
        scenarios = stress_scenarios._multi_seed_scenarios(
            random_samples=stress_scenarios.DEFAULT_RANDOM_SAMPLES,
            permutation_samples=stress_scenarios.DEFAULT_PERMUTATION_SAMPLES,
            seeds=stress_scenarios.DEFAULT_SEEDS,
        )
        ordered_ids = [str(item["scenario_id"]) for item in scenarios]
        encoded_ids = json.dumps(ordered_ids, separators=(",", ":")).encode()

        self.assertEqual(
            hashlib.sha256(encoded_ids).hexdigest(),
            "73d6f2bd580490cd6e0bf7af9a72b79af60ef2bd7890774949304d40cb52bb5c",
        )
        self.assertEqual(
            stress_scenarios._scenario_signature(scenarios),
            "ceb116649ced622bd5aa653c6734fbfbb241c4e20853c98939b6689d940ed223",
        )

    def test_gate_and_promotion_payloads_are_frozen(self) -> None:
        results = self._complete_small_plan()

        self.assertEqual(
            stress_metrics._hard_gates(results),
            {
                "passed": True,
                "checks": {
                    "random_p90_drawdown_at_most_20pct": True,
                    "random_worst_drawdown_at_most_22pct": True,
                    "all_worst_drawdown_at_most_22_5pct": True,
                    "prefix_9_to_10_wealth_above_minus_10pct": True,
                    "worst_adjacent_wealth_at_least_minus_30pct": True,
                    "worst_add_one_wealth_at_least_minus_18pct": True,
                    "random_p90_date_symbol_side_buckets_at_most_160": True,
                    "all_date_symbol_side_buckets_at_most_200": True,
                },
                "observed": {
                    "random_p90_drawdown": 0.1,
                    "random_worst_drawdown": -0.1,
                    "all_worst_drawdown": -0.1,
                    "prefix_9_to_10_wealth_change": 0.0,
                    "worst_adjacent_wealth_change": 0.0,
                    "worst_add_one_wealth_change": 0.0,
                    "random_p90_date_symbol_side_buckets": 0.0,
                    "all_worst_date_symbol_side_buckets": 0.0,
                },
            },
        )
        self.assertEqual(
            stress_metrics._promotion_gates(
                results, self._accepted_current_incumbent(results)
            ),
            {
                "baseline": "incumbent_universe_stress",
                "permutation_invariance": {
                    "checked_groups": 1,
                    "worst_deviation": 0.0,
                    "invariant": True,
                },
                "status": "compared",
                "incumbent_scenario_count": 89,
                "shared_scenario_count": 89,
                "tolerances": {
                    "prefix_wealth_ratio": 0.99,
                    "dd_p90": 0.005,
                    "dd_p95": 0.005,
                    "worst_dd": 0.01,
                    "worst_return": 0.02,
                    "add_one_wealth": 0.03,
                    "date_symbol_side_buckets_p90": 5.0,
                    "date_symbol_side_buckets_worst": 10.0,
                    "risk_action_median": 2.0,
                },
                "passed": True,
                "checks": {
                    "permutation_invariant": True,
                    "fixed_prefix_wealth_at_least_99pct": True,
                    "random_dd_p90_not_worse": True,
                    "random_dd_p95_not_worse": True,
                    "random_worst_return_not_worse": True,
                    "random_date_symbol_side_buckets_p90_not_increased": True,
                    "random_risk_actions_not_increased": True,
                    "all_worst_dd_not_significantly_worse": True,
                    "all_worst_date_symbol_side_buckets_not_increased": True,
                    "leave_one_out_worst_return_not_worse": True,
                    "add_one_discontinuity_not_worse": True,
                },
                "observed": {
                    "prefix_wealth_ratio_min": 1.0,
                    "random_dd_p90": 0.1,
                    "random_dd_p95": 0.1,
                    "random_worst_return": 1.0,
                    "random_date_symbol_side_buckets_p90": 0.0,
                    "random_risk_action_orders_median": 0.0,
                    "all_worst_dd": -0.1,
                    "all_worst_date_symbol_side_buckets": 0.0,
                    "leave_one_out_worst_return": 1.0,
                    "worst_add_one_wealth_change": 0.0,
                },
            },
        )

    def test_selects_exact_add_one_scenario_without_changing_definition(self) -> None:
        scenarios = stress_scenarios._multi_seed_scenarios(
            random_samples=1,
            permutation_samples=1,
            seeds=(7,),
        )

        selected, formal_plan_complete = select_scenarios(
            scenarios,
            scenario_id="add-one-05-688205",
            scenario_type=None,
            shard_index=None,
            shard_count=None,
        )

        self.assertEqual(
            selected,
            [
                {
                    "scenario_id": "add-one-05-688205",
                    "scenario_type": "add_one",
                    "base_size": 5,
                    "added_symbol": "688205",
                    "symbols": [
                        "300308",
                        "300502",
                        "300394",
                        "688008",
                        "603986",
                        "688205",
                    ],
                }
            ],
        )
        self.assertFalse(formal_plan_complete)

    def test_selects_add_one_family_in_formal_plan_order(self) -> None:
        scenarios = stress_scenarios._multi_seed_scenarios(
            random_samples=1,
            permutation_samples=1,
            seeds=(7,),
        )

        selected, formal_plan_complete = select_scenarios(
            scenarios,
            scenario_id=None,
            scenario_type="add_one",
            shard_index=None,
            shard_count=None,
        )

        self.assertEqual(len(selected), 39)
        self.assertEqual(selected[0]["scenario_id"], "add-one-05-002409")
        self.assertEqual(selected[-1]["scenario_id"], "add-one-13-688082")
        self.assertTrue(all(item["scenario_type"] == "add_one" for item in selected))
        self.assertFalse(formal_plan_complete)

    def test_shard_membership_uses_original_zero_based_indices(self) -> None:
        scenarios = stress_scenarios._multi_seed_scenarios(
            random_samples=1,
            permutation_samples=1,
            seeds=(7,),
        )[:8]

        selected, formal_plan_complete = select_scenarios(
            scenarios,
            scenario_id=None,
            scenario_type=None,
            shard_index=1,
            shard_count=3,
        )

        self.assertEqual(
            [item["scenario_id"] for item in selected],
            ["prefix-02", "prefix-05", "prefix-08"],
        )
        self.assertFalse(formal_plan_complete)

    def test_selector_rejects_invalid_shard_arguments(self) -> None:
        scenarios = [{"scenario_id": "only", "scenario_type": "prefix"}]
        invalid = (
            (None, 2),
            (0, None),
            (0, 0),
            (-1, 2),
            (2, 2),
        )

        for shard_index, shard_count in invalid:
            with self.subTest(shard_index=shard_index, shard_count=shard_count):
                with self.assertRaisesRegex(ValueError, "shard"):
                    select_scenarios(
                        scenarios,
                        scenario_id=None,
                        scenario_type=None,
                        shard_index=shard_index,
                        shard_count=shard_count,
                    )

    def test_selector_rejects_unknown_or_empty_selection(self) -> None:
        scenarios = [{"scenario_id": "only", "scenario_type": "prefix"}]

        for scenario_id, scenario_type in (
            ("unknown", None),
            (None, "unknown"),
            ("only", "add_one"),
        ):
            with self.subTest(scenario_id=scenario_id, scenario_type=scenario_type):
                with self.assertRaisesRegex(ValueError, "no scenarios"):
                    select_scenarios(
                        scenarios,
                        scenario_id=scenario_id,
                        scenario_type=scenario_type,
                        shard_index=None,
                        shard_count=None,
                    )

    def test_unfiltered_selection_is_the_only_formal_complete_plan(self) -> None:
        scenarios = stress_scenarios._multi_seed_scenarios(
            random_samples=50,
            permutation_samples=50,
            seeds=stress_scenarios.DEFAULT_SEEDS,
        )
        reduced = stress_scenarios._multi_seed_scenarios(
            random_samples=1,
            permutation_samples=1,
            seeds=(7,),
        )

        selected, formal_plan_complete = select_scenarios(
            scenarios,
            scenario_id=None,
            scenario_type=None,
            shard_index=None,
            shard_count=None,
        )
        sharded, sharded_complete = select_scenarios(
            scenarios,
            scenario_id=None,
            scenario_type=None,
            shard_index=0,
            shard_count=1,
        )
        reduced_selected, reduced_complete = select_scenarios(
            reduced,
            scenario_id=None,
            scenario_type=None,
            shard_index=None,
            shard_count=None,
        )

        self.assertEqual(selected, scenarios)
        self.assertTrue(formal_plan_complete)
        self.assertEqual(sharded, scenarios)
        self.assertFalse(sharded_complete)
        self.assertEqual(reduced_selected, reduced)
        self.assertFalse(reduced_complete)

    def test_checkpoint_rejects_mismatched_scenario_definition(self) -> None:
        scenarios = stress_scenarios._multi_seed_scenarios(
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
            stress_artifacts._validated_checkpoint_results(
                {"results": [result]}, scenarios
            )

    def test_checkpoint_envelope_validation_returns_completed_results(self) -> None:
        scenario = stress_scenarios._multi_seed_scenarios(
            random_samples=1,
            permutation_samples=1,
            seeds=(7,),
        )[0]
        result = self._complete_result(scenario)
        provenance = {"run_signature": "expected-signature"}
        selection = {"scenario_id": "prefix-01"}

        completed = stress_artifacts._validated_checkpoint(
            {
                "signature": "expected-signature",
                "provenance": provenance,
                "selection": selection,
                "results": [result],
            },
            [scenario],
            signature="expected-signature",
            provenance=provenance,
            diagnostic_selection=selection,
        )

        self.assertEqual(completed, {"prefix-01": result})

    def test_checkpoint_envelope_rejects_signature_mismatch(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Stress checkpoint code, data, or scenario signature changed",
        ):
            stress_artifacts._validated_checkpoint(
                {"signature": "stale", "provenance": {}, "results": []},
                [],
                signature="current",
                provenance={},
                diagnostic_selection=None,
            )

    def test_checkpoint_envelope_rejects_provenance_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "Stress checkpoint provenance changed"):
            stress_artifacts._validated_checkpoint(
                {
                    "signature": "current",
                    "provenance": {"source_revision": "stale"},
                    "results": [],
                },
                [],
                signature="current",
                provenance={"source_revision": "current"},
                diagnostic_selection=None,
            )

    def test_checkpoint_envelope_rejects_diagnostic_selection_mismatch(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "Stress diagnostic checkpoint selection changed"
        ):
            stress_artifacts._validated_checkpoint(
                {
                    "signature": "current",
                    "provenance": {},
                    "selection": {"scenario_id": "stale"},
                    "results": [],
                },
                [],
                signature="current",
                provenance={},
                diagnostic_selection={"scenario_id": "current"},
            )

    def test_checkpoint_signature_covers_canonical_source_tree(self) -> None:
        """A canonical implementation change must invalidate old checkpoints."""
        fingerprints: list[tuple[Path, ...]] = []

        def capture(paths: list[Path]) -> str:
            fingerprints.append(tuple(paths))
            return "fingerprint"

        with TemporaryDirectory() as tmpdir, patch.object(
            stress_artifacts, "_tree_fingerprint", side_effect=capture
        ):
            stress_artifacts._run_signature(
                [],
                Path(tmpdir),
                Path(tmpdir),
                source_revision="a" * 40,
            )

        application_dir = (
            stress_artifacts.PROJECT_ROOT / "quantfusion" / "application"
        )
        for filename in (
            "stress.py",
            "stress_scenarios.py",
            "stress_metrics.py",
            "stress_artifacts.py",
        ):
            with self.subTest(filename=filename):
                self.assertIn(application_dir / filename, fingerprints[0])

    def test_provenance_exposes_independent_source_data_and_scenario_hashes(
        self,
    ) -> None:
        scenarios = stress_scenarios._multi_seed_scenarios(
            random_samples=1,
            permutation_samples=1,
            seeds=(7,),
        )
        with TemporaryDirectory() as tmpdir, patch.object(
            stress_artifacts,
            "_tree_fingerprint",
            side_effect=("source-fingerprint", "data-fingerprint"),
        ):
            provenance = stress_artifacts._build_provenance(
                scenarios,
                Path(tmpdir),
                Path(tmpdir),
                source_revision="a" * 40,
            )

        self.assertEqual(
            provenance,
            {
                "source_revision": "a" * 40,
                "source_fingerprint": "source-fingerprint",
                "data_fingerprint": "data-fingerprint",
                "scenario_signature": (
                    "aea62b6423a0b3fdc587c4b5cb080b184850c67bc086fa92aaa8b9483b87cee1"
                ),
                "run_signature": (
                    "6eb1b66bc1a181ab6eb032efffc3863a2b3a384f978a0c9e164e3406f0d7eacd"
                ),
                "scenario_count": len(scenarios),
                "start_date": "2025-04-01",
                "end_date": "2026-07-20",
                "initial_capital": 2_000_000.0,
                "engine": "ProductionReplayEngine",
                "deployment_policy": "production_daily_replay",
            },
        )

    def test_checkpoint_requires_trade_and_bucket_counts_separately(self) -> None:
        scenarios = stress_scenarios._multi_seed_scenarios(
            random_samples=1,
            permutation_samples=1,
            seeds=(7,),
        )
        result = {
            **scenarios[0],
            "total_return": 1.0,
            "max_drawdown": -0.1,
            "sharpe": 1.0,
            "calmar": 1.0,
            "total_trades": 2,
            "sleeve_fill_count": 2,
            "reason_attribution": {category: 0 for category in stress_metrics.ATTRIBUTION_CATEGORIES},
            "max_concurrent_symbols": 1,
            "terminal_risk_lock": False,
            "deployment_policy": "production_daily_replay",
        }

        with self.assertRaisesRegex(ValueError, "date_symbol_side_count"):
            stress_artifacts._validated_checkpoint_results({"results": [result]}, scenarios)

    def test_permutation_invariance_includes_bucket_semantics(self) -> None:
        common = {
            "scenario_type": "permutation",
            "seed": 7,
            "total_return": 1.0,
            "max_drawdown": -0.1,
            "sharpe": 1.0,
            "calmar": 1.0,
            "total_trades": 2,
            "sleeve_fill_count": 2,
            "max_concurrent_symbols": 1,
            "terminal_risk_lock": False,
            "reason_attribution": {category: 0 for category in stress_metrics.ATTRIBUTION_CATEGORIES},
        }
        result = stress_metrics._permutation_invariance(
            [
                {**common, "scenario_id": "permutation-7-001", "date_symbol_side_count": 1},
                {**common, "scenario_id": "permutation-7-002", "date_symbol_side_count": 2},
            ]
        )

        self.assertFalse(result["invariant"])

    def test_diagnostic_selection_cannot_enter_formal_publication(self) -> None:
        with TemporaryDirectory() as tmpdir, patch.object(
            stress_artifacts, "VALIDATION_ARTIFACT_DIR", Path(tmpdir)
        ):
            prefix_path = Path(tmpdir) / "prefix_stress.json"
            universe_path = Path(tmpdir) / "universe_stress.json"
            prefix_path.write_text("existing prefix\n", encoding="utf-8")
            universe_path.write_text("existing universe\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "diagnostic"):
                stress_artifacts._publish_formal_artifacts(
                    {},
                    {},
                    scenarios=[],
                    provenance={},
                    incumbent=None,
                    formal_plan_complete=False,
                )

            self.assertEqual(
                prefix_path.read_text(encoding="utf-8"), "existing prefix\n"
            )
            self.assertEqual(
                universe_path.read_text(encoding="utf-8"), "existing universe\n"
            )
            self.assertEqual(
                sorted(path.name for path in Path(tmpdir).iterdir()),
                ["prefix_stress.json", "universe_stress.json"],
            )

    def test_cli_exact_diagnostic_uses_only_diagnostic_paths(self) -> None:
        source_revision = "3" * 40
        full_plan = stress_scenarios._multi_seed_scenarios(
            random_samples=1,
            permutation_samples=1,
            seeds=(7,),
        )
        selected, _ = select_scenarios(
            full_plan,
            scenario_id="add-one-05-688205",
            scenario_type=None,
            shard_index=None,
            shard_count=None,
        )
        provenance = stress_artifacts._build_provenance(
            selected,
            stress.DATA_DIR,
            stress.REGIME_DATA_DIR,
            source_revision=source_revision,
        )
        result = self._complete_result(selected[0])

        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            formal_checkpoint = root / "formal-checkpoint.json"
            diagnostic_checkpoint = root / "diagnostic-checkpoint.json"
            diagnostic_output = root / "diagnostic-output.json"
            validation_dir = root / "validation"
            validation_dir.mkdir()
            prefix_path = validation_dir / "prefix_stress.json"
            universe_path = validation_dir / "universe_stress.json"
            prefix_path.write_text("existing prefix\n", encoding="utf-8")
            universe_path.write_text("existing universe\n", encoding="utf-8")
            stress_artifacts._atomic_json(
                diagnostic_checkpoint,
                {
                    "signature": provenance["run_signature"],
                    "provenance": provenance,
                    "selection": {
                        "scenario_id": "add-one-05-688205",
                        "scenario_type": None,
                        "shard_index": None,
                        "shard_count": None,
                    },
                    "completed": 1,
                    "scenario_count": 1,
                    "results": [result],
                },
            )
            argv = [
                "quantfusion.application.stress",
                "--workers",
                "1",
                "--random-samples",
                "1",
                "--permutation-samples",
                "1",
                "--seeds",
                "7",
                "--scenario-id",
                "add-one-05-688205",
                "--checkpoint",
                str(formal_checkpoint),
                "--diagnostic-checkpoint",
                str(diagnostic_checkpoint),
                "--diagnostic-output",
                str(diagnostic_output),
                "--source-revision",
                source_revision,
            ]

            with patch("sys.argv", argv), patch.object(
                stress_artifacts, "VALIDATION_ARTIFACT_DIR", validation_dir
            ), contextlib.redirect_stdout(io.StringIO()) as stdout:
                exit_code = stress.main()

            payload = json.loads(diagnostic_output.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["artifact_status"], "diagnostic")
            self.assertFalse(payload["formal_plan_complete"])
            self.assertFalse(payload["canonical"])
            self.assertEqual(payload["selection"]["scenario_id"], "add-one-05-688205")
            self.assertEqual(
                [item["scenario_id"] for item in payload["results"]],
                ["add-one-05-688205"],
            )
            self.assertNotIn("hard_gates", payload)
            self.assertNotIn("promotion_gates", payload)
            self.assertIn('"artifact_status": "diagnostic"', stdout.getvalue())
            self.assertFalse(formal_checkpoint.exists())
            self.assertEqual(
                prefix_path.read_text(encoding="utf-8"), "existing prefix\n"
            )
            self.assertEqual(
                universe_path.read_text(encoding="utf-8"), "existing universe\n"
            )

    def test_cli_diagnostic_rejects_canonical_output_path(self) -> None:
        source_revision = "4" * 40
        full_plan = stress_scenarios._multi_seed_scenarios(
            random_samples=1,
            permutation_samples=1,
            seeds=(7,),
        )
        selected, _ = select_scenarios(
            full_plan,
            scenario_id="add-one-05-688205",
            scenario_type=None,
            shard_index=None,
            shard_count=None,
        )
        provenance = stress_artifacts._build_provenance(
            selected,
            stress.DATA_DIR,
            stress.REGIME_DATA_DIR,
            source_revision=source_revision,
        )

        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            validation_dir = root / "validation"
            validation_dir.mkdir()
            universe_path = validation_dir / "universe_stress.json"
            universe_path.write_text("existing universe\n", encoding="utf-8")
            diagnostic_checkpoint = root / "diagnostic-checkpoint.json"
            stress_artifacts._atomic_json(
                diagnostic_checkpoint,
                {
                    "signature": provenance["run_signature"],
                    "provenance": provenance,
                    "selection": {
                        "scenario_id": "add-one-05-688205",
                        "scenario_type": None,
                        "shard_index": None,
                        "shard_count": None,
                    },
                    "completed": 1,
                    "scenario_count": 1,
                    "results": [self._complete_result(selected[0])],
                },
            )
            argv = [
                "quantfusion.application.stress",
                "--workers",
                "1",
                "--random-samples",
                "1",
                "--permutation-samples",
                "1",
                "--seeds",
                "7",
                "--scenario-id",
                "add-one-05-688205",
                "--diagnostic-checkpoint",
                str(diagnostic_checkpoint),
                "--diagnostic-output",
                str(universe_path),
                "--source-revision",
                source_revision,
            ]

            with patch("sys.argv", argv), patch.object(
                stress_artifacts, "VALIDATION_ARTIFACT_DIR", validation_dir
            ), contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(
                ValueError, "separate"
            ):
                stress.main()

            self.assertEqual(
                universe_path.read_text(encoding="utf-8"), "existing universe\n"
            )

    def test_cli_diagnostic_rejects_candidate_namespace_paths(self) -> None:
        with TemporaryDirectory() as tmpdir:
            validation_dir = Path(tmpdir) / "validation"
            candidate_path = validation_dir / "candidates" / "diagnostic.json"
            base_argv = [
                "quantfusion.application.stress",
                "--random-samples",
                "1",
                "--permutation-samples",
                "1",
                "--seeds",
                "7",
                "--scenario-id",
                "add-one-05-688205",
                "--source-revision",
                "invalid",
            ]

            for option in ("--diagnostic-checkpoint", "--diagnostic-output"):
                with self.subTest(option=option), patch(
                    "sys.argv", [*base_argv, option, str(candidate_path)]
                ), patch.object(
                    stress_artifacts, "VALIDATION_ARTIFACT_DIR", validation_dir
                ), self.assertRaisesRegex(ValueError, "validation namespace"):
                    stress.main()

            self.assertFalse(candidate_path.exists())

    def test_cli_formal_checkpoint_rejects_validation_namespace_paths(self) -> None:
        with TemporaryDirectory() as tmpdir:
            validation_dir = Path(tmpdir) / "validation"
            validation_dir.mkdir()
            canonical_path = validation_dir / "universe_stress.json"
            canonical_path.write_text("accepted sentinel\n", encoding="utf-8")
            candidate_path = validation_dir / "candidates" / "checkpoint.json"

            for checkpoint in (canonical_path, candidate_path):
                with self.subTest(checkpoint=checkpoint), patch(
                    "sys.argv",
                    [
                        "quantfusion.application.stress",
                        "--checkpoint",
                        str(checkpoint),
                        "--source-revision",
                        "a" * 40,
                    ],
                ), patch.object(
                    stress_artifacts, "VALIDATION_ARTIFACT_DIR", validation_dir
                ), patch.object(
                    stress_scenarios,
                    "_multi_seed_scenarios",
                    side_effect=AssertionError("scenario plan must not be built"),
                ) as build_plan, patch.object(
                    stress,
                    "ProcessPoolExecutor",
                    side_effect=AssertionError("executor must not be called"),
                ) as executor, self.assertRaisesRegex(
                    ValueError, "validation namespace"
                ):
                    stress.main()
                build_plan.assert_not_called()
                executor.assert_not_called()

            self.assertEqual(
                canonical_path.read_text(encoding="utf-8"), "accepted sentinel\n"
            )
            self.assertFalse(candidate_path.exists())

    def test_noncanonical_plan_cannot_spoof_formal_publication(self) -> None:
        canonical = stress_scenarios._multi_seed_scenarios(
            random_samples=50,
            permutation_samples=50,
            seeds=stress_scenarios.DEFAULT_SEEDS,
        )
        plans = {
            "reduced": stress_scenarios._multi_seed_scenarios(
                random_samples=1,
                permutation_samples=1,
                seeds=(7,),
            ),
            "altered_seeds": stress_scenarios._multi_seed_scenarios(
                random_samples=50,
                permutation_samples=50,
                seeds=(*stress_scenarios.DEFAULT_SEEDS[:-1], 999),
            ),
            "altered_order": [canonical[1], canonical[0], *canonical[2:]],
        }

        with TemporaryDirectory() as tmpdir, patch.object(
            stress_artifacts, "VALIDATION_ARTIFACT_DIR", Path(tmpdir)
        ):
            for name, scenarios in plans.items():
                with self.subTest(name=name), self.assertRaisesRegex(
                    ValueError, "canonical scenario plan"
                ):
                    stress_artifacts._publish_formal_artifacts(
                        {},
                        {},
                        scenarios=scenarios,
                        provenance={},
                        incumbent=None,
                        formal_plan_complete=True,
                    )

            self.assertEqual(list(Path(tmpdir).iterdir()), [])

    def test_reduced_complete_candidate_cannot_publish(self) -> None:
        scenarios = stress_scenarios._multi_seed_scenarios(
            random_samples=1,
            permutation_samples=1,
            seeds=(7,),
        )
        results = [self._complete_result(item) for item in scenarios]
        provenance = self._provenance("9" * 40, len(scenarios))
        incumbent = self._accepted_current_incumbent(results)
        prefix_artifact = {
            **provenance,
            "results": [
                item for item in results if item["scenario_type"] == "prefix"
            ],
        }
        universe_artifact = {
            **provenance,
            "trade_count_semantics": stress_metrics.TRADE_COUNT_SEMANTICS,
            "seeds": [7],
            "scenario_count": len(results),
            "hard_gates": stress_metrics._hard_gates(results),
            "promotion_gates": stress_metrics._promotion_gates(results, incumbent),
            "results": results,
        }

        with TemporaryDirectory() as tmpdir, patch.object(
            stress_artifacts, "VALIDATION_ARTIFACT_DIR", Path(tmpdir)
        ):
            with self.assertRaisesRegex(ValueError, "canonical scenario plan"):
                stress_artifacts._publish_formal_artifacts(
                    prefix_artifact,
                    universe_artifact,
                    scenarios=scenarios,
                    provenance=provenance,
                    incumbent=incumbent,
                    formal_plan_complete=True,
                )

            self.assertEqual(list(Path(tmpdir).iterdir()), [])

    def test_failed_complete_candidate_is_retained_without_canonical_publish(
        self,
    ) -> None:
        scenarios = stress_scenarios._multi_seed_scenarios(
            random_samples=stress_scenarios.DEFAULT_RANDOM_SAMPLES,
            permutation_samples=stress_scenarios.DEFAULT_PERMUTATION_SAMPLES,
            seeds=stress_scenarios.DEFAULT_SEEDS,
        )
        results = [self._complete_result(item) for item in scenarios]
        by_id = {item["scenario_id"]: item for item in results}
        by_id["add-one-05-688205"]["total_return"] = 0.0
        provenance = self._provenance("a" * 40, len(scenarios))
        prefix_artifact = {
            **provenance,
            "artifact_status": "current",
            "results": [
                item for item in results if item["scenario_type"] == "prefix"
            ],
        }
        universe_artifact = {
            **provenance,
            "artifact_status": "current",
            "trade_count_semantics": stress_metrics.TRADE_COUNT_SEMANTICS,
            "seeds": list(stress_scenarios.DEFAULT_SEEDS),
            "scenario_count": len(results),
            "hard_gates": stress_metrics._hard_gates(results),
            "promotion_gates": stress_metrics._promotion_gates(
                results, self._accepted_current_incumbent(results)
            ),
            "results": results,
        }

        with patch.object(stress_artifacts, "_atomic_json") as write_artifact:
            published = stress_artifacts._publish_formal_artifacts(
                prefix_artifact,
                universe_artifact,
                scenarios=scenarios,
                provenance=provenance,
                incumbent=self._accepted_current_incumbent(results),
                formal_plan_complete=True,
            )

        self.assertFalse(published)
        write_artifact.assert_called_once()
        path, candidate = write_artifact.call_args.args
        self.assertEqual(
            path,
            stress_artifacts.VALIDATION_ARTIFACT_DIR
            / "candidates"
            / f"stress-{'a' * 40}-rejected.json",
        )
        self.assertEqual(candidate["artifact_status"], "current_candidate")
        self.assertEqual(candidate["acceptance_status"], "rejected")
        self.assertFalse(candidate["canonical"])
        self.assertEqual(candidate["scenario_count"], len(scenarios))
        self.assertEqual(candidate["results"], results)
        self.assertEqual(
            candidate["rejection_reasons"],
            [
                {
                    "gate_family": "hard_gates",
                    "gate": "worst_add_one_wealth_at_least_minus_18pct",
                }
            ],
        )

    def test_accepted_complete_candidate_updates_canonical_artifacts(self) -> None:
        scenarios = stress_scenarios._multi_seed_scenarios(
            random_samples=stress_scenarios.DEFAULT_RANDOM_SAMPLES,
            permutation_samples=stress_scenarios.DEFAULT_PERMUTATION_SAMPLES,
            seeds=stress_scenarios.DEFAULT_SEEDS,
        )
        results = [self._complete_result(item) for item in scenarios]
        provenance = self._provenance("b" * 40, len(scenarios))
        prefix_artifact = {
            **provenance,
            "artifact_status": "current",
            "results": [
                item for item in results if item["scenario_type"] == "prefix"
            ],
        }
        universe_artifact = {
            **provenance,
            "artifact_status": "current",
            "trade_count_semantics": stress_metrics.TRADE_COUNT_SEMANTICS,
            "seeds": list(stress_scenarios.DEFAULT_SEEDS),
            "scenario_count": len(results),
            "hard_gates": stress_metrics._hard_gates(results),
            "promotion_gates": stress_metrics._promotion_gates(
                results, self._accepted_current_incumbent(results)
            ),
            "results": results,
        }

        with patch.object(stress_artifacts, "_atomic_json") as write_artifact:
            published = stress_artifacts._publish_formal_artifacts(
                prefix_artifact,
                universe_artifact,
                scenarios=scenarios,
                provenance=provenance,
                incumbent=self._accepted_current_incumbent(results),
                formal_plan_complete=True,
            )

        self.assertTrue(published)
        self.assertEqual(write_artifact.call_count, 2)
        self.assertEqual(
            [call.args[0].name for call in write_artifact.call_args_list],
            ["prefix_stress.json", "universe_stress.json"],
        )

    def test_incomplete_candidate_is_not_retained(self) -> None:
        scenarios = stress_scenarios._multi_seed_scenarios(
            random_samples=stress_scenarios.DEFAULT_RANDOM_SAMPLES,
            permutation_samples=stress_scenarios.DEFAULT_PERMUTATION_SAMPLES,
            seeds=stress_scenarios.DEFAULT_SEEDS,
        )
        provenance = self._provenance("c" * 40, len(scenarios))
        universe_artifact = {
            **provenance,
            "trade_count_semantics": stress_metrics.TRADE_COUNT_SEMANTICS,
            "seeds": list(stress_scenarios.DEFAULT_SEEDS),
            "hard_gates": {"passed": False, "checks": {"floor": False}},
            "promotion_gates": {
                "passed": None,
                "permutation_invariance": {"invariant": True},
            },
            "scenario_count": len(scenarios),
            "results": [self._complete_result(scenarios[0])],
        }

        with patch.object(stress_artifacts, "_atomic_json") as write_artifact:
            with self.assertRaisesRegex(ValueError, "exact scenario plan"):
                stress_artifacts._publish_formal_artifacts(
                    provenance,
                    universe_artifact,
                    scenarios=scenarios,
                    provenance=provenance,
                    incumbent=None,
                    formal_plan_complete=True,
                )

        write_artifact.assert_not_called()

    def test_provenance_mismatch_is_not_retained(self) -> None:
        scenarios = stress_scenarios._multi_seed_scenarios(
            random_samples=stress_scenarios.DEFAULT_RANDOM_SAMPLES,
            permutation_samples=stress_scenarios.DEFAULT_PERMUTATION_SAMPLES,
            seeds=stress_scenarios.DEFAULT_SEEDS,
        )
        expected = self._provenance("d" * 40, len(scenarios))
        actual = {**expected, "source_revision": "e" * 40}
        universe_artifact = {
            **actual,
            "trade_count_semantics": stress_metrics.TRADE_COUNT_SEMANTICS,
            "seeds": list(stress_scenarios.DEFAULT_SEEDS),
            "hard_gates": {"passed": False, "checks": {"floor": False}},
            "promotion_gates": {
                "passed": None,
                "permutation_invariance": {"invariant": True},
            },
            "results": [self._complete_result(item) for item in scenarios],
        }

        with patch.object(stress_artifacts, "_atomic_json") as write_artifact:
            with self.assertRaisesRegex(ValueError, "provenance changed"):
                stress_artifacts._publish_formal_artifacts(
                    expected,
                    universe_artifact,
                    scenarios=scenarios,
                    provenance=expected,
                    incumbent=None,
                    formal_plan_complete=True,
                )

        write_artifact.assert_not_called()

    def test_tampered_gate_summary_cannot_publish(self) -> None:
        scenarios = stress_scenarios._multi_seed_scenarios(
            random_samples=stress_scenarios.DEFAULT_RANDOM_SAMPLES,
            permutation_samples=stress_scenarios.DEFAULT_PERMUTATION_SAMPLES,
            seeds=stress_scenarios.DEFAULT_SEEDS,
        )
        results = [self._complete_result(item) for item in scenarios]
        by_id = {item["scenario_id"]: item for item in results}
        by_id["add-one-05-688205"]["total_return"] = 0.0
        self.assertFalse(stress_metrics._hard_gates(results)["passed"])
        provenance = self._provenance("f" * 40, len(scenarios))
        prefix_artifact = {
            **provenance,
            "results": [
                item for item in results if item["scenario_type"] == "prefix"
            ],
        }
        universe_artifact = {
            **provenance,
            "trade_count_semantics": stress_metrics.TRADE_COUNT_SEMANTICS,
            "seeds": list(stress_scenarios.DEFAULT_SEEDS),
            "hard_gates": {"passed": True, "checks": {}},
            "promotion_gates": stress_metrics._promotion_gates(
                results, self._accepted_current_incumbent(results)
            ),
            "results": results,
        }

        with patch.object(stress_artifacts, "_atomic_json") as write_artifact, self.assertRaisesRegex(
            ValueError, "hard gates changed"
        ):
            stress_artifacts._publish_formal_artifacts(
                prefix_artifact,
                universe_artifact,
                scenarios=scenarios,
                provenance=provenance,
                incumbent=self._accepted_current_incumbent(results),
                formal_plan_complete=True,
            )

        write_artifact.assert_not_called()

    def test_wrong_seed_list_cannot_publish(self) -> None:
        scenarios = stress_scenarios._multi_seed_scenarios(
            random_samples=stress_scenarios.DEFAULT_RANDOM_SAMPLES,
            permutation_samples=stress_scenarios.DEFAULT_PERMUTATION_SAMPLES,
            seeds=stress_scenarios.DEFAULT_SEEDS,
        )
        results = [self._complete_result(item) for item in scenarios]
        provenance = self._provenance("1" * 40, len(scenarios))
        prefix_artifact = {
            **provenance,
            "results": [
                item for item in results if item["scenario_type"] == "prefix"
            ],
        }
        universe_artifact = {
            **provenance,
            "trade_count_semantics": stress_metrics.TRADE_COUNT_SEMANTICS,
            "seeds": [999],
            "hard_gates": stress_metrics._hard_gates(results),
            "promotion_gates": stress_metrics._promotion_gates(
                results, self._accepted_current_incumbent(results)
            ),
            "results": results,
        }

        with patch.object(stress_artifacts, "_atomic_json") as write_artifact, self.assertRaisesRegex(
            ValueError, "seeds changed"
        ):
            stress_artifacts._publish_formal_artifacts(
                prefix_artifact,
                universe_artifact,
                scenarios=scenarios,
                provenance=provenance,
                incumbent=self._accepted_current_incumbent(results),
                formal_plan_complete=True,
            )

        write_artifact.assert_not_called()

    def test_missing_prefix_result_cannot_publish(self) -> None:
        scenarios = stress_scenarios._multi_seed_scenarios(
            random_samples=stress_scenarios.DEFAULT_RANDOM_SAMPLES,
            permutation_samples=stress_scenarios.DEFAULT_PERMUTATION_SAMPLES,
            seeds=stress_scenarios.DEFAULT_SEEDS,
        )
        results = [self._complete_result(item) for item in scenarios]
        provenance = self._provenance("2" * 40, len(scenarios))
        prefix_results = [
            item for item in results if item["scenario_type"] == "prefix"
        ][1:]
        prefix_artifact = {**provenance, "results": prefix_results}
        universe_artifact = {
            **provenance,
            "trade_count_semantics": stress_metrics.TRADE_COUNT_SEMANTICS,
            "seeds": list(stress_scenarios.DEFAULT_SEEDS),
            "hard_gates": stress_metrics._hard_gates(results),
            "promotion_gates": stress_metrics._promotion_gates(
                results, self._accepted_current_incumbent(results)
            ),
            "results": results,
        }

        with patch.object(stress_artifacts, "_atomic_json") as write_artifact, self.assertRaisesRegex(
            ValueError, "prefix scenario plan"
        ):
            stress_artifacts._publish_formal_artifacts(
                prefix_artifact,
                universe_artifact,
                scenarios=scenarios,
                provenance=provenance,
                incumbent=self._accepted_current_incumbent(results),
                formal_plan_complete=True,
            )

        write_artifact.assert_not_called()

    def test_corrupt_incumbent_fails_closed(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "universe_stress.json"
            path.write_text("{not-json", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Cannot read incumbent"):
                stress_artifacts._load_incumbent(path)

    def test_non_trade_record_semantics_is_rejected(self) -> None:
        current = {
            "scenario_id": "prefix-01",
            "scenario_type": "prefix",
            "total_return": 1.0,
            "max_drawdown": -0.1,
            "total_trades": 24,
            "sleeve_fill_count": 24,
            "date_symbol_side_count": 5,
        }
        invalid_incumbent = {
            "trade_count_semantics": "date_symbol_side_bucket",
            "results": [{**current, "total_trades": 5}],
        }

        with self.assertRaisesRegex(ValueError, "trade_records"):
            stress_metrics._promotion_gates([current], invalid_incumbent)

    def test_load_incumbent_rejects_non_trade_record_semantics(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "universe_stress.json"
            path.write_text(
                json.dumps(
                    {
                        "trade_count_semantics": "date_symbol_side_bucket",
                        "results": [],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "trade_records"):
                stress_artifacts._load_incumbent(path)

    def test_no_incumbent_baseline_fails_closed(self) -> None:
        result = self._complete_result(
            {
                "scenario_id": "permutation-7-001",
                "scenario_type": "permutation",
                "seed": 7,
                "symbols": list(stress_scenarios.ORDERED_CODES),
            }
        )

        promotion = stress_metrics._promotion_gates([result], None)

        self.assertEqual(promotion["status"], "no_incumbent_baseline")
        self.assertFalse(promotion["applicable"])
        self.assertFalse(promotion["passed"])
        self.assertFalse(stress_metrics._promotion_accepted(promotion))
        self.assertIn(
            {
                "gate_family": "promotion_gates",
                "gate": "accepted_current_semantic_incumbent",
            },
            stress_artifacts._rejection_reasons(
                {
                    "hard_gates": {"checks": {}},
                    "promotion_gates": promotion,
                }
            ),
        )

    def test_rejected_current_artifact_cannot_be_compared_as_incumbent(self) -> None:
        result = self._complete_result(
            {
                "scenario_id": "prefix-01",
                "scenario_type": "prefix",
                "symbols": [stress_scenarios.ORDERED_CODES[0]],
            }
        )
        rejected = {
            "acceptance_status": "rejected",
            "canonical": False,
            "trade_count_semantics": "trade_records",
            "results": [result],
        }

        with self.assertRaisesRegex(ValueError, "accepted and canonical"):
            stress_metrics._promotion_gates([result], rejected)

    def test_load_incumbent_rejects_noncanonical_current_candidate(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "universe_stress.json"
            path.write_text(
                json.dumps(
                    {
                        "acceptance_status": "rejected",
                        "canonical": False,
                        "trade_count_semantics": "trade_records",
                        "results": [],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "accepted and canonical"):
                stress_artifacts._load_incumbent(path)

    def test_empty_accepted_current_incumbent_cannot_pass_promotion(self) -> None:
        results = self._complete_small_plan()

        with self.assertRaisesRegex(ValueError, "non-empty results"):
            stress_metrics._promotion_gates(
                results,
                self._accepted_current_incumbent([]),
            )

    def test_load_incumbent_rejects_empty_accepted_current_results(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "universe_stress.json"
            path.write_text(
                json.dumps(self._accepted_current_incumbent([])),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "non-empty results"):
                stress_artifacts._load_incumbent(path)

    def test_disjoint_accepted_current_incumbent_cannot_pass_promotion(self) -> None:
        results = self._complete_small_plan()
        disjoint = [
            {**item, "scenario_id": f"incumbent-{index}"}
            for index, item in enumerate(results)
        ]

        with self.assertRaisesRegex(ValueError, "shared scenario"):
            stress_metrics._promotion_gates(
                results,
                self._accepted_current_incumbent(disjoint),
            )

    def test_incumbent_cannot_omit_mandatory_fixed_scenario_ids(self) -> None:
        results = self._complete_small_plan()
        incomplete = [
            item for item in results if item["scenario_id"] != "prefix-22"
        ]

        with self.assertRaisesRegex(ValueError, "mandatory fixed scenario"):
            stress_metrics._promotion_gates(
                results,
                self._accepted_current_incumbent(incomplete),
            )

    def test_incumbent_cannot_omit_random_comparison_family(self) -> None:
        results = self._complete_small_plan()
        without_random = [
            item for item in results if item["scenario_type"] != "random_subset"
        ]

        with self.assertRaisesRegex(ValueError, "random_subset"):
            stress_metrics._promotion_gates(
                results,
                self._accepted_current_incumbent(without_random),
            )

    def test_incumbent_results_must_be_structured_objects(self) -> None:
        results = self._complete_small_plan()
        malformed = self._accepted_current_incumbent(results)
        malformed["results"] = [{}]

        with self.assertRaisesRegex(ValueError, "structured result"):
            stress_metrics._promotion_gates(results, malformed)

    def test_incumbent_rejects_duplicate_scenario_ids(self) -> None:
        results = self._complete_small_plan()
        duplicate = [*results, results[0]]

        with self.assertRaisesRegex(ValueError, "duplicate scenario_id"):
            stress_metrics._promotion_gates(
                results,
                self._accepted_current_incumbent(duplicate),
            )

    def test_incumbent_cannot_relabel_mandatory_fixed_scenario(self) -> None:
        results = self._complete_small_plan()
        relabeled = [
            (
                {**item, "scenario_type": "random_subset"}
                if item["scenario_id"] == "prefix-22"
                else item
            )
            for item in results
        ]

        with self.assertRaisesRegex(ValueError, "mandatory fixed scenario"):
            stress_metrics._promotion_gates(
                results,
                self._accepted_current_incumbent(relabeled),
            )

    def test_rejected_candidate_retains_complete_current_evidence(self) -> None:
        path = (
            stress_artifacts.VALIDATION_ARTIFACT_DIR
            / "candidates"
            / "stress-117a0ea17a333be17fbd345a14eb67fb328d046c-rejected.json"
        )
        payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["artifact_status"], "current_candidate")
        self.assertEqual(payload["acceptance_status"], "rejected")
        self.assertFalse(payload["canonical"])
        self.assertEqual(payload["scenario_count"], 983)
        self.assertEqual(len(payload["results"]), 983)
        self.assertEqual(
            len({item["scenario_id"] for item in payload["results"]}),
            983,
        )
        self.assertEqual(
            payload["source_fingerprint"],
            "98b6bcd7d39ab9de3352af24dca05721b95d01b45fa3facbae7890a35dfc6ea1",
        )
        self.assertEqual(payload["trade_count_semantics"], "trade_records")
        self.assertEqual(
            payload["promotion_gates"]["status"],
            "no_incumbent_baseline",
        )
        self.assertFalse(payload["promotion_gates"]["applicable"])
        self.assertFalse(payload["promotion_gates"]["passed"])

    def test_hard_gates_keep_current_ceilings_on_side_buckets(self) -> None:
        def scenario(
            scenario_id: str,
            scenario_type: str,
            *,
            symbol_count: int = 10,
        ) -> dict[str, object]:
            return {
                "scenario_id": scenario_id,
                "scenario_type": scenario_type,
                "symbol_count": symbol_count,
                "total_return": 1.0,
                "max_drawdown": -0.1,
                "total_trades": 300,
                "sleeve_fill_count": 300,
                "date_symbol_side_count": 10,
            }

        prefix_09 = scenario("prefix-09", "prefix", symbol_count=9)
        prefix_10 = scenario("prefix-10", "prefix")
        add_one = {
            **scenario("add-one", "add_one"),
            "base_size": 9,
        }
        random_subset = scenario("random", "random_subset")

        result = stress_metrics._hard_gates(
            [prefix_09, prefix_10, add_one, random_subset]
        )

        self.assertTrue(result["passed"])
        self.assertTrue(
            result["checks"][
                "all_date_symbol_side_buckets_at_most_200"
            ]
        )
        self.assertEqual(
            result["observed"]["all_worst_date_symbol_side_buckets"],
            10.0,
        )


if __name__ == "__main__":
    unittest.main()
