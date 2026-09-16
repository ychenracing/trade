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


def _slice_to_boundary(frame: pd.DataFrame, boundary: pd.Timestamp) -> pd.DataFrame:
    """Slice causally while retaining source coverage needed for pre-listing N/A."""
    source_first_date: pd.Timestamp | None = None
    if len(frame.index):
        source_first_date = _normalized_timestamp(str(frame.index.min()))
    sliced = frame.loc[frame.index <= boundary].copy()
    if source_first_date is not None:
        sliced.attrs["source_first_date"] = str(source_first_date.date())
    return sliced


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
    return _slice_to_boundary(frame, boundary)


def _leader_quality_inputs(
    frame: pd.DataFrame, closes: pd.Series
) -> dict[str, float] | None:
    """Return causal, scale-comparable opportunity-quality inputs."""
    if len(closes) < 61 or "volume" not in frame.columns:
        return None
    daily_returns = closes.pct_change().dropna()
    if len(daily_returns) < 60:
        return None
    ret20 = float(closes.iloc[-1] / closes.iloc[-21] - 1.0)
    ret60 = float(closes.iloc[-1] / closes.iloc[-61] - 1.0)
    vol20 = float(daily_returns.iloc[-20:].std())
    vol60 = float(daily_returns.iloc[-60:].std())
    high60 = float(closes.iloc[-60:].max())
    breakout = float(closes.iloc[-1] / high60) if high60 > 0 else float("nan")
    volumes = cast(pd.Series, pd.to_numeric(frame["volume"], errors="coerce"))
    current_volume = float(volumes.iloc[-1])
    average_volume = float(volumes.iloc[-21:-1].mean())
    volume_ratio = (
        current_volume / average_volume
        if math.isfinite(current_volume)
        and math.isfinite(average_volume)
        and average_volume > 0
        else float("nan")
    )
    if (
        not all(
            math.isfinite(value)
            for value in (ret20, ret60, vol20, vol60, breakout, volume_ratio)
        )
        or vol20 <= 0
        or vol60 <= 0
    ):
        return None
    return {
        "ret20": ret20,
        "ret60": ret60,
        "ra20": ret20 / (vol20 * math.sqrt(20.0)),
        "ra60": ret60 / (vol60 * math.sqrt(60.0)),
        "breakout": breakout,
        "volume": volume_ratio,
    }


