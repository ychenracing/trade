"""Trend-following strategy implementations."""

from __future__ import annotations

import numpy as np
import pandas as pd

from quantfusion.domain.models import BarContext, Position, Signal
from quantfusion.domain.rules import floor_to_lot, is_finite_number

_floor_to_lot = floor_to_lot
_is_finite_number = is_finite_number


class BaseStrategy:
    """Define shared sizing, signal construction, and reversal protection."""

    name: str = "base"

    def __init__(self, cfg: dict) -> None:
        """Bind one validated symbol configuration to the strategy."""
        self.cfg = cfg
        self.position: Position | None = None

    def on_bar(self, ctx: BarContext) -> Signal | None:
        """Generate a close-based signal for one bar."""
        raise NotImplementedError

    def effective_exit_floor(self) -> float:
        """Return the current close-known downside exit used by risk controls."""
        return float(self.position.stop_loss) if self.position is not None else 0.0

    def _calc_shares(
        self, capital: float, price: float, atr_val: float, unit_number: int = 1
    ) -> int:
        """Convert a stop-distance risk budget into board-lot shares."""
        risk_pct = float(self.cfg.get("risk_pct", 0.01))
        atr_mult = float(self.cfg.get("atr_multiplier", 1.0))
        decay = float(self.cfg.get("pyramid_risk_decay", 1.0)) ** max(
            unit_number - 1, 0
        )
        if (
            not _is_finite_number(capital)
            or not _is_finite_number(price)
            or (not _is_finite_number(atr_val))
            or (not _is_finite_number(risk_pct))
            or (not _is_finite_number(atr_mult))
            or (not _is_finite_number(decay))
            or (capital <= 0)
            or (price <= 0)
            or (atr_val <= 0)
            or (risk_pct <= 0)
            or (atr_mult <= 0)
            or (decay <= 0)
        ):
            return 0
        # Risk-budget sizing: shares = cash risk budget / stop distance.
        # Later execution checks cap this theoretical size by cash and exposure.
        n = capital * risk_pct * decay / (atr_val * atr_mult)
        return _floor_to_lot(n)

    def _make_buy_signal(
        self,
        ctx: BarContext,
        shares: int,
        stop_loss: float,
        reason: str,
        atr_val: float = 0.0,
    ) -> Signal:
        """Create an immutable buy instruction from current close data."""
        price = float(ctx.df["close"].iloc[ctx.i])
        return Signal(
            symbol=ctx.symbol,
            strategy_name=self.name,
            direction="buy",
            target_shares=shares,
            price=price,
            stop_loss=stop_loss,
            reason=reason,
            signal_date=ctx.date,
            atr=float(atr_val) if _is_finite_number(atr_val) else 0.0,
        )

    def _make_sell_signal(self, ctx: BarContext, reason: str) -> Signal:
        """Create a full-position sell instruction from current close data."""
        return Signal(
            symbol=ctx.symbol,
            strategy_name=self.name,
            direction="sell",
            target_shares=self.position.shares if self.position else 0,
            price=float(ctx.df["close"].iloc[ctx.i]),
            reason=reason,
            signal_date=ctx.date,
        )

    def _fast_reversal_exit(self, ctx: BarContext) -> Signal | None:
        """Return an early exit after a confirmed reversal or failed trend."""
        pos = self.position
        if pos is None:
            return None
        i, df, cfg = (ctx.i, ctx.df, self.cfg)
        close = float(df["close"].iloc[i])
        if not _is_finite_number(close) or close <= 0:
            return None
        pos.highest_close_since_entry = max(pos.highest_close_since_entry, close)
        strategy_switch = {
            "turtle_breakout": "reversal_turtle_enabled",
            "dual_ma": "reversal_dual_ma_enabled",
            "atr_channel": "reversal_atr_channel_enabled",
        }.get(self.name)
        if strategy_switch and (not bool(cfg.get(strategy_switch, True))):
            return None
        peak_close = pos.highest_close_since_entry
        peak_gain = peak_close / pos.entry_price - 1 if pos.entry_price > 0 else 0.0
        activation = float(cfg.get("profit_lock_activation", 0.3))
        giveback = float(cfg.get("profit_lock_giveback", 0.18))
        # Profit protection activates only after a meaningful gain. Before that,
        # a loss exit also requires a weakening short moving average.
        if peak_gain >= activation:
            lock_stop = peak_close * (1 - giveback)
            pos.stop_loss = max(pos.stop_loss, lock_stop)
            if close <= lock_stop:
                return self._make_sell_signal(
                    ctx,
                    f"reversal profit protection ({giveback:.0%} giveback) at {lock_stop:.2f}",
                )
            exit_period = int(cfg.get("reversal_exit_period", 6))
            break_giveback = float(cfg.get("reversal_break_giveback", 0.12))
            if i >= exit_period:
                reversal_low = ctx.indicators.get("reversal_low")
                prior_low = reversal_low.iloc[i] if reversal_low is not None else np.nan
                ma_short = ctx.indicators.get("ma_short")
                ma_value = ma_short.iloc[i] if ma_short is not None else np.nan
                if (
                    _is_finite_number(prior_low)
                    and _is_finite_number(ma_value)
                    and (close <= peak_close * (1 - break_giveback))
                    and (close <= float(prior_low))
                    and (close < float(ma_value))
                ):
                    return self._make_sell_signal(
                        ctx,
                        f"short-term reversal breakdown({break_giveback:.0%}giveback+{exit_period}day low+short MA)",
                    )
        loss_cut = float(cfg.get("reversal_loss_cut", 0.1))
        ma_short = ctx.indicators.get("ma_short")
        if i >= 1 and ma_short is not None:
            ma_now, ma_prev = (ma_short.iloc[i], ma_short.iloc[i - 1])
            if (
                _is_finite_number(ma_now)
                and _is_finite_number(ma_prev)
                and (close <= pos.entry_price * (1 - loss_cut))
                and (close < float(ma_now) < float(ma_prev))
            ):
                return self._make_sell_signal(
                    ctx,
                    f"failed-trend reversal exit ({loss_cut:.0%} loss and weakening short MA)",
                )
        return None


