"""Experiment E: enlarged fixed regime referee toward RISK_BASKET ∩ universe."""

from __future__ import annotations

import math

from quantfusion.config.overlay import RISK_BASKET
from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.config.universe import ORDERED_SYMBOLS
from quantfusion.engine.universe import BacktestEngine

MAIN_FIVE = ("300308", "300502", "300394", "688008", "603986")


def test_regime_symbols_are_main5_union_risk_basket_in_universe() -> None:
    extras = tuple(
        s for s in RISK_BASKET if s in ORDERED_SYMBOLS and s not in MAIN_FIVE
    )
    expected = MAIN_FIVE + extras
    policy = PortfolioPolicy()
    assert policy.regime_symbols == expected
    assert len(policy.regime_symbols) == 14
    assert "688008" in policy.regime_symbols  # kept even though out of ORDERED
    # No auto-selection from a trading pool: fixed tuple only
    assert all(s in RISK_BASKET or s in MAIN_FIVE for s in policy.regime_symbols)


def test_sector_guard_min_symbols_follows_ceil_80pct() -> None:
    engine = BacktestEngine(2_000_000)
    n = len(PortfolioPolicy().regime_symbols)
    assert engine.cfg["sector_guard_min_symbols"] == max(1, math.ceil(n * 0.8))
    assert engine.cfg["sector_guard_min_symbols"] == 12
