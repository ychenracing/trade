"""Read-only position, breadth, volatility, and concentration evidence."""

from __future__ import annotations

# pyright: reportAttributeAccessIssue=false

# ruff: noqa: F401

from dataclasses import replace
from typing import Any

import pandas as pd

from quantfusion.config.overlay import (
    CATASTROPHE_STOP_PCT,
    CATASTROPHE_COOLDOWN_DAYS,
    COST_ABS_STOP_PCT,
    LAYERED_ATR_MULTIPLIER,
    PROFIT_TIER_GIVEBACK,
    MIN_LAYERED_STOP_PCT,
    LAYERED_ARM_PORTFOLIO_DRAWDOWN,
    RISK_BASKET,
    RISK_SUB_BASKETS,
    SYMBOL_SUB_INDUSTRY,
    RISK_SUB_FAST_RETURN_SHOCK,
    RISK_SUB_BREADTH_SHOCK,
    RISK_FAST_DAYS,
    RISK_FAST_RETURN_SHOCK,
    RISK_BREADTH_SHOCK,
    RISK_BELOW_MA20_SHOCK,
    RISK_MIN_OBSERVED,
    RISK_HOLD_BREADTH_SHOCK,
    RISK_MIN_HELD,
    RISK_ESCALATION_DAYS,
    RISK_CONTINUOUS_CONFIRM_DAYS,
    RISK_SEVERE_DIRECT_RETURN,
    RISK_SEVERE_DIRECT_BREADTH,
    RISK_LEVEL2_DRAWDOWN,
    RISK_LEVEL3_DRAWDOWN,
    RISK_TRIM_FAST_DAYS,
    RISK_TRIM_REQUIRE_DECLINE,
    RISK_LEVEL2_TRIM_RATIO,
    RISK_LEVEL3_TRIM_RATIO,
    CONCENTRATION_CAP,
    CONCENTRATION_DRAWDOWN,
    CONCENTRATION_MIN_CLUSTER,
    CONCENTRATION_UNMAPPED_LIMIT,
    CONCENTRATION_MAX_TRIM_RATIO,
    SHOCK_FAST_DAYS,
    SHOCK_FAST_RETURN,
    SHOCK_BREADTH_THRESHOLD,
    SHOCK_VOL_SURGE,
    SHOCK_MIN_HELD,
    SHOCK_TRIM_DRAWDOWN,
    SHOCK_TRIM_RATIO,
    RISK_ACTION_PRIORITY,
    RISK_ACTION_DEFAULT_PRIORITY,
)
from quantfusion.domain.models import Signal
from quantfusion.domain.rules import floor_to_lot
from quantfusion.indicators.technical import Indicators
from quantfusion.risk.overlay.adapter import apply_risk_actions
from quantfusion.risk.overlay.models import RiskAction


