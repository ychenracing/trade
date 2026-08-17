"""Contracts for the research optimizer and production replay boundary."""

from __future__ import annotations

import importlib
import unittest
from unittest.mock import patch


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

    def test_resume_fingerprints_cover_every_economic_layer(self) -> None:
        """A production dependency change must invalidate optimizer resumes."""
        fingerprints = importlib.import_module("quantfusion.research.fingerprints")
        required = {
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
        }
        captured: list[set[str]] = []

        def capture(paths) -> str:
            captured.append({path.name for path in paths})
            return "fingerprint"

        with patch.object(fingerprints, "_source_sha", side_effect=capture):
            fingerprints.engine_source_sha()
            fingerprints.replay_source_sha()

        self.assertTrue(required <= captured[0])
        self.assertTrue(required <= captured[1])

    def test_economic_sequence_fingerprints_are_stable_and_sensitive(self) -> None:
        fingerprints = importlib.import_module("quantfusion.research.fingerprints")
        baseline = {
            "trades": [{"symbol": "300308", "shares": 100}],
            "order_events": [{"event": "filled"}],
            "risk_events": [{"event": "sector_guard_on"}],
            "fusion_events": [{"votes": 2}],
            "regime_state_series": [{"state": "TREND"}],
            "pending_signals": [],
        }
        first = fingerprints.economic_sequence_fingerprints(baseline)
        second = fingerprints.economic_sequence_fingerprints(dict(baseline))
        changed = fingerprints.economic_sequence_fingerprints(
            {**baseline, "trades": [{"symbol": "300308", "shares": 200}]}
        )

        self.assertEqual(first, second)
        self.assertNotEqual(first["trades_sha256"], changed["trades_sha256"])
        self.assertEqual(first["order_event_count"], 1)
        self.assertEqual(first["pending_signal_count"], 0)


if __name__ == "__main__":
    unittest.main()
