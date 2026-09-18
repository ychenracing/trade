"""Experiment D: sector observation drops the worst daily return before averaging."""

from __future__ import annotations

import pandas as pd

from quantfusion.engine.sector_risk import CoreSectorRiskMixin


def _history(closes: list[float], start: str = "2026-01-02") -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame({"close": closes}, index=idx)


def test_drop_worst_indices_keeps_all_but_minimum() -> None:
    keep = CoreSectorRiskMixin._drop_worst_indices([-0.20, -0.01, 0.0, 0.02], drop=1)
    assert keep == [1, 2, 3]


def test_drop_worst_indices_noop_when_too_small() -> None:
    assert CoreSectorRiskMixin._drop_worst_indices([-0.5], drop=1) == [0]
    assert CoreSectorRiskMixin._drop_worst_indices([], drop=1) == []


def test_build_observation_trims_single_name_blowup() -> None:
    """Four flat names + one -20% crash must not look like a -5% sector day."""
    dates = list(pd.bdate_range("2026-01-02", periods=6))
    flat = [100.0, 100.0, 100.0, 100.0, 100.0, 100.0]
    crash = [100.0, 100.0, 100.0, 100.0, 100.0, 80.0]  # -20% on last day
    data_map = {
        "a": _history(flat),
        "b": _history(flat),
        "c": _history(flat),
        "d": _history(flat),
        "blowup": _history(crash),
    }
    # Align indices explicitly
    for df in data_map.values():
        assert list(df.index) == dates

    obs = CoreSectorRiskMixin._build_sector_observation(
        data_map, dates[-1], max_ma=2, shock_ma=2, recovery_ma=2
    )
    assert obs is not None
    assert obs.symbol_count == 5
    # After dropping the -20% name, remaining returns are ~0
    assert abs(obs.equal_return) < 1e-12
    # Plain mean would have been -0.04; robust mean must not inherit the blowup
    plain = (-0.20 + 0.0 + 0.0 + 0.0 + 0.0) / 5.0
    assert plain == -0.04
    assert obs.equal_return > plain
    # Normalized series exclude the blowup name (recovery shares membership)
    assert len(obs.normalized_series) == 4


def test_build_observation_plain_mean_with_single_name() -> None:
    dates = list(pd.bdate_range("2026-01-02", periods=4))
    data_map = {"only": pd.DataFrame({"close": [100.0, 100.0, 100.0, 90.0]}, index=dates)}
    obs = CoreSectorRiskMixin._build_sector_observation(
        data_map, dates[-1], max_ma=2, shock_ma=2, recovery_ma=2
    )
    assert obs is not None
    assert obs.symbol_count == 1
    assert abs(obs.equal_return - (-0.10)) < 1e-12
    assert len(obs.normalized_series) == 1
