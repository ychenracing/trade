"""Universe stress-scenario generation and summary tests."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import stress_test_prefixes as stress


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
            "total_return": 1.0,
            "max_drawdown": -0.1,
            "sharpe": 1.0,
            "calmar": 1.0,
            "total_trades": 0,
            "sleeve_fill_count": 0,
            "date_symbol_side_count": 0,
            "reason_attribution": {
                category: 0 for category in stress.ATTRIBUTION_CATEGORIES
            },
            "max_concurrent_symbols": 0,
            "terminal_risk_lock": False,
            "deployment_policy": "production_daily_replay",
        }

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

    def test_formal_plan_has_983_unique_scenario_ids(self) -> None:
        scenarios = stress._multi_seed_scenarios(
            random_samples=50,
            permutation_samples=50,
            seeds=stress.DEFAULT_SEEDS,
        )

        self.assertEqual(len(scenarios), 983)
        self.assertEqual(
            len({str(item["scenario_id"]) for item in scenarios}),
            983,
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

    def test_checkpoint_signature_covers_canonical_source_tree(self) -> None:
        """A canonical implementation change must invalidate old checkpoints."""
        fingerprints: list[tuple[Path, ...]] = []

        def capture(paths: list[Path]) -> str:
            fingerprints.append(tuple(paths))
            return "fingerprint"

        with TemporaryDirectory() as tmpdir, patch.object(
            stress, "_tree_fingerprint", side_effect=capture
        ):
            stress._run_signature(
                [],
                Path(tmpdir),
                Path(tmpdir),
                source_revision="a" * 40,
            )

        canonical_source = (
            stress.PROJECT_ROOT / "quantfusion" / "application" / "stress.py"
        )
        self.assertIn(canonical_source, fingerprints[0])

    def test_provenance_exposes_independent_source_data_and_scenario_hashes(
        self,
    ) -> None:
        scenarios = stress._multi_seed_scenarios(
            random_samples=1,
            permutation_samples=1,
            seeds=(7,),
        )
        with TemporaryDirectory() as tmpdir, patch.object(
            stress,
            "_tree_fingerprint",
            side_effect=("source-fingerprint", "data-fingerprint"),
        ):
            provenance = stress._build_provenance(
                scenarios,
                Path(tmpdir),
                Path(tmpdir),
                source_revision="a" * 40,
            )

        self.assertEqual(provenance["source_revision"], "a" * 40)
        self.assertEqual(provenance["source_fingerprint"], "source-fingerprint")
        self.assertEqual(provenance["data_fingerprint"], "data-fingerprint")
        self.assertEqual(provenance["scenario_count"], len(scenarios))
        self.assertEqual(provenance["initial_capital"], stress.INITIAL_CAPITAL)
        self.assertRegex(provenance["scenario_signature"], r"^[0-9a-f]{64}$")
        self.assertRegex(provenance["run_signature"], r"^[0-9a-f]{64}$")

    def test_checkpoint_requires_trade_and_bucket_counts_separately(self) -> None:
        scenarios = stress._multi_seed_scenarios(
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
            "reason_attribution": {category: 0 for category in stress.ATTRIBUTION_CATEGORIES},
            "max_concurrent_symbols": 1,
            "terminal_risk_lock": False,
            "deployment_policy": "production_daily_replay",
        }

        with self.assertRaisesRegex(ValueError, "date_symbol_side_count"):
            stress._validated_checkpoint_results({"results": [result]}, scenarios)

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
            "reason_attribution": {category: 0 for category in stress.ATTRIBUTION_CATEGORIES},
        }
        result = stress._permutation_invariance(
            [
                {**common, "scenario_id": "permutation-7-001", "date_symbol_side_count": 1},
                {**common, "scenario_id": "permutation-7-002", "date_symbol_side_count": 2},
            ]
        )

        self.assertFalse(result["invariant"])

    def test_failed_complete_candidate_is_retained_without_canonical_publish(
        self,
    ) -> None:
        scenarios = stress._multi_seed_scenarios(
            random_samples=1,
            permutation_samples=1,
            seeds=(7,),
        )
        results = [self._complete_result(item) for item in scenarios]
        provenance = self._provenance("a" * 40, len(scenarios))
        prefix_artifact = {**provenance, "artifact_status": "current"}
        universe_artifact = {
            **provenance,
            "artifact_status": "current",
            "trade_count_semantics": stress.TRADE_COUNT_SEMANTICS,
            "seeds": [7],
            "scenario_count": len(results),
            "hard_gates": {
                "passed": False,
                "checks": {"absolute_floor": False},
            },
            "promotion_gates": {
                "status": "incomparable_economic_contract",
                "passed": None,
                "permutation_invariance": {"invariant": True},
            },
            "results": results,
        }

        with patch.object(stress, "_atomic_json") as write_artifact:
            published = stress._publish_formal_artifacts(
                prefix_artifact,
                universe_artifact,
                scenarios=scenarios,
                provenance=provenance,
            )

        self.assertFalse(published)
        write_artifact.assert_called_once()
        path, candidate = write_artifact.call_args.args
        self.assertEqual(
            path,
            stress.VALIDATION_ARTIFACT_DIR
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
            [{"gate_family": "hard_gates", "gate": "absolute_floor"}],
        )

    def test_accepted_complete_candidate_updates_canonical_artifacts(self) -> None:
        scenarios = stress._multi_seed_scenarios(
            random_samples=1,
            permutation_samples=1,
            seeds=(7,),
        )
        results = [self._complete_result(item) for item in scenarios]
        provenance = self._provenance("b" * 40, len(scenarios))
        prefix_artifact = {**provenance, "artifact_status": "current"}
        universe_artifact = {
            **provenance,
            "artifact_status": "current",
            "trade_count_semantics": stress.TRADE_COUNT_SEMANTICS,
            "seeds": [7],
            "scenario_count": len(results),
            "hard_gates": {"passed": True, "checks": {"absolute_floor": True}},
            "promotion_gates": {
                "status": "incomparable_economic_contract",
                "passed": None,
                "permutation_invariance": {"invariant": True},
            },
            "results": results,
        }

        with patch.object(stress, "_atomic_json") as write_artifact:
            published = stress._publish_formal_artifacts(
                prefix_artifact,
                universe_artifact,
                scenarios=scenarios,
                provenance=provenance,
            )

        self.assertTrue(published)
        self.assertEqual(write_artifact.call_count, 2)
        self.assertEqual(
            [call.args[0].name for call in write_artifact.call_args_list],
            ["prefix_stress.json", "universe_stress.json"],
        )

    def test_incomplete_candidate_is_not_retained(self) -> None:
        scenarios = stress._multi_seed_scenarios(
            random_samples=1,
            permutation_samples=1,
            seeds=(7,),
        )
        provenance = self._provenance("c" * 40, len(scenarios))
        universe_artifact = {
            **provenance,
            "trade_count_semantics": stress.TRADE_COUNT_SEMANTICS,
            "seeds": [7],
            "hard_gates": {"passed": False, "checks": {"floor": False}},
            "promotion_gates": {
                "passed": None,
                "permutation_invariance": {"invariant": True},
            },
            "scenario_count": len(scenarios),
            "results": [self._complete_result(scenarios[0])],
        }

        with patch.object(stress, "_atomic_json") as write_artifact:
            with self.assertRaisesRegex(ValueError, "exact scenario plan"):
                stress._publish_formal_artifacts(
                    provenance,
                    universe_artifact,
                    scenarios=scenarios,
                    provenance=provenance,
                )

        write_artifact.assert_not_called()

    def test_provenance_mismatch_is_not_retained(self) -> None:
        scenarios = stress._multi_seed_scenarios(
            random_samples=1,
            permutation_samples=1,
            seeds=(7,),
        )
        expected = self._provenance("d" * 40, len(scenarios))
        actual = {**expected, "source_revision": "e" * 40}
        universe_artifact = {
            **actual,
            "trade_count_semantics": stress.TRADE_COUNT_SEMANTICS,
            "seeds": [7],
            "hard_gates": {"passed": False, "checks": {"floor": False}},
            "promotion_gates": {
                "passed": None,
                "permutation_invariance": {"invariant": True},
            },
            "results": [self._complete_result(item) for item in scenarios],
        }

        with patch.object(stress, "_atomic_json") as write_artifact:
            with self.assertRaisesRegex(ValueError, "provenance changed"):
                stress._publish_formal_artifacts(
                    expected,
                    universe_artifact,
                    scenarios=scenarios,
                    provenance=expected,
                )

        write_artifact.assert_not_called()

    def test_persisted_formal_artifacts_are_explicit_historical_baselines(
        self,
    ) -> None:
        scenarios = stress._multi_seed_scenarios(
            random_samples=50,
            permutation_samples=50,
            seeds=stress.DEFAULT_SEEDS,
        )
        signature = stress._run_signature(
            scenarios,
            stress.DATA_DIR,
            stress.REGIME_DATA_DIR,
            source_revision="a" * 40,
        )
        for name in ("prefix_stress.json", "universe_stress.json"):
            payload = json.loads(
                (
                    stress.VALIDATION_ARTIFACT_DIR / name
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                payload["artifact_status"],
                "historical_pre_minimal_account_correctness",
            )
            self.assertEqual(
                payload["trade_count_semantics"],
                "legacy_date_symbol_side_bucket",
            )
            self.assertEqual(
                payload["source_revision"],
                "2066fbf0f99be94142c5d0cb0b6c99d276c2472d",
            )
            self.assertEqual(
                payload["run_signature"],
                "f4fe4580e6c792461bdeffeaea96c12f1c4ab49e63dce468e30b5fbbd19202df",
            )
            self.assertNotEqual(payload["run_signature"], signature)

    def test_rejected_candidate_retains_complete_current_evidence(self) -> None:
        path = (
            stress.VALIDATION_ARTIFACT_DIR
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
        self.assertEqual(
            hashlib.sha256(path.read_bytes()).hexdigest(),
            "a47cf9dedf3e161dc7459369b0f05543fe8bd0732b2352e177dccd9899d687e3",
        )

    def test_historical_pre_pr13_promotion_baseline_is_incomparable(self) -> None:
        current = {
            "scenario_id": "prefix-01",
            "scenario_type": "prefix",
            "total_return": 1.0,
            "max_drawdown": -0.1,
            "total_trades": 24,
            "sleeve_fill_count": 24,
            "date_symbol_side_count": 5,
        }
        incumbent = {
            "artifact_status": "historical_pre_minimal_account_correctness",
            "trade_count_semantics": "legacy_date_symbol_side_bucket",
            "results": [{**current, "total_trades": 5}],
        }

        result = stress._promotion_gates([current], incumbent)

        self.assertEqual(
            result["status"], "incomparable_economic_contract"
        )
        self.assertFalse(result["applicable"])
        self.assertIsNone(result["passed"])
        self.assertTrue(result["permutation_invariance"]["invariant"])
        self.assertNotIn("checks", result)

    def test_hard_gates_keep_legacy_ceilings_on_side_buckets(self) -> None:
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

        result = stress._hard_gates(
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
