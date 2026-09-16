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
from quantfusion.domain.health import (
    HealthIssue,
    HealthReport,
    HealthState,
    invalid_issue,
    issue_from_exception,
    unavailable_issue,
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
    issues: list[HealthIssue] = []
    for code in REGIME_INDEX_FILES.values():
        source = f"regime_index:{code}"
        try:
            frame = _local_frame(data_dir, code, str(boundary.date()))
            closes = pd.Series(
                pd.to_numeric(frame["close"], errors="coerce"), index=frame.index
            ).dropna()
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
            issues.append(issue_from_exception(source, exc))
            continue
        if len(closes) < 60:
            issues.append(unavailable_issue(source, "fewer than 60 close observations"))
            continue
        close = float(closes.iloc[-1])
        ma20 = float(closes.tail(20).mean())
        ma60 = float(closes.tail(60).mean())
        if not all(
            math.isfinite(value) and value > 0 for value in (close, ma20, ma60)
        ):
            issues.append(invalid_issue(source, "non-finite or non-positive trend inputs"))
            continue
        observed_date = _normalized_timestamp(str(closes.index[-1]))
        if (boundary - observed_date).days > MAX_EVIDENCE_STALENESS_DAYS:
            issues.append(
                unavailable_issue(
                    source,
                    f"stale evidence last observed {observed_date.date()}",
                )
            )
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
        health=HealthReport.from_issues(issues),
    )


