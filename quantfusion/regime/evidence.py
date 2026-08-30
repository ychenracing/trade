"""Point-in-time index evidence and positive-momentum leader selection."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Callable, Sequence, cast

import numpy as np
import pandas as pd

from quantfusion.config.regime import (
    EMERGING_MIN_DAYS,
    LEADER_LOOKBACK,
    MAX_EMERGING_LEADERS,
    MAX_EVIDENCE_STALENESS_DAYS,
    MAX_LEADERS,
    REGIME_INDEX_FILES,
)
from quantfusion.data.providers import DataFetcher
from quantfusion.regime.models import IndexTrend, LeaderSelection, RegimeEvidence


def _timestamp(value: str | pd.Timestamp) -> pd.Timestamp:
    """Parse one finite timestamp and narrow pandas' optional NaT type."""
    parsed = pd.Timestamp(value)
    if parsed is pd.NaT:
        raise ValueError("date must not be NaT")
    return cast(pd.Timestamp, parsed)


def _normalized_timestamp(value: str | pd.Timestamp) -> pd.Timestamp:
    """Return a normalized finite timestamp."""
    return cast(pd.Timestamp, _timestamp(value).normalize())


def _local_frame(data_dir: str | Path, code: str, end_date: str) -> pd.DataFrame:
    """Load a local validated frame without reading beyond ``end_date``."""
    boundary = _normalized_timestamp(end_date)
    start = cast(pd.Timestamp, boundary - pd.Timedelta(days=900)).strftime(
        "%Y-%m-%d"
    )
    frame = DataFetcher.load_stock_data(
        code,
        start,
        boundary.strftime("%Y-%m-%d"),
        data_dir=str(data_dir),
    )
    return frame.loc[frame.index <= boundary].copy()


def detect_regime(data_dir: str | Path, *, as_of: str) -> RegimeEvidence:
    """Require both fixed indices to have fresh, complete trend evidence."""
    boundary = _normalized_timestamp(as_of)
    observations: list[IndexTrend] = []
    for code in REGIME_INDEX_FILES.values():
        try:
            frame = _local_frame(data_dir, code, str(boundary.date()))
        except (OSError, RuntimeError, ValueError):
            continue
        closes = pd.Series(
            pd.to_numeric(frame["close"], errors="coerce"), index=frame.index
        ).dropna()
        if len(closes) < 60:
            continue
        close = float(closes.iloc[-1])
        ma20 = float(closes.tail(20).mean())
        ma60 = float(closes.tail(60).mean())
        if not all(
            math.isfinite(value) and value > 0 for value in (close, ma20, ma60)
        ):
            continue
        observed_date = _normalized_timestamp(str(closes.index[-1]))
        if (boundary - observed_date).days > MAX_EVIDENCE_STALENESS_DAYS:
            continue
        observations.append(
            IndexTrend(
                code=code,
                observed_date=str(observed_date.date()),
                close=close,
                ma20=ma20,
                ma60=ma60,
                trending=ma20 > ma60,
            )
        )
    if len(observations) != len(REGIME_INDEX_FILES):
        regime = "unknown"
    else:
        regime = "trending" if all(item.trending for item in observations) else "choppy"
    return RegimeEvidence(
        as_of=str(boundary.date()),
        regime=regime,
        observations=tuple(observations),
    )


