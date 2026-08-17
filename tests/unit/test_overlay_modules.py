"""Contracts for overlay policy actions and the pending-signal adapter."""

from __future__ import annotations

import importlib
import unittest
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pandas as pd


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

    def test_policy_evaluate_returns_actions_without_mutating_pending(self) -> None:
        """Policy decisions stay immutable until the engine adapter applies them."""
        policy = importlib.import_module("quantfusion.risk.overlay.policy")
        adapter = importlib.import_module("quantfusion.risk.overlay.adapter")
        date = pd.Timestamp("2026-01-06")
        index = pd.to_datetime(["2026-01-05", "2026-01-06"])
        frame = pd.DataFrame(
            {
                "open": [70.0, 70.0],
                "high": [70.0, 70.0],
                "low": [70.0, 70.0],
                "close": [70.0, 70.0],
                "volume": [1_000_000, 1_000_000],
            },
            index=index,
        )
        position = SimpleNamespace(
            shares=100,
            highest_close_since_entry=100.0,
            entry_price=90.0,
        )
        sleeve = SimpleNamespace(positions={"AAA": {"fast": position}})
        state = SimpleNamespace(sleeve=sleeve, data_map={"AAA": frame}, pending=[])
        overlay = policy.CrossMarketOverlay()

        actions = overlay.evaluate(
            [state], date, date_pos=1,
            assets=2_000_000, peak=2_000_000, scoring_fn=None,
        )

        self.assertEqual(state.pending, [])
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].reason, "catastrophe_stop")
        adapter.apply_risk_actions(
            actions, [state], date_str="2026-01-06", events=overlay.events
        )
        self.assertEqual(state.pending[0][0].reason.split(":")[0], "catastrophe_stop")

    def test_adapter_uses_action_priority_and_existing_pending(self) -> None:
        """Adaptation consolidates new and carried risk sells by action priority."""
        adapter = importlib.import_module("quantfusion.risk.overlay.adapter")
        models = importlib.import_module("quantfusion.risk.overlay.models")
        carried = adapter.make_sell_signal(
            "300308", "fast", 100, 10.0, "2026-08-16", "sector_risk_trim"
        )
        state = SimpleNamespace(pending=[(carried, None)])
        action = models.RiskAction(
            symbol="300308",
            strategy_name="fast",
            shares=80,
            price=9.0,
            signal_date="2026-08-17",
            reason="custom_priority_exit",
            priority=10_000,
        )
        events: list[dict] = []

        adapter.apply_risk_actions(
            (action,), [state], date_str="2026-08-17", events=events
        )

        self.assertEqual(len(state.pending), 1)
        self.assertEqual(state.pending[0][0].reason, "custom_priority_exit")
        self.assertEqual(events[0]["winner_reason"], "custom_priority_exit")


if __name__ == "__main__":
    unittest.main()
