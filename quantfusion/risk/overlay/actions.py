"""Prioritized graded trims and the action-to-pending boundary."""

from __future__ import annotations

# pyright: reportAttributeAccessIssue=false

# ruff: noqa: F401

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
from quantfusion.domain.rules import floor_to_lot
from quantfusion.risk.overlay.models import RiskAction


class OverlayActionMixin:
    """Prioritized graded trims and the action-to-pending boundary."""

    def _apply_graded_trim(
        self, states: list, prices: dict[str, float], date_str: str,
        scoring_fn, drawdown: float,
    ) -> tuple[RiskAction, ...]:
        """Trim weakest non-core holdings at risk Level 2/3 (P1-1).

        Level 2 requires ``drawdown >= RISK_LEVEL2_DRAWDOWN`` and Level 3
        requires ``drawdown >= RISK_LEVEL3_DRAWDOWN``; if the portfolio is not
        genuinely off its peak, no trim happens (bull-silent). Only the
        weakest non-core names are trimmed, preserving the strongest name.
        """
        actions: list[RiskAction] = []
        if self._risk_level >= 3 and drawdown < self.level3_drawdown:
            return ()
        if self._risk_level == 2 and drawdown < self.level2_drawdown:
            return ()
        held = self._held_positions(states)
        shares_by_symbol: dict[str, int] = {}
        strats: dict[str, list[tuple]] = {}
        for state, symbol, strat_name, pos in held:
            shares_by_symbol[symbol] = shares_by_symbol.get(symbol, 0) + pos.shares
            strats.setdefault(symbol, []).append((state, strat_name, pos))
        if not shares_by_symbol:
            return ()
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
            return ()
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
                action = self._sell_action(
                    states, state, weak, strat_name, take, price, date_str,
                    "sector_risk_trim", f"level={self._risk_level}",
                )
                if action is not None:
                    actions.append(action)
                trimmed += take
            if trimmed > 0:
                self.events.append({
                    "date": date_str, "event": "sector_risk_trim",
                    "symbol": weak, "shares": trimmed,
                    "level": self._risk_level,
                })
        return tuple(actions)

    def _apply_transition_trim(
        self,
        states: list,
        prices: dict[str, float],
        date_str: str,
        scoring_fn,
        *,
        ratio: float,
    ) -> tuple[RiskAction, ...]:
        """Trim only the weakest symbol after sustained dual-signal transition."""
        actions: list[RiskAction] = []
        held = self._held_positions(states)
        shares_by_symbol: dict[str, int] = {}
        books: dict[str, list[tuple]] = {}
        for state, symbol, strategy_name, position in held:
            shares_by_symbol[symbol] = (
                shares_by_symbol.get(symbol, 0) + position.shares
            )
            books.setdefault(symbol, []).append(
                (state, strategy_name, position)
            )
        if len(shares_by_symbol) < 2:
            return ()
        weakest = min(
            shares_by_symbol,
            key=lambda symbol: (
                scoring_fn(symbol) if scoring_fn else 0.0,
                symbol,
            ),
        )
        target = int(shares_by_symbol[weakest] * ratio)
        trimmed = 0
        for state, strategy_name, position in books[weakest]:
            take = min(position.shares, max(target - trimmed, 0))
            if take <= 0:
                continue
            action = self._sell_action(
                states,
                state,
                weakest,
                strategy_name,
                take,
                prices.get(weakest, 0.0),
                date_str,
                "sector_risk_trim",
                "sustained_transition_level1",
            )
            if action is not None:
                actions.append(action)
            trimmed += take
        if trimmed > 0:
            self.events.append(
                {
                    "date": date_str,
                    "event": "transition_risk_trim",
                    "symbol": weakest,
                    "shares": trimmed,
                    "ratio": ratio,
                }
            )
        return tuple(actions)

    def _apply_concentration_guard(
        self, states: list, prices: dict[str, float], date_str: str,
        scoring_fn, drawdown: float, assets: float, *,
        early_s_evidence: bool = False,
    ) -> tuple[RiskAction, ...]:
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
        actions: list[RiskAction] = []
        if drawdown < CONCENTRATION_DRAWDOWN and not early_s_evidence:
            return ()
        if self._portfolio_fast_return() >= 0:
            return ()
        held = self._held_positions(states)
        if not held:
            return ()
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
            return ()
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
            return ()
        cluster_value: dict[str | None, tuple[float, list[str]]] = {}
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
            return ()
        members = cluster_value[worst_cluster][1]
        # Only trim the weakest name inside the over-concentrated cluster.
        ranked = sorted(
            members,
            key=lambda sym: (scoring_fn(sym) if scoring_fn else 0.0, sym),
        )
        weak = ranked[0]
        excess_value = cluster_value[worst_cluster][0] - CONCENTRATION_CAP * assets
        if excess_value <= 0:
            return ()
        weak_value = value_by_symbol.get(weak, 0.0)
        if weak_value <= 0:
            return ()
        trim_value = min(excess_value, weak_value * CONCENTRATION_MAX_TRIM_RATIO)
        price = prices.get(weak, 0.0)
        if price <= 0:
            return ()
        trim_shares = int(trim_value / price)
        trim_shares = floor_to_lot(trim_shares)
        if trim_shares <= 0:
            return ()
        trimmed = 0
        for state, strat_name, pos in strats[weak]:
            if trimmed >= trim_shares:
                break
            take = max(0, min(pos.shares, trim_shares - trimmed))
            if take <= 0:
                continue
            action = self._sell_action(
                states, state, weak, strat_name, take, price, date_str,
                "concentration_trim", f"cluster={worst_cluster}",
            )
            if action is not None:
                actions.append(action)
            trimmed += take
        if trimmed > 0:
            self.events.append({
                "date": date_str, "event": "concentration_trim",
                "symbol": weak, "shares": trimmed,
                "cluster": worst_cluster,
                "cluster_weight": round(worst_weight, 4),
            })
        return tuple(actions)

    @staticmethod
    def _sell_action(
        states: list, state, symbol: str, strat_name: str, shares: int,
        price: float, date_str: str, reason: str, extra: str = "",
    ) -> RiskAction | None:
        if shares <= 0 or price <= 0:
            return None
        state_index = next(
            index for index, candidate in enumerate(states) if candidate is state
        )
        return RiskAction(
            symbol=symbol,
            strategy_name=strat_name,
            shares=shares,
            price=price,
            signal_date=date_str,
            reason=reason,
            extra=extra,
            priority=RISK_ACTION_PRIORITY.get(
                reason, RISK_ACTION_DEFAULT_PRIORITY
            ),
            state_index=state_index,
        )

    def _trim_laggards(self, states: list, prices: dict[str, float],
                       date_str: str, scoring_fn) -> tuple[RiskAction, ...]:
        actions: list[RiskAction] = []
        held = self._held_positions(states)
        shares_by_symbol: dict[str, int] = {}
        strats: dict[str, list[tuple]] = {}
        for state, symbol, strat_name, pos in held:
            shares_by_symbol[symbol] = shares_by_symbol.get(symbol, 0) + pos.shares
            strats.setdefault(symbol, []).append((state, strat_name, pos))
        if not shares_by_symbol:
            return ()
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
            action = self._sell_action(
                states, state, weak, strat_name, take, price, date_str,
                "shock_trim", "structural_shock_de_risk",
            )
            if action is not None:
                actions.append(action)
            trimmed += take
        if trimmed > 0:
            self.events.append({
                "date": date_str, "event": "shock_trim",
                "symbol": weak, "shares": trimmed,
            })
        return tuple(actions)
