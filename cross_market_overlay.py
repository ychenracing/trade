"""穿越牛熊 (Cross-Market-Cycle) Risk Overlay for the ensemble engine.

This module layers defensive behaviour ON TOP of the three-sleeve ensemble
without consolidating capital or reducing bull-market deployment. The core
design rule is **bull-silent**: every mechanism defaults ON but only fires on
a genuine risk signal, so a clean bull market (no shock, no catastrophe drop)
is left completely untouched.

The overlay provides three mechanisms (report sections 4.1 and 4.2):

1. ``layered_stops`` — per-position, peak-based, volatility- and
   profit-graded protection. The old single 28% peak-drawdown catastrophe
   stop is replaced by a *layered* protection line:

       protection = max(cost_abs_stop, atr_chandelier,
                        profit_tier_giveback, sector_stop)

   The profit-tier giveback tightens as a position's peak gain grows
   (allow less giveback for big winners). To remain bull-silent, the tighter
   profit-tier lines only ARM when a sector-risk level (Level >= 2) is
   active; in a clean bull (Level 0) the protection falls back to the
   conservative 28% floor so normal leader pull-backs are never cut.

2. ``early_sector_risk`` — a multi-evidence, low-frequency, graded early
   sector-risk layer. It reads a fixed AI risk basket (independent of the
   user's current holdings) and, when the held names are broad enough, also
   cross-checks the held names' breadth. It produces a daily risk ``level``:

   - Level 0 (normal): no intervention.
   - Level 1 (warning): freeze new high-risk additions; record the event.
   - Level 2 (confirmed shock): trim the weakest non-core position and arm
     the layered stop's tighter profit-tier lines.
   - Level 3 (sustained failure): de-risk the weakest non-core holdings so
     portfolio exposure drops toward a strong-only basket.

   This complements the existing confirmed/emergency/terminal drawdown
   circuit breakers by acting on market *structure* earlier, but only after
   real evidence so it never preempts fresh bull entries.

3. ``shock_trims`` — market-wide structural shock fast de-risking (historical
   opt-in). When a breadth/volume shock is detected across the currently held
   names AND the portfolio is already off its peak by ``SHOCK_TRIM_DRAWDOWN``,
   the weakest position is trimmed by ``SHOCK_TRIM_RATIO``. This is kept OFF
   by default because the multi-evidence early sector-risk layer now covers
   this role more precisely; it is retained for explicit opt-in.

The overlay is designed to be fed the sleeve states and to append sell
signals directly into each sleeve's ``pending`` queue for T+1 execution
through the normal ensemble machinery.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pandas as pd

if TYPE_CHECKING:
    from quant_fusion import Signal

# ── Layered catastrophe-stop constants (report 4.2) ──────────────────────
# Conservative 28% peak-drawdown floor (unchanged from the original overlay).
# The profit-tier giveback ratios below are only armed at sector-risk
# Level >= 2, so a clean bull keeps the 28% floor.
CATASTROPHE_STOP_PCT = 0.28
CATASTROPHE_COOLDOWN_DAYS = 10

# Cost absolute stop: never let an unprofitable position fall more than this
# much below cost before the layered stop intervenes.
COST_ABS_STOP_PCT = 0.18

# ATR chandelier multiplier for the layered stop. Looser than the weak-regime
# 3 ATR so it stays a backstop, not the primary exit. The 4 ATR line cut
# volatile AI leaders on routine 20-26% pull-backs (report P0-1 ablation: "若
# 保护线设置过紧，也可能损害牛市收益"), so it is set to 6 ATR — a genuine
# crash sized to these names' volatility, not a normal bull pull-back.
LAYERED_ATR_MULTIPLIER = 6.0

# Profit-tier giveback: maximum allowed drawdown from the held peak (fraction
# of peak price) before the layered stop exits. Measured as a price drawdown
# so the line is stable across very different absolute gains.
#
# Bull-preservation (report P0-1 ablation): the earlier design expressed the
# giveback as a fraction of peak *profit*, which for a large winner (e.g. a
# 300%+ gainer) translated into only a ~14% price pull-back. A volatile AI
# momentum leader routinely breathes 15-20% even while its trend is intact,
# so that tight line repeatedly cut the biggest bull winners on shallow
# pull-backs and destroyed long-run compounding ("若保护线设置过紧，也可能损害
# 牛市收益"). The giveback is therefore re-scaled to a *fraction of peak price*
# and made more generous for large winners: the 300%+ tier converges to the
# 28% catastrophe floor, so a huge winner is only cut by a genuine catastrophe
# drop, while moderate winners (30-80%) are still locked in on a 15% pull-back.
#   peak_gain < 30%          -> cost-based stop (COST_ABS_STOP_PCT)
#   30%  <= peak_gain < 80%  -> exit on a 18% pull-back from peak
#   80%  <= peak_gain < 150% -> exit on a 22% pull-back from peak
#   150% <= peak_gain < 300% -> exit on a 26% pull-back from peak
#   300% <= peak_gain        -> exit on a 28% pull-back (the catastrophe floor)
#
# The givebacks above are deliberately looser than the initial 15%/18%/22%
# calibration. Report P0-1's ablation warns that over-tight protection lines cut
# volatile AI momentum leaders on routine pull-backs and destroy bull compounding
# ("若保护线设置过紧，也可能损害牛市收益"). In the ensemble backtests the
# tighter 15%/18% lines repeatedly exited 80-300% winners on recoverable 15-24%
# pull-backs during a confirmed-shock window, costing ~8% of return on the
# 13-symbol pool and ~3% on the 22-symbol pool while leaving drawdown unchanged.
# The looser lines let moderate-large winners breathe to the wide 300%+ tier
# (28% catastrophe floor) while still locking in gains on genuine deeper drops.
PROFIT_TIER_GIVEBACK = ((0.30, 0.18), (0.80, 0.22), (1.50, 0.26), (3.00, 0.28))
# Looseness guard: the profit-tier stop must never be tighter than this
# distance from the held peak (never trigger on a smaller pull-back). Applied
# with ``min`` so it only prevents over-tightend lines, never cuts a big winner
# on a shallow dip.
MIN_LAYERED_STOP_PCT = 0.14

# Portfolio-drawdown arm gate for the tighter layered lines (cost-absolute /
# ATR chandelier / profit-tier). The tighter lines only arm once the WHOLE
# account is materially off its peak (report "bull-silent" principle): in a
# clean bull the portfolio rides near its high-water mark, so a normal 20-26%
# leader pull-back is never cut by the 4-ATR chandelier / cost line — only a
# genuine account-level decline (>= this drawdown) plus the sector-risk warning
# arms them. This keeps bull-market returns while still protecting on real risk.
LAYERED_ARM_PORTFOLIO_DRAWDOWN = 0.05

# ── Early sector-risk layer constants (report 4.1) ────────────────────────
# Fixed AI risk basket (independent of the user's held names). This is used
# to judge the *market* regime without making route decisions depend on the
# user's current stock pool.
RISK_BASKET = (
    "300308",  # 中际旭创 - 光模块
    "300502",  # 新易盛 - 光模块
    "300394",  # 天孚通信 - 光模块
    "688008",  # 澜起科技 - 存储接口
    "603986",  # 兆易创新 - 存储/设计
    "002409",  # 雅克科技 - 半导体材料
    "688072",  # 拓荆科技 - 半导体设备
    "688256",  # 寒武纪 - 国产算力
    "300054",  # 鼎龙股份 - 材料
    "688082",  # 盛美上海 - 设备
)

# Report 4.7: layered / sub-industry risk baskets. The TOTAL AI basket above
# decides the overall route; the sub-industry baskets let the layer detect a
# STRUCTURED stress inside one sub-sector (e.g. equipment or materials) even
# when the rest of the basket is holding up. These are additive EVIDENCE only:
# they never add a new exit switch on their own — a sub-basket shock can only
# raise the graded risk level (which requires the same portfolio-drawdown and
# escalation gates as the total-basket path).
RISK_SUB_BASKETS = {
    "optical": ("300308", "300502", "300394"),
    "memory": ("688008", "603986"),
    "compute": ("688256",),
    "equipment": ("688072", "688082"),
    "material": ("002409", "300054"),
}

# Report 4.7/4.8: a symbol -> sub-industry map used to judge whether a
# sub-basket's stress is RELEVANT to the user's current book. A sub-basket
# shock is only allowed to escalate the risk level / trim when the held
# portfolio is actually exposed to that sub-industry. This prevents, e.g.,
# an equipment-only stress from trimming an optical winner the user holds.
SYMBOL_SUB_INDUSTRY: dict[str, str] = {
    "300308": "optical", "300502": "optical", "300394": "optical",
    "688008": "memory", "603986": "memory",
    "688256": "compute",
    "688072": "equipment", "688082": "equipment",
    "002409": "material", "300054": "material",
}
# The inverse: sub-industry -> held symbols it covers (only those above).
_SUB_TO_SYMBOLS: dict[str, tuple[str, ...]] = {}
for _ind, _members in RISK_SUB_BASKETS.items():
    _SUB_TO_SYMBOLS[_ind] = tuple(
        s for s in _members if s in SYMBOL_SUB_INDUSTRY
    )
# A sub-basket is considered under stress when its equal-weight 3-day return
# is at or below this shock AND the majority of its observed names declined.
RISK_SUB_FAST_RETURN_SHOCK = -0.06
RISK_SUB_BREADTH_SHOCK = 0.60

# Multi-evidence thresholds (all must be met / combined to grade).
RISK_FAST_DAYS = 3
RISK_FAST_RETURN_SHOCK = -0.06     # 3-day equal-weight return shock
RISK_BREADTH_SHOCK = 0.70          # fraction of basket declining -> shock
RISK_BELOW_MA20_SHOCK = 0.60       # fraction below MA20 -> shock
RISK_MIN_OBSERVED = 4              # need at least this many observed names
RISK_HOLD_BREADTH_SHOCK = 0.80     # held-names decline breadth for shock
RISK_MIN_HELD = 2                  # need at least 2 held names to judge breadth

# Escalation window: Level 2 ("预警后再次冲击") requires a PRIOR warning
# within this many trading days, so a one-off pull-back never trims.
RISK_ESCALATION_DAYS = 5

# Portfolio drawdown gates (report 4.1: Level 2 requires "组合回撤超过5%-8%").
# A trim only arms once the PORTFOLIO is actually deep off its peak, so the
# layer stays bull-silent during a clean rally's ordinary pull-backs (which
# rarely take a diversified book deep off peak). The 8% / 12% floors are
# deliberately above the 5%-8% band so a single-name squeeze or a normal
# multi-day pull-back never triggers a trim of bull winners.
RISK_LEVEL2_DRAWDOWN = 0.08        # portfolio must be >=8% off peak to trim
RISK_LEVEL3_DRAWDOWN = 0.12        # portfolio must be >=12% off peak to de-risk

# Level 2/3 trims additionally require the PORTFOLIO to be currently in a
# decline (its fast-window return is negative). A pull-back that is already
# recovering on the signal day is not trimmed, so the layer never sells into a
# bottom while it is green. This is the concrete "不出售赢家" bull-silent guard.
RISK_TRIM_FAST_DAYS = 3
RISK_TRIM_REQUIRE_DECLINE = True

# Graded response trims (only the weakest NON-CORE position is trimmed, and
# only after the above escalation + portfolio-drawdown gates are crossed).
RISK_LEVEL2_TRIM_RATIO = 0.30      # trim weakest at Level 2 by this fraction
RISK_LEVEL3_TRIM_RATIO = 0.50      # de-risk weakest at Level 3 by this fraction

# ── Report 4.8: industry-concentration / correlation-cluster guard ────────
# "最多6只" is not true diversification: six names can still be one sector.
# This guard caps the total weight a single sub-industry cluster may reach in
# the held book. It is BULL-SILENT by design: it only trims when (a) a single
# sub-industry cluster exceeds the concentration cap, (b) the portfolio is
# genuinely off its peak, and (c) the portfolio is currently declining. In a
# clean bull (no drawdown, no decline) the guard never fires, so a run that
# concentrates in an optical winner is not disturbed.
CONCENTRATION_CAP = 0.80          # a single sub-industry may not exceed 80%
CONCENTRATION_DRAWDOWN = 0.08     # portfolio must be >=8% off peak to trim
CONCENTRATION_MIN_CLUSTER = 2     # ignore single-name clusters (<=1 symbol)
# Report P1-5: if unmapped holdings exceed this share of the account, the
# sub-industry coverage is too incomplete to judge concentration, so the guard
# FAILS CLOSED (no trim) rather than trimming on a partial picture. A small
# unmapped tail is ignored so a book of mostly-mapped names is still protected.
CONCENTRATION_UNMAPPED_LIMIT = 0.05
# Only the weakest name in the over-concentrated cluster is trimmed, and only
# by just enough to bring the cluster back under the cap (never a full exit).
CONCENTRATION_MAX_TRIM_RATIO = 0.25

# ── Historical shock-trim constants (retained, opt-in) ────────────────────
SHOCK_FAST_DAYS = 3
SHOCK_FAST_RETURN = -0.06
SHOCK_BREADTH_THRESHOLD = 0.70
SHOCK_VOL_SURGE = 2.0
SHOCK_MIN_HELD = 2
SHOCK_TRIM_DRAWDOWN = 0.08
SHOCK_TRIM_RATIO = 0.30

# ── Unified risk-action priority (report P1-1) ────────────────────────────
# trade previously had several independent risk mechanisms (core strategy
# stops, portfolio drawdown, regime route, industry protection, early risk
# level, layered protection, catastrophe stop, concentration trim, weak-market
# stop, route switch) that could each emit a sell for the SAME symbol on the
# SAME day, causing duplicate sells, over-trimming, and opaque backtests.
# This overlay now consolidates every sell it emits under ONE priority so that
# only the highest-priority action for a symbol executes in a given day.
#
# The ordering mirrors report P1-1 (higher = more conservative/more urgent):
#   - a full liquidation (catastrophe / layered full exit) outright wins;
#   - followed by a graded sector-risk trim;
#   - then a partial concentration trim / structural-shock trim;
#   - ordinary strategy exits and rebalances are handled by the sleeves.
RISK_ACTION_PRIORITY: dict[str, int] = {
    "catastrophe_stop": 100,      # full exit, always armed
    "cost_stop": 90,              # full exit, layered cost line
    "atr_stop": 90,               # full exit, layered ATR chandelier
    "profit_tier_stop": 90,       # full exit, layered profit-protection line
    "layered_stop": 90,           # generic full-exit tag
    "sector_risk_trim": 60,       # graded early-sector-risk trim (L2/L3)
    "concentration_trim": 50,     # sub-industry concentration guard trim
    "shock_trim": 40,             # structural-shock fast de-risk (opt-in)
}
# Priority used when a sell reason is not otherwise mapped (fail safe: treat an
# unknown reason as a normal/ordinary exit, lower than any overlay trim).
RISK_ACTION_DEFAULT_PRIORITY = 10


class CrossMarketOverlay:
    """Per-day defensive checks over the ensemble sleeves."""

    def __init__(
        self,
        catastrophe_stop_pct: float = CATASTROPHE_STOP_PCT,
        shock_trim_drawdown: float = SHOCK_TRIM_DRAWDOWN,
        shock_trim_ratio: float = SHOCK_TRIM_RATIO,
        enable_shock_trim: bool = False,
        *,
        enable_early_sector_risk: bool = True,
    ) -> None:
        self.catastrophe_stop_pct = float(catastrophe_stop_pct)
        self.shock_trim_drawdown = float(shock_trim_drawdown)
        self.shock_trim_ratio = float(shock_trim_ratio)
        # Structural-shock fast de-risking is OFF by default: the ensemble
        # already carries regime-based de-risking and the multi-evidence early
        # sector-risk layer (below) covers this role more precisely.
        self.enable_shock_trim = bool(enable_shock_trim)
        # Multi-evidence early sector-risk layer is ON by default (bull-silent).
        self.enable_early_sector_risk = bool(enable_early_sector_risk)
        # Industry-concentration / correlation-cluster guard is ON by default
        # (bull-silent report 4.8): only trims an over-concentrated cluster
        # while the portfolio is off peak and declining.
        self.enable_concentration_guard = True
        self._catastrophe_cooldown: dict[str, int] = {}  # symbol -> expiry pos
        self._risk_level = 0
        self._risk_level_day = -1
        self._last_warning_pos = -10**9  # last trading pos a Level 1 warning fired
        self._assets_history: list[float] = []  # recent portfolio asset values
        # The sub-industry basket that is currently under structured stress (if
        # any). Report 4.7/4.8: a Level 2/3 trim is restricted to holdings in
        # this same sub-industry so an equipment-only stress never cuts an
        # optical winner the user holds (bull-silent relevance guard).
        self._stressed_sub: str | None = None
        # Whether the risk level has RECOVERED to 0 since the last warning.
        # Report 4.1 "预警后再次冲击" means warning -> recovery -> RE-shock; a
        # trim must not fire on the same continuous shock that first warned.
        self._recovered_since_warning = False
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

    @staticmethod
    def _frame_for(states: list, symbol: str):
        for state in states:
            frame = state.data_map.get(symbol)
            if frame is not None:
                return frame
        return None

    # ── daily entry point ─────────────────────────────────────────────

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
        drawdown = (1.0 - assets / peak) if peak > 0 else 0.0
        # Track portfolio assets so a Level 2/3 trim can require the portfolio
        # to be currently declining (bull-silent: never trim a recovering book).
        self._assets_history.append(float(assets))
        if len(self._assets_history) > RISK_TRIM_FAST_DAYS + 1:
            del self._assets_history[0]

        # 0) Multi-evidence early sector-risk layer (P1-1). It only *records*
        #    the risk level and applies Level 2/3 trims (gated on portfolio
        #    drawdown); the layered stop uses the level to arm tighter
        #    profit-tier lines on a confirmed shock.
        if self.enable_early_sector_risk and date_pos != self._risk_level_day:
            self._update_risk_level(states, date, date_pos, held, drawdown)
            self._risk_level_day = date_pos

        # 1) Layered catastrophe stops (P0-1, replacing the fixed 28%).
        # Two passes so every sleeve's position in a crashing symbol is exited
        # on the same day: first determine which symbols qualify, then sell all
        # positions in them. The cooldown only gates FUTURE re-entry, never the
        # sibling exits on the triggering day. Crucially, each armed protection
        # line triggers on ITS OWN price break (no unified peak_drawdown >= 28%
        # gate), so a cost-absolute / ATR / profit-tier stop exits as soon as it
        # is armed and broken — while a clean bull (level 0) keeps only the 28%
        # catastrophe floor and is never cut.
        exit_info: dict[str, tuple[str, float]] = {}
        for state, symbol, strat_name, pos in held:
            if date_pos < self._catastrophe_cooldown.get(symbol, -1):
                continue
            price = prices.get(symbol, 0.0)
            if price <= 0:
                continue
            stop, trigger_type = self._layered_protection(
                state, symbol, pos, date, drawdown
            )
            if stop <= 0:
                continue
            peak_close = max(
                float(getattr(pos, "highest_close_since_entry", 0.0)),
                float(pos.entry_price),
            )
            peak_drop = (peak_close - price) / peak_close if peak_close > 0 else 0.0
            # A position qualifies when price breaks the BINDING armed line.
            if price <= stop:
                if symbol not in exit_info or peak_drop > exit_info[symbol][1]:
                    exit_info[symbol] = (trigger_type, peak_drop)
        for symbol, (trigger_type, peak_drop) in exit_info.items():
            price = prices.get(symbol, 0.0)
            for state, strat_name, pos in (
                (st, sn, p) for st, sy, sn, p in held if sy == symbol
            ):
                self._queue_sell(
                    state, symbol, strat_name, pos.shares, price,
                    date_str, trigger_type,
                    f"drop_from_peak={peak_drop:.1%}",
                )
            self._catastrophe_cooldown[symbol] = date_pos + CATASTROPHE_COOLDOWN_DAYS
            self.events.append({
                "date": date_str, "event": "layered_stop",
                "symbol": symbol, "trigger_type": trigger_type,
                "drop_from_peak": round(peak_drop, 4),
            })

        # 2) Early sector-risk Level 2/3 graded trims of the weakest non-core
        #    holdings (P1-1). Core (highest-scoring) names are preserved, and
        #    the trim only arms once the portfolio is genuinely off its peak.
        if self.enable_early_sector_risk and self._risk_level >= 2:
            self._apply_graded_trim(states, prices, date_str, scoring_fn, drawdown)

        # 2b) Industry-concentration / correlation-cluster guard (report 4.8).
        #     Bull-silent: only trims an over-concentrated sub-industry cluster
        #     when the portfolio is off peak AND currently declining. Additive
        #     to the graded trim above (different trigger: concentration, not
        #     sector shock), gated separately so it never double-trims.
        if self.enable_concentration_guard:
            self._apply_concentration_guard(
                states, prices, date_str, scoring_fn, drawdown, assets
            )

        # 3) Historical structural-shock fast de-risk (opt-in only).
        if self.enable_shock_trim and peak > 0 and assets < peak * (1.0 - self.shock_trim_drawdown):
            if self._is_shock(states, date, prices):
                self._trim_laggards(states, prices, date_str, scoring_fn)

        # 4) Unified risk-action priority (report P1-1). The overlay is a
        #    single authority for its own defensive sells: several mechanisms
        #    above may have queued a sell for the SAME symbol on this day (e.g.
        #    a layered full exit AND a concentration trim). Keeping them all
        #    would double-sell / over-trim and inflate the transaction count.
        #    We therefore collapse every overlay-emitted sell (strategy is
        #    None) per symbol to the single highest-priority action.
        self._consolidate_risk_sells(states, date_str)

    # ── unified risk-action priority (report P1-1) ─────────────────────

    def _consolidate_risk_sells(self, states: list, date_str: str) -> None:
        """Collapse overlay-emitted sells per symbol to the highest-priority action.

        Report P1-1: the overlay must not let several risk mechanisms fire
        independent sells for the same (symbol, strategy) book on the same day.
        Overlay sells are queued with ``strategy=None`` (see ``_queue_sell``),
        so we can isolate them from the sleeves' ordinary strategy signals. For
        each (symbol, strategy) we keep the single most-urgent action (highest
        ``RISK_ACTION_PRIORITY``; ties broken by the larger target first, then
        full exits over partial trims) and drop the rest, recording the
        suppressed actions in the audit trail. Note the key is per
        (symbol, strategy), NOT just per symbol: a full layered exit legitimately
        sells every sleeve's position in the crashing symbol on the same day
        (sibling exits), so those are distinct actions that must all survive;
        only conflicting actions from DIFFERENT mechanisms are collapsed.
        """
        suppressed: list[dict[str, Any]] = []
        winner_by_book: dict[tuple[str, str], tuple[Signal, int]] = {}
        for state in states:
            for signal, strategy in state.pending:
                if strategy is not None or signal.direction != "sell":
                    continue
                book = (str(signal.symbol), str(signal.strategy_name))
                priority = RISK_ACTION_PRIORITY.get(
                    signal.reason.split(":")[0], RISK_ACTION_DEFAULT_PRIORITY
                )
                current = winner_by_book.get(book)
                if current is None or self._risk_action_beats(
                    signal, priority, current[0], current[1]
                ):
                    winner_by_book[book] = (signal, priority)

        # Rebuild each state's pending, keeping only the winning overlay sell
        # per (symbol, strategy) and logging every suppressed action.
        for state in states:
            retained: list[tuple[Signal, object]] = []
            for signal, strategy in state.pending:
                if strategy is not None or signal.direction != "sell":
                    retained.append((signal, strategy))
                    continue
                book = (str(signal.symbol), str(signal.strategy_name))
                winner, _ = winner_by_book[book]
                if signal is winner:
                    retained.append((signal, strategy))
                else:
                    suppressed.append({
                        "date": date_str,
                        "event": "risk_action_suppressed",
                        "symbol": str(signal.symbol),
                        "strategy": str(signal.strategy_name),
                        "reason": signal.reason,
                        "target_shares": int(signal.target_shares),
                        "winner_reason": winner.reason,
                    })
            state.pending = retained
        if suppressed:
            self.events.extend(suppressed)

    @staticmethod
    def _risk_action_beats(
        candidate: Signal, candidate_priority: int,
        current: Signal, current_priority: int,
    ) -> bool:
        """True if ``candidate`` should replace ``current`` as the day's action.

        The higher ``RISK_ACTION_PRIORITY`` wins; on a tie the larger target
        (more conservative) wins; a further tie resolves deterministically by
        reason so the audit is reproducible.
        """
        if candidate_priority != current_priority:
            return candidate_priority > current_priority
        if candidate.target_shares != current.target_shares:
            return candidate.target_shares > current.target_shares
        return candidate.reason < current.reason

    # ── catastrophe-cooldown buy blocking (report P0-4) ──────────────

    def block_cooldown_buys(
        self, states: list, date: pd.Timestamp, date_pos: int
    ) -> None:
        """Block every pending buy for a symbol still in catastrophe cooldown.

        Report P0-4: the cooldown table must not just suppress repeat exits — it
        must form a hard buy admission gate so a symbol that just exited via a
        layered/catastrophe stop cannot be re-entered by ANY trend sleeve until
        the trading-day cooldown expires. This is called at the open of every
        trading day, before buys are authorized and filled, so all three trend
        sleeves are blocked together. The blocking reason is audited per sleeve.
        """
        date_str = date.strftime("%Y-%m-%d")
        for state in states:
            blocked: list[tuple[Any, Any]] = []
            for signal, strategy in state.pending:
                if signal.direction == "buy" and date_pos < self._catastrophe_cooldown.get(
                    str(signal.symbol), -1
                ):
                    sleeve = getattr(state, "sleeve", None)
                    if sleeve is not None and hasattr(sleeve, "_record_order_event"):
                        record = sleeve._record_order_event
                    else:
                        record = None
                    if record is not None:
                        try:
                            record(
                                date=date_str,
                                signal=signal,
                                event="blocked_catastrophe_cooldown",
                                cooldown_days=CATASTROPHE_COOLDOWN_DAYS,
                            )
                        except TypeError:
                            pass
                    self.events.append({
                        "date": date_str,
                        "event": "cooldown_blocked_buy",
                        "symbol": str(signal.symbol),
                        "reason": "catastrophe_cooldown",
                    })
                    continue
                blocked.append((signal, strategy))
            state.pending = blocked

    # ── layered protection stop (P0-1) ────────────────────────────────

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
            giveback = PROFIT_TIER_GIVEBACK[-1][1]
            for gain_threshold, ratio in PROFIT_TIER_GIVEBACK:
                if peak_gain < gain_threshold:
                    giveback = ratio
                    break
            profit_stop = peak_close * (1.0 - giveback)
            # Looseness floor: never tighten the profit-tier stop below this
            # distance from peak (i.e. never trigger on a smaller pull-back than
            # MIN_LAYERED_STOP_PCT). ``min`` keeps the *looser* of the computed
            # giveback line and this guard — the earlier ``max`` forced every
            # winner, including a 300%+ gainer, to exit on a shallow ~14%
            # pull-back, which cut the biggest bull winners (report P0-1
            # ablation "若保护线设置过紧损害牛市收益").
            profit_stop = min(profit_stop, peak_close * (1.0 - MIN_LAYERED_STOP_PCT))

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
            from quant_fusion import Indicators
            atr_series = Indicators.atr(frame, period=20, method="wilder")
            value = float(atr_series.iloc[loc])
            return value if value > 0 else float("nan")
        except (KeyError, IndexError, TypeError, ValueError):
            return float("nan")

    # ── early sector-risk layer (P1-1) ────────────────────────────────

    def _basket_metrics(self, states: list, date: pd.Timestamp) -> dict:
        """Compute multi-evidence metrics from the fixed risk basket."""
        fast_returns: list[float] = []
        declining_3d = 0
        below_ma20 = 0
        observed = 0
        for symbol in RISK_BASKET:
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
            fast_returns.append(fast_return)
            observed += 1
            if fast_return < 0:
                declining_3d += 1
            if loc >= 19:
                ma20 = float(closes.iloc[loc - 19: loc + 1].mean())
                if ma20 > 0 and float(closes.iloc[loc]) < ma20:
                    below_ma20 += 1
        return {
            "observed": observed,
            "fast_returns": fast_returns,
            "declining_3d": declining_3d,
            "below_ma20": below_ma20,
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

    def _update_risk_level(
        self, states: list, date: pd.Timestamp, date_pos: int, held: list,
        drawdown: float,
    ) -> None:
        """Grade the daily early sector-risk level (0/1/2/3).

        This is deliberately LOW-FREQUENCY and graded (report 4.1): a warning
        (Level 1) only *records* state and never trims; a trim only arms at
        Level 2/3 AND once the portfolio is genuinely off its peak. In a clean
        bull the account is almost never deep off peak, so the layer stays
        silent and the golden-metric bull-silent invariant holds. Only records
        an audit event when the level actually changes.
        """
        previous = self._risk_level
        metrics = self._basket_metrics(states, date)
        observed = metrics["observed"]
        sub_stress = self._sub_basket_stress(states, date, held)
        # Portfolio fast-window return (bull-silent guard): a Level 2/3 trim
        # only arms when the portfolio is currently declining, never while it
        # is green/holding its gains on the signal day.
        portfolio_fast_return = self._portfolio_fast_return()
        if observed < RISK_MIN_OBSERVED:
            # Not enough basket evidence -> cannot confirm a structural shock.
            # Fail toward a warning only if held breadth is bad; keep previous
            # otherwise (never jump to a trim without basket evidence).
            held_decline = self._held_decline_breadth(states, date, held)
            self._risk_level = 1 if held_decline >= RISK_HOLD_BREADTH_SHOCK else min(self._risk_level, 1)
        else:
            avg_return = sum(metrics["fast_returns"]) / len(metrics["fast_returns"])
            breadth = metrics["declining_3d"] / observed
            below_ma20_ratio = metrics["below_ma20"] / observed
            held_decline = self._held_decline_breadth(states, date, held)

            # A structural break is signalled by a broad part of the basket
            # falling through its MA20 on top of the fast-return + breadth hit,
            # OR by a structured stress inside one sub-industry basket (report
            # 4.7). The sub-basket leg is additive evidence only — it still
            # requires the same fast-return shock, escalation and portfolio-
            # drawdown gates below, so it never adds a new exit switch.
            structural_break = (
                (below_ma20_ratio >= RISK_BELOW_MA20_SHOCK
                 and breadth >= RISK_BREADTH_SHOCK)
                or sub_stress is not None
            )
            # Escalation ("预警后再次冲击"): a trim must follow a PRIOR warning
            # that has since RECOVERED, then a fresh re-shock. A single
            # continuous shock that first warned must not trim on its own
            # (bull-silent: a normal pull-back never escalates). The re-shock
            # must arrive within the escalation window of that recovered warning.
            escalated = (
                self._recovered_since_warning
                and date_pos - self._last_warning_pos <= RISK_ESCALATION_DAYS
            )

            # Level 3: sustained failure — prior shock plus a new low, a
            # structural break, and the portfolio materially off peak AND
            # currently declining (bull-silent: never trim a recovering book).
            if (self._risk_level >= 2 and avg_return <= RISK_FAST_RETURN_SHOCK
                    and structural_break
                    and drawdown >= RISK_LEVEL3_DRAWDOWN
                    and portfolio_fast_return < 0):
                self._risk_level = 3
            # Level 2: confirmed re-shock — requires a warning that has fully
            # recovered then re-shocked, a structural break, the portfolio off
            # peak AND currently declining (bull-silent guard).
            elif (avg_return <= RISK_FAST_RETURN_SHOCK
                    and structural_break
                    and escalated
                    and drawdown >= RISK_LEVEL2_DRAWDOWN
                    and portfolio_fast_return < 0):
                self._risk_level = 2
            # Level 1: warning only (no trim). Requires real breadth + return
            # deterioration. This arms the escalation counter for Level 2.
            elif (avg_return <= RISK_FAST_RETURN_SHOCK
                    and breadth >= RISK_BREADTH_SHOCK):
                self._risk_level = 1
            else:
                # Recovery: drop back toward 0 one step at a time (never jump
                # to full deployment in a single day).
                self._risk_level = max(self._risk_level - 1, 0)

        # Track the currently stressed sub-industry (used to restrict trims).
        self._stressed_sub = sub_stress if self._risk_level >= 2 else None

        if self._risk_level == 1:
            # Remember when a Level 1 warning fired so a later recover->re-shock
            # can escalate ("预警后再次冲击"). A warning does NOT yet arm a trim.
            self._last_warning_pos = date_pos
            self._recovered_since_warning = False
        elif self._risk_level <= 0 and previous >= 1:
            # The level has fully recovered to normal: a subsequent re-shock
            # within the escalation window is now eligible to trim.
            self._recovered_since_warning = True

        if self._risk_level != previous:
            self.events.append({
                "date": date.strftime("%Y-%m-%d"),
                "event": "sector_risk_level",
                "level": self._risk_level,
                "basket_observed": observed,
                "sub_basket_stress": sub_stress,
                "basket_3d_return": round(
                    sum(metrics["fast_returns"]) / len(metrics["fast_returns"])
                    if metrics["fast_returns"] else 0.0, 4,
                ),
                "basket_breadth": round(
                    metrics["declining_3d"] / observed
                    if observed else 0.0, 4,
                ),
                "basket_below_ma20": round(
                    metrics["below_ma20"] / observed
                    if observed else 0.0, 4,
                ),
                "held_decline_breadth": round(
                    self._held_decline_breadth(states, date, held), 4,
                ),
            })

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

    def _apply_graded_trim(
        self, states: list, prices: dict[str, float], date_str: str,
        scoring_fn, drawdown: float,
    ) -> None:
        """Trim weakest non-core holdings at risk Level 2/3 (P1-1).

        Level 2 requires ``drawdown >= RISK_LEVEL2_DRAWDOWN`` and Level 3
        requires ``drawdown >= RISK_LEVEL3_DRAWDOWN``; if the portfolio is not
        genuinely off its peak, no trim happens (bull-silent). Only the
        weakest non-core names are trimmed, preserving the strongest name.
        """
        if self._risk_level >= 3 and drawdown < RISK_LEVEL3_DRAWDOWN:
            return
        if self._risk_level == 2 and drawdown < RISK_LEVEL2_DRAWDOWN:
            return
        held = self._held_positions(states)
        shares_by_symbol: dict[str, int] = {}
        strats: dict[str, list[tuple]] = {}
        for state, symbol, strat_name, pos in held:
            shares_by_symbol[symbol] = shares_by_symbol.get(symbol, 0) + pos.shares
            strats.setdefault(symbol, []).append((state, strat_name, pos))
        if not shares_by_symbol:
            return
        trim_count = 1 if self._risk_level == 2 else 2
        # Bull-silent bear or relevance guard (report 4.7/4.8): a graded trim
        # only targets holdings that belong to the SAME sub-industry that is
        # currently under structured stress. If no sub-basket is stressed
        # (e.g. a broad total-basket break only), the trim falls back to the
        # weakest named positions. This prevents an equipment-only stress from
        # ever cutting an optical winner the user holds.
        eligible = [
            sym for sym in shares_by_symbol
            if self._stressed_sub is None
            or SYMBOL_SUB_INDUSTRY.get(sym) == self._stressed_sub
        ]
        if len(eligible) <= trim_count:
            return
        ranked = sorted(
            eligible,
            key=lambda sym: (scoring_fn(sym) if scoring_fn else 0.0, sym),
        )
        # Report 4.1: "减少最弱的非核心仓" and "保留最强1-2只或现金". Only the
        # single weakest name is trimmed at Level 2; at Level 3 the two weakest
        # are trimmed. Core (strongest) names are always preserved.
        trims = ranked[: trim_count]
        ratio = RISK_LEVEL3_TRIM_RATIO if self._risk_level >= 3 else RISK_LEVEL2_TRIM_RATIO
        for weak in trims:
            trim_shares = int(shares_by_symbol[weak] * ratio)
            if trim_shares <= 0:
                continue
            trimmed = 0
            price = prices.get(weak, 0.0)
            for state, strat_name, pos in strats[weak]:
                if trimmed >= trim_shares:
                    break
                take = max(0, min(pos.shares, trim_shares - trimmed))
                if take <= 0:
                    continue
                self._queue_sell(
                    state, weak, strat_name, take, price, date_str,
                    "sector_risk_trim",
                    f"level={self._risk_level}",
                )
                trimmed += take
            if trimmed > 0:
                self.events.append({
                    "date": date_str, "event": "sector_risk_trim",
                    "symbol": weak, "shares": trimmed,
                    "level": self._risk_level,
                })

    def _apply_concentration_guard(
        self, states: list, prices: dict[str, float], date_str: str,
        scoring_fn, drawdown: float, assets: float,
    ) -> None:
        """Trim an over-concentrated sub-industry cluster (report 4.8 / P1-5).

        Bull-silent by design: it only acts when (a) one sub-industry cluster
        accounts for more than ``CONCENTRATION_CAP`` of the book, (b) the
        portfolio is at least ``CONCENTRATION_DRAWDOWN`` off its peak, and
        (c) the portfolio is currently declining. When all three hold, only the
        weakest name *inside that same cluster* is trimmed by just enough to
        bring the cluster back under the cap, capped at
        ``CONCENTRATION_MAX_TRIM_RATIO`` of that name. This reduces same-sector
        synchronous losses without ever cutting a leader in a clean bull.

        Report P1-5 — concentration is computed on the REAL account net
        exposure, not on per-sleeve books:
          - each symbol is counted exactly once (positions are aggregated by
            symbol into ``value_by_symbol``, so the same name held across
            multiple sleeves is never double-counted);
          - the industry weight uses the account market value (``value /
            assets``) and ``assets`` is the full account including cash, so
            cash enters the denominator;
          - a held symbol that cannot be mapped to a sub-industry makes the
            cluster-coverage INCOMPLETE, so the guard FAILS CLOSED (no trim)
            instead of trimming on a partial picture. The skip is audited.
        """
        if drawdown < CONCENTRATION_DRAWDOWN:
            return
        if self._portfolio_fast_return() >= 0:
            return
        held = self._held_positions(states)
        if not held:
            return
        # Market value of each held symbol, then aggregate by sub-industry.
        value_by_symbol: dict[str, float] = {}
        strats: dict[str, list[tuple]] = {}
        for state, symbol, strat_name, pos in held:
            price = prices.get(symbol, 0.0)
            if price <= 0:
                continue
            value_by_symbol[symbol] = value_by_symbol.get(symbol, 0.0) + pos.shares * price
            strats.setdefault(symbol, []).append((state, strat_name, pos))
        if not value_by_symbol or assets <= 0:
            return
        # P1-5: a held symbol we cannot map to any sub-industry makes the
        # cluster coverage incomplete. Fail closed (do not trim) when that
        # unmapped weight is MATERIAL, so we never trim on a partial / wrong
        # concentration picture; a negligible unmapped tail is ignored. The
        # skip is audited.
        unmapped = sorted(
            symbol for symbol in value_by_symbol
            if SYMBOL_SUB_INDUSTRY.get(symbol) not in RISK_SUB_BASKETS
        )
        unmapped_value = sum(value_by_symbol[s] for s in unmapped)
        if unmapped and (unmapped_value / assets) >= CONCENTRATION_UNMAPPED_LIMIT:
            self.events.append({
                "date": date_str, "event": "concentration_guard_fail_closed",
                "unmapped_symbols": sorted(unmapped),
                "unmapped_weight": round(unmapped_value / assets, 4),
                "reason": "incomplete_sub_industry_coverage",
            })
            return
        cluster_value: dict[str, tuple[float, list[str]]] = {}
        for symbol, value in value_by_symbol.items():
            cluster = SYMBOL_SUB_INDUSTRY.get(symbol)
            cur, members = cluster_value.get(cluster, (0.0, []))
            cluster_value[cluster] = (cur + value, members + [symbol])
        # Find the most over-concentrated multi-name cluster.
        worst_cluster: str | None = None
        worst_weight = 0.0
        for cluster, (value, members) in cluster_value.items():
            if len(members) < CONCENTRATION_MIN_CLUSTER:
                continue
            weight = value / assets
            if weight > worst_weight:
                worst_weight = weight
                worst_cluster = cluster
        if worst_cluster is None or worst_weight <= CONCENTRATION_CAP:
            return
        members = cluster_value[worst_cluster][1]
        # Only trim the weakest name inside the over-concentrated cluster.
        ranked = sorted(
            members,
            key=lambda sym: (scoring_fn(sym) if scoring_fn else 0.0, sym),
        )
        weak = ranked[0]
        excess_value = cluster_value[worst_cluster][0] - CONCENTRATION_CAP * assets
        if excess_value <= 0:
            return
        weak_value = value_by_symbol.get(weak, 0.0)
        if weak_value <= 0:
            return
        trim_value = min(excess_value, weak_value * CONCENTRATION_MAX_TRIM_RATIO)
        price = prices.get(weak, 0.0)
        if price <= 0:
            return
        trim_shares = int(trim_value / price)
        from quant_fusion import _floor_to_lot
        trim_shares = _floor_to_lot(trim_shares)
        if trim_shares <= 0:
            return
        trimmed = 0
        for state, strat_name, pos in strats[weak]:
            if trimmed >= trim_shares:
                break
            take = max(0, min(pos.shares, trim_shares - trimmed))
            if take <= 0:
                continue
            self._queue_sell(
                state, weak, strat_name, take, price, date_str,
                "concentration_trim",
                f"cluster={worst_cluster}",
            )
            trimmed += take
        if trimmed > 0:
            self.events.append({
                "date": date_str, "event": "concentration_trim",
                "symbol": weak, "shares": trimmed,
                "cluster": worst_cluster,
                "cluster_weight": round(worst_weight, 4),
            })

    def _queue_sell(
        self, state, symbol: str, strat_name: str, shares: int,
        price: float, date_str: str, reason: str, extra: str = "",
    ) -> None:
        if shares <= 0 or price <= 0:
            return
        sig = _make_sell_signal(symbol, strat_name, shares, price, date_str,
                                reason, extra)
        state.pending.append((sig, None))

    # ── historical structural-shock detection (opt-in) ────────────────

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

    def _trim_laggards(self, states: list, prices: dict[str, float],
                       date_str: str, scoring_fn) -> None:
        held = self._held_positions(states)
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