def select_positive_momentum_leaders(
    symbols: Sequence[str],
    *,
    data_dir: str | Path,
    as_of: str,
    maximum: int = MAX_LEADERS,
    frame_loader: Callable[[str, str], pd.DataFrame] | None = None,
) -> LeaderSelection:
    """Select positive long-horizon leaders with explicit input health.

    Uses multi-factor weak-market scoring (section 12.2):
    - 240-day momentum (25%)
    - 120-day relative strength vs reference basket (25%)
    - 60-day momentum (20%)
    - Drawdown resilience (15%)
    - Trend repair: 5-day vs 20-day momentum (15%)

    Research callers receive degraded results plus diagnostics. Production
    callers decide whether to accept degradation via ``LeaderSelection`` rather
    than inferring data health from an empty selection.
    """
    normalized = tuple(sorted(str(symbol) for symbol in symbols))
    if not normalized or len(normalized) != len(set(normalized)):
        raise ValueError("symbols must be a non-empty set without duplicates")
    if maximum < 1:
        raise ValueError("maximum must be positive")
    boundary = _normalized_timestamp(as_of)
    observations: list[tuple[float, str, bool]] = []
    observed_codes: set[str] = set()
    invalid_codes: set[str] = set()
    issues: list[HealthIssue] = []

    def load_frame(code: str) -> pd.DataFrame:
        if frame_loader is None:
            return _local_frame(data_dir, code, str(boundary.date()))
        frame = frame_loader(code, str(boundary.date()))
        return frame.loc[frame.index <= boundary].copy()

    # The fixed reference basket is an optional ranking enrichment.  Preserve
    # its established zero-baseline fallback when reference history is absent
    # or incomplete; decision-critical health applies to requested symbols.
    reference_symbols = ("300308", "300502", "300394", "688008", "603986")
    ref_returns: list[float] = []
    for ref_code in reference_symbols:
        try:
            ref_frame = load_frame(ref_code)
            ref_closes = pd.Series(
                pd.to_numeric(ref_frame["close"], errors="coerce"),
                index=ref_frame.index,
            ).dropna()
        except (OSError, RuntimeError, ValueError, TypeError, KeyError):
            continue
        if len(ref_closes) < 121:
            continue
        observed_date = _normalized_timestamp(str(ref_closes.index[-1]))
        if (boundary - observed_date).days > MAX_EVIDENCE_STALENESS_DAYS:
            continue
        ref_ret = float(ref_closes.iloc[-1] / ref_closes.iloc[-121] - 1.0)
        if not math.isfinite(ref_ret):
            continue
        ref_returns.append(ref_ret)
    ref_avg_return = float(np.mean(ref_returns)) if ref_returns else 0.0

    for code in normalized:
        source = f"leader_symbol:{code}"
        try:
            frame = load_frame(code)
            closes = pd.Series(
                pd.to_numeric(frame["close"], errors="coerce"), index=frame.index
            ).dropna()
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
            issue = issue_from_exception(source, exc)
            issues.append(issue)
            if issue.state is HealthState.INVALID:
                invalid_codes.add(code)
            continue
        if closes.empty:
            issues.append(unavailable_issue(source, "no close observations"))
            continue
        observed_date = _normalized_timestamp(str(closes.index[-1]))
        if (boundary - observed_date).days > MAX_EVIDENCE_STALENESS_DAYS:
            issues.append(
                unavailable_issue(
                    source,
                    f"stale evidence last observed {observed_date.date()}",
                )
            )
            continue
        # Fresh source data is observable even when a newly listed symbol has
        # not accumulated enough sessions to enter the emerging-leader model.
        # Insufficient lookback makes the symbol ineligible for ranking; it is
        # not missing market evidence and must not degrade the whole route.
        observed_codes.add(code)
        if len(closes) < EMERGING_MIN_DAYS:
            continue
        close = float(closes.iloc[-1])

        # Mature-channel gate: needs the full 240-day history and positive
        # long-horizon momentum. Symbols that fail this gate are STILL eligible
        # for the emerging channel.
        has_mature_history = len(closes) >= LEADER_LOOKBACK + 1
        momentum_240 = 0.0
        if has_mature_history:
            momentum_240 = float(closes.iloc[-1] / closes.iloc[-LEADER_LOOKBACK - 1] - 1.0)
        is_mature = has_mature_history and math.isfinite(momentum_240) and momentum_240 > 0

        # Multi-factor scoring (mature + emerging dual channel).
        # Both channels are scored against a FIXED technology reference pool (not the
        # caller's pool) so adding/removing a symbol never changes an unchanged
        # symbol's score.
        if len(closes) >= 61:
            momentum_60 = float(closes.iloc[-1] / closes.iloc[-61] - 1.0)
        else:
            momentum_60 = 0.0

        if len(closes) >= 21:
            momentum_20 = float(closes.iloc[-1] / closes.iloc[-21] - 1.0)
        else:
            momentum_20 = 0.0

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

        if len(closes) >= 121:
            symbol_120 = float(closes.iloc[-1] / closes.iloc[-121] - 1.0)
            rs_120 = symbol_120 - ref_avg_return
        else:
            rs_120 = 0.0

        if len(closes) >= 60:
            peak_60 = float(closes.iloc[-60:].max())
            drawdown_from_peak = 1.0 - float(closes.iloc[-1] / peak_60) if peak_60 > 0 else 0.0
            resilience = 1.0 - min(1.0, drawdown_from_peak)
        else:
            resilience = 0.0

        if len(closes) >= 21:
            mom_5 = float(closes.iloc[-1] / closes.iloc[-6] - 1.0)
            mom_20 = float(closes.iloc[-1] / closes.iloc[-21] - 1.0)
            trend_repair = mom_5 - mom_20
        else:
            trend_repair = 0.0

        volume_expansion = 0.0
        if "volume" in frame.columns and len(frame) >= 21:
            volumes = cast(
                pd.Series, pd.to_numeric(frame["volume"], errors="coerce")
            )
            cur_vol = float(volumes.iloc[-1])
            avg_vol = float(volumes.iloc[-21:-1].mean())
            if math.isfinite(cur_vol) and math.isfinite(avg_vol) and avg_vol > 0:
                volume_expansion = max(0.0, min(2.0, cur_vol / avg_vol))

        mature_score = (
            0.25 * max(0.0, momentum_240)
            + 0.25 * max(0.0, rs_120)
            + 0.20 * momentum_60
            + 0.15 * resilience
            + 0.15 * max(0.0, trend_repair)
        )
        emerging_score = (
            0.30 * momentum_60
            + 0.25 * momentum_20
            + 0.20 * breakout_quality
            + 0.15 * min(1.0, volume_expansion)
            + 0.10 * max(0.0, trend_repair)
        )
        weak_score = 0.6 * mature_score + 0.4 * emerging_score
        if math.isfinite(weak_score):
            observations.append((weak_score, code, is_mature))
    ranked = sorted(observations, key=lambda item: (-item[0], item[1]))
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
        unavailable_symbols=tuple(sorted(set(normalized) - observed_codes - invalid_codes)),
        invalid_symbols=tuple(sorted(invalid_codes)),
        health=HealthReport.from_issues(issues),
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