def _leader_quality_score(
    inputs: dict[str, float],
    reference_inputs: dict[str, dict[str, float]],
) -> float | None:
    """Rank broad opportunity quality by median fixed-reference percentile."""
    if len(reference_inputs) != 5:
        return None
    reference_rows = tuple(reference_inputs.values())
    reference_ret60 = float(np.mean([row["ret60"] for row in reference_rows]))
    values = {
        "ra20": inputs["ra20"],
        "ra60": inputs["ra60"],
        "rs60": inputs["ret60"] - reference_ret60,
        "breakout": inputs["breakout"],
        "volume": inputs["volume"],
    }
    reference_values = {
        "ra20": [row["ra20"] for row in reference_rows],
        "ra60": [row["ra60"] for row in reference_rows],
        "rs60": [row["ret60"] - reference_ret60 for row in reference_rows],
        "breakout": [row["breakout"] for row in reference_rows],
        "volume": [row["volume"] for row in reference_rows],
    }
    percentiles = sorted(
        sum(reference <= values[name] for reference in reference_values[name])
        / len(reference_values[name])
        for name in ("ra20", "ra60", "rs60", "breakout", "volume")
    )
    return float(percentiles[len(percentiles) // 2])


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
    """Select positive leaders while preserving mature ranking continuity.

    Mature/emerging eligibility, leader count and the established blended weak
    score remain unchanged. The legacy score first forms the candidate set and
    keeps mature leaders in their existing order. Complete fixed-reference
    quality evidence may only improve the single existing emerging-leader slot:
    it can swap one emerging candidate for a better emerging candidate, or let
    the best emerging candidate challenge the weakest selected mature leader
    when its broad quality percentile is strictly higher. This narrows the alpha
    change to earlier leader discovery without re-ranking established leaders or
    introducing fitted weights, switches, thresholds, or new execution rules.

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
    observations: list[tuple[float, float | None, str, bool]] = []
    observed_codes: set[str] = set()
    pre_listing_codes: set[str] = set()
    invalid_codes: set[str] = set()
    issues: list[HealthIssue] = []

    def load_frame(code: str) -> pd.DataFrame:
        if frame_loader is None:
            return _local_frame(data_dir, code, str(boundary.date()))
        return _slice_to_boundary(frame_loader(code, str(boundary.date())), boundary)

    # The fixed reference basket is an optional ranking enrichment. Preserve
    # the established weak score whenever reference quality is incomplete;
    # decision-critical health applies only to requested symbols.
    reference_symbols = ("300308", "300502", "300394", "688008", "603986")
    ref_returns: list[float] = []
    reference_quality: dict[str, dict[str, float]] = {}
    for ref_code in reference_symbols:
        try:
            ref_frame = load_frame(ref_code)
            ref_closes = pd.Series(
                pd.to_numeric(ref_frame["close"], errors="coerce"),
                index=ref_frame.index,
            ).dropna()
        except (OSError, RuntimeError, ValueError, TypeError, KeyError):
            continue
        if len(ref_closes) < 61:
            continue
        observed_date = _normalized_timestamp(str(ref_closes.index[-1]))
        if (boundary - observed_date).days > MAX_EVIDENCE_STALENESS_DAYS:
            continue
        if len(ref_closes) >= 121:
            ref_ret = float(ref_closes.iloc[-1] / ref_closes.iloc[-121] - 1.0)
            if math.isfinite(ref_ret):
                ref_returns.append(ref_ret)
        quality_inputs = _leader_quality_inputs(ref_frame, ref_closes)
        if quality_inputs is not None:
            reference_quality[ref_code] = quality_inputs
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
            first_date = frame.attrs.get("source_first_date")
            if first_date is not None and _normalized_timestamp(first_date) > boundary:
                pre_listing_codes.add(code)
                continue
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
        observed_codes.add(code)
        if len(closes) < EMERGING_MIN_DAYS:
            continue
        close = float(closes.iloc[-1])

        has_mature_history = len(closes) >= LEADER_LOOKBACK + 1
        momentum_240 = 0.0
        if has_mature_history:
            momentum_240 = float(
                closes.iloc[-1] / closes.iloc[-LEADER_LOOKBACK - 1] - 1.0
            )
        is_mature = (
            has_mature_history
            and math.isfinite(momentum_240)
            and momentum_240 > 0
        )

        momentum_60 = (
            float(closes.iloc[-1] / closes.iloc[-61] - 1.0)
            if len(closes) >= 61
            else 0.0
        )
        momentum_20 = (
            float(closes.iloc[-1] / closes.iloc[-21] - 1.0)
            if len(closes) >= 21
            else 0.0
        )
        if len(closes) >= 20:
            high20 = float(closes.iloc[-20:].max())
            breakout_quality = close / high20 if high20 > 0 else 0.0
        else:
            breakout_quality = 0.0
        is_emerging = (
            math.isfinite(momentum_60)
            and momentum_60 > 0
            and math.isfinite(momentum_20)
            and momentum_20 > 0
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
            drawdown_from_peak = (
                1.0 - float(closes.iloc[-1] / peak_60) if peak_60 > 0 else 0.0
            )
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
        quality_inputs = _leader_quality_inputs(frame, closes)
        quality_score = (
            _leader_quality_score(quality_inputs, reference_quality)
            if quality_inputs is not None
            else None
        )
        if math.isfinite(weak_score):
            observations.append((weak_score, quality_score, code, is_mature))

    # Preserve the established selection as the baseline contract.
    legacy_ranked = sorted(observations, key=lambda item: (-item[0], item[2]))
    selected: list[tuple[float, float | None, str, bool]] = []
    emerging_selected = 0
    for item in legacy_ranked:
        if not item[3]:
            if emerging_selected >= MAX_EMERGING_LEADERS:
                continue
            emerging_selected += 1
        selected.append(item)
        if len(selected) >= maximum:
            break

    # Quality may only improve the one emerging slot. Mature leaders are never
    # re-ordered relative to one another. Missing quality evidence leaves the
    # complete legacy result untouched.
    quality_complete = len(reference_quality) == len(reference_symbols)
    quality_observations: list[tuple[float, float, str, bool]] = []
    for weak_score, quality_score, code, is_mature in observations:
        if quality_score is not None:
            quality_observations.append(
                (weak_score, quality_score, code, is_mature)
            )
    emerging_quality = [item for item in quality_observations if not item[3]]
    if quality_complete and emerging_quality and len(quality_observations) == len(observations):
        best_emerging = sorted(
            emerging_quality,
            key=lambda item: (-item[1], -item[0], item[2]),
        )[0]
        current_emerging_index = next(
            (index for index, item in enumerate(selected) if not item[3]),
            None,
        )
        if current_emerging_index is not None:
            current_emerging = selected[current_emerging_index]
            current_quality = cast(float, current_emerging[1])
            if (
                best_emerging[2] != current_emerging[2]
                and best_emerging[1] > current_quality
            ):
                selected[current_emerging_index] = best_emerging
        elif len(selected) >= maximum and MAX_EMERGING_LEADERS > 0:
            mature_indexes = [
                index for index, item in enumerate(selected) if item[3]
            ]
            if mature_indexes:
                weakest_index = min(
                    mature_indexes,
                    key=lambda index: (selected[index][0], selected[index][2]),
                )
                weakest_mature = selected[weakest_index]
                weakest_quality = cast(float, weakest_mature[1])
                if best_emerging[1] > weakest_quality:
                    selected[weakest_index] = best_emerging

    # Keep selected-return semantics on the established weak score. Replacement
    # stays in the displaced slot so relative mature ordering remains stable.
    return LeaderSelection(
        as_of=str(_timestamp(as_of).date()),
        requested_symbols=normalized,
        observed_symbols=len(observed_codes),
        selected_symbols=tuple(item[2] for item in selected),
        selected_returns=tuple(item[0] for item in selected),
        unavailable_symbols=tuple(
            sorted(set(normalized) - observed_codes - pre_listing_codes - invalid_codes)
        ),
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
