"""Compatibility facade for the canonical cross-market risk overlay."""

# ruff: noqa: F401

from quantfusion.config.overlay import *  # noqa: F403
from quantfusion.risk.overlay.adapter import make_sell_signal
from quantfusion.risk.overlay.models import RiskAction
from quantfusion.risk.overlay.policy import CrossMarketOverlay

_make_sell_signal = make_sell_signal
