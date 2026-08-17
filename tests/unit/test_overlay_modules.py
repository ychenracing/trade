"""Contracts for overlay policy actions and the pending-signal adapter."""

from __future__ import annotations

import importlib
import unittest
from dataclasses import FrozenInstanceError
from types import SimpleNamespace


class OverlayModuleContracts(unittest.TestCase):
    """Overlay decisions are canonical immutable actions before adaptation."""

    def test_legacy_overlay_is_canonical_policy(self) -> None:
        legacy = importlib.import_module("cross_market_overlay")
        policy = importlib.import_module("quantfusion.risk.overlay.policy")
        self.assertIs(legacy.CrossMarketOverlay, policy.CrossMarketOverlay)

    def test_risk_governance_is_canonical(self) -> None:
        legacy = importlib.import_module("risk_governance")
        canonical = importlib.import_module("quantfusion.risk.governance")
        self.assertIs(legacy.build_risk_opinion, canonical.build_risk_opinion)
        self.assertIs(legacy.assess_warmup_health, canonical.assess_warmup_health)

    def test_risk_action_is_immutable(self) -> None:
        models = importlib.import_module("quantfusion.risk.overlay.models")
        action = models.RiskAction(
            symbol="300308",
            strategy_name="turtle",
            shares=100,
            price=10.0,
            signal_date="2026-08-17",
            reason="catastrophe_stop",
            priority=100,
        )
        with self.assertRaises(FrozenInstanceError):
            action.shares = 200

    def test_adapter_preserves_pending_signal_contract(self) -> None:
        adapter = importlib.import_module("quantfusion.risk.overlay.adapter")
        models = importlib.import_module("quantfusion.risk.overlay.models")
        state = SimpleNamespace(pending=[])
        action = models.RiskAction(
            symbol="300308",
            strategy_name="turtle",
            shares=100,
            price=10.0,
            signal_date="2026-08-17",
            reason="sector_risk_trim",
            extra="level=2",
            priority=60,
        )
        adapter.apply_risk_actions((action,), state)
        signal, strategy = state.pending[0]
        self.assertIsNone(strategy)
        self.assertEqual(signal.reason, "sector_risk_trim:level=2")
        self.assertEqual(signal.target_shares, 100)

    def test_action_priority_is_resolved_before_pending_adaptation(self) -> None:
        adapter = importlib.import_module("quantfusion.risk.overlay.adapter")
        models = importlib.import_module("quantfusion.risk.overlay.models")
        actions = (
            models.RiskAction(
                symbol="300308",
                strategy_name="turtle",
                shares=100,
                price=10.0,
                signal_date="2026-08-17",
                reason="concentration_trim",
                priority=40,
            ),
            models.RiskAction(
                symbol="300308",
                strategy_name="turtle",
                shares=100,
                price=10.0,
                signal_date="2026-08-17",
                reason="catastrophe_stop",
                priority=100,
            ),
        )
        winners, suppressed = adapter.resolve_risk_actions(actions)
        self.assertEqual([action.reason for action in winners], ["catastrophe_stop"])
        self.assertEqual([action.reason for action in suppressed], ["concentration_trim"])

    def test_adapter_reconciles_new_actions_with_existing_risk_pending(self) -> None:
        """A carried T+1 action still participates in next-day priority resolution."""
        adapter = importlib.import_module("quantfusion.risk.overlay.adapter")
        models = importlib.import_module("quantfusion.risk.overlay.models")
        states = [SimpleNamespace(pending=[]), SimpleNamespace(pending=[])]
        carried = models.RiskAction(
            symbol="300502",
            strategy_name="turtle_breakout",
            shares=1_600,
            price=300.0,
            signal_date="2026-03-26",
            reason="concentration_trim",
            priority=40,
            state_index=0,
        )
        newer = models.RiskAction(
            symbol="300502",
            strategy_name="turtle_breakout",
            shares=600,
            price=310.0,
            signal_date="2026-03-27",
            reason="concentration_trim",
            priority=40,
            state_index=1,
        )

        adapter.apply_risk_actions((carried,), states)
        winners, suppressed = adapter.apply_risk_actions((newer,), states)

        self.assertEqual([len(state.pending) for state in states], [1, 0])
        signal = states[0].pending[0][0]
        self.assertEqual(signal.target_shares, 1_600)
        self.assertEqual(signal.risk_priority, 40)
        self.assertEqual([action.shares for action in winners], [1_600])
        self.assertEqual([action.shares for action in suppressed], [600])

    def test_policy_exposes_evaluation_before_engine_adaptation(self) -> None:
        policy = importlib.import_module("quantfusion.risk.overlay.policy")
        self.assertTrue(callable(policy.CrossMarketOverlay().evaluate_actions))


if __name__ == "__main__":
    unittest.main()
