from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from quantfusion.engine.causal import CausalBacktestEngine
from quantfusion.engine.universe import BacktestEngine


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
    return result


def _policy() -> SimpleNamespace:
    return SimpleNamespace(
        candidate_lookbacks=(10, 20, 40),
        regime_symbols=("r1", "r2", "r3", "r4", "r5"),
    )


def test_fixed_reference_ranking_prefers_consistent_multi_horizon_strength() -> None:
    """One hot horizon must not outweigh weak evidence across the full trend path."""
    engine = object.__new__(CausalBacktestEngine)
    engine.policy = _policy()
    engine._candidate_score_series = _score_series()

    scores = engine._fixed_reference_scores(
        pd.Timestamp("2026-01-05"), {"spiky", "consistent"}
    )

    assert scores["consistent"] > scores["spiky"]


def test_daily_candidate_ranking_uses_the_same_consistency_preference() -> None:
    """Sleeve admission and portfolio allocation must share ranking semantics."""
    engine = object.__new__(BacktestEngine)
    engine.policy = _policy()
    engine._candidate_score_series = _score_series()

    scores = engine._candidate_reference_scores(
        pd.Timestamp("2026-01-02"), {"spiky", "consistent"}
    )

    assert scores["consistent"] > scores["spiky"]
