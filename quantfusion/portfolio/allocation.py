"""Portfolio admission, cash allocation, and cross-sleeve netting."""

from __future__ import annotations

# pyright: reportAttributeAccessIssue=false

import math
from dataclasses import replace
from typing import Any

import numpy as np
import pandas as pd

from quantfusion.config.universe import ESTABLISHED_EXPANSION_CORE
from quantfusion.domain.models import Signal
from quantfusion.execution.priorities import EXECUTION_PRIORITY

_ESTABLISHED_EXPANSION_CORE = ESTABLISHED_EXPANSION_CORE


class PortfolioAllocationMixin:
    """Own portfolio-wide admission, idle cash, and net exposure rules."""

    @staticmethod
    def _held_portfolio_symbols(states: list[Any]) -> set[str]:
        """Return the distinct symbols held by any virtual subaccount."""
        return {
            symbol
            for state in states
            for symbol, positions in state.sleeve.positions.items()
            if positions
        }

    def _current_position_limit(
        self, states: list[Any], external_risk_level: int = 0
    ) -> int:
        """Return the causal three-to-six position limit for the current regime."""
        hard_limit = int(self.cfg["max_positions"])
        if (
            not bool(self.cfg.get("adaptive_max_positions", True))
            or not states
            or external_risk_level < 1
        ):
            return hard_limit
        regime = str(getattr(states[0].sleeve, "_regime_state", "TREND"))
        if regime == "CHOPPY":
            return min(hard_limit, int(self.cfg.get("choppy_max_positions", 3)))
        if regime == "TRANSITION":
            return min(
                hard_limit, int(self.cfg.get("transition_max_positions", 4))
            )
        return hard_limit

    def _rebalance_free_sleeve_cash(
        self, states: list[Any], date: pd.Timestamp
    ) -> None:
        """Shift idle cash without merging positions, strategies, or pending orders."""
        if (
            not bool(self.cfg.get("dynamic_sleeve_weights", True))
            or len(states) != 3
        ):
            return
        regime = str(getattr(states[0].sleeve, "_regime_state", "TREND"))
        if self._last_sleeve_weight_regime is None:
            self._last_sleeve_weight_regime = regime
            return
        if regime == self._last_sleeve_weight_regime:
            return
        self._last_sleeve_weight_regime = regime
        prefix = regime.lower() if regime in {"TRANSITION", "CHOPPY"} else None
        weights = (
            [1.0 / 3.0] * 3
            if prefix is None
            else [
                float(self.cfg[f"{prefix}_{name}_weight"])
                for name in ("fast", "base", "slow")
            ]
        )
        total_cash = sum(float(state.sleeve.cash) for state in states)
        if total_cash <= 0:
            return
        before = [float(state.sleeve.cash) for state in states]
        targets = [total_cash * weight for weight in weights]
        targets[-1] = total_cash - sum(targets[:-1])
        if all(
            math.isclose(old, new, rel_tol=0.0, abs_tol=0.01)
            for old, new in zip(before, targets, strict=True)
        ):
            return
        for state, old, target in zip(states, before, targets, strict=True):
            state.sleeve.cash = target
            cash_flow = target - old
            risk = state.sleeve.risk
            for attribute in (
                "peak_assets",
                "lifetime_peak_assets",
                "daily_start_assets",
            ):
                if hasattr(risk, attribute):
                    adjusted = max(
                        0.0, float(getattr(risk, attribute, 0.0)) + cash_flow
                    )
                    setattr(risk, attribute, adjusted)
        self._sleeve_weight_events.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "event": "free_cash_sleeve_reweight",
                "regime": regime,
                "weights": dict(
                    zip(("fast", "base", "slow"), weights, strict=True)
                ),
                "cash_before": before,
                "cash_after": targets,
            }
        )

    @staticmethod
    def _overlay_allocation_score(states: list[Any], date: pd.Timestamp):
        """Mean allocation score across sleeves, used to rank laggards for trim."""
        def _score(symbol: str) -> float:
            samples = []
            for state in states:
                try:
                    scores = state.sleeve._allocation_scores(state.data_map, date)
                except Exception:
                    scores = {}
                samples.append(float(scores.get(symbol, 0.0)))
            return float(np.mean(samples)) if samples else 0.0
        return _score

    def _authorize_portfolio_buys(
        self,
        states: list[Any],
        date: pd.Timestamp,
        external_risk_level: int = 0,
    ) -> None:
        """Admit symbols by the mean of comparable percentile ranks (Borda score)."""
        held = self._held_portfolio_symbols(states)
        hard_limit = int(self.cfg["max_positions"])
        maximum = self._current_position_limit(states, external_risk_level)
        if len(held) > hard_limit:
            raise RuntimeError("portfolio symbol limit was already exceeded")
        candidate_symbols: set[str] = set()
        for state in states:
            candidates = {
                signal.symbol
                for signal, _ in state.pending
                if signal.direction == "buy"
                and signal.symbol not in held
                and signal.symbol in state.data_map
                and date in state.data_map[signal.symbol].index
            }
            candidate_symbols.update(candidates)
        score_samples = {
            symbol: [] for symbol in candidate_symbols
        }
        for state in states:
            scores = state.sleeve._allocation_scores(state.data_map, date)
            candidates = {
                signal.symbol
                for signal, _ in state.pending
                if signal.direction == "buy"
                and signal.symbol not in held
                and signal.symbol in state.data_map
                and date in state.data_map[signal.symbol].index
            }
            for symbol in candidates:
                score_samples[symbol].append(scores.get(symbol, 0.0))
        date_str = date.strftime("%Y-%m-%d")
        route_migrations = {
            signal.symbol
            for state in states
            for signal, strategy in state.pending
            if signal.direction == "buy"
            and signal.symbol not in held
            and getattr(strategy, "name", "") == "positive_momentum_hold"
        }
        admission_scores = {
            symbol: float(np.mean(samples))
            for symbol, samples in score_samples.items()
        }
        # A six-to-twelve-name expansion keeps the fixed five-name production
        # basket on its established path; only additional names must earn
        # new-candidate evidence.  Reclassifying the same core as "new" at
        # seven names creates an artificial 6 -> 7 discontinuity.
        reference_core = set(self.policy.regime_symbols)
        tradable_symbols = (
            set(states[0].sleeve._tradable_symbol_codes) if states else set()
        )
        fixed_core = (
            reference_core
            if 6 <= self._runtime_tradable_count <= 12
            and reference_core.issubset(tradable_symbols)
            else set()
        )
        if self._runtime_tradable_count >= 6:
            score_eligible = fixed_core | {
                symbol
                for symbol, score in admission_scores.items()
                if score >= 0.50
            }
        else:
            score_eligible = set(admission_scores)

        # Expanded pools are sensitive to a single noisy add-one candidate.
        # Preserve the five-name core through 6-12 names. Once the established
        # 13-name production pool is present, preserve its existing admission
        # path too, while requiring only symbols outside it to sustain four
        # executable intent days. Interrupted evidence resets. Existing
        # holdings and outer-route migrations bypass this new-entry gate.
        established_expansion = (
            self._runtime_tradable_count == 14
            and _ESTABLISHED_EXPANSION_CORE.issubset(tradable_symbols)
        )
        confirmation_core = (
            _ESTABLISHED_EXPANSION_CORE if established_expansion else fixed_core
        )
        confirmation_required = (
            6 <= self._runtime_tradable_count <= 12 or established_expansion
        )
        if confirmation_required:
            required_confirmation_days = (
                2
                if established_expansion
                else (4 if self._runtime_tradable_count >= 9 else 2)
            )
            current_intent = (
                score_eligible & set(score_samples)
            ) - confirmation_core
            if established_expansion:
                expansion_min_score = float(
                    self.cfg.get("established_expansion_min_score", 0.80)
                )
                current_intent = {
                    symbol
                    for symbol in current_intent
                    if admission_scores.get(symbol, 0.0) >= expansion_min_score
                }
            previous = self._new_candidate_intent_streak
            self._new_candidate_intent_streak = {
                symbol: previous.get(symbol, 0) + 1
                for symbol in current_intent
            }
            confirmation_eligible = confirmation_core | {
                symbol
                for symbol, streak in self._new_candidate_intent_streak.items()
                if streak >= required_confirmation_days
            }
        else:
            required_confirmation_days = 1
            self._new_candidate_intent_streak = {}
            confirmation_eligible = set(score_samples)

        eligible_new = set(score_samples) & score_eligible & confirmation_eligible
        ranked = sorted(
            eligible_new,
            key=lambda symbol: (
                -admission_scores[symbol],
                EXECUTION_PRIORITY.get(symbol, 9999),
                symbol,
            ),
        )
        migration_capacity = max(maximum - len(held), 0)
        admitted_migrations = set(
            sorted(
                route_migrations,
                key=lambda symbol: (EXECUTION_PRIORITY.get(symbol, 9999), symbol),
            )[:migration_capacity]
        )
        candidate_capacity = max(
            maximum - len(held) - len(admitted_migrations), 0
        )
        allowed = held | admitted_migrations | set(ranked[:candidate_capacity])
        for state in states:
            retained: list[tuple[Signal, Any]] = []
            for signal, strategy in state.pending:
                if signal.direction == "buy" and signal.symbol not in allowed:
                    if signal.symbol in route_migrations:
                        event = "rejected_portfolio_symbol_limit"
                    elif (
                        signal.symbol in score_samples
                        and signal.symbol not in score_eligible
                    ):
                        event = "rejected_new_candidate_allocation_score"
                    elif (
                        confirmation_required
                        and signal.symbol in score_samples
                        and signal.symbol not in confirmation_eligible
                    ):
                        event = "rejected_new_candidate_confirmation"
                    else:
                        event = "rejected_portfolio_symbol_limit"
                    state.sleeve._record_order_event(
                        date=date_str,
                        signal=signal,
                        event=event,
                        portfolio_max_positions=maximum,
                        allocation_score=admission_scores.get(signal.symbol),
                        confirmation_days=self._new_candidate_intent_streak.get(
                            signal.symbol, 0
                        ),
                        required_confirmation_days=required_confirmation_days,
                    )
                    continue
                retained.append((signal, strategy))
            state.pending = retained

    @staticmethod
    def _net_cross_sleeve_orders(
        states: list[Any], date: pd.Timestamp
    ) -> None:
        """Net same-symbol same-day cross-sleeve buys against sells by share.

        Report P0-6: the real account holds at most one position per symbol, so
        a symbol that is being sold by any sleeve must not also be bought by
        another sleeve on the same day. But naively CANCelling every buy proved
        destructive to returns (a sell from one sleeve is rarely a full exit the
        account wants; a trend sleeve is often concurrently re-entering the same
        name), which is why the earlier blanket-cancel version over-suppressed
        re-entry and destroyed ~27% of returns on the 3/5/13 pools. We therefore
        net by SHARE COUNT toward the larger side:

        - buys are absorbed by the same-day sell pool CUMULATIVELY across all
          sleeves (each buy nets against the shares still left to sell), so a
          buy whose target is fully absorbed by the residual sell shares is
          redundant round-trip churn and is cancelled (recorded as
          ``netted_cross_sleeve_buy``);
        - a buy that EXCEEDS the residual sell shares represents genuine net ADD
          exposure the account still wants and is retained — kept as the tail
          that exceeds the sell pool (``buy_shares - rem``) when it partially
          overlaps — so the aggregate cross-sleeve intent survives and the
          position is never over-sold.

        This removes same-symbol round-trip churn and the fee/slippage drag
        without suppressing legitimate re-entry, keeping returns intact.
        """
        date_str = date.strftime("%Y-%m-%d")
        sell_shares: dict[str, int] = {}
        selling: set[str] = set()
        for state in states:
            for signal, _ in state.pending:
                if signal.direction == "sell":
                    symbol = str(signal.symbol)
                    selling.add(symbol)
                    sell_shares[symbol] = sell_shares.get(symbol, 0) + int(
                        signal.target_shares
                    )
        if not selling:
            return
        # Cumulative remaining sell pool per symbol. This pool is decremented
        # as buys are absorbed ACROSS all sleeves, so multiple buys for the same
        # symbol are netted against the same sell pool instead of each buy being
        # compared to the full sell total (which would over-net and cancel
        # legitimate net ADD exposure).
        remaining_sell: dict[str, int] = dict(sell_shares)
        for state in states:
            retained: list[tuple[Signal, Any]] = []
            for signal, strategy in state.pending:
                if signal.direction == "buy" and str(signal.symbol) in selling:
                    symbol = str(signal.symbol)
                    buy_shares = int(signal.target_shares)
                    rem = remaining_sell[symbol]
                    if rem <= 0:
                        # Sell pool already fully absorbed by earlier buys -> the
                        # whole buy is genuine net ADD exposure; retain it as-is.
                        retained.append((signal, strategy))
                        continue
                    if buy_shares <= rem:
                        # Fully absorbed by the remaining sell pool -> redundant
                        # same-day round-trip churn; cancel it and decrement.
                        state.sleeve._record_order_event(
                            date=date_str,
                            signal=signal,
                            event="netted_cross_sleeve_buy",
                            because="same_symbol_sell_pending_absorbs_buy",
                            sell_shares=buy_shares,
                        )
                        remaining_sell[symbol] = rem - buy_shares
                        continue
                    # Partially absorbed: the overlap with the sell pool is
                    # redundant churn; only the genuine NET-ADD tail survives.
                    # The retained buy is the portion that exceeds the residual
                    # sell (buy_shares - rem), so the cumulative cross-sleeve
                    # net-add intent is preserved instead of being trimmed down
                    # to the sell size (which would over-suppress re-entry).
                    state.sleeve._record_order_event(
                        date=date_str,
                        signal=signal,
                        event="netted_cross_sleeve_buy",
                        because="same_symbol_sell_pending_absorbs_buy_partial",
                        sell_shares=rem,
                    )
                    retained.append(
                        (replace(signal, target_shares=buy_shares - rem), strategy)
                    )
                    remaining_sell[symbol] = 0
                    continue
                retained.append((signal, strategy))
            state.pending = retained
