"""Causal market-regime evidence, transitions, and signal scaling."""

from __future__ import annotations

# pyright: reportAttributeAccessIssue=false

# ruff: noqa: F401

import contextlib
import io
import math
from dataclasses import replace
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from quantfusion.config.universe import ESTABLISHED_EXPANSION_CORE
from quantfusion.data.providers import DataFetcher
from quantfusion.domain.models import MarketRegimeObservation, Signal
from quantfusion.domain.rules import floor_to_lot, require_int
from quantfusion.engine.core import CoreBacktestEngine
from quantfusion.engine.ensemble import (
    EnsembleBacktestEngine,
    EnsembleSleeveBacktestEngine,
    PreparedSleeveRun,
    RunRequest,
)
from quantfusion.execution.priorities import EXECUTION_PRIORITY
from quantfusion.indicators.technical import Indicators
from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.risk.managers import RecoverableDrawdownRiskManager, RiskManager
from quantfusion.strategy.trend import BaseStrategy

_CoreBacktestEngine = CoreBacktestEngine
_ESTABLISHED_EXPANSION_CORE = ESTABLISHED_EXPANSION_CORE
_EnsembleBacktestEngine = EnsembleBacktestEngine
_EnsembleSleeveBacktestEngine = EnsembleSleeveBacktestEngine
_PreparedSleeveRun = PreparedSleeveRun
_RunRequest = RunRequest
_floor_to_lot = floor_to_lot
_require_int = require_int