class TurtleBreakoutStrategy(BaseStrategy):
    """Trade prior-window Donchian breakouts with ATR pyramiding and layered exits."""

    name = "turtle_breakout"

    def on_bar(self, ctx: BarContext) -> Signal | None:
        """Generate Donchian breakout, pyramid, stop, or exit signals."""
        i, df, ind = (ctx.i, ctx.df, ctx.indicators)
        cfg = self.cfg
        entry_period = cfg.get("entry_period", 20)
        exit_period = cfg.get("exit_period", 10)
        adx_threshold = cfg.get("adx_threshold", 15)
        max_units = cfg.get("max_units", 8)
        atr_stop_mult = cfg.get("atr_multiplier", 2)
        trail_mult = cfg.get("trail_atr_mult", 2.5)
        if i < max(entry_period, exit_period, cfg.get("adx_period", 14) + 5):
            return None
        close = df["close"].iloc[i]
        high = df["high"].iloc[i]
        atr_val = ind["atr"].iloc[i]
        adx_val = ind["adx"].iloc[i]
        upper = ind["donchian_upper"].iloc[i]
        lower = ind["donchian_lower"].iloc[i]
        if (
            pd.isna(atr_val)
            or pd.isna(adx_val)
            or pd.isna(upper)
            or pd.isna(lower)
            or (atr_val <= 0)
        ):
            return None
        if self.position is not None:
            pos = self.position
            pos.highest_since_entry = max(pos.highest_since_entry, high)
            reversal_signal = self._fast_reversal_exit(ctx)
            if reversal_signal is not None:
                return reversal_signal
            # Stops are monotonic: a later bar may tighten but never loosen them.
            trail_stop = pos.highest_since_entry - trail_mult * atr_val
            initial_stop = pos.entry_price - atr_stop_mult * atr_val
            pos.stop_loss = max(pos.stop_loss, trail_stop, initial_stop)
            if close <= pos.stop_loss:
                return self._make_sell_signal(
                    ctx, f"ATR trailing stop@{pos.stop_loss:.2f}"
                )
            if close <= pos.entry_price * (1 - cfg.get("hard_stop", 0.15)):
                return self._make_sell_signal(
                    ctx, f"hard stop{cfg.get('hard_stop', 0.15):.0%}"
                )
            if close <= lower:
                return self._make_sell_signal(ctx, f"Donchian exit@{lower:.2f}")
            if pos.units < max_units:
                add_gap = atr_val * float(cfg.get("pyramid_add_atr", 0.5))
                base_add_price = (
                    pos.last_add_price if pos.last_add_price > 0 else pos.entry_price
                )
                if close >= base_add_price + add_gap:
                    capital = ctx.current_assets * cfg.get("strategy_weight", 0.95)
                    shares = self._calc_shares(
                        capital, close, atr_val, unit_number=pos.units + 1
                    )
                    if shares > 0:
                        new_stop = high - trail_mult * atr_val
                        return Signal(
                            symbol=ctx.symbol,
                            strategy_name=self.name,
                            direction="buy",
                            target_shares=shares,
                            price=close,
                            stop_loss=max(pos.stop_loss, new_stop),
                            reason=f"Turtle pyramid add (unit {pos.units + 1})",
                            signal_date=ctx.date,
                            atr=float(atr_val),
                        )
            return None
        if adx_val > adx_threshold and close > upper:
            capital = ctx.current_assets * cfg.get("strategy_weight", 0.95)
            shares = self._calc_shares(capital, close, atr_val)
            if shares > 0:
                stop_loss = close - atr_stop_mult * atr_val
                return self._make_buy_signal(
                    ctx,
                    shares,
                    stop_loss,
                    f"Turtle breakout(ADX={adx_val:.1f})",
                    atr_val,
                )
        return None


