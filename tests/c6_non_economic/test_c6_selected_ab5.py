"""The selected release uses AB5, never the unselected early-S action."""
from __future__ import annotations

import inspect
from pathlib import Path

from quantfusion.risk.overlay.actions import OverlayActionMixin
from quantfusion.risk.overlay.policy import CrossMarketOverlay


def test_selected_ab5_does_not_offer_early_s_execution() -> None:
    assert CrossMarketOverlay.C6_S_PRODUCTION is False
    assert "early_s_evidence" not in inspect.signature(
        OverlayActionMixin._apply_concentration_guard
    ).parameters


def test_selected_ab5_has_no_unselected_s_entrypoint() -> None:
    root = Path(__file__).resolve().parents[2]
    assert not (root / "quantfusion/application/c6_s_qualification.py").exists()