def select_positive_momentum_leaders(
    symbols: Sequence[str],
    *,
    data_dir: str | Path,
    as_of: str,
    maximum: int = MAX_LEADERS,
    frame_loader: Callable[[str, str], pd.DataFrame] | None = None,
) -> LeaderSelection:
    """Select positive long-horizon leaders with deterministic tie-breaking.

    Uses multi-factor weak-market scoring (section 12.2):
    - 240-day momentum (25%)
    - 120-day relative strength vs reference basket (25%)
    - 60-day momentum (20%)
    - Drawdown resilience (15%)
    - Trend repair: 5-day vs 20-day momentum (15%)
    """
    normalized = tuple(sorted(str(symbol) for symbol in symbols))
    if not normalized or len(normalized) != len(set(normalized)):
        raise ValueError("symbols must be a non-empty set without duplicates")
    if maximum < 1:
        raise ValueError("maximum must be positive")
    boundary = _normalized_timestamp(as_of)
    observations: list[tuple[float, str, bool]] = []
    observed_codes: set[str] = set()

    def load_frame(code: str) -> pd.DataFrame:
        if frame_loader is None:
            return _local_frame(data_dir, code, str(boundary.date()))
        frame = frame_loader(code, str(boundary.date()))
        return frame.loc[frame.index <= boundary].copy()

    # Load reference basket for relative strength calculation
    reference_symbols = ("300308", "300502", "300394", "688008", "603986")
    ref_returns: list[float] = []
    for ref_code in reference_symbols:
        try:
            ref_frame = load_frame(ref_code)
            ref_closes = pd.Series(
                pd.to_numeric(ref_frame["close"], errors="coerce"),
                index=ref_frame.index,
            ).dropna()
            if len(ref_closes) >= 121:
                ref_ret = float(ref_closes.iloc[-1] / ref_closes.iloc[-121] - 1.0)
                ref_returns.append(ref_ret)
        except (OSError, RuntimeError, ValueError):
            continue
    ref_avg_return = float(np.mean(ref_returns)) if ref_returns else 0.0

    for code in normalized:
        try:
            frame = load_frame(code)
            closes = pd.Series(
                pd.to_numeric(frame["close"], errors="coerce"), index=frame.index
            ).dropna()
        except (OSError, RuntimeError, ValueError):
            continue
        # Report P0-5: a symbol only needs the SHORT emerging-window history to
        # be considered (the old hard `len(closes) < LEADER_LOOKBACK + 1`
        # continue blocked every genuinely new leader). A mature channel still
        # requires the full 240-day history and positive 240-day momentum; an
        # emerging channel requires only short-horizon momentum + breakout.
        if len(closes) < EMERGING_MIN_DAYS:
            continue
        observed_date = _normalized_timestamp(str(closes.index[-1]))
        if (boundary - observed_date).days > MAX_EVIDENCE_STALENESS_DAYS:
            continue
        observed_codes.add(code)
        close = float(closes.iloc[-1])

        # Mature-channel gate: needs the full 240-day history and positive
        # long-horizon momentum. Symbols that fail this gate are STILL eligible
        # for the emerging channel (report P0-5).
        has_mature_history = len(closes) >= LEADER_LOOKBACK + 1
        momentum_240 = 0.0
        if has_mature_history:
            momentum_240 = float(closes.iloc[-1] / closes.iloc[-LEADER_LOOKBACK - 1] - 1.0)
        is_mature = has_mature_history and math.isfinite(momentum_240) and momentum_240 > 0

        # Multi-factor scoring (report 4.5: mature + emerging dual channel).
        # Both channels are scored against a FIXED AI reference pool (not the
        # caller's pool) so adding/removing a symbol never changes an unchanged
        # symbol's score.
        # 60-day momentum
        if len(closes) >= 61:
            momentum_60 = float(closes.iloc[-1] / closes.iloc[-61] - 1.0)
        else:
            momentum_60 = 0.0

        # 20-day momentum (drives the emerging-leader channel).
        if len(closes) >= 21:
            momentum_20 = float(closes.iloc[-1] / closes.iloc[-21] - 1.0)
        else:
            momentum_20 = 0.0

        # Emerging-channel gate: short history + positive short-horizon momentum
        # + a real breakout setup (price near its 20-day high). This replaces the
        # old 240-day requirement for NEW leaders (report P0-5).
        if len(closes) >= 20:
            high20 = float(closes.iloc[-20:].max())
            breakout_quality = close / high20 if high20 > 0 else 0.0
        else:
            breakout_quality = 0.0
        is_emerging = (
            math.isfinite(momentum_60) and momentum_60 > 0
            and math.isfinite(momentum_20) and momentum_20 > 0
            and breakout_quality >= 0.90
        )
        if not (is_mature or is_emerging):
            continue

        # Relative strength vs reference basket (120-day): compare the
        # symbol's own 120-day return against the basket's 120-day return.
        if len(closes) >= 121:
            symbol_120 = float(closes.iloc[-1] / closes.iloc[-121] - 1.0)
            # Relative strength vs the reference basket. When the basket is
            # empty/missing its average falls back to 0, in which case the
            # symbol's own 120-day return is its relative strength (subtracting
            # 0 is a no-op) rather than being discarded.
            rs_120 = symbol_120 - ref_avg_return
        else:
            rs_120 = 0.0

        # Drawdown resilience: how far from 60-day peak (lower is better)
        if len(closes) >= 60:
            peak_60 = float(closes.iloc[-60:].max())
            drawdown_from_peak = 1.0 - float(closes.iloc[-1] / peak_60) if peak_60 > 0 else 0.0
            resilience = 1.0 - min(1.0, drawdown_from_peak)
        else:
            resilience = 0.0

        # Trend repair: 5-day vs 20-day momentum
        if len(closes) >= 21:
            mom_5 = float(closes.iloc[-1] / closes.iloc[-6] - 1.0)
            mom_20 = float(closes.iloc[-1] / closes.iloc[-21] - 1.0)
            trend_repair = mom_5 - mom_20
        else:
            trend_repair = 0.0

        # Volume expansion over 20 days (from the raw frame, if available).
        volume_expansion = 0.0
        if "volume" in frame.columns and len(frame) >= 21:
            volumes = cast(
                pd.Series, pd.to_numeric(frame["volume"], errors="coerce")
            )
            cur_vol = float(volumes.iloc[-1])
            avg_vol = float(volumes.iloc[-21:-1].mean())
            if math.isfinite(cur_vol) and math.isfinite(avg_vol) and avg_vol > 0:
                volume_expansion = max(0.0, min(2.0, cur_vol / avg_vol))

        # Mature-leader channel: long-horizon strength + relative strength +
        # resilience + trend repair (report 4.5).
        mature_score = (
            0.25 * max(0.0, momentum_240)
            + 0.25 * max(0.0, rs_120)
            + 0.20 * momentum_60
            + 0.15 * resilience
            + 0.15 * max(0.0, trend_repair)
        )
        # Emerging-leader channel: short-horizon momentum + breakout quality +
        # volume expansion + trend repair, so new market leaders are captured
        # even when they have no long 240-day history (report 4.5/P0-5).
        emerging_score = (
            0.30 * momentum_60
            + 0.25 * momentum_20
            + 0.20 * breakout_quality
            + 0.15 * min(1.0, volume_expansion)
            + 0.10 * max(0.0, trend_repair)
        )
        # Combined score: favor the mature channel but let a strong emerging
        # leader (positive 60-day momentum) still rise to the top.
        weak_score = 0.6 * mature_score + 0.4 * emerging_score
        if math.isfinite(weak_score):
            observations.append((weak_score, code, is_mature))
    ranked = sorted(observations, key=lambda item: (-item[0], item[1]))
    # Report P0-5: cap how many EMERGING-ONLY (immature) leaders enter the
    # selection so short-history names never crowd out the mature core.
    selected_codes: list[str] = []
    emerging_selected = 0
    for _, code, is_mature in ranked:
        if not is_mature:
            if emerging_selected >= MAX_EMERGING_LEADERS:
                continue
            emerging_selected += 1
        selected_codes.append(code)
        if len(selected_codes) >= maximum:
            break
    leaders = [
        (score, code)
        for score, code, _ in sorted(
            observations, key=lambda item: (-item[0], item[1])
        )
        if code in selected_codes
    ]
    return LeaderSelection(
        as_of=str(_timestamp(as_of).date()),
        requested_symbols=normalized,
        observed_symbols=len(observed_codes),
        selected_symbols=tuple(code for _, code in leaders),
        selected_returns=tuple(score for score, _ in leaders),
        unavailable_symbols=tuple(sorted(set(normalized) - observed_codes)),
    )


timestamp = _timestamp
normalized_timestamp = _normalized_timestamp
local_frame = _local_frame

__all__ = [
    "detect_regime",
    "local_frame",
    "normalized_timestamp",
    "select_positive_momentum_leaders",
    "timestamp",
]