class DualMAStrategy(BaseStrategy):
    """Trade RSI-confirmed moving-average crosses with ATR risk controls."""

    name = "dual_ma"

    def on_bar(self, ctx: BarContext) -> Signal | None:
        """Generate dual-moving-average trend signals."""
        i, df, ind = (ctx.i, ctx.df, ctx.indicators)
        cfg = self.cfg
        if i < cfg.get("ma_long", 60) + 2:
            return None
        close = df["close"].iloc[i]
        high = df["high"].iloc[i]
        ma_s = ind["ma_short"].iloc[i]
        ma_l = ind["ma_long"].iloc[i]
        rsi_val = ind["rsi"].iloc[i]
        atr_val = ind["atr"].iloc[i]
        trail_mult = cfg.get("trail_atr_mult", 2.5)
        if pd.isna(ma_s) or pd.isna(ma_l) or pd.isna(rsi_val):
            return None
        if self.position is not None:
            pos = self.position
            pos.highest_since_entry = max(pos.highest_since_entry, high)
            reversal_signal = self._fast_reversal_exit(ctx)
            if reversal_signal is not None:
                return reversal_signal
            if ma_s < ma_l:
                return self._make_sell_signal(
                    ctx,
                    f"MA{cfg.get('ma_short', 20)}crossed below MA{cfg.get('ma_long', 60)}",
                )
            if not pd.isna(atr_val) and atr_val > 0:
                trail_stop = pos.highest_since_entry - trail_mult * atr_val
                initial_stop = pos.entry_price - cfg.get("atr_multiplier", 2) * atr_val
                pos.stop_loss = max(pos.stop_loss, trail_stop, initial_stop)
                if close <= pos.stop_loss:
                    return self._make_sell_signal(
                        ctx, f"ATR trailing stop@{pos.stop_loss:.2f}"
                    )
            if close <= pos.entry_price * (1 - cfg.get("hard_stop", 0.15)):
                return self._make_sell_signal(
                    ctx, f"hard stop{cfg.get('hard_stop', 0.15):.0%}"
                )
            return None
        prev_ma_s = ind["ma_short"].iloc[i - 1]
        prev_ma_l = ind["ma_long"].iloc[i - 1]
        if pd.isna(prev_ma_s) or pd.isna(prev_ma_l):
            return None
        golden_cross = prev_ma_s <= prev_ma_l and ma_s > ma_l
        if golden_cross and rsi_val > 50:
            capital = ctx.current_assets * cfg.get("strategy_weight", 0.95)
            atr_fallback = atr_val if not pd.isna(atr_val) else close * 0.03
            shares = self._calc_shares(capital, close, atr_fallback)
            if shares > 0:
                stop_loss = close - cfg.get("atr_multiplier", 2) * atr_fallback
                return self._make_buy_signal(
                    ctx,
                    shares,
                    stop_loss,
                    f"MA golden cross(RSI={rsi_val:.0f})",
                    atr_fallback,
                )
        return None


