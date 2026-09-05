"""Daily risk state, admission gates, priority, and escalation policy."""

from __future__ import annotations

# pyright: reportAttributeAccessIssue=false

# ruff: noqa: F401

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
)
from quantfusion.risk.overlay.models import RiskAction


class OverlayPolicyMixin:
    """Daily risk state, admission gates, priority, and escalation policy."""

    def __init__(
        self,
        catastrophe_stop_pct: float = CATASTROPHE_STOP_PCT,
        shock_trim_drawdown: float = SHOCK_TRIM_DRAWDOWN,
        shock_trim_ratio: float = SHOCK_TRIM_RATIO,
        enable_shock_trim: bool = False,
        *,
        enable_early_sector_risk: bool = True,
        risk_frames: dict[str, pd.DataFrame] | None = None,
        enable_trend_health: bool = True,
        continuous_confirm_days: int = RISK_CONTINUOUS_CONFIRM_DAYS,
        level2_drawdown: float = RISK_LEVEL2_DRAWDOWN,
        level3_drawdown: float = RISK_LEVEL3_DRAWDOWN,
        severe_direct_return: float = RISK_SEVERE_DIRECT_RETURN,
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
        self.risk_frames = dict(risk_frames or {})
        self.enable_trend_health = bool(enable_trend_health)
        self.continuous_confirm_days = max(int(continuous_confirm_days), 2)
        self.level2_drawdown = float(level2_drawdown)
        self.level3_drawdown = float(level3_drawdown)
        self.severe_direct_return = float(severe_direct_return)
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
        self._continuous_stress_days = 0
        self._transition_trim_active = False
        # The production outer router owns execution while it is already in a
        # defensive weak/cash route. The overlay keeps measuring and persisting
        # market risk there, but must not stack a second set of sells or entry
        # bans on top of the dedicated weak-market strategy.
        self._outer_defensive_mode = False
        self._outer_route: str | None = None
        # 2026-08-16 报告 P1-2: latest basket coverage measurement (governance audits).
        self._last_metrics: dict[str, Any] = {}
        self._last_metrics_date: pd.Timestamp | None = None
        self.events: list[dict[str, Any]] = []

    @property
    def blocks_new_positions(self) -> bool:
        """Whether confirmed market risk must reject every new symbol entry."""
        return self.enable_early_sector_risk and self._risk_level >= 2

    @property
    def risk_level(self) -> int:
        """Expose the current validated integer risk level for result audits."""
        return int(self._risk_level)

    def state_snapshot(self) -> dict[str, Any]:
        """Return the persistent warning, cooldown, and escalation state."""
        return {
            "risk_level": int(self._risk_level),
            "continuous_stress_days": int(self._continuous_stress_days),
            "last_warning_position": int(self._last_warning_pos),
            "recovered_since_warning": bool(self._recovered_since_warning),
            "stressed_subindustry": self._stressed_sub,
            "catastrophe_cooldowns": dict(self._catastrophe_cooldown),
            "transition_trim_active": bool(self._transition_trim_active),
            "outer_route": self._outer_route,
            "execution_owner": (
                "production_route" if self._outer_defensive_mode else "overlay"
            ),
        }

    def has_active_catastrophe_cooldown(self, date_pos: int) -> bool:
        """Whether any symbol is still inside its catastrophe cooldown window."""
        return any(expiry > date_pos for expiry in self._catastrophe_cooldown.values())

    def set_outer_route(self, route: str | None, date: pd.Timestamp) -> None:
        """Select one risk-execution owner without discarding overlay evidence."""
        defensive = route in {"weak", "cash", "transition_to_trend"}
        if defensive != self._outer_defensive_mode:
            self.events.append(
                {
                    "date": date.strftime("%Y-%m-%d"),
                    "event": "risk_execution_owner_changed",
                    "owner": "production_route" if defensive else "overlay",
                    "route": route,
                }
            )
        self._outer_route = route
        self._outer_defensive_mode = defensive

    @property
    def blocks_pyramiding(self) -> bool:
        """Whether warning-or-worse market risk must reject additions to holdings."""
        return self.enable_early_sector_risk and self._risk_level >= 1


    def evaluate(
        self,
        states: list,
        date: pd.Timestamp,
        date_pos: int,
        assets: float,
        peak: float,
        scoring_fn,
    ) -> tuple[RiskAction, ...]:
        """Evaluate one day and return ordered immutable defensive actions.

        ``scoring_fn`` maps a symbol to an allocation score (lower = weaker),
        used to rank which names get trimmed first on a structural shock.
        """
        actions: list[RiskAction] = []
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

        if getattr(self, "_c6_diagnostic_evidence_enabled", False):
            observed = self.observe_c6_s_evidence(
                states, date, date_pos, prices, assets, drawdown, scoring_fn
            )
            previous = getattr(self, "c6_s_evidence", None)
            if previous is None:
                self.c6_s_evidence = observed
            elif (
                previous["first_early_sell_required_close"] is None
                and observed["first_early_sell_required_close"] is not None
            ):
                first_causal = (
                    previous["first_causal_stressed_cluster_close"]
                    or observed["first_causal_stressed_cluster_close"]
                )
                self.c6_s_evidence = observed
                self.c6_s_evidence[
                    "first_causal_stressed_cluster_close"
                ] = first_causal
            elif (
                previous["first_causal_stressed_cluster_close"] is None
                and observed["first_causal_stressed_cluster_close"] is not None
            ):
                previous["first_causal_stressed_cluster_close"] = observed[
                    "first_causal_stressed_cluster_close"
                ]

        # In a dedicated weak/cash route the same persistent account is already
        # being managed by the outer defensive strategy. Keep the cross-market
        # state hot for recovery and re-shock decisions, but do not duplicate
        # its stops, trims, cooldowns, or buy bans.
        if self._outer_defensive_mode:
            return ()

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
                action = self._sell_action(
                    states, state, symbol, strat_name, pos.shares, price,
                    date_str, trigger_type,
                    f"drop_from_peak={peak_drop:.1%}",
                )
                if action is not None:
                    actions.append(action)
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
            actions.extend(
                self._apply_graded_trim(
                    states, prices, date_str, scoring_fn, drawdown
                )
            )

        # Sustained internal TRANSITION plus an external Level-1 warning is the
        # only path that trims during transition. This avoids globally reducing
        # healthy bull exposure while still cutting the weakest name by 10% once
        # both independent signals agree and the account is genuinely declining.
        transition_confirmed = any(
            getattr(state.sleeve, "_regime_state", "TREND") == "TRANSITION"
            and int(getattr(state.sleeve, "_regime_transition_days", 0)) >= 5
            for state in states
        )
        if (
            self._risk_level >= 1
            and transition_confirmed
            and not self._transition_trim_active
            and drawdown >= 0.05
            and self._portfolio_fast_return() < 0
        ):
            actions.extend(
                self._apply_transition_trim(
                    states, prices, date_str, scoring_fn, ratio=0.10
                )
            )
            self._transition_trim_active = True
        elif self._risk_level == 0 or not transition_confirmed:
            self._transition_trim_active = False

        # 2b) Industry-concentration / correlation-cluster guard (report 4.8).
        #     Bull-silent: only trims an over-concentrated sub-industry cluster
        #     when the portfolio is off peak AND currently declining. Additive
        #     to the graded trim above (different trigger: concentration, not
        #     sector shock), gated separately so it never double-trims.
        if self.enable_concentration_guard:
            actions.extend(
                self._apply_concentration_guard(
                    states, prices, date_str, scoring_fn, drawdown, assets
                )
            )

        # 3) Structural-shock fast de-risk (opt-in only).
        if self.enable_shock_trim and peak > 0 and assets < peak * (1.0 - self.shock_trim_drawdown):
            if self._is_shock(states, date, prices):
                actions.extend(
                    self._trim_laggards(states, prices, date_str, scoring_fn)
                )

        return tuple(actions)


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
        # 2026-08-16 报告 P1-2: keep the latest basket coverage measurement
        # for governance audits (risk confidence) without altering any decision below.
        self._last_metrics = dict(metrics)
        self._last_metrics_date = date
        observed = metrics["observed"]
        observed_industries = metrics["observed_industries"]
        sub_stress = self._sub_basket_stress(states, date, held)
        # Portfolio fast-window return (bull-silent guard): a Level 2/3 trim
        # only arms when the portfolio is currently declining, never while it
        # is green/holding its gains on the signal day.
        portfolio_fast_return = self._portfolio_fast_return()
        if observed < RISK_MIN_OBSERVED or observed_industries < 3:
            # Not enough basket evidence -> cannot confirm a structural shock.
            # Fail toward a warning only if held breadth is bad; keep previous
            # otherwise (never jump to a trim without basket evidence).
            held_decline = self._held_decline_breadth(states, date, held)
            self._risk_level = 1 if held_decline >= RISK_HOLD_BREADTH_SHOCK else min(self._risk_level, 1)
        else:
            avg_return = sum(metrics["fast_returns"]) / len(metrics["fast_returns"])
            breadth = float(metrics["declining_ratio"])
            below_ma20_ratio = float(metrics["below_ma20_ratio"])
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
            stressed = (
                avg_return <= RISK_FAST_RETURN_SHOCK
                and breadth >= RISK_BREADTH_SHOCK
                and structural_break
            )
            self._continuous_stress_days = (
                self._continuous_stress_days + 1 if stressed else 0
            )
            continuous_escalation = (
                previous >= 1
                and self._continuous_stress_days >= self.continuous_confirm_days
            )
            severe_direct = (
                avg_return <= self.severe_direct_return
                and breadth >= RISK_SEVERE_DIRECT_BREADTH
                and below_ma20_ratio >= RISK_BELOW_MA20_SHOCK
                and held_decline >= RISK_HOLD_BREADTH_SHOCK
            )

            # Level 3: sustained failure — prior shock plus a new low, a
            # structural break, and the portfolio materially off peak AND
            # currently declining (bull-silent: never trim a recovering book).
            if (self._risk_level >= 2 and avg_return <= RISK_FAST_RETURN_SHOCK
                    and structural_break
                    and drawdown >= self.level3_drawdown
                    and portfolio_fast_return < 0):
                self._risk_level = 3
            # Level 2: confirmed re-shock — requires a warning that has fully
            # recovered then re-shocked, a structural break, the portfolio off
            # peak AND currently declining (bull-silent guard).
            elif (avg_return <= RISK_FAST_RETURN_SHOCK
                    and structural_break
                    and (escalated or continuous_escalation or severe_direct)
                    and drawdown >= self.level2_drawdown
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
                self._continuous_stress_days = 0

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
                "basket_observed_industries": observed_industries,
                "continuous_stress_days": int(self._continuous_stress_days),
                "sub_basket_stress": sub_stress,
                "basket_3d_return": round(
                    sum(metrics["fast_returns"]) / len(metrics["fast_returns"])
                    if metrics["fast_returns"] else 0.0, 4,
                ),
                "basket_breadth": round(
                    float(metrics["declining_ratio"]), 4,
                ),
                "basket_below_ma20": round(
                    float(metrics["below_ma20_ratio"]), 4,
                ),
                "held_decline_breadth": round(
                    self._held_decline_breadth(states, date, held), 4,
                ),
            })
