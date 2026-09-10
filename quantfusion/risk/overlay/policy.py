"""Composed cross-market overlay policy."""

from __future__ import annotations

from quantfusion.risk.overlay.actions import OverlayActionMixin
from quantfusion.risk.overlay.evidence import OverlayEvidenceMixin
from quantfusion.risk.overlay.policy_base import OverlayPolicyMixin


class CrossMarketOverlay(
    OverlayPolicyMixin,
    OverlayEvidenceMixin,
    OverlayActionMixin,
):
    """Evaluate cross-market evidence into immutable risk actions."""

    C6_S_PRODUCTION = True
