"""Low-turnover weak-market position policy."""

from __future__ import annotations

import math
from typing import Any, cast

import pandas as pd

from quantfusion.config.regime import (
    DEFAULT_EXIT_COOLDOWN,
    LEADER_LOOKBACK,
    PROFIT_ACTIVATION,
    TRAILING_ATR_MULTIPLIER,
    WEAK_ENTRY_ATR_MULTIPLIER,
    WEAK_EXIT_COOLDOWN,
    WEAK_HARD_STOP,
    WEAK_PROBE_CONFIRM_DAYS,
    WEAK_PROBE_WEIGHT_RATIO,
    WEAK_REENTRY_FAIL_LIMIT,
    WEAK_REENTRY_MAX_DRAWDOWN,
    WEAK_TIME_STOP_DAYS,
    WEAK_TIME_STOP_RETURN,
)
from quantfusion.domain.models import BarContext, Signal
from quantfusion.domain.rules import floor_to_lot
from quantfusion.strategy.trend import BaseStrategy

class PositiveMomentumHoldStrategy(BaseStrategy):
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

    def _closes(self, ctx: BarContext):
        return pd.Series(
            pd.to_numeric(ctx.df["close"], errors="coerce"), index=ctx.df.index
        ).dropna()

    def _target_shares(self, ctx: BarContext, ratio: float) -> int:
        """Board-lot shares for ``ratio`` of the normal target weight."""
        weight = float(self.cfg["strategy_weight"]) * ratio
        return floor_to_lot(ctx.current_assets * weight / float(ctx.df["close"].iloc[ctx.i]))

    def effective_exit_floor(self) -> float:
        """Expose the active disaster/chandelier floor without changing exits."""
        return max(super().effective_exit_floor(), float(self._trail_stop))

    def _reentry_ok(self, ctx: BarContext) -> bool:
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

    def _confirm_ok(self, ctx: BarContext) -> bool:
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

    def _finalize_exit(self, ctx: BarContext) -> None:
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

    def on_bar(self, ctx: BarContext) -> Signal | None:
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
            add = floor_to_lot(full_shares - position.shares)
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


class CashPreservationStrategy(BaseStrategy):
    """Deliberately emit no orders when causal evidence is insufficient."""

    name = "cash_preservation"

    def on_bar(self, ctx: BarContext) -> None:
        del ctx
        return None


__all__ = [
    "CashPreservationStrategy",
    "PositiveMomentumHoldStrategy",
]
