"""Causal regime routing layered over the frozen Quant Fusion engine.

The production trend engine remains untouched. This module selects between
that engine and a low-turnover weak-regime policy using information available
strictly before the requested deployment period.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Sequence, cast

import numpy as np
import pandas as pd

import quant_fusion as qf


REGIME_INDEX_FILES = {"broad": "000300", "technology": "000682"}
LEADER_LOOKBACK = 240
MAX_LEADERS = 3
MAX_SYMBOL_WEIGHT = 0.59
# ── Mature / emerging dual-channel leader qualification (report P0-5) ──
# A "mature" leader needs the full 240-day history and positive 240-day
# momentum. A "emerging" leader has only a short history (>= 61 days) but shows
# short-horizon momentum + breakout / volume expansion; it is NOT forced to meet
# the 240-day gate that previously blocked every genuinely new leader.
EMERGING_MIN_DAYS = 61
# At most this many emerging-only (short-history) leaders may be selected in one
# cycle, so immature names never crowd out the mature core (report P0-5 "新兴
# 龙头最多10%~30% / 每个周期最多新增1只").
MAX_EMERGING_LEADERS = 1
PROFIT_ACTIVATION = 0.30
TRAILING_ATR_MULTIPLIER = 3.0
WEAK_ENTRY_ATR_MULTIPLIER = 5.0
WEAK_HARD_STOP = 0.22
WEAK_TIME_STOP_DAYS = 80
WEAK_TIME_STOP_RETURN = -0.10
MAX_EVIDENCE_STALENESS_DAYS = 10

# The weak route retains its low-turnover stock-level protection, but no longer
# disables portfolio risk almost completely. These thresholds leave validated
# sub-20% paths unchanged while forcing correlated weak-market books to de-risk
# after a confirmed portfolio drawdown.
WEAK_DRAWDOWN_ALERT = 0.15
WEAK_CONFIRMED_DRAWDOWN = 0.20
WEAK_EMERGENCY_DRAWDOWN = 0.23
WEAK_TERMINAL_DRAWDOWN = 0.26
WEAK_DAILY_LOSS_LIMIT = 0.12

# ── Weak-market re-entry (report 3.5) ─────────────────────────────────
# The weak leader strategy may re-enter after a full exit, subject to a
# per-reason cooldown and graded (probe -> confirmed) position building, so a
# V-shaped repair or a second true reversal is not missed while a repeated
# bear-market bottom-fish is filtered out.
# Exit-reason -> cooldown in trading days (report 3.5 table).
WEAK_EXIT_COOLDOWN = {
    "profit_chandelier": 6,   # profitable ATR chandelier take-profit: 5-7 days
    "time_stop": 12,          # time stop: 10-15 days
    "hard_stop": 16,          # initial disaster stop: 12-20 days
    "portfolio_risk": 12,     # external portfolio-risk exit: ~12 days default
    "catastrophe": 25,        # catastrophe stop: 20-30 days
}
DEFAULT_EXIT_COOLDOWN = 12
# First re-entry uses only this fraction of the normal target weight; after a
# probe confirm the position is lifted toward the full target (report 3.5).
WEAK_PROBE_WEIGHT_RATIO = 0.30
WEAK_CONFIRM_WEIGHT_RATIO = 0.70
# Hold a probe this many trading days (and meet confirm conditions) before the
# position is scaled up toward the full target.
WEAK_PROBE_CONFIRM_DAYS = 5
# After this many consecutive failed (stop-out) re-entries the cooldown doubles
# to slow repeated bottom-fishing.
WEAK_REENTRY_FAIL_LIMIT = 2
# Portfolio-level drawsgate: the full re-entry target is only reached when the
# portfolio is within this drawdown of its prior peak (else stay at probe size).
WEAK_REENTRY_MAX_DRAWDOWN = 0.12


@dataclass(frozen=True, slots=True)
class IndexTrend:
    """One index observation available at the deployment boundary."""

    code: str
    observed_date: str
    close: float
    ma20: float
    ma60: float
    trending: bool


class RegimeRoute(Enum):
    """Daily dynamic outer route (report 3.3).

    The route is a state machine that persists across trading days and only
    switches on confirmed, causally-available evidence so a clean bull stays in
    TREND (frozen trend engine) and a confirmed bear drifts to WEAK, with
    explicit TRANSITION states that need consecutive-day confirmation.
    """

    TREND = "trend"
    WEAK = "weak"
    CASH = "cash"
    TRANSITION_TO_TREND = "transition_to_trend"
    TRANSITION_TO_WEAK = "transition_to_weak"


@dataclass(frozen=True, slots=True)
class DailyRouteStep:
    """One auditable row of the daily route sequence."""

    date: str
    route: str


# ── Dynamic route state-machine constants (report 3.3) ─────────────────
# The route is deliberately LOW-FREQUENCY. It uses a medium-term trend
# signal (MA60 vs MA120) on the two fixed indices — far more stable than the
# short MA20/MA60 cross, which whipsaws inside a single bull market on normal
# pull-backs. A minimum hold and consecutive-day confirmation filter the
# remaining noise, so a clean bull journals TREND and a sustained
# deterioration drifts toward WEAK without single-day flips (report 3.3
# "避免状态抖动").
ROUTE_TREND_FAST_MA = 60
ROUTE_TREND_SLOW_MA = 120
# Minimum trading days a state is held before a same-direction transition can
# complete, and the consecutive-day confirmation required for a completed
# switch (avoids single-day whipsaw and TREND -> WEAK -> TREND the same day).
ROUTE_MIN_HOLD_DAYS = 10
ROUTE_CONFIRM_DAYS = 3
# Recovery needs materially more evidence than de-risking. A premature return
# to three trend sleeves creates a turnover burst near bear-market rallies;
# thirty consecutive sessions keep recovery deliberately slower than de-risking
# while avoiding an unnecessarily long lag after a durable trend repair. The weak book
# remains invested in positive leaders during confirmation, so a V-shaped
# repair is participated in instead of sitting in cash, while a bull already
# confirmed during warm history starts directly in TREND.
ROUTE_RECOVERY_CONFIRM_DAYS = 30
# A route maps to an engine name for the deployment boundary.
_ROUTE_TO_ENGINE = {
    RegimeRoute.TREND: "frozen_trend_engine",
    RegimeRoute.TRANSITION_TO_TREND: "frozen_trend_engine",
    RegimeRoute.TRANSITION_TO_WEAK: "positive_momentum_hold",
    RegimeRoute.WEAK: "positive_momentum_hold",
    RegimeRoute.CASH: "cash_preservation",
}


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


@dataclass(frozen=True, slots=True)
class RegimeEvidence:
    """Fixed-index regime evidence with explicit failure-closed coverage."""

    as_of: str
    regime: str
    observations: tuple[IndexTrend, ...]


@dataclass(frozen=True, slots=True)
class LeaderSelection:
    """Positive 240-session leaders observable before deployment."""

    as_of: str
    requested_symbols: tuple[str, ...]
    observed_symbols: int
    selected_symbols: tuple[str, ...]
    selected_returns: tuple[float, ...]
    unavailable_symbols: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DeploymentDecision:
    """Auditable choice between the frozen trend and weak-regime policies."""

    name: str
    boundary: str
    reason: str
    regime: RegimeEvidence
    leaders: LeaderSelection | None


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
    frame = qf.DataFetcher.load_stock_data(
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

    # Load reference basket for relative strength calculation
    reference_symbols = ("300308", "300502", "300394", "688008", "603986")
    ref_returns: list[float] = []
    for ref_code in reference_symbols:
        try:
            ref_frame = _local_frame(data_dir, ref_code, str(boundary.date()))
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
            frame = _local_frame(data_dir, code, str(boundary.date()))
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


class PositiveMomentumHoldStrategy(qf.BaseStrategy):
    """Hold a causal positive-leader entry, then protect gains, with re-entry.

    Unlike the original one-shot entry, this strategy may RE-ENTER after a full
    exit (report 3.5): a per-reason trading-day cooldown filters repeated
    bottom-fishing, and the first re-entry is a small probe (30% of target)
    that is only scaled up toward the full target once a confirm condition is
    met. This captures V-shaped repairs and a second true reversal without
    re-establishing a full position into a falling knife.
    """

    name = "positive_momentum_hold"

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self._has_entered = False
        self._was_positioned = False
        self._pending_exit_reason: str | None = None
        self._exit_bar: int | None = None
        self._cooldown_end: int | None = None
        self._exit_reason: str | None = None
        self._probe_phase = False
        self._probe_entry_price = 0.0
        self._probe_entry_bar = 0
        self._failures = 0
        self._assets_peak = 0.0
        # Chandelier trailing stop, tracked separately from the hard disaster
        # stop. ``position.stop_loss`` is the hard lower bound only; the
        # profit-activated chandelier line lives here so a trailing take-profit
        # exit is classified as ``profit_chandelier`` (6-day cooldown) instead
        # of being mis-reported as a ``hard_stop`` disaster (16-day cooldown
        # plus a failure count) — see on_bar profit-chandelier branch.
        self._trail_stop = 0.0

    # ── helpers ──────────────────────────────────────────────────────

    def _closes(self, ctx: qf.BarContext):
        return pd.Series(
            pd.to_numeric(ctx.df["close"], errors="coerce"), index=ctx.df.index
        ).dropna()

    def _target_shares(self, ctx: qf.BarContext, ratio: float) -> int:
        """Board-lot shares for ``ratio`` of the normal target weight."""
        weight = float(self.cfg["strategy_weight"]) * ratio
        return qf._floor_to_lot(ctx.current_assets * weight / float(ctx.df["close"].iloc[ctx.i]))

    def _reentry_ok(self, ctx: qf.BarContext) -> bool:
        """Re-entry gates (report 3.5): momentum repaired, trend restored."""
        closes = self._closes(ctx)
        i = ctx.i
        if i < 60 or len(closes) < 61:
            return False
        close = float(closes.iloc[i])
        if not math.isfinite(close) or close <= 0:
            return False
        # 240-day momentum must remain positive.
        if i >= LEADER_LOOKBACK:
            mom_240 = close / float(closes.iloc[i - LEADER_LOOKBACK]) - 1.0
            if not (math.isfinite(mom_240) and mom_240 > 0):
                return False
        # 60-day momentum re-turned positive.
        mom_60 = close / float(closes.iloc[i - 60]) - 1.0
        if not (math.isfinite(mom_60) and mom_60 > 0):
            return False
        # Close back above MA20 and MA20 above MA60 (genuine trend repair,
        # not a dead-cat bounce inside a long downtrend).
        ma20 = float(closes.iloc[i - 19: i + 1].mean())
        if not (math.isfinite(ma20) and close > ma20):
            return False
        if i >= 60:
            ma60 = float(closes.iloc[i - 59: i + 1].mean())
            if not (math.isfinite(ma60) and ma20 > ma60):
                return False
        # 5-day trend repair positive (5d momentum > 20d momentum).
        if i >= 21:
            mom_5 = close / float(closes.iloc[i - 5]) - 1.0
            mom_20 = close / float(closes.iloc[i - 20]) - 1.0
            if not (math.isfinite(mom_5) and math.isfinite(mom_20) and mom_5 > mom_20):
                return False
        return True

    def _confirm_ok(self, ctx: qf.BarContext) -> bool:
        """Confirm conditions to scale a probe toward the full target."""
        closes = self._closes(ctx)
        i = ctx.i
        if i < 40:
            return False
        close = float(closes.iloc[i])
        if not math.isfinite(close) or close <= 0:
            return False
        # 20-day trend restored.
        mom_20 = close / float(closes.iloc[i - 20]) - 1.0
        if not (math.isfinite(mom_20) and mom_20 > 0):
            return False
        # Price above the probe's cost.
        if self._probe_entry_price > 0 and close <= self._probe_entry_price:
            return False
        return True

    def _finalize_exit(self, ctx: qf.BarContext) -> None:
        """Record an exit and set the re-entry cooldown (report 3.5)."""
        reason = self._pending_exit_reason or "portfolio_risk"
        self._exit_reason = reason
        self._exit_bar = ctx.i
        cooldown = WEAK_EXIT_COOLDOWN.get(reason, DEFAULT_EXIT_COOLDOWN)
        if self._failures >= WEAK_REENTRY_FAIL_LIMIT:
            cooldown *= 2
        self._cooldown_end = ctx.i + cooldown
        self._pending_exit_reason = None
        self._probe_phase = False
        self._probe_entry_price = 0.0
        self._probe_entry_bar = 0
        # Reset the trailing chandelier so a re-entry starts from a clean slate.
        self._trail_stop = 0.0

    def on_bar(self, ctx: qf.BarContext) -> qf.Signal | None:
        close = float(ctx.df["close"].iloc[ctx.i])
        if not math.isfinite(close) or close <= 0:
            return None
        self._assets_peak = max(self._assets_peak, ctx.current_assets)

        # Detect a full exit that happened since the previous bar (either an
        # exit this strategy signalled, or an external portfolio-level exit).
        if self.position is None and self._was_positioned:
            self._finalize_exit(ctx)
        self._was_positioned = self.position is not None

        if self.position is None:
            if not self._has_entered:
                # First-ever entry is a full deployment (no cooldown yet).
                shares = self._target_shares(ctx, 1.0)
                if shares <= 0:
                    return None
                atr = ctx.indicators.get("atr")
                atr_value = float(atr.iloc[ctx.i]) if atr is not None else float("nan")
                hard_stop = close * (1.0 - WEAK_HARD_STOP)
                atr_stop = (
                    close - WEAK_ENTRY_ATR_MULTIPLIER * atr_value
                    if math.isfinite(atr_value) and atr_value > 0
                    else hard_stop
                )
                self._has_entered = True
                return self._make_buy_signal(
                    ctx, shares, stop_loss=max(hard_stop, atr_stop),
                    reason="causal positive-240-session leader entry with disaster stop",
                )
            # Re-entry path: cooldown must have elapsed and all gates must hold.
            if self._cooldown_end is not None and ctx.i < self._cooldown_end:
                return None
            if not self._reentry_ok(ctx):
                return None
            shares = self._target_shares(ctx, WEAK_PROBE_WEIGHT_RATIO)
            if shares <= 0:
                return None
            atr = ctx.indicators.get("atr")
            atr_value = float(atr.iloc[ctx.i]) if atr is not None else float("nan")
            hard_stop = close * (1.0 - WEAK_HARD_STOP)
            atr_stop = (
                close - WEAK_ENTRY_ATR_MULTIPLIER * atr_value
                if math.isfinite(atr_value) and atr_value > 0
                else hard_stop
            )
            self._probe_phase = True
            self._probe_entry_price = close
            self._probe_entry_bar = ctx.i
            return self._make_buy_signal(
                ctx, shares, stop_loss=max(hard_stop, atr_stop),
                reason="weak-regime re-entry probe after cooldown",
            )

        # ── Positioned: manage exits and probe confirmation ──────────
        position = self.position
        position.highest_close_since_entry = max(
            position.highest_close_since_entry, close
        )
        # Hard disaster stop only: ``position.stop_loss`` is the initial entry
        # cost floor and is never raised by the trailing chandelier (see
        # ``_trail_stop`` below), so any exit hit here is a genuine disaster.
        if position.stop_loss > 0 and close <= position.stop_loss:
            self._pending_exit_reason = "hard_stop"
            self._failures += 1
            return self._make_sell_signal(ctx, "weak-regime disaster stop")
        try:
            entry_timestamp = pd.Timestamp(position.entry_date)
            if entry_timestamp is pd.NaT:
                raise ValueError("entry_date must resolve to a valid timestamp")
            entry_index = int(cast(Any, ctx.df.index).searchsorted(entry_timestamp))
            held_days = max(ctx.i - entry_index, 0)
        except (TypeError, ValueError):
            held_days = 0
        return_since_entry = close / position.entry_price - 1.0
        if (
            held_days >= WEAK_TIME_STOP_DAYS
            and return_since_entry <= WEAK_TIME_STOP_RETURN
        ):
            self._pending_exit_reason = "time_stop"
            return self._make_sell_signal(ctx, "weak-regime time stop")

        # Probe confirmation: scale a probe toward the full target once the
        # confirm window elapses and conditions hold, and the portfolio is not
        # deeply off its peak (never go to full target into a deep drawdown).
        if self._probe_phase:
            drawdown = 1.0 - ctx.current_assets / self._assets_peak if self._assets_peak > 0 else 0.0
            confirm = (
                held_days >= WEAK_PROBE_CONFIRM_DAYS
                and ctx.i - self._probe_entry_bar >= WEAK_PROBE_CONFIRM_DAYS
                and self._confirm_ok(ctx)
            )
            full_shares = self._target_shares(ctx, 1.0)
            add = qf._floor_to_lot(full_shares - position.shares)
            if confirm and add > 0 and drawdown <= WEAK_REENTRY_MAX_DRAWDOWN:
                self._probe_phase = False
                self._failures = 0
                return self._make_buy_signal(
                    ctx, add, stop_loss=position.stop_loss,
                    reason="weak-regime re-entry probe confirmed -> full target",
                )

        peak_gain = position.highest_close_since_entry / position.entry_price - 1.0
        if peak_gain < PROFIT_ACTIVATION:
            return None
        atr = ctx.indicators.get("atr")
        atr_value = float(atr.iloc[ctx.i]) if atr is not None else float("nan")
        if not math.isfinite(atr_value) or atr_value <= 0:
            return None
        trail = position.highest_close_since_entry - TRAILING_ATR_MULTIPLIER * atr_value
        # Track the chandelier line separately so a trailing take-profit exit is
        # classified as ``profit_chandelier`` (6-day cooldown), never as a
        # ``hard_stop`` disaster. The line only ratchets up (never down).
        self._trail_stop = max(self._trail_stop, trail)
        if close > self._trail_stop:
            return None
        self._pending_exit_reason = "profit_chandelier"
        return self._make_sell_signal(
            ctx,
            f"profit-activated {TRAILING_ATR_MULTIPLIER:g}-ATR chandelier",
        )


class CashPreservationStrategy(qf.BaseStrategy):
    """Deliberately emit no orders when causal evidence is insufficient."""

    name = "cash_preservation"

    def on_bar(self, ctx: qf.BarContext) -> None:
        del ctx
        return None


def _weak_regime_policy() -> qf.PortfolioPolicy:
    """Apply independent weak-market portfolio drawdown protection."""
    return qf.PortfolioPolicy(
        allocation_mode="single",
        drawdown_alert=WEAK_DRAWDOWN_ALERT,
        confirmed_drawdown=WEAK_CONFIRMED_DRAWDOWN,
        emergency_drawdown=WEAK_EMERGENCY_DRAWDOWN,
        terminal_drawdown=WEAK_TERMINAL_DRAWDOWN,
        concentration_drawdown_adjustment=0.01,
        candidate_reference_percentile=0.0,
        market_regime_enabled=False,
    )


def _weak_regime_config(symbol_count: int) -> dict[str, Any]:
    slots = max(1, symbol_count)
    target_weight = min(MAX_SYMBOL_WEIGHT, 1.0 / slots)
    return {
        "strategy_weight": target_weight,
        "max_symbol_weight": target_weight,
        "max_total_weight": 1.0,
        "max_positions": slots,
        "max_units": 1,
        "group_min_slots": 0,
        "daily_loss_limit": WEAK_DAILY_LOSS_LIMIT,
        "sector_guard_enabled": False,
        "market_regime_enabled": False,
        "fusion_single_scale": 1.0,
        "combined_group_weight_limits": {
            "overseas_compute": 1.0,
            "domestic_semiconductor": 1.0,
        },
    }


class ProductionRouteController:
    """Apply the daily outer route inside one persistent production ledger.

    The controller never injects an account snapshot and never replaces the
    ensemble's execution engine. It filters or adds close-generated T+1 orders
    while the existing sleeve cash, positions, pending orders, sticky state,
    risk peaks, cooldowns, and strategy instances continue across every route
    transition.
    """

    def __init__(
        self,
        route_sequence: Sequence[DailyRouteStep],
        *,
        leader_data_dir: str | Path,
    ) -> None:
        self.route_by_date = {step.date: step.route for step in route_sequence}
        self.starts_defensive = bool(route_sequence) and route_sequence[0].route in {
            RegimeRoute.WEAK.value,
            RegimeRoute.CASH.value,
        }
        self.leader_data_dir = str(leader_data_dir)
        self.previous_route: str | None = None
        self.events: list[dict[str, Any]] = []
        self.journal: list[dict[str, Any]] = []
        self._leader_cache: dict[str, tuple[str, ...]] = {}
        self._weak_strategies: dict[
            tuple[str, str], PositiveMomentumHoldStrategy
        ] = {}
        self._weak_episode_leaders: tuple[str, ...] = ()
        self._carry_trend_book = False
        self._restoring_trend_cash = False

    @staticmethod
    def _drop_buys(states: list[Any]) -> None:
        for state in states:
            state.pending = [
                item for item in state.pending if item[0].direction != "buy"
            ]

    @staticmethod
    def _queue_liquidations(
        states: list[Any], date_str: str, *, weak_only: bool
    ) -> None:
        for state in states:
            liquidations = state.sleeve._generate_liquidation_signals(
                date_str,
                reason="production outer-route migration",
            )
            selected = [
                item
                for item in liquidations
                if (
                    item[0].strategy_name == PositiveMomentumHoldStrategy.name
                ) == weak_only
            ]
            if not selected:
                continue
            state.pending = state.sleeve._dedupe_pending_signals(
                [item for item in state.pending if item[0].direction == "sell"]
                + selected
            )

    def _leaders(self, symbols: Sequence[str], date_str: str) -> tuple[str, ...]:
        cached = self._leader_cache.get(date_str)
        if cached is not None:
            return cached
        try:
            selected = select_positive_momentum_leaders(
                tuple(symbols),
                data_dir=self.leader_data_dir,
                as_of=date_str,
            ).selected_symbols
        except (OSError, RuntimeError, ValueError):
            selected = ()
        self._leader_cache[date_str] = tuple(selected)
        return tuple(selected)

    def _append_weak_signals(
        self,
        states: list[Any],
        date: pd.Timestamp,
        symbols_dict: dict[str, str],
    ) -> tuple[str, ...]:
        date_str = date.strftime("%Y-%m-%d")
        leaders = self._weak_episode_leaders
        if not leaders:
            return ()
        # Weak routing uses one account sleeve, not three duplicate weak books.
        # All idle cash is migrated to this sleeve on the route transition.
        for state in states[:1]:
            current_assets = state.sleeve._total_assets(state.data_map, date)
            for symbol in symbols_dict:
                key = (str(state.sleeve.sleeve_name), symbol)
                strategy = self._weak_strategies.get(key)
                if strategy is None:
                    cfg = dict(state.sleeve.cfg)
                    cfg.update(_weak_regime_config(max(len(leaders), 1)))
                    strategy = PositiveMomentumHoldStrategy(cfg)
                    self._weak_strategies[key] = strategy
                # Dynamic weak-route strategies own real positions and therefore
                # must participate in every liquidation path. Keep them in the
                # external registry so the sleeve risk/sector/route controls can
                # find them without the core signal loop evaluating them a second
                # time on the same close.
                registered = state.sleeve.external_strategy_instances.setdefault(
                    symbol, []
                )
                if strategy not in registered:
                    registered.append(strategy)
                if symbol not in leaders and strategy.position is None:
                    continue
                frame = state.data_map.get(symbol)
                indicators = state.indicator_map.get(symbol)
                if frame is None or indicators is None or date not in frame.index:
                    continue
                ctx = qf.BarContext(
                    i=frame.index.get_loc(date),
                    df=frame,
                    current_assets=current_assets,
                    indicators=indicators,
                    symbol=symbol,
                    date=date_str,
                )
                signal = strategy.on_bar(ctx)
                if signal is None:
                    continue
                if signal.direction == "buy":
                    if symbol not in leaders or state.sleeve._pending_has_buy(
                        state.pending, symbol, strategy.name
                    ):
                        continue
                elif signal.direction == "sell":
                    if state.sleeve._pending_has_sell(
                        state.pending, symbol, strategy.name
                    ):
                        continue
                state.pending.append((signal, strategy))
            state.pending = state.sleeve._dedupe_pending_signals(state.pending)
        return leaders

    @staticmethod
    def _shift_free_cash(states: list[Any], weights: Sequence[float]) -> None:
        """Move idle cash causally and neutralize the external flow in risk peaks."""
        total_cash = sum(float(state.sleeve.cash) for state in states)
        if total_cash <= 0:
            return
        targets = [total_cash * float(weight) for weight in weights]
        targets[-1] = total_cash - sum(targets[:-1])
        for state, target in zip(states, targets, strict=True):
            old = float(state.sleeve.cash)
            state.sleeve.cash = target
            flow = target - old
            risk = state.sleeve.risk
            for attribute in (
                "peak_assets",
                "lifetime_peak_assets",
                "daily_start_assets",
            ):
                if hasattr(risk, attribute):
                    setattr(
                        risk,
                        attribute,
                        max(0.0, float(getattr(risk, attribute, 0.0)) + flow),
                    )

    def after_close(
        self,
        states: list[Any],
        date: pd.Timestamp,
        symbols_dict: dict[str, str],
    ) -> None:
        """Route close-generated orders while preserving all execution state."""
        date_str = date.strftime("%Y-%m-%d")
        route = self.route_by_date.get(
            date_str, self.previous_route or RegimeRoute.CASH.value
        )
        changed = route != self.previous_route
        if changed:
            self.events.append(
                {
                    "date": date_str,
                    "event": "production_route_transition",
                    "from": self.previous_route,
                    "to": route,
                }
            )

        leaders: tuple[str, ...] = ()
        if route == RegimeRoute.TRANSITION_TO_WEAK.value:
            # The cross-market overlay is the execution owner for an existing
            # trend book. It applies the dual-confirmed transition throttle;
            # stacking another route-level scale here changes the downstream
            # strategy path and double-counts the same risk evidence.
            pass
        elif route == RegimeRoute.CASH.value:
            self._drop_buys(states)
            if changed:
                self._queue_liquidations(states, date_str, weak_only=False)
                self._queue_liquidations(states, date_str, weak_only=True)
            self._carry_trend_book = False
            self._weak_episode_leaders = ()
        elif route in {
            RegimeRoute.WEAK.value,
            RegimeRoute.TRANSITION_TO_TREND.value,
        }:
            if changed and route == RegimeRoute.WEAK.value:
                self._carry_trend_book = any(
                    strat_name != PositiveMomentumHoldStrategy.name
                    for state in states
                    for positions in state.sleeve.positions.values()
                    for strat_name in positions
                )
            if self._carry_trend_book:
                # Preserve an established trend book through an index-led risk
                # episode. Existing strategies retain their own sell logic;
                # the cross-market overlay remains the sole buy/trim authority
                # so route and overlay cannot multiply the same reduction.
                pass
            else:
                self._drop_buys(states)
                if changed and not self._weak_episode_leaders:
                    # Freeze the leaders for this weak episode. Re-ranking every
                    # close turned the defensive book into a hidden rotation
                    # strategy, increasing trades precisely when conditions are
                    # least forgiving. A new weak episode receives a new,
                    # causal selection from its transition close.
                    self._weak_episode_leaders = self._leaders(
                        tuple(symbols_dict), date_str
                    )
                self._shift_free_cash(states, (1.0, 0.0, 0.0))
                leaders = self._append_weak_signals(states, date, symbols_dict)
        else:
            carried_trend_book = self._carry_trend_book
            self._carry_trend_book = False
            self._weak_episode_leaders = ()
            if changed and self.previous_route in {
                RegimeRoute.WEAK.value,
                RegimeRoute.CASH.value,
                RegimeRoute.TRANSITION_TO_TREND.value,
            }:
                self._queue_liquidations(states, date_str, weak_only=True)
                self._restoring_trend_cash = not carried_trend_book
            if self._restoring_trend_cash:
                self._shift_free_cash(states, (1 / 3, 1 / 3, 1 / 3))
                weak_positions = any(
                    PositiveMomentumHoldStrategy.name in positions
                    for state in states
                    for positions in state.sleeve.positions.values()
                )
                if not weak_positions:
                    self._restoring_trend_cash = False

        sleeve_rows = []
        for state in states:
            risk = state.sleeve.risk
            sleeve_rows.append(
                {
                    "name": state.sleeve.sleeve_name,
                    "cash": float(state.sleeve.cash),
                    "positions": sorted(state.sleeve.positions),
                    "pending": len(state.pending),
                    "risk_lock": bool(getattr(risk, "persistent_lock", False)),
                    "risk_peak": float(getattr(risk, "peak_assets", 0.0)),
                    "sticky_leader": getattr(
                        state.sleeve, "_sticky_leader", None
                    ),
                }
            )
        self.journal.append(
            {
                "date": date_str,
                "route": route,
                "leaders": list(leaders),
                "sleeves": sleeve_rows,
            }
        )
        self.previous_route = route

    def result_snapshot(self) -> dict[str, Any]:
        """Return the complete serializable route and persistence audit."""
        cooldowns = {
            f"{sleeve}:{symbol}": {
                "cooldown_end": strategy._cooldown_end,
                "exit_reason": strategy._exit_reason,
                "failures": strategy._failures,
            }
            for (sleeve, symbol), strategy in sorted(self._weak_strategies.items())
        }
        return {
            "engine": "ProductionReplayEngine",
            "route_sequence": [
                {"date": date, "route": route}
                for date, route in sorted(self.route_by_date.items())
            ],
            "transition_events": list(self.events),
            "daily_journal": list(self.journal),
            "weak_cooldowns": cooldowns,
        }

    @property
    def current_route(self) -> str | None:
        """Return the route only when it owns overlay execution next session."""
        # An established trend account retains the normal cross-market overlay
        # as its single risk owner. Only a dedicated weak/cash book suppresses
        # duplicate overlay execution.
        if self._carry_trend_book and self.previous_route in {
            RegimeRoute.WEAK.value,
            RegimeRoute.TRANSITION_TO_TREND.value,
        }:
            return None
        return self.previous_route


class ProductionReplayEngine:
    """Run daily causal routing through one persistent Quant Fusion account."""

    def __init__(
        self,
        initial_capital: float = 2_000_000,
        cfg: dict | None = None,
        policy: qf.PortfolioPolicy | None = None,
    ) -> None:
        self.initial_capital = qf._require_finite(
            "initial_capital", initial_capital, min_value=0.01
        )
        self.cfg = dict(cfg or {})
        self.policy = policy
        self.delegate: qf.BacktestEngine | None = None

    def run(
        self,
        symbols_dict: dict[str, str],
        start_date: str,
        end_date: str,
        *,
        data_dir: str,
        regime_data_dir: str,
        leader_data_dir: str | None = None,
        indicator_state: str = "warm",
        warmup_calendar_days: int = 365,
        per_symbol_config: dict[str, dict] | None = None,
        profile: str | None = None,
        config_route: str = "auto",
        risk_state: dict | None = None,
        account_state: qf.AccountState | None = None,
    ) -> dict[str, Any]:
        route_sequence = simulate_route_sequence(
            regime_data_dir,
            start_date=start_date,
            end_date=end_date,
        )
        controller = ProductionRouteController(
            route_sequence,
            leader_data_dir=leader_data_dir or data_dir,
        )
        starts_defensive = controller.starts_defensive
        replay_policy = self.policy or (
            replace(
                qf.PortfolioPolicy(),
                drawdown_alert=WEAK_DRAWDOWN_ALERT,
                confirmed_drawdown=WEAK_CONFIRMED_DRAWDOWN,
                emergency_drawdown=WEAK_EMERGENCY_DRAWDOWN,
                terminal_drawdown=WEAK_TERMINAL_DRAWDOWN,
                concentration_drawdown_adjustment=0.01,
            )
            if starts_defensive
            else qf.PortfolioPolicy()
        )
        self.delegate = qf.BacktestEngine(
            self.initial_capital,
            cfg=self.cfg,
            policy=replay_policy,
        )
        result = self.delegate.run(
            symbols_dict,
            start_date,
            end_date,
            per_symbol_config=per_symbol_config,
            profile=profile,
            config_route=config_route,
            data_dir=data_dir,
            indicator_state=indicator_state,
            warmup_calendar_days=warmup_calendar_days,
            allocation_mode="ensemble",
            risk_state=risk_state,
            account_state=account_state,
            route_controller=controller,
        )
        result["route_sequence"] = result["production_replay"]["route_sequence"]
        result["deployment_policy"] = "production_daily_replay"
        result["requested_symbols"] = sorted(symbols_dict)
        result["selected_symbols"] = sorted(symbols_dict)
        result["unavailable_symbols"] = []
        return result


class RegimeAdaptiveBacktestEngine:
    """Preserve the trend engine and route weak deployments causally."""

    def __init__(
        self,
        initial_capital: float = 2_000_000,
        cfg: dict | None = None,
        policy: qf.PortfolioPolicy | None = None,
    ) -> None:
        self.initial_capital = qf._require_finite(
            "initial_capital", initial_capital, min_value=0.01
        )
        self.cfg = dict(cfg or {})
        self.policy = policy
        self.last_decision: DeploymentDecision | None = None
        self.delegate: Any = None

    @staticmethod
    def _available_local_symbols(
        symbols_dict: dict[str, str],
        *,
        data_dir: str | None,
        start_date: str,
        end_date: str,
        warmup_calendar_days: int,
    ) -> dict[str, str]:
        """Return symbols with at least one causal observation in the run window."""
        if data_dir is None:
            return dict(symbols_dict)
        earliest = _normalized_timestamp(start_date) - pd.Timedelta(
            days=warmup_calendar_days
        )
        latest = _normalized_timestamp(end_date)
        available: dict[str, str] = {}
        for code, name in symbols_dict.items():
            try:
                frame = _local_frame(data_dir, code, str(latest.date()))
            except (OSError, RuntimeError, ValueError):
                continue
            if not frame.loc[(frame.index >= earliest) & (frame.index <= latest)].empty:
                available[code] = name
        return available

    @staticmethod
    def _boundary(start_date: str, selection_boundary: str | None) -> str:
        if selection_boundary is not None:
            boundary = _normalized_timestamp(selection_boundary)
            if boundary >= _normalized_timestamp(start_date):
                raise ValueError("selection_boundary must be before start_date")
            return str(boundary.date())
        return str((_normalized_timestamp(start_date) - pd.Timedelta(days=1)).date())

    def decide_current(
        self,
        symbols_dict: dict[str, str],
        *,
        as_of: str,
        data_dir: str | Path,
        leader_data_dir: str | Path | None = None,
    ) -> DeploymentDecision:
        """Make a point-in-time route decision from data through ``as_of``.

        This is the CURRENT-day route used by ``daily_signal_scan`` and the
        account engine (report 3.3/3.4 "历史和账户使用同一状态机"). It is driven
        by the same low-frequency daily state machine as the audited
        ``route_sequence``, so the label the user sees each day matches the
        route that drives the decision. It fails closed to CASH on stale or
        incomplete evidence.
        """
        boundary = _normalized_timestamp(as_of)
        route = boundary_route(data_dir, as_of=str(boundary.date()))
        regime = detect_regime(data_dir, as_of=str(boundary.date()))
        when = str(boundary.date())
        if route == RegimeRoute.CASH:
            return DeploymentDecision(
                name="cash_preservation",
                boundary=when,
                reason="dynamic route failed closed to CASH (stale/incomplete evidence)",
                regime=regime,
                leaders=None,
            )
        if route in (RegimeRoute.TREND, RegimeRoute.TRANSITION_TO_TREND):
            return DeploymentDecision(
                name="frozen_trend_engine",
                boundary=when,
                reason="dynamic route is in a confirmed medium-term uptrend",
                regime=regime,
                leaders=None,
            )
        leaders = select_positive_momentum_leaders(
            tuple(symbols_dict),
            data_dir=leader_data_dir or data_dir,
            as_of=when,
        )
        name = (
            "positive_momentum_hold" if leaders.selected_symbols else "cash_preservation"
        )
        return DeploymentDecision(
            name,
            when,
            "dynamic route drifted to weak; selected only positive leaders",
            regime,
            leaders,
        )

    def decide(
        self,
        symbols_dict: dict[str, str],
        *,
        start_date: str,
        data_dir: str | Path,
        leader_data_dir: str | Path | None = None,
        selection_boundary: str | None = None,
    ) -> DeploymentDecision:
        """Choose a route using only complete evidence available at the boundary."""
        boundary = self._boundary(start_date, selection_boundary)
        regime = detect_regime(data_dir, as_of=boundary)
        if regime.regime == "unknown":
            return DeploymentDecision(
                name="cash_preservation",
                boundary=boundary,
                reason="fixed-index evidence is incomplete, invalid, or stale",
                regime=regime,
                leaders=None,
            )
        if regime.regime == "trending":
            return DeploymentDecision(
                name="frozen_trend_engine",
                boundary=boundary,
                reason="both fixed indices had MA20 above MA60 before deployment",
                regime=regime,
                leaders=None,
            )
        leaders = select_positive_momentum_leaders(
            tuple(symbols_dict),
            data_dir=leader_data_dir or data_dir,
            as_of=boundary,
        )
        name = "positive_momentum_hold" if leaders.selected_symbols else "cash_preservation"
        reason = (
            "fixed-index trend was not confirmed; selected only positive "
            "240-session leaders"
            if leaders.selected_symbols
            else "fixed-index trend was not confirmed and no positive "
            "240-session leader was observable"
        )
        return DeploymentDecision(name, boundary, reason, regime, leaders)

    def run(
        self,
        symbols_dict: dict[str, str],
        start_date: str,
        end_date: str,
        per_symbol_config: dict[str, dict] | None = None,
        profile: str | None = None,
        config_route: str = "auto",
        data_dir: str | None = None,
        *,
        indicator_state: str = "cold",
        warmup_calendar_days: int = 365,
        allocation_mode: str | None = None,
        risk_state: dict | None = None,
        account_state: qf.AccountState | None = None,
        selection_boundary: str | None = None,
        deployment_mode: str = "auto",
        regime_data_dir: str | None = None,
        leader_data_dir: str | None = None,
        allow_unavailable_symbols: bool = False,
    ) -> dict:
        """Run a trend-preserving or weak-regime deployment with one schema.

        The trend route fails closed when a requested symbol has no observable
        local data. Research callers that intentionally evaluate pre-listing
        universes may opt in to filtering with ``allow_unavailable_symbols``.
        """
        mode = str(deployment_mode).lower()
        if mode not in {"auto", "replay", "trend", "weak"}:
            raise ValueError("deployment_mode must be auto, replay, trend, or weak")
        if not isinstance(allow_unavailable_symbols, bool):
            raise ValueError("allow_unavailable_symbols must be bool")
        evidence_dir = regime_data_dir or data_dir
        if evidence_dir is None:
            raise ValueError(
                "regime-adaptive mode requires a local data_dir or regime_data_dir"
            )
        if mode in {"auto", "replay"}:
            if data_dir is None:
                raise ValueError("production replay requires a local stock data_dir")
            tradable_symbols = self._available_local_symbols(
                symbols_dict,
                data_dir=data_dir,
                start_date=start_date,
                end_date=end_date,
                warmup_calendar_days=warmup_calendar_days,
            )
            unavailable = tuple(sorted(set(symbols_dict) - set(tradable_symbols)))
            if unavailable and not allow_unavailable_symbols:
                raise RuntimeError(
                    "requested replay symbols have no observable data: "
                    + ", ".join(unavailable)
                )
            if not tradable_symbols:
                raise RuntimeError("production replay has no observable trade symbols")
            replay = ProductionReplayEngine(
                self.initial_capital,
                cfg=self.cfg,
                policy=self.policy,
            )
            result = replay.run(
                tradable_symbols,
                start_date,
                end_date,
                data_dir=data_dir,
                regime_data_dir=str(evidence_dir),
                leader_data_dir=leader_data_dir,
                indicator_state=indicator_state,
                warmup_calendar_days=warmup_calendar_days,
                per_symbol_config=per_symbol_config,
                profile=profile,
                config_route=config_route,
                risk_state=risk_state,
                account_state=account_state,
            )
            self.delegate = replay.delegate
            decision = self.decide_current(
                tradable_symbols,
                as_of=end_date,
                data_dir=evidence_dir,
                leader_data_dir=leader_data_dir,
            )
            self.last_decision = decision
            result["deployment_decision"] = asdict(decision)
            result["unavailable_symbols"] = list(unavailable)
            return result
        decision = self.decide(
            symbols_dict,
            start_date=start_date,
            data_dir=evidence_dir,
            leader_data_dir=leader_data_dir,
            selection_boundary=selection_boundary,
        )
        if mode == "trend":
            decision = replace(
                decision,
                name="frozen_trend_engine",
                reason="trend mode forced by caller",
            )
        elif mode == "weak":
            leaders = select_positive_momentum_leaders(
                tuple(symbols_dict),
                data_dir=leader_data_dir or evidence_dir,
                as_of=decision.boundary,
            )
            decision = replace(
                decision,
                name=(
                    "positive_momentum_hold"
                    if leaders.selected_symbols
                    else "cash_preservation"
                ),
                reason="weak mode forced by caller",
                leaders=leaders,
            )
        self.last_decision = decision
        executed_symbols: tuple[str, ...] = ()
        unavailable_symbols: tuple[str, ...] = ()
        result: dict[str, Any] | None = None

        if decision.name == "frozen_trend_engine":
            tradable_symbols = self._available_local_symbols(
                symbols_dict,
                data_dir=data_dir,
                start_date=start_date,
                end_date=end_date,
                warmup_calendar_days=warmup_calendar_days,
            )
            unavailable_symbols = tuple(
                sorted(set(symbols_dict) - set(tradable_symbols))
            )
            if unavailable_symbols and not allow_unavailable_symbols:
                raise RuntimeError(
                    "requested trend symbols have no observable data: "
                    + ", ".join(unavailable_symbols)
                )
            if not tradable_symbols:
                leaders = LeaderSelection(
                    as_of=decision.boundary,
                    requested_symbols=tuple(sorted(symbols_dict)),
                    observed_symbols=0,
                    selected_symbols=(),
                    selected_returns=(),
                    unavailable_symbols=tuple(sorted(symbols_dict)),
                )
                decision = replace(
                    decision,
                    name="cash_preservation",
                    reason="trend was confirmed but no requested symbol had observable data",
                    leaders=leaders,
                )
                self.last_decision = decision
            else:
                executed_symbols= tuple(sorted(tradable_symbols))
                # Trend route keeps the default three-sleeve ensemble. The
                # total capital (e.g. 2,000,000) is split across the fast,
                # base and slow virtual sub-accounts; the sum never exceeds
                # the total capital and no leverage is used.
                effective_allocation = allocation_mode or "ensemble"
                self.delegate = qf.BacktestEngine(
                    self.initial_capital, cfg=self.cfg, policy=self.policy
                )
                result = self.delegate.run(
                    tradable_symbols,
                    start_date,
                    end_date,
                    per_symbol_config=per_symbol_config,
                    profile=profile,
                    config_route=config_route,
                    data_dir=data_dir,
                    indicator_state=indicator_state,
                    warmup_calendar_days=warmup_calendar_days,
                    allocation_mode=effective_allocation,
                    risk_state=risk_state,
                    account_state=account_state,
                )
        if decision.name != "frozen_trend_engine":
            if self.cfg or self.policy is not None:
                raise ValueError(
                    "weak-regime policy does not accept constructor cfg or policy overrides"
                )
            if per_symbol_config or profile is not None or config_route != "auto":
                raise ValueError("weak-regime policy does not accept trend profile overrides")
            if risk_state is not None or account_state is not None:
                raise NotImplementedError("weak-regime policy does not inject external state")
            leaders = decision.leaders
            selected = leaders.selected_symbols if leaders is not None else ()
            unavailable_symbols = (
                leaders.unavailable_symbols if leaders is not None else ()
            )
            executed_symbols = tuple(selected)
            run_symbols = (
                {code: symbols_dict[code] for code in selected}
                if selected
                else {code: code for code in qf.PortfolioPolicy().regime_symbols}
            )
            self.delegate = qf.SleeveBacktestEngine(
                self.initial_capital,
                cfg=_weak_regime_config(len(selected)),
                policy=_weak_regime_policy(),
                allocation_lookbacks=(5, 10, 20),
                sleeve_name="weak_regime",
            )
            self.delegate.strategy_templates = [
                PositiveMomentumHoldStrategy if selected else CashPreservationStrategy
            ]
            weak_result = self.delegate.run(
                run_symbols,
                start_date,
                end_date,
                data_dir=data_dir,
                indicator_state=indicator_state,
                warmup_calendar_days=warmup_calendar_days,
            )
            weak_result.update(
                {
                    "effective_portfolio_policy": self.delegate.policy.as_dict(),
                    "portfolio_max_positions": max(1, len(selected)),
                    "portfolio_cash_model": "single_account",
                    "allocation_mode": "single",
                }
            )
            result = weak_result
        if result is None:
            raise RuntimeError("deployment route completed without a backtest result")
        result["deployment_decision"] = asdict(decision)
        result["deployment_policy"] = decision.name
        result["requested_symbols"] = sorted(symbols_dict)
        result["selected_symbols"] = list(executed_symbols)
        result["unavailable_symbols"] = list(unavailable_symbols)
        # Report 3.3/3.4: emit the auditable daily route sequence so the
        # historical replay, the current-day account route and the report all
        # share the same state machine (P0-4 "历史和账户使用同一状态机").
        try:
            route_seq = simulate_route_sequence(
                evidence_dir, start_date=start_date, end_date=end_date
            )
            result["route_sequence"] = [
                {"date": step.date, "route": step.route} for step in route_seq
            ]
        except (OSError, RuntimeError, ValueError, TypeError):
            result["route_sequence"] = []
        return result
