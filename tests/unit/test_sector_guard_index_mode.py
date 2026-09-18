"""Experiment C: sector guard driven by fixed product indices."""

from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest

from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.engine.universe import SleeveBacktestEngine
from quantfusion.engine.universe_risk import (
    GUARD_SCOPE_BY_MODE,
    INDEX_MODE_DUAL_CONFIRM,
    INDEX_MODE_TECH_ONLY,
)


def _sleeve(mode: str, *, min_symbols: int | None = None) -> SleeveBacktestEngine:
    policy = PortfolioPolicy(allocation_mode="single")
    cfg: dict = {
        "sector_guard_index_mode": mode,
        "sector_shock_ma": 2,
        "sector_recovery_ma": 2,
        "sector_shock_return": -0.05,
        "sector_shock_breadth": 0.2,
        "sector_shock_window": 4,
        "sector_shock_confirmations": 2,
        "sector_recovery_breadth": 0.8,
        "sector_recovery_confirmations": 2,
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
    sleeve._update_market_regime = MagicMock()  # type: ignore[method-assign]
    return sleeve


def _frame(closes: list[float], dates: list[pd.Timestamp]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "close": closes,
            "open": closes,
            "high": closes,
            "low": closes,
            "volume": 1.0,
        },
        index=dates,
    )


def test_tech_only_observes_000682_not_regime_symbols() -> None:
    sleeve = _sleeve(INDEX_MODE_TECH_ONLY, min_symbols=1)
    dates = list(pd.bdate_range("2026-01-02", periods=5))
    crash = [100.0, 100.0, 100.0, 90.0, 80.0]
    sleeve._sector_guard_index_frames = {
        "000300": _frame(crash, dates),
        "000682": _frame(crash, dates),
    }
    scoped = sleeve._sector_guard_observation_data(
        {"300308": _frame(crash, dates), "688008": _frame(crash, dates)}
    )
    assert set(scoped) == {"000682"}
    assert sleeve._sector_guard_scope_mode() == GUARD_SCOPE_BY_MODE[INDEX_MODE_TECH_ONLY]


def test_dual_confirm_requires_both_indices_to_shock() -> None:
    dates = list(pd.bdate_range("2026-01-02", periods=10))
    crash = [100, 100, 100, 100, 100, 90, 80, 70, 60, 50]
    mild = [100, 100, 100, 100, 100, 99, 98, 97, 96, 95]
    only_tech = _sleeve(INDEX_MODE_DUAL_CONFIRM, min_symbols=2)
    only_tech._sector_guard_index_frames = {
        "000300": _frame(mild, dates),
        "000682": _frame(crash, dates),
    }
    for day in dates[3:]:
        only_tech._update_sector_guard(
            {}, day, dates, {d: i for i, d in enumerate(dates)}
        )
    assert only_tech.sector_guard_active is False
    assert not any(e["event"] == "sector_guard_on" for e in only_tech.risk_events)

    both = _sleeve(INDEX_MODE_DUAL_CONFIRM, min_symbols=2)
    both._sector_guard_index_frames = {
        "000300": _frame(crash, dates),
        "000682": _frame(crash, dates),
    }
    for day in dates[3:]:
        both._update_sector_guard({}, day, dates, {d: i for i, d in enumerate(dates)})
    assert both.sector_guard_active is True
    assert any(e["event"] == "sector_guard_on" for e in both.risk_events)


def test_missing_index_directory_fails_closed() -> None:
    sleeve = _sleeve(INDEX_MODE_TECH_ONLY, min_symbols=1)
    sleeve.cfg = dict(sleeve.cfg)
    sleeve.cfg["sector_guard_index_data_dir"] = "/tmp/qf-missing-regime-index-dir"
    with pytest.raises(RuntimeError, match="fail closed"):
        sleeve._ensure_sector_guard_index_frames()