class ATRChannelStrategy(BaseStrategy):
    """Trade ADX-confirmed ATR channel breakouts with channel and trailing exits."""

    name = "atr_channel"

    def on_bar(self, ctx: BarContext) -> Signal | None:
        """Generate ATR-channel breakout and risk-exit signals."""
        i, df, ind = (ctx.i, ctx.df, ctx.indicators)
        cfg = self.cfg
        period = cfg.get("atr_period", 20)
        if i < period + 5:
            return None
        close = df["close"].iloc[i]
        high = df["high"].iloc[i]
        atr_val = ind["atr"].iloc[i]
        adx_val = ind["adx"].iloc[i]
        ma = ind["ma_short"].iloc[i]
        trail_mult = cfg.get("trail_atr_mult", 2.5)
        if pd.isna(atr_val) or pd.isna(adx_val) or pd.isna(ma) or (atr_val <= 0):
            return None
        upper_channel = ma + cfg.get("channel_mult", 2.5) * atr_val
        lower_channel = ma - cfg.get("channel_lower_mult", 2.0) * atr_val
        if self.position is not None:
            pos = self.position
            pos.highest_since_entry = max(pos.highest_since_entry, high)
            reversal_signal = self._fast_reversal_exit(ctx)
            if reversal_signal is not None:
                return reversal_signal
            trail_stop = pos.highest_since_entry - trail_mult * atr_val
            initial_stop = pos.entry_price - cfg.get("atr_multiplier", 2) * atr_val
            pos.stop_loss = max(pos.stop_loss, trail_stop, initial_stop)
            if close <= pos.stop_loss:
                return self._make_sell_signal(
                    ctx, f"ATR trailing stop@{pos.stop_loss:.2f}"
                )
            if close <= lower_channel:
                return self._make_sell_signal(
                    ctx, f"ATR lower-channel exit@{lower_channel:.2f}"
                )
            if close <= pos.entry_price * (1 - cfg.get("hard_stop", 0.15)):
                return self._make_sell_signal(
                    ctx, f"hard stop{cfg.get('hard_stop', 0.15):.0%}"
                )
            return None
        if adx_val > cfg.get("adx_threshold", 15) and close > upper_channel:
            capital = ctx.current_assets * cfg.get("strategy_weight", 0.95)
            shares = self._calc_shares(capital, close, atr_val)
            if shares > 0:
                stop_loss = close - cfg.get("atr_multiplier", 2) * atr_val
                return self._make_buy_signal(
                    ctx,
                    shares,
                    stop_loss,
                    f"ATR channel breakout(ADX={adx_val:.1f})",
                    atr_val,
                )
        return None
