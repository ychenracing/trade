"""Sector guard observes the trade pool; market regime keeps regime_symbols."""

from __future__ import annotations

import math
from unittest.mock import MagicMock

import pandas as pd
import pytest

from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.engine.universe import BacktestEngine, SleeveBacktestEngine
from quantfusion.engine.universe_risk import GUARD_SCOPE_MODE


def _sleeve(
    *,
    tradable: tuple[str, ...],
    min_symbols: int | None = None,
) -> SleeveBacktestEngine:
    policy = PortfolioPolicy(allocation_mode="single")
    cfg: dict = {
        "sector_shock_ma": 2,
        "sector_recovery_ma": 2,
        "market_regime_enabled": False,
    }
    if min_symbols is not None:
        cfg["sector_guard_min_symbols"] = min_symbols
    sleeve = SleeveBacktestEngine(
        1_000_000,
        cfg=cfg,
        policy=policy,
        allocation_lookbacks=policy.single_lookbacks,
        sleeve_name="test",
    )
    sleeve._tradable_symbol_codes = set(tradable)
    sleeve._update_market_regime = MagicMock()  # type: ignore[method-assign]
    return sleeve


def _flat_map(symbols: tuple[str, ...], dates: list[pd.Timestamp]) -> dict[str, pd.DataFrame]:
    return {
        symbol: pd.DataFrame({"close": [100.0] * len(dates)}, index=dates)
        for symbol in symbols
    }


def test_guard_scopes_to_tradable_not_regime_symbols() -> None:
    policy = PortfolioPolicy()
    trade_pool = ("300308", "300502", "300394")
    assert "688008" in policy.regime_symbols
    assert "688008" not in trade_pool

    sleeve = _sleeve(tradable=trade_pool, min_symbols=1)
    dates = list(pd.bdate_range("2026-01-02", periods=5))
    data_map = _flat_map(trade_pool + ("688008", "603986"), dates)

    scoped = sleeve._sector_guard_observation_data(data_map)
    assert set(scoped) == set(trade_pool)
    assert "688008" not in scoped


def test_guard_ignores_out_of_pool_regime_ref_for_quorum() -> None:
    """688008 in data_map must not satisfy trade-pool min_symbols alone."""
    trade_pool = ("300308",)
    sleeve = _sleeve(tradable=trade_pool, min_symbols=2)
    dates = list(pd.bdate_range("2026-01-02", periods=5))
    data_map = _flat_map(("300308", "688008"), dates)
    # Truncate the only tradable so observation count is 0 from trade pool
    # while regime ref remains complete — guard must report insufficient.
    data_map["300308"] = data_map["300308"].iloc[:-1]
    sleeve.sector_guard_active = True

    state = sleeve._update_sector_guard(
        data_map,
        dates[-1],
        dates,
        {date: index for index, date in enumerate(dates)},
    )

    assert state == "active"
    assert sleeve.risk_events[-1]["event"] == "sector_guard_data_insufficient"
    assert sleeve.risk_events[-1]["observed_symbols"] == 0
    sleeve._update_market_regime.assert_called_once()


def test_market_regime_still_invoked_with_full_data_map() -> None:
    trade_pool = ("300308", "300502")
    sleeve = _sleeve(tradable=trade_pool, min_symbols=1)
    dates = list(pd.bdate_range("2026-01-02", periods=5))
    data_map = _flat_map(trade_pool + ("688008",), dates)

    sleeve._update_sector_guard(
        data_map,
        dates[-1],
        dates,
        {date: index for index, date in enumerate(dates)},
    )

    args, _kwargs = sleeve._update_market_regime.call_args
    assert args[0] is data_map
    assert "688008" in args[0]


def test_runtime_sleeve_cfg_scales_min_to_trade_pool() -> None:
    engine = BacktestEngine(2_000_000)
    assert engine._sector_guard_min_from_user is False
    for n in (1, 3, 5, 13, 17):
        cfg = engine._runtime_sleeve_cfg(n)
        assert cfg["sector_guard_min_symbols"] == max(1, math.ceil(n * 0.8))


def test_runtime_sleeve_cfg_preserves_explicit_min_symbols() -> None:
    engine = BacktestEngine(2_000_000, cfg={"sector_guard_min_symbols": 2})
    assert engine._sector_guard_min_from_user is True
    cfg = engine._runtime_sleeve_cfg(17)
    assert cfg["sector_guard_min_symbols"] == 2


def test_guard_scope_mode_constant() -> None:
    assert GUARD_SCOPE_MODE == "tradable_pool_breadth"


def test_empty_tradable_does_not_fall_back_to_regime_basket() -> None:
    sleeve = _sleeve(tradable=(), min_symbols=1)
    dates = list(pd.bdate_range("2026-01-02", periods=5))
    data_map = _flat_map(PortfolioPolicy().regime_symbols, dates)
    scoped = sleeve._sector_guard_observation_data(data_map)
    assert scoped == {}
