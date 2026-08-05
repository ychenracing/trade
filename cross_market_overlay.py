"""穿越牛熊 (Cross-Market-Cycle) Risk Overlay for the ensemble engine.

This module layers defensive behaviour ON TOP of the three-sleeve ensemble
without consolidating capital or reducing bull-market deployment. The core
design rule is **bull-silent**: every mechanism defaults ON but only fires on
a genuine risk signal, so a clean bull market (no shock, no catastrophe drop)
is left completely untouched.

Two mechanisms are provided:

1. ``catastrophe_stops`` — per-symbol, peak-based stop. Any held position
   whose close collapses ``CATASTROPHE_STOP_PCT`` (28%) off its own
   ``highest_close_since_entry`` is fully exited. In a healthy bull a leader
   pulls back 15-20% before resuming, so this is effectively silent; in a
   crash it caps each single-name loss and protects the gains already banked
   on a runner.

2. ``shock_trims`` — market-wide structural shock fast-de-risking. When a
   breadth/volume shock is detected across the currently held names AND the
   portfolio is already off its peak by ``SHOCK_TRIM_DRAWDOWN``, the weakest
   positions (lowest allocation score) are trimmed by ``SHOCK_TRIM_RATIO``.
   This complements the existing confirmed/emergency/terminal drawdown
   circuit breakers by acting on market *structure* earlier, but only after
   a real drawdown is already underway so it never preempts fresh bull
   entries.

The overlay is designed to be fed the sleeve states and to append sell
signals directly into each sleeve's ``pending`` queue for T+1 execution
through the normal ensemble machinery.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

# Percentage a position must fall off its own peak to trigger a full exit.
CATASTROPHE_STOP_PCT = 0.28

# Fast-de-risk window and severity for structural shock detection.
SHOCK_FAST_DAYS = 3
SHOCK_FAST_RETURN = -0.06
SHOCK_BREADTH_THRESHOLD = 0.70   # 70% of held names declining
SHOCK_VOL_SURGE = 2.0            # 2x average volume
SHOCK_MIN_HELD = 2               # need at least 2 held names to judge breadth

# Shock-trim gating: only de-risk once the portfolio is off its peak by this
# much (so fresh bull entries are never blocked) and trim this fraction.
SHOCK_TRIM_DRAWDOWN = 0.08
SHOCK_TRIM_RATIO = 0.30

# Re-entry cooldown (trading days) after a catastrophe exit.
CATASTROPHE_COOLDOWN_DAYS = 10


class CrossMarketOverlay:
    """Per-day defensive checks over the ensemble sleeves."""

    def __init__(
        self,
        catastrophe_stop_pct: float = CATASTROPHE_STOP_PCT,
        shock_trim_drawdown: float = SHOCK_TRIM_DRAWDOWN,
        shock_trim_ratio: float = SHOCK_TRIM_RATIO,
        enable_shock_trim: bool = False,
    ) -> None:
        self.catastrophe_stop_pct = float(catastrophe_stop_pct)
        self.shock_trim_drawdown = float(shock_trim_drawdown)
        self.shock_trim_ratio = float(shock_trim_ratio)
        # Structural-shock fast de-risking is OFF by default: the ensemble
        # already carries regime-based de-risking and drawdown circuit
        # breakers, and double-de-risking trims winners on normal pullbacks
        # (cutting bull returns). Kept available for explicit opt-in.
        self.enable_shock_trim = bool(enable_shock_trim)
        self._catastrophe_cooldown: dict[str, int] = {}  # symbol -> expiry pos
        self.events: list[dict[str, Any]] = []

    # ── per-position helpers ──────────────────────────────────────────

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

    # ── catastrophe stop ─────────────────────────────────────────────

    def on_day(
        self,
        states: list,
        date: pd.Timestamp,
        date_pos: int,
        assets: float,
        peak: float,
        scoring_fn,
    ) -> None:
        """Run overlay checks for one day, appending T+1 sell signals.

        ``scoring_fn`` maps a symbol to an allocation score (lower = weaker),
        used to rank which names get trimmed first on a structural shock.
        """
        date_str = date.strftime("%Y-%m-%d")
        prices = self._close_prices(states, date)
        held = self._held_positions(states)

        # 1) Per-symbol catastrophe stops (bull-silent).
        # Two passes so every sleeve's position in a crashing symbol is exited
        # on the same day: first determine which symbols qualify, then sell all
        # positions in them. The cooldown only gates FUTURE re-entry, never the
        # sibling exits on the triggering day.
        exit_symbols: dict[str, float] = {}
        for state, symbol, strat_name, pos in held:
            if date_pos < self._catastrophe_cooldown.get(symbol, -1):
                continue
            peak_close = max(float(getattr(pos, "highest_close_since_entry", 0.0)),
                             float(pos.entry_price))
            price = prices.get(symbol, 0.0)
            if peak_close <= 0 or price <= 0:
                continue
            drop_pct = (peak_close - price) / peak_close
            if drop_pct >= self.catastrophe_stop_pct:
                if symbol not in exit_symbols or drop_pct > exit_symbols[symbol]:
                    exit_symbols[symbol] = drop_pct
        for symbol, drop_pct in exit_symbols.items():
            price = prices.get(symbol, 0.0)
            for state, strat_name, pos in (
                (st, sn, p) for st, sy, sn, p in held if sy == symbol
            ):
                self._queue_sell(
                    state, symbol, strat_name, pos.shares, price,
                    date_str, "catastrophe_stop",
                    f"drop_from_peak={drop_pct:.1%}",
                )
            self._catastrophe_cooldown[symbol] = date_pos + CATASTROPHE_COOLDOWN_DAYS
            self.events.append({
                "date": date_str, "event": "catastrophe_stop",
                "symbol": symbol, "drop_from_peak": round(drop_pct, 4),
            })

        # 2) Structural-shock fast de-risk (opt-in only — see __init__).
        if self.enable_shock_trim and peak > 0 and assets < peak * (1.0 - self.shock_trim_drawdown):
            if self._is_shock(states, date, prices):
                self._trim_laggards(states, prices, date_str, scoring_fn)

    def _queue_sell(
        self, state, symbol: str, strat_name: str, shares: int,
        price: float, date_str: str, reason: str, extra: str = "",
    ) -> None:
        if shares <= 0 or price <= 0:
            return
        sig = _make_sell_signal(symbol, strat_name, shares, price, date_str,
                                reason, extra)
        state.pending.append((sig, None))

    # ── structural shock detection ───────────────────────────────────

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

    @staticmethod
    def _frame_for(states: list, symbol: str):
        for state in states:
            frame = state.data_map.get(symbol)
            if frame is not None:
                return frame
        return None

    # ── laggard trimming on shock ────────────────────────────────────

    def _trim_laggards(self, states: list, prices: dict[str, float],
                       date_str: str, scoring_fn) -> None:
        held = self._held_positions(states)
        # Aggregate shares per symbol across all sleeves.
        shares_by_symbol: dict[str, int] = {}
        strats: dict[str, list[tuple]] = {}
        for state, symbol, strat_name, pos in held:
            shares_by_symbol[symbol] = shares_by_symbol.get(symbol, 0) + pos.shares
            strats.setdefault(symbol, []).append((state, strat_name, pos))
        if not shares_by_symbol:
            return
        ranked = sorted(
            shares_by_symbol,
            key=lambda sym: (scoring_fn(sym) if scoring_fn else 0.0, sym),
        )
        # Trim only the single weakest name by the shock ratio.
        weak = ranked[0]
        trim_shares = int(shares_by_symbol[weak] * self.shock_trim_ratio)
        trimmed = 0
        price = prices.get(weak, 0.0)
        for state, strat_name, pos in strats[weak]:
            if trimmed >= trim_shares:
                break
            take = max(0, min(pos.shares, trim_shares - trimmed))
            if take <= 0:
                continue
            self._queue_sell(state, weak, strat_name, take, price, date_str,
                             "shock_trim", "structural_shock_de_risk")
            trimmed += take
        if trimmed > 0:
            self.events.append({
                "date": date_str, "event": "shock_trim",
                "symbol": weak, "shares": trimmed,
            })


def _make_sell_signal(symbol: str, strat_name: str, shares: int,
                      price: float, date_str: str, reason: str,
                      extra: str = "") -> Any:
    """Build a sell Signal that the ensemble sleeve can execute at T+1 open."""
    from quant_fusion import Signal
    full_reason = f"{reason}:{extra}" if extra else reason
    return Signal(
        symbol=symbol,
        strategy_name=strat_name,
        direction="sell",
        target_shares=shares,
        price=price,
        reason=full_reason,
        signal_date=date_str,
    )
