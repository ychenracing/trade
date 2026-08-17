"""Contracts for the research optimizer and production replay boundary."""

from __future__ import annotations

import importlib
import unittest


class OptimizerModuleContracts(unittest.TestCase):
    """Research primitives have one canonical implementation."""

    def test_legacy_optimizer_resolves_to_canonical_application(self) -> None:
        legacy = importlib.import_module("quant_fusion_optimizer")
        canonical = importlib.import_module("quantfusion.application.optimizer")
        self.assertIs(legacy, canonical)

    def test_candidates_evaluation_and_search_are_canonical(self) -> None:
        optimizer = importlib.import_module("quant_fusion_optimizer")
        candidates = importlib.import_module("quantfusion.research.candidates")
        evaluation = importlib.import_module("quantfusion.research.evaluation")
        search = importlib.import_module("quantfusion.research.search")
        self.assertIs(optimizer.Candidate, candidates.Candidate)
        self.assertIs(optimizer.CandidateRunner, evaluation.CandidateRunner)
        self.assertIs(optimizer.WalkForwardOptimizer, search.WalkForwardOptimizer)

    def test_candidate_runner_uses_production_replay(self) -> None:
        evaluation = importlib.import_module("quantfusion.research.evaluation")
        replay = importlib.import_module("quantfusion.engine.replay")
        self.assertIs(evaluation.ProductionReplayEngine, replay.ProductionReplayEngine)

    def test_production_fingerprint_covers_every_execution_dependency(self) -> None:
        fingerprints = importlib.import_module("quantfusion.research.fingerprints")
        covered = {path.name for path in fingerprints.production_source_paths()}
        self.assertTrue(
            {
                "domain",
                "config",
                "data",
                "indicators",
                "strategy",
                "execution",
                "portfolio",
                "risk",
                "regime",
                "engine",
            }.issubset(covered)
        )


if __name__ == "__main__":
    unittest.main()