class MarketRegimeMixin:
    """Causal market-regime evidence, transitions, and signal scaling."""

    def _reset_run_state(self, symbols_dict: dict[str, str]) -> None:
        """Reset tradable and regime metadata at every independent run."""
        # Concrete classes place this cooperative mixin before the sleeve engine.
        super()._reset_run_state(  # pyright: ignore[reportAttributeAccessIssue]
            symbols_dict
        )
        self._tradable_symbol_codes: set[str] = set(symbols_dict)
        self._candidate_score_series: dict[str, dict[int, pd.Series]] = {}
        # Report P1-3: sticky-candidate rotation state. ``_sticky_beat_days``
        # counts consecutive days a NEW candidate has clearly beaten the weakest
        # replaceable held name (confirmation before rotating); ``_sticky_leader``
        # is the candidate currently being confirmed so the count only advances
        # while the SAME candidate keeps qualifying (consecutive-day rule);
        # ``_sticky_last_rotation_pos`` is the last trading position a rotation
        # happened at (one per cycle); ``_sticky_rotated`` maps recently
        # rotated-out names to their rotation position for a bounded cooldown.
        self._sticky_beat_days: dict[str, int] = {}
        self._sticky_leader: str | None = None
        self._sticky_last_rotation_pos: int = -1_000_000
        self._sticky_rotated: dict[str, int] = {}
        # Market regime state machine: start in TREND (full trading) and let the
        # basket indicators demote the state when conditions deteriorate.
        self._regime_state: str = "TREND"
        self._regime_state_series: list[dict[str, Any]] = []
        self._regime_indicator_series: dict[str, pd.Series] = {}
        self._regime_to_choppy_streak: int = 0
        self._regime_to_trend_streak: int = 0
        self._regime_non_choppy_streak: int = 0
        self._regime_to_transition_streak: int = 0
        self._regime_state_start_pos: int = 0
        self._regime_prev_state: str = "TREND"
        self._regime_latest_observation: MarketRegimeObservation | None = None
        self._regime_transition_days: int = 0
        self._regime_transition_trimmed: bool = False
        self._external_risk_level: int = 0
        self._regime_effective_state: str = "TREND"

    def _prepare_run(
        self,
        symbols_dict: dict[str, str],
        start_date: str,
        end_date: str,
        start_ts: pd.Timestamp,
        end_ts: pd.Timestamp,
        per_symbol_config: dict[str, dict] | None,
        profile: str | None,
        config_route: str,
        data_dir: str | None,
        cache_dir: str | None,
    ) -> tuple[
        dict[str, pd.DataFrame],
        dict[str, dict[str, pd.Series]],
        list[pd.Timestamp],
        dict[pd.Timestamp, int],
    ]:
        """Load a fixed signal-only regime basket beside tradable symbols."""
        self._tradable_symbol_codes = set(symbols_dict)
        combined = dict(symbols_dict)
        for code in self.policy.regime_symbols:
            combined.setdefault(code, code)
        prepared = super()._prepare_run(  # pyright: ignore[reportAttributeAccessIssue]
            combined,
            start_date,
            end_date,
            start_ts,
            end_ts,
            per_symbol_config,
            profile,
            config_route,
            data_dir,
            cache_dir,
        )
        self._tradable_symbol_codes = set(symbols_dict)
        self._candidate_score_series = self._build_candidate_score_series(prepared[0])
        self._regime_indicator_series = self._build_regime_indicator_series(prepared[0])
        return prepared

    def _build_candidate_score_series(
        self, data_map: dict[str, pd.DataFrame]
    ) -> dict[str, dict[int, pd.Series]]:
        """Precompute causal multi-horizon risk-adjusted momentum series."""
        cache: dict[str, dict[int, pd.Series]] = {}
        for code, frame in data_map.items():
            close = frame["close"].astype(float)
            daily_returns = close.pct_change()
            cache[code] = {}
            for window in self.policy.candidate_lookbacks:
                volatility = daily_returns.rolling(window, min_periods=window).std()
                valid_volatility = volatility.where(volatility > 0)
                cache[code][window] = close.pct_change(window) / valid_volatility
        return cache

    @staticmethod
    def _rolling_slope_pct(series: pd.Series, window: int) -> pd.Series:
        """Return the rolling linear-regression slope as a window percentage.

        The per-bar slope of a least-squares fit over ``window`` observations is
        scaled by ``window`` and divided by the window mean so the result is
        comparable to a percentage move over the window (e.g. +0.02 for a 2%
        uptrend). The output is lag-safe: each bar uses only itself and prior
        bars.
        """
        x = np.arange(window, dtype=float)
        x_mean = x.mean()
        denom = float(((x - x_mean) ** 2).sum())
        if denom <= 0.0:
            return pd.Series(np.nan, index=series.index, dtype="float64")

        def _slope(y: np.ndarray) -> float:
            y_arr = np.asarray(y, dtype=float)
            if y_arr.size != window or np.isnan(y_arr).any():
                return float("nan")
            y_mean = float(y_arr.mean())
            if y_mean <= 0.0:
                return float("nan")
            slope = float(((x - x_mean) * (y_arr - y_mean)).sum() / denom)
            return slope * window / y_mean

        return series.rolling(window, min_periods=window).apply(_slope, raw=True)

    def _build_regime_indicator_series(
        self, data_map: dict[str, pd.DataFrame]
    ) -> dict[str, pd.Series]:
        """Precompute the five causal market-regime indicator series.

        Every returned series is indexed by trading date and uses only data on
        or before that date (rolling windows end at the current bar), so the
        regime state machine never looks past the close it is scoring. The
        equal-weight basket index (EWI) cumulates the cross-sectional mean of
        daily returns across the fixed ``regime_symbols`` basket.
        """
        if not bool(self.cfg.get("market_regime_enabled", True)):
            return {}
        regime_codes = [code for code in self.policy.regime_symbols if code in data_map]
        if not regime_codes:
            return {}

        ewi_lookback = _require_int(
            "regime_ewi_lookback",
            int(self.cfg.get("regime_ewi_lookback", 20)),
            min_value=2,
        )
        breadth_ma = _require_int(
            "regime_breadth_ma_long",
            int(self.cfg.get("regime_breadth_ma_long", 20)),
            min_value=1,
        )
        adx_period = _require_int(
            "adx_period", int(self.cfg.get("adx_period", 14)), min_value=1
        )
        hurst_window = _require_int(
            "regime_hurst_window",
            int(self.cfg.get("regime_hurst_window", 100)),
            min_value=10,
        )
        vol_lookback = _require_int(
            "regime_vol_lookback",
            int(self.cfg.get("regime_vol_lookback", 60)),
            min_value=2,
        )

        close_frames: list[pd.Series] = []
        adx_frames: list[pd.Series] = []
        breadth_frames: list[pd.Series] = []
        for code in regime_codes:
            frame = data_map[code]
            close = pd.to_numeric(frame["close"], errors="coerce")
            close_frames.append(close)
            adx_frames.append(Indicators.adx(frame, adx_period))
            ma = close.rolling(breadth_ma, min_periods=breadth_ma).mean()
            breadth_frames.append((close > ma).astype(float))

        aligned_close = pd.concat(close_frames, axis=1, keys=regime_codes).sort_index()
        basket_return = aligned_close.pct_change().mean(axis=1, skipna=True)
        # Anchor the index at 1.0 and treat any fully-missing day as flat.
        basket_return = basket_return.fillna(0.0)
        ewi = (1.0 + basket_return).cumprod()

        ewi_slope = self._rolling_slope_pct(ewi, ewi_lookback)
        breadth = pd.concat(breadth_frames, axis=1, keys=regime_codes).mean(
            axis=1, skipna=True
        )
        adx_median = pd.concat(adx_frames, axis=1, keys=regime_codes).median(
            axis=1, skipna=True
        )

        ewi_log_returns = np.log(ewi / ewi.shift(1))
        hurst = ewi_log_returns.rolling(
            hurst_window, min_periods=hurst_window
        ).apply(lambda arr: Indicators.hurst_rs(arr, hurst_window), raw=True)

        volatility = ewi_log_returns.rolling(
            vol_lookback, min_periods=vol_lookback
        ).std()
        vol_percentile = volatility.rolling(
            vol_lookback, min_periods=vol_lookback
        ).rank(pct=True)

        return {
            "ewi_slope": ewi_slope,
            "breadth": breadth,
            "adx_median": adx_median,
            "hurst": hurst,
            "vol_percentile": vol_percentile,
        }

    def _score_regime_candidate(self, date: pd.Timestamp) -> MarketRegimeObservation:
        """Vote the five indicators at date into a raw score and candidate state."""
        series_map = self._regime_indicator_series

        def _value(key: str) -> float:
            series = series_map.get(key)
            if series is None or date not in series.index:
                return float("nan")
            value = float(series.loc[date])
            return value if math.isfinite(value) else float("nan")

        ewi_slope = _value("ewi_slope")
        breadth = _value("breadth")
        adx_median = _value("adx_median")
        hurst = _value("hurst")
        vol_percentile = _value("vol_percentile")

        score = 0
        if math.isfinite(ewi_slope):
            if ewi_slope > float(self.cfg.get("regime_ewi_slope_trend", 0.02)):
                score += 1
            elif ewi_slope < float(self.cfg.get("regime_ewi_slope_choppy", -0.02)):
                score -= 1
        if math.isfinite(breadth):
            if breadth > 0.6:
                score += 1
            elif breadth < 0.4:
                score -= 1
        if math.isfinite(adx_median):
            if adx_median > float(self.cfg.get("regime_adx_trend", 25)):
                score += 1
            elif adx_median < float(self.cfg.get("regime_adx_choppy", 20)):
                score -= 1
        if math.isfinite(hurst):
            if hurst > float(self.cfg.get("regime_hurst_trend", 0.55)):
                score += 1
            elif hurst < float(self.cfg.get("regime_hurst_choppy", 0.45)):
                score -= 1
        if math.isfinite(vol_percentile) and vol_percentile > float(
            self.cfg.get("regime_vol_extreme_pct", 0.9)
        ):
            score -= 1

        trend_threshold = int(self.cfg.get("regime_score_trend", 2))
        choppy_threshold = int(self.cfg.get("regime_score_choppy", -3))
        if score >= trend_threshold:
            candidate = "TREND"
        elif score <= choppy_threshold:
            candidate = "CHOPPY"
        else:
            candidate = "TRANSITION"

        return MarketRegimeObservation(
            ewi_slope=ewi_slope,
            breadth_above_ma=breadth,
            adx_median=adx_median,
            hurst=hurst,
            volatility_percentile=vol_percentile,
            raw_score=int(score),
            candidate_state=candidate,
        )

    def _advance_regime_state(
        self, candidate: str, pos: int, current: str
    ) -> str:
        """Apply confirmation gates and minimum hold to one candidate transition.

        Three-state machine with early-warning TRANSITION:
        - TREND → TRANSITION: when the composite score softens (candidate
          becomes TRANSITION) for *trend_to_transition_confirmations* days.
          This activates position-size scaling before full CHOPPY defense.
        - TREND/TRANSITION → CHOPPY: when the candidate is CHOPPY for
          *choppy_confirmations* day(s).  Fast defense — even one day of
          strong negative score triggers the block.
        - CHOPPY → TRANSITION/TREND: slow recovery via *recovery_confirmations*.
        """
        choppy_confirmations = int(self.cfg.get("regime_choppy_confirmations", 2))
        trend_confirmations = int(self.cfg.get("regime_trend_confirmations", 3))
        recovery_confirmations = int(self.cfg.get("regime_recovery_confirmations", 3))
        min_hold = int(self.cfg.get("regime_min_state_hold", 3))
        trend_to_transition = int(
            self.cfg.get("regime_trend_to_transition_confirmations", 3)
        )

        # Update confirmation streaks from the freshly scored candidate.
        if candidate == "CHOPPY":
            self._regime_to_choppy_streak += 1
        else:
            self._regime_to_choppy_streak = 0
        if candidate == "TREND":
            self._regime_to_trend_streak += 1
        else:
            self._regime_to_trend_streak = 0
        if candidate != "CHOPPY":
            self._regime_non_choppy_streak += 1
        else:
            self._regime_non_choppy_streak = 0
        if candidate == "TRANSITION":
            self._regime_to_transition_streak += 1
        else:
            self._regime_to_transition_streak = 0

        can_leave = (pos - self._regime_state_start_pos) >= min_hold

        if current == "CHOPPY":
            # Exit CHOPPY only after sustained non-choppy evidence (slow).
            if can_leave and self._regime_non_choppy_streak >= recovery_confirmations:
                new_state = (
                    "TREND"
                    if self._regime_to_trend_streak >= trend_confirmations
                    else "TRANSITION"
                )
            else:
                new_state = "CHOPPY"
        elif current == "TREND":
            # Early warning: demote to TRANSITION when momentum softens.
            if can_leave and self._regime_to_transition_streak >= trend_to_transition:
                new_state = "TRANSITION"
            # Fast defense: jump straight to CHOPPY when score confirms.
            elif can_leave and self._regime_to_choppy_streak >= choppy_confirmations:
                new_state = "CHOPPY"
            else:
                new_state = "TREND"
        else:  # TRANSITION
            if can_leave and self._regime_to_choppy_streak >= choppy_confirmations:
                new_state = "CHOPPY"
            elif can_leave and self._regime_to_trend_streak >= trend_confirmations:
                new_state = "TREND"
            else:
                new_state = "TRANSITION"

        if new_state != current:
            self._regime_state_start_pos = pos
            # Reset streaks after a committed transition so confirmation windows
            # restart cleanly from the new state.
            self._regime_to_choppy_streak = 0
            self._regime_to_trend_streak = 0
            self._regime_non_choppy_streak = 0
            self._regime_to_transition_streak = 0
        return new_state

    def _update_market_regime(
        self,
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
        all_dates: list[pd.Timestamp],
        date_to_pos: dict[pd.Timestamp, int],
    ) -> None:
        """Score the regime basket and advance the three-state machine by one day.

        Called after ``_update_sector_guard`` and before any new signal
        generation so entries respect the current regime. The EWI slope is the
        primary indicator: when it (or the indicator series) is unavailable the
        prior state is preserved without advancing confirmation streaks.
        """
        del all_dates
        # Guard: _regime_state is initialised in _reset_run_state; tests that
        # call _update_sector_guard directly (without a full run) skip it.
        if not hasattr(self, "_regime_state"):
            return
        previous_state = self._regime_state
        if not bool(self.cfg.get("market_regime_enabled", True)):
            self._regime_state = "TREND"
            return
        if not self._regime_indicator_series:
            self._regime_state = "TREND"
            return

        date = pd.Timestamp(date)
        pos = date_to_pos.get(date)
        if pos is None:
            return

        observation = self._score_regime_candidate(date)
        self._regime_latest_observation = observation
        # Insufficient causal history: preserve the prior state and record the
        # gap without advancing the confirmation streaks.
        if not math.isfinite(observation.ewi_slope):
            self._regime_state_series.append(
                {
                    "date": date.strftime("%Y-%m-%d"),
                    "state": previous_state,
                    "previous_state": previous_state,
                    "candidate": None,
                    "score": None,
                    "ewi_slope": None,
                    "breadth": None,
                    "adx_median": None,
                    "hurst": None,
                    "vol_percentile": None,
                }
            )
            return

        new_state = self._advance_regime_state(
            observation.candidate_state, pos, previous_state
        )
        self._regime_prev_state = previous_state
        self._regime_state = new_state
        self._safe_mode_active = (new_state == "CHOPPY")
        if new_state == "TRANSITION":
            self._regime_transition_days += 1
        else:
            self._regime_transition_days = 0
            self._regime_transition_trimmed = False
        self._regime_state_series.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "state": new_state,
                "previous_state": previous_state,
                "candidate": observation.candidate_state,
                "score": observation.raw_score,
                "ewi_slope": observation.ewi_slope,
                "breadth": observation.breadth_above_ma,
                "adx_median": observation.adx_median,
                "hurst": observation.hurst,
                "vol_percentile": observation.volatility_percentile,
            }
        )

    def _merge_unblocked_daily_signals(
        self,
        symbols_dict: dict[str, str],
        data_map: dict[str, pd.DataFrame],
        indicator_map: dict[str, dict[str, pd.Series]],
        date: pd.Timestamp,
        date_str: str,
        current_assets: float,
        pending: list[tuple[Signal, BaseStrategy]],
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Rank candidates, fuse new signals, and remove conflicting pending buys.

        The market-regime state machine can veto new entries: when the current
        regime is CHOPPY, buys are suppressed (exits remain unaffected) by
        forwarding ``allow_buys=False`` to the strategy signal collector.

        On the *first day* the regime enters CHOPPY, a partial-position
        reduction is queued via ``_generate_regime_reduction_signals`` to
        actively cut exposure — not merely block new entries.

        The volatility fast-path was removed: in AI super-cycle markets,
        ``vol_percentile`` frequently hits 1.0 during V-shaped corrections
        (every new high sets the rank to 1.0), which blocked entries even in
        TREND state and reduced returns by ~20%.  The state machine's
        multi-day confirmation mechanism is sufficient to detect sustained
        choppy markets without blocking trend entries on temporary vol spikes.
        """
        regime_enabled = bool(self.cfg.get("market_regime_enabled", True))

        # --- Buy gate -------------------------------------------------------
        # Only the state machine gates entries: CHOPPY blocks buys, TRANSITION
        # scales them (handled in _fuse_daily_signals), TREND is fully open.
        allow_buys = self._regime_state != "CHOPPY"

        # --- Regime-mandated sells -----------------------------------------
        # TRANSITION trims only after sustained weak evidence and only once per
        # transition episode. CHOPPY still cuts immediately on entry.
        regime_sells: list[tuple[Signal, BaseStrategy]] = []
        if regime_enabled:
            if (
                self._regime_state == "TRANSITION"
                and not self._regime_transition_trimmed
                and self._regime_transition_days
                >= int(self.cfg.get("regime_transition_trim_confirmations", 5))
                and self._external_risk_level >= 1
                and self._regime_latest_observation is not None
                and self._regime_latest_observation.raw_score <= 0
            ):
                trim = float(self.cfg.get("regime_transition_exit_ratio", 0.0))
                if trim > 0:
                    regime_sells = self._generate_regime_reduction_signals(
                        date_str, trim
                    )
                    if regime_sells:
                        print(
                            f"  REGIME [{date_str}] TRANSITION entered — "
                            f"queue {len(regime_sells)} trim sells "
                            f"({trim:.0%} ratio)"
                        )
                self._regime_transition_trimmed = True
            elif (
                self._regime_state == "CHOPPY"
                and self._regime_prev_state != "CHOPPY"
            ):
                exit_ratio = float(self.cfg.get("regime_choppy_exit_ratio", 0.3))
                regime_sells = self._generate_regime_reduction_signals(
                    date_str, exit_ratio
                )
                if regime_sells:
                    print(
                        f"  REGIME [{date_str}] CHOPPY entered — "
                        f"queue {len(regime_sells)} partial sells "
                        f"({exit_ratio:.0%} ratio)"
                    )

        # Momentum ranks only allocate scarce slots; they do not create a buy
        # unless an underlying strategy independently emits an entry signal.
        top_symbols = self._select_momentum_candidates(data_map, symbols_dict, date)
        daily_signals = self._collect_strategy_signals(
            symbols_dict,
            data_map,
            indicator_map,
            date,
            date_str,
            current_assets,
            pending,
            allow_buys=allow_buys,
            top_symbols=top_symbols,
        )
        fused_daily = self._fuse_daily_signals(daily_signals, date_str)
        # Append regime-mandated partial sells after fusion so they survive
        # the buy/sell conflict resolution pass.
        fused_daily.extend(regime_sells)
        sells = {
            (signal.symbol, signal.strategy_name)
            for signal, _ in fused_daily
            if signal.direction == "sell"
        }
        if sells:
            sell_symbols = {symbol for symbol, _ in sells}
            symbol_veto = bool(self.cfg["symbol_level_sell_veto"])
            pending = [
                item
                for item in pending
                if not (
                    item[0].direction == "buy"
                    and (
                        item[0].symbol in sell_symbols
                        if symbol_veto
                        else (item[0].symbol, item[0].strategy_name) in sells
                    )
                )
            ]
        pending.extend(fused_daily)
        return pending

    def _fuse_daily_signals(
        self, daily: list[tuple[Signal, BaseStrategy]], date_str: str
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Resolve buy/sell conflicts and scale same-symbol strategy confirmations.

        The market-regime state machine can throttle entries: when the current
        regime is TRANSITION, surviving buy signals have their target shares
        scaled by ``regime_transition_scale`` (default 1.0) on top of the
        fusion-vote scaling applied by the base implementation. CHOPPY already
        blocks buys upstream, so only TRANSITION needs a post-pass here.
        """
        fused = super()._fuse_daily_signals(  # pyright: ignore[reportAttributeAccessIssue]
            daily, date_str
        )
        if self._regime_state != "TRANSITION" or self._external_risk_level < 1:
            return fused
        adjusted: list[tuple[Signal, BaseStrategy]] = []
        for signal, strategy in fused:
            if signal.direction != "buy":
                adjusted.append((signal, strategy))
                continue
            is_pyramid = strategy.position is not None
            scale = float(
                self.cfg.get(
                    "regime_transition_pyramid_scale"
                    if is_pyramid
                    else "regime_transition_scale",
                    0.0 if is_pyramid else 0.85,
                )
            )
            if scale >= 1.0:
                adjusted.append((signal, strategy))
                continue
            target_shares = _floor_to_lot(signal.target_shares * scale)
            if target_shares <= 0:
                # Regime throttle eliminated the entry; drop it but keep exits.
                continue
            adjusted.append(
                (
                    replace(
                        signal,
                        target_shares=target_shares,
                        reason=(
                            f"[regime transition x{scale:.2f}] {signal.reason}"
                        ),
                    ),
                    strategy,
                )
            )
        return adjusted
