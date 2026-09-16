from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from quantfusion.engine.universe import SleeveBacktestEngine


def _series(value: float) -> pd.Series:
    return pd.Series(
        [value],
        index=pd.to_datetime(["2026-01-02"]),
        dtype="float64",
    )


def _score_series() -> dict[str, dict[int, pd.Series]]:
    windows = (10, 20, 40)
    result: dict[str, dict[int, pd.Series]] = {}
    for index in range(1, 6):
        result[f"r{index}"] = {
            window: _series(float(index)) for window in windows
        }
    result["spiky"] = {
        10: _series(6.0),
        20: _series(6.0),
        40: _series(1.0),
    }
    result["consistent"] = {
        10: _series(3.0),
        20: _series(3.0),
        40: _series(3.0),
    }
    for code in ("low1", "low2", "low3"):
        result[code] = {window: _series(1.0) for window in windows}
    return result


def _policy() -> SimpleNamespace:
    return SimpleNamespace(
        candidate_lookbacks=(10, 20, 40),
        regime_symbols=("r1", "r2", "r3", "r4", "r5"),
        candidate_reference_percentile=0.50,
    )


def test_quality_ordering_does_not_change_existing_admission_scores() -> None:
    engine = object.__new__(SleeveBacktestEngine)
    engine.policy = _policy()
    engine.cfg = {
        "max_positions": 1,
        "adaptive_max_positions": False,
        "sticky_candidates": False,
    }
    engine._external_risk_level = 0
    engine._regime_state = "NORMAL"
    engine._candidate_score_series = _score_series()
    candidates = {"spiky", "consistent", "low1", "low2", "low3"}
    engine._tradable_symbol_codes = set(candidates)

    scores = engine._candidate_reference_scores(
        pd.Timestamp("2026-01-02"), candidates
    )
    assert abs(scores["spiky"] - (11.0 / 15.0)) < 1e-12
    assert abs(scores["consistent"] - 0.60) < 1e-12

    selected = engine._select_momentum_candidates(
        {}, {code: code for code in candidates}, pd.Timestamp("2026-01-02")
    )

    assert selected == {"consistent"}