class OverlayEvidenceMixin:
    """Read-only position, breadth, volatility, and concentration evidence."""

    @staticmethod
    def _held_positions(states: list) -> list[tuple]:
        """Return (state, symbol, strat_name, pos) for every held position."""
        out: list[tuple] = []
        for state in states:
            sleeve = state.sleeve
            for symbol, positions in sleeve.positions.items():
                for strat_name, pos in positions.items():
                    if pos.shares > 0:
                        out.append((state, symbol, strat_name, pos))
        return out

    @staticmethod
    def _close_prices(states: list, date: pd.Timestamp) -> dict[str, float]:
        prices: dict[str, float] = {}
        for state in states:
            for symbol, frame in state.data_map.items():
                if date in frame.index:
                    prices[symbol] = float(frame.loc[date]["close"])
        return prices

    def _frame_for(self, states: list, symbol: str):
        reference = self.risk_frames.get(symbol)
        if reference is not None:
            return reference
        for state in states:
            frame = state.data_map.get(symbol)
            if frame is not None:
                return frame
        return None

    def coverage_metrics(self) -> dict[str, Any]:
        """Return the latest risk-basket coverage measurement (2026-08-16 P1-2).

        Pure audit accessor: exposes how much of the independent risk basket
        and how many sub-industries were actually observed on the last graded
        day, so governance layers can compute a risk confidence without
        re-reading any decision state.
        """
        metrics = self._last_metrics or {}
        return {
            "observed": int(metrics.get("observed", 0)),
            "observed_industries": int(metrics.get("observed_industries", 0)),
            "total_basket": len(RISK_BASKET),
            "total_industries": len(RISK_SUB_BASKETS),
            "date": (
                self._last_metrics_date.strftime("%Y-%m-%d")
                if self._last_metrics_date is not None
                else None
            ),
        }

    def _layered_protection(
        self, state, symbol: str, pos, date: pd.Timestamp, drawdown: float
    ) -> tuple[float, str]:
        """Compute the layered protection line and its binding trigger (P0-1).

        Each protection line has its OWN independent trigger semantics (report
        P0-1). The old implementation required ``peak_drawdown >= 28%`` for ALL
        lines, which neutered the cost-absolute / ATR-chandelier / profit-tier
        protections and made the "layered" stop behave like a fixed 28%
        catastrophe stop. That unified peak-drawdown gate is removed: an armed
        line now exits as soon as the close breaks IT.

        Arm rules (matching report P0-1, plus a bull-silent account-drawdown
        gate so normal bull pull-backs are never cut):
          - catastrophe (28% peak-drawdown floor): ALWAYS armed;
          - cost-absolute (18% below entry): armed once the early sector-risk
            layer warns (level >= 1) AND the account is off peak by
            ``LAYERED_ARM_PORTFOLIO_DRAWDOWN``;
          - ATR chandelier (held peak - ATR): armed once market risk warns
            (level >= 1) AND the account is off peak by the same gate;
          - profit-tier giveback (peak-price giveback): armed only on a
            confirmed shock (level >= 2) AND off peak by the same gate.

        In a clean bull (level 0, or account at/near its peak) only the
        catastrophe floor is armed, so a normal 20% leader pull-back is never
        cut (golden-metric bull-silent). The binding line is whichever armed
        line is highest (earliest trigger).
        """
        entry = float(pos.entry_price)
        peak_close = max(
            float(getattr(pos, "highest_close_since_entry", 0.0)), entry
        )
        if entry <= 0:
            return 0.0, "none"

        # 4) Sector catastrophe floor (the original 28% peak-drawdown line).
        #    This is always armed and is the effective protection in a clean bull.
        sector_stop = peak_close * (1.0 - self.catastrophe_stop_pct)

        # Tighter lines only arm once early sector risk is detected AND the
        # whole account is genuinely off its peak (bull-silent: a clean bull
        # near its high-water mark never arms the tight cost/ATR/profit lines,
        # so routine 20-26% leader pull-backs are not cut).
        if self._risk_level < 1 or drawdown < LAYERED_ARM_PORTFOLIO_DRAWDOWN:
            return sector_stop, "catastrophe_stop"

        # 1) Cost absolute stop (armed once market risk warns).
        cost_stop = entry * (1.0 - COST_ABS_STOP_PCT)

        # 2) ATR chandelier from the held peak (armed once market risk warns).
        atr_value = float("nan")
        frame = self._frame_for([state], symbol)
        if frame is not None and date in frame.index:
            loc = frame.index.get_loc(date)
            atr_value = self._atr_at(frame, loc)
        atr_stop = peak_close - LAYERED_ATR_MULTIPLIER * atr_value
        if not (atr_value > 0) or atr_stop <= 0:
            atr_stop = 0.0

        # 3) Profit-tier giveback (only armed on a confirmed sector shock).
        #    ``giveback`` is the max pull-back from the held peak (fraction of
        #    peak price) before this line exits. Re-scaled from a peak-profit
        #    fraction so a large winner is not cut on a normal momentum
        #    pull-back (see PROFIT_TIER_GIVEBACK doc above).
        profit_stop = 0.0
        if self._risk_level >= 2:
            peak_gain = peak_close / entry - 1.0
            # Profit-tier giveback: take the LAST tier whose gain threshold the
            # peak gain reaches (PROFIT_TIER_GIVEBACK doc: 30-80% -> 18%,
            # 80-150% -> 22%, 150-300% -> 26%, >=300% -> 28%). We must NOT pick
            # the first tier whose threshold EXCEEDS ``peak_gain`` (that yields
            # the NEXT tier's looser giveback — 50% would get 0.22 instead of
            # 0.18, 100% would get 0.26 instead of 0.22, etc.), which would make
            # the layered stop weaker than calibrated and bleed moderate winners
            # through to a looser protection line.
            giveback = PROFIT_TIER_GIVEBACK[0][1]
            for gain_threshold, ratio in PROFIT_TIER_GIVEBACK:
                if peak_gain >= gain_threshold:
                    giveback = ratio
            # Trend-health adaptation is active only inside confirmed market
            # risk. Healthy leaders above rising MA20/MA60 get three extra
            # percentage points of breathing room; structurally weak holdings
            # surrender three points sooner. The catastrophe floor remains the
            # widest possible line and the looseness guard remains the tightest.
            giveback += self._trend_health_giveback_adjustment(
                state, symbol, date
            )
            giveback = min(
                self.catastrophe_stop_pct,
                max(MIN_LAYERED_STOP_PCT, giveback),
            )
            profit_stop = peak_close * (1.0 - giveback)
            # Looseness floor: giveback is already clamped below by
            # MIN_LAYERED_STOP_PCT (L888), so the stop can never sit tighter
            # than a ~14% pull-back from peak. This keeps a 300%+ winner from
            # being cut on a shallow pull-back that would harm bull returns
            # (report P0-1 ablation "若保护线设置过紧损害牛市收益").

        # Binding line = the highest armed line (earliest trigger).
        candidates = (
            ("cost_stop", cost_stop),
            ("atr_stop", atr_stop),
            ("profit_tier_stop", profit_stop),
            ("catastrophe_stop", sector_stop),
        )
        trigger_type = "catastrophe_stop"
        protection = sector_stop
        for name, line in candidates:
            if line > protection:
                protection = line
                trigger_type = name
        return (protection if protection > 0 else 0.0), trigger_type

    def _trend_health_giveback_adjustment(
        self, state, symbol: str, date: pd.Timestamp
    ) -> float:
        """Return a bounded giveback adjustment from causal trend health."""
        if not self.enable_trend_health:
            return 0.0
        frame = self._frame_for([state], symbol)
        if frame is None or date not in frame.index:
            return 0.0
        loc = frame.index.get_loc(date)
        if loc < 59:
            return 0.0
        closes = pd.to_numeric(frame["close"], errors="coerce")
        current = float(closes.iloc[loc])
        ma20 = float(closes.iloc[loc - 19: loc + 1].mean())
        ma60 = float(closes.iloc[loc - 59: loc + 1].mean())
        previous_ma20 = float(closes.iloc[loc - 20: loc].mean())
        if current > ma20 > ma60 and ma20 >= previous_ma20:
            return 0.03
        if current < ma20 <= ma60:
            return -0.03
        return 0.0

    @staticmethod
    def _atr_at(frame: pd.DataFrame, loc: int) -> float:
        """ATR (Wilder) at ``loc``, reusing the ensemble's unified ``Indicators.atr``.

        Report P0-2: the old overlay ATR used a SINGLE fixed previous close
        (``close.iloc[loc - 1]``) compared against every day's high/low in the
        window, which distorted the true range. The correct implementation
        uses each day's OWN previous close (``close.shift(1)``); the core
        ``Indicators.atr`` already does exactly this, so we reuse it instead of
        re-implementing a divergent ATR. Only data up to ``loc`` is used (no
        future leakage).
        """
        try:
            atr_series = Indicators.atr(frame, period=20, method="wilder")
            value = float(atr_series.iloc[loc])
            return value if value > 0 else float("nan")
        except (KeyError, IndexError, TypeError, ValueError):
            return float("nan")

    def _basket_metrics(self, states: list, date: pd.Timestamp) -> dict:
        """Compute sub-industry-equal metrics from the independent risk basket.

        Each available sub-industry contributes one vote regardless of how many
        constituents it contains. This prevents the large equipment group from
        dominating the market signal and caps every individual stock's impact.
        """
        industry_returns: list[float] = []
        industry_breadth: list[float] = []
        industry_below_ma20: list[float] = []
        observed = 0
        observed_industries = 0
        for members in RISK_SUB_BASKETS.values():
            returns: list[float] = []
            declines = 0
            below_ma20 = 0
            for symbol in members:
                frame = self._frame_for(states, symbol)
                if frame is None or date not in frame.index:
                    continue
                loc = frame.index.get_loc(date)
                closes = pd.to_numeric(frame["close"], errors="coerce")
                if loc < RISK_FAST_DAYS:
                    continue
                recent = closes.iloc[loc - RISK_FAST_DAYS + 1: loc + 1]
                if len(recent) < 2 or recent.isna().any():
                    continue
                fast_return = float(recent.iloc[-1] / recent.iloc[0] - 1.0)
                returns.append(fast_return)
                declines += int(fast_return < 0)
                if loc >= 19:
                    ma20 = float(closes.iloc[loc - 19: loc + 1].mean())
                    below_ma20 += int(ma20 > 0 and float(closes.iloc[loc]) < ma20)
            if not returns:
                continue
            observed += len(returns)
            observed_industries += 1
            industry_returns.append(sum(returns) / len(returns))
            industry_breadth.append(declines / len(returns))
            industry_below_ma20.append(below_ma20 / len(returns))
        return {
            "observed": observed,
            "observed_industries": observed_industries,
            "fast_returns": industry_returns,
            "declining_ratio": (
                sum(industry_breadth) / len(industry_breadth)
                if industry_breadth else 0.0
            ),
            "below_ma20_ratio": (
                sum(industry_below_ma20) / len(industry_below_ma20)
                if industry_below_ma20 else 0.0
            ),
        }

    def _sub_basket_stress(self, states: list, date: pd.Timestamp,
                           held: list) -> str | None:
        """Return the first held-relevant sub-industry under structured stress.

        A sub-basket is stressed when its equal-weight 3-day return is at or
        below ``RISK_SUB_FAST_RETURN_SHOCK`` AND the majority of its observed
        names declined. Report 4.7/4.8: only sub-baskets the user ACTUALLY holds
        are considered, so (e.g.) an equipment-only stress never trims an
        optical-heavy book. This is additive evidence only — it can raise the
        graded risk level but never adds a new exit switch on its own.
        """
        held_symbols = {sym for _, sym, _, _ in held}
        stressed: str | None = None
        for label, members in RISK_SUB_BASKETS.items():
            # Only consider a sub-basket that overlaps with the current book.
            if not any(m in held_symbols for m in members):
                continue
            returns: list[float] = []
            declining = 0
            for symbol in members:
                frame = self._frame_for(states, symbol)
                if frame is None or date not in frame.index:
                    continue
                loc = frame.index.get_loc(date)
                closes = pd.to_numeric(frame["close"], errors="coerce")
                if loc < RISK_FAST_DAYS:
                    continue
                recent = closes.iloc[loc - RISK_FAST_DAYS + 1: loc + 1]
                if len(recent) < 2 or recent.isna().any():
                    continue
                r = float(recent.iloc[-1] / recent.iloc[0] - 1.0)
                returns.append(r)
                if r < 0:
                    declining += 1
            if not returns:
                continue
            avg = sum(returns) / len(returns)
            if avg <= RISK_SUB_FAST_RETURN_SHOCK and (
                declining / len(returns) >= RISK_SUB_BREADTH_SHOCK
            ):
                # Deterministic tie-break so the audit is reproducible.
                if stressed is None or label < stressed:
                    stressed = label
        return stressed

    def _held_decline_breadth(self, states: list, date: pd.Timestamp, held: list) -> float:
        """Fraction of held names declining over the fast window."""
        symbols = sorted({sym for _, sym, _, _ in held})
        if len(symbols) < RISK_MIN_HELD:
            return 0.0
        declines = 0
        total = 0
        for symbol in symbols:
            frame = self._frame_for(states, symbol)
            if frame is None or date not in frame.index:
                continue
            loc = frame.index.get_loc(date)
            if loc < RISK_FAST_DAYS:
                continue
            closes = pd.to_numeric(frame["close"], errors="coerce")
            recent = closes.iloc[loc - RISK_FAST_DAYS + 1: loc + 1]
            if len(recent) < 2 or recent.isna().any():
                continue
            total += 1
            if float(recent.iloc[-1] / recent.iloc[0] - 1.0) < 0:
                declines += 1
        return declines / total if total > 0 else 0.0

    def _portfolio_fast_return(self) -> float:
        """Return the portfolio's fast-window (relative) return.

        Used as a bull-silent guard: a Level 2/3 trim only arms while the
        portfolio is currently declining. A pull-back that is already
        recovering on the signal day has a non-negative fast return and is
        never trimmed, so the layer does not sell winners into a bottom.
        """
        if len(self._assets_history) < RISK_TRIM_FAST_DAYS + 1:
            return 0.0
        older = self._assets_history[-RISK_TRIM_FAST_DAYS - 1]
        latest = self._assets_history[-1]
        if older <= 0:
            return 0.0
        return float(latest / older - 1.0)

    def _is_shock(self, states: list, date: pd.Timestamp,
                  prices: dict[str, float]) -> bool:
        held = self._held_positions(states)
        symbols = sorted({sym for _, sym, _, _ in held})
        if len(symbols) < SHOCK_MIN_HELD:
            return False
        declines = 0
        fast_returns: list[float] = []
        vol_surges = 0
        total = 0
        for symbol in symbols:
            frame = self._frame_for(states, symbol)
            if frame is None or date not in frame.index:
                continue
            loc = frame.index.get_loc(date)
            if loc < SHOCK_FAST_DAYS:
                continue
            total += 1
            recent = frame["close"].iloc[loc - SHOCK_FAST_DAYS + 1: loc + 1]
            fast_return = float(recent.iloc[-1] / recent.iloc[0] - 1.0)
            fast_returns.append(fast_return)
            if fast_return < 0:
                declines += 1
            if "volume" in frame.columns:
                avg_vol = frame["volume"].iloc[max(0, loc - 20): loc].mean()
                cur_vol = frame["volume"].iloc[loc]
                if avg_vol > 0 and cur_vol > avg_vol * SHOCK_VOL_SURGE:
                    vol_surges += 1
        if total == 0:
            return False
        breadth = declines / total
        avg_return = sum(fast_returns) / len(fast_returns)
        return (
            avg_return <= SHOCK_FAST_RETURN and breadth >= SHOCK_BREADTH_THRESHOLD
        ) or (
            avg_return <= SHOCK_FAST_RETURN * 1.5
            and breadth >= 0.80
            and vol_surges >= max(1, total // 3)
        )
