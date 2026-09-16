from __future__ import annotations

import numpy as np
import pandas as pd

from quantfusion.regime.evidence import select_positive_momentum_leaders


REFERENCE_SYMBOLS = ("300308", "300502", "300394", "688008", "603986")


def _trend_frame(drift: float, volume_ratio: float) -> pd.DataFrame:
    dates = pd.bdate_range(end="2026-01-30", periods=260)
    close = 100.0 * np.exp(drift * np.arange(len(dates), dtype=float))
    volume = np.full(len(dates), 1_000_000.0)
    volume[-1] *= volume_ratio
    return pd.DataFrame({"close": close, "volume": volume}, index=dates)


def _spiky_frame() -> pd.DataFrame:
    """Long-horizon winner with a recent volatile, weakening transition."""
    frame = _trend_frame(0.0025, 0.60)
    peak = float(frame["close"].iloc[-21])
    frame.loc[frame.index[-20:], "close"] = np.linspace(
        peak * (1.0 - 0.0025), peak * 0.95, 20
    )
    return frame


def test_leader_ranking_prefers_consistent_risk_adjusted_strength() -> None:
    """One old long-horizon surge must not outrank broad current quality."""
    frames = {
        code: _trend_frame(drift, volume_ratio)
        for code, drift, volume_ratio in zip(
            REFERENCE_SYMBOLS,
            (0.0004, 0.0006, 0.0008, 0.0010, 0.0012),
            (0.80, 1.00, 1.20, 1.40, 1.60),
            strict=True,
        )
    }
    frames["spiky"] = _spiky_frame()
    frames["consistent"] = _trend_frame(0.0011, 1.40)

    def load(code: str, boundary: str) -> pd.DataFrame:
        del boundary
        return frames[code].copy()

    selection = select_positive_momentum_leaders(
        ("spiky", "consistent"),
        data_dir="unused",
        as_of="2026-01-30",
        maximum=1,
        frame_loader=load,
    )

    assert selection.status == "READY"
    assert selection.selected_symbols == ("consistent",)
