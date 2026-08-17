"""Composed cross-market overlay policy."""

from __future__ import annotations

import pandas as pd

from quantfusion.risk.overlay.adapter import (
    apply_cooldown_buy_gate,
    apply_risk_actions,
    apply_risk_buy_gate,
    consolidate_risk_sells,
)
from quantfusion.risk.overlay.actions import OverlayActionMixin
from quantfusion.risk.overlay.evidence import OverlayEvidenceMixin
from quantfusion.risk.overlay.policy_base import OverlayPolicyMixin


class CrossMarketOverlay(
    OverlayPolicyMixin,
    OverlayEvidenceMixin,
    OverlayActionMixin,
):
    """Evaluate policy decisions and expose legacy adapter entry points."""

    def on_day(
        self,
        states: list,
        date: pd.Timestamp,
        date_pos: int,
        assets: float,
        peak: float,
        scoring_fn,
    ) -> None:
        """Compatibility wrapper: evaluate, then adapt actions to pending."""
        actions = self.evaluate(
            states, date, date_pos, assets, peak, scoring_fn
        )
        apply_risk_actions(
            actions,
            states,
            date_str=date.strftime("%Y-%m-%d"),
            events=self.events,
        )

    def _consolidate_risk_sells(self, states: list, date_str: str) -> None:
        """Compatibility wrapper for the queue adapter's consolidation."""
        consolidate_risk_sells(states, date_str, self.events)

    def block_cooldown_buys(
        self, states: list, date: pd.Timestamp, date_pos: int
    ) -> None:
        """Compatibility wrapper for catastrophe-cooldown admission."""
        apply_cooldown_buy_gate(self, states, date, date_pos)

    def block_risk_buys(
        self, states: list, date: pd.Timestamp, held_symbols: set[str]
    ) -> None:
        """Compatibility wrapper for market-risk buy admission."""
        apply_risk_buy_gate(self, states, date, held_symbols)
