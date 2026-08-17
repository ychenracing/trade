"""Causal outer-route state transitions."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pandas as pd

from quantfusion.config.regime import (
    REGIME_INDEX_FILES,
    ROUTE_CONFIRM_DAYS,
    ROUTE_MIN_HOLD_DAYS,
    ROUTE_RECOVERY_CONFIRM_DAYS,
    ROUTE_TREND_FAST_MA,
    ROUTE_TREND_SLOW_MA,
)
from quantfusion.regime.evidence import local_frame, normalized_timestamp
from quantfusion.regime.models import DailyRouteStep, RegimeRoute

_local_frame = local_frame
_normalized_timestamp = normalized_timestamp

def simulate_route_sequence(
    data_dir: str | Path,
    *,
    start_date: str,
    end_date: str,
) -> tuple[DailyRouteStep, ...]:
    """Simulate the daily route state machine over a causal window.

    The state machine uses only the two fixed indices (broad + technology),
    each evaluated at its own as-of date (never future data). Missing or stale
    evidence fails closed to CASH. The trend signal is the MEDIUM-TERM
    ``MA60 > MA120`` cross (not the noisy short MA20/MA60 cross), and every
    switch requires a minimum hold plus consecutive-day confirmation, so a
    clean bull journals TREND throughout and a confirmed deterioration drifts
    toward WEAK without single-day flips or intra-bull whipsaw.
    """
    boundary_start = _normalized_timestamp(start_date)
    boundary_end = _normalized_timestamp(end_date)
    # Build per-index close series over the window (plus warm-up for MA120).
    lhs_date = boundary_start - pd.Timedelta(days=700)
    series: dict[str, pd.Series] = {}
    for code in REGIME_INDEX_FILES.values():
        try:
            frame = _local_frame(data_dir, code, str(boundary_end.date()))
        except (OSError, RuntimeError, ValueError):
            series[code] = pd.Series(dtype=float)
            continue
        closes = pd.Series(
            pd.to_numeric(frame["close"], errors="coerce"), index=frame.index
        ).dropna()
        series[code] = closes.loc[(closes.index >= lhs_date) & (closes.index <= boundary_end)]
    # Union of trading dates across both indices.
    current_state: RegimeRoute = RegimeRoute.CASH
    all_dates: list[pd.Timestamp] = sorted(
        {
            _normalized_timestamp(str(date))
            for values in series.values()
            if not values.empty
            for date in values.index
        }
    )
    all_dates = [d for d in all_dates if lhs_date <= d <= boundary_end]
    if not all_dates:
        return (DailyRouteStep(str(boundary_end.date()), "cash"),)
    hold_days = 0
    confirm_count = 0
    steps: list[DailyRouteStep] = []
    for date in all_dates:
        # Causal medium-term trend evidence at this date for each index.
        trending_flags: list[bool] = []
        evidence_ok = True
        for code in REGIME_INDEX_FILES.values():
            s = series.get(code)
            if s is None or s.empty or date not in s.index:
                evidence_ok = False
                break
            loc = int(cast(Any, s.index).get_loc(date))
            if loc < ROUTE_TREND_SLOW_MA:
                evidence_ok = False
                break
            fast = float(s.iloc[loc - ROUTE_TREND_FAST_MA + 1: loc + 1].mean())
            slow = float(s.iloc[loc - ROUTE_TREND_SLOW_MA + 1: loc + 1].mean())
            trending_flags.append(fast > slow)
        if not evidence_ok:
            current_state = RegimeRoute.CASH
            hold_days = 0
            confirm_count = 0
            if date >= boundary_start:
                steps.append(
                    DailyRouteStep(date.strftime("%Y-%m-%d"), current_state.value)
                )
            continue

        both_trending = all(trending_flags)
        any_trending = any(trending_flags)

        hold_days += 1
        # Explicit state-transition table (report P0-3). The direction of every
        # transition is now correct: a TRANSITION_TO_WEAK confirms WEAK only on
        # SUSTAINED weakness (`not any_trending`), never on trend repair; CASH
        # recovers to TRANSITION_TO_TREND (or a WEAK observation state) on
        # recovery, and never drifts to "转弱" because an index turned strong.
        # A clean bull stays in TREND; a confirmed deterioration drifts toward
        # WEAK; recovery re-enters TREND through TRANSITION_TO_TREND.
        if current_state is RegimeRoute.TREND:
            # Deterioration: at least one index breaks its medium-term
            # (MA60>MA120) trend -> drift toward weak (after a minimum hold).
            if not both_trending and hold_days >= ROUTE_MIN_HOLD_DAYS:
                current_state = RegimeRoute.TRANSITION_TO_WEAK
                confirm_count = 1
                hold_days = 0
        elif current_state is RegimeRoute.TRANSITION_TO_WEAK:
            if both_trending:
                # Indices recovered to a full uptrend -> back to TREND (never
                # confirm the weak transition with a trend REPAIR).
                current_state = RegimeRoute.TREND
                confirm_count = 0
                hold_days = 0
            elif not any_trending:
                # Sustained weakness -> confirm the weak transition.
                confirm_count += 1
                if confirm_count >= ROUTE_CONFIRM_DAYS:
                    current_state = RegimeRoute.WEAK
                    confirm_count = 0
                    hold_days = 0
            else:
                # Mixed evidence (one index strong, one weak): the weak
                # transition is NOT sustained, so reset the confirmation streak.
                # Confirmation must be consecutive (report 3.3 "避免状态抖动"),
                # otherwise a run of weak days interrupted by a strong day could
                # still accumulate to ROUTE_CONFIRM_DAYS and drift to WEAK.
                confirm_count = 0
        elif current_state is RegimeRoute.WEAK:
            # Both indices sustained an uptrend -> begin recovering to trend.
            if both_trending and hold_days >= ROUTE_MIN_HOLD_DAYS:
                current_state = RegimeRoute.TRANSITION_TO_TREND
                confirm_count = 1
                hold_days = 0
            # else: sustained weakness -> stay WEAK (risk route remains).
        elif current_state is RegimeRoute.TRANSITION_TO_TREND:
            if both_trending:
                confirm_count += 1
                if confirm_count >= ROUTE_RECOVERY_CONFIRM_DAYS:
                    current_state = RegimeRoute.TREND
                    confirm_count = 0
                    hold_days = 0
            elif not both_trending:
                # Repair failed and deteriorated again -> back to WEAK.
                current_state = RegimeRoute.WEAK
                confirm_count = 0
                hold_days = 0
        elif current_state is RegimeRoute.CASH:
            if both_trending:
                if hold_days >= ROUTE_MIN_HOLD_DAYS:
                    current_state = RegimeRoute.TRANSITION_TO_TREND
                    confirm_count = 1
                    hold_days = 0
            elif any_trending:
                # Partial recovery -> enter a WEAK observation state (NOT a
                # "转弱" transition) so we hold cash-ish until a full recovery.
                if hold_days >= ROUTE_MIN_HOLD_DAYS:
                    current_state = RegimeRoute.WEAK
                    confirm_count = 0
                    hold_days = 0
            # else: no recovery -> stay CASH.
        if date >= boundary_start:
            steps.append(DailyRouteStep(date.strftime("%Y-%m-%d"), current_state.value))
    return tuple(steps)


def boundary_route(data_dir: str | Path, *, as_of: str) -> RegimeRoute:
    """Return the state-machine route at ``as_of`` (fail-closed to CASH)."""
    start = str((_normalized_timestamp(as_of) - pd.Timedelta(days=120)).date())
    seq = simulate_route_sequence(data_dir, start_date=start, end_date=as_of)
    if not seq:
        return RegimeRoute.CASH
    return RegimeRoute(seq[-1].route)
