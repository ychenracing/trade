from __future__ import annotations

import numpy as np
import pandas as pd

from quantfusion.regime.evidence import select_positive_momentum_leaders


REFERENCE_SYMBOLS = ("300308", "300502", "300394", "688008", "603986")


def _trend_frame(
    drift: float, volume_ratio: float, *, periods: int = 260
) -> pd.DataFrame:
    dates = pd.bdate_range(end="2026-01-30", periods=periods)
    close = 100.0 * np.exp(drift * np.arange(len(dates), dtype=float))
    volume = np.full(len(dates), 1_000_000.0)
    volume[-1] *= volume_ratio
    return pd.DataFrame({"close": close, "volume": volume}, index=dates)


def _spiky_frame() -> pd.DataFrame:
    """Mature long-horizon winner with a recent weakening transition."""
    frame = _trend_frame(0.0025, 0.60)
    peak = float(frame["close"].iloc[-21])
    frame.loc[frame.index[-20:], "close"] = np.linspace(
        peak * (1.0 - 0.0025), peak * 0.95, 20
    )
    return frame


def _challenge_frames() -> dict[str, pd.DataFrame]:
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
    # 120 observations: enough for the emerging channel but intentionally below
    # the 241-session mature-leader requirement.
    frames["consistent"] = _trend_frame(0.0011, 1.40, periods=120)
    return frames


def test_emerging_leader_can_challenge_mature_spike_on_broad_quality() -> None:
    """A genuinely strong emerging leader may replace a weakening mature name."""
    frames = _challenge_frames()

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


def test_invalid_reference_feature_contract_disables_optional_quality_enrichment() -> None:
    """Malformed optional quality evidence must preserve the legacy leader path."""
    frames = _challenge_frames()
    malformed = frames[REFERENCE_SYMBOLS[0]].copy()
    malformed.index = list(malformed.index[:-1]) + [malformed.index[-2]]
    frames[REFERENCE_SYMBOLS[0]] = malformed

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
    assert selection.selected_symbols == ("spiky",)
