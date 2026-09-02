"""Stable universe routing and sticky candidate selection."""

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


class UniverseSelectionMixin:
    """Stable universe routing and sticky candidate selection."""

    def _resolve_symbol_configs(
        self,
        symbols_dict: dict[str, str],
        per_symbol_config: dict[str, dict] | None,
        config_route: str,
    ) -> dict[str, dict]:
        """Install the recoverable manager after inherited parameter routing."""
        resolved = super()._resolve_symbol_configs(  # pyright: ignore[reportAttributeAccessIssue]
            symbols_dict, per_symbol_config, config_route
        )
        symbol_groups = dict(self.risk.symbol_groups)
        self.risk = RecoverableDrawdownRiskManager(self.cfg, self.policy)
        self.risk.configure_groups(symbol_groups)
        return resolved

    def _select_momentum_candidates(
        self,
        data_map: dict[str, pd.DataFrame],
        symbols_dict: dict[str, str],
        date: pd.Timestamp,
    ) -> set[str]:
        """Rank tradable symbols with stable multi-horizon momentum evidence."""
        del data_map
        tradable = sorted(
            code for code in symbols_dict if code in self._tradable_symbol_codes
        )
        if not tradable:
            return set()
        maximum = int(self.cfg.get("max_positions", 6))
        if (
            bool(self.cfg.get("adaptive_max_positions", True))
            and self._external_risk_level >= 1
        ):
            if self._regime_state == "TRANSITION":
                maximum = min(
                    maximum, int(self.cfg.get("transition_max_positions", 4))
                )
            elif self._regime_state == "CHOPPY":
                maximum = min(
                    maximum, int(self.cfg.get("choppy_max_positions", 3))
                )
        # Tiny pools have no meaningful cross-section.  Starting at five names,
        # however, every sleeve uses the same fixed reference basket even when
        # its local slot limit is not full; otherwise a 5-name base pool and its
        # 6th add-one candidate are evaluated on incompatible scales.
        if len(tradable) < 5:
            return set(tradable)

        scores = self._candidate_reference_scores(date, tradable)

        universe_size = len(tradable)
        if universe_size <= 8:
            reference_threshold = 0.50
        elif universe_size <= 12:
            reference_threshold = 0.55
        else:
            reference_threshold = 0.65
        reference_threshold = max(
            reference_threshold, self.policy.candidate_reference_percentile
        )
        eligible = [
            code
            for code in tradable
            if code in scores and scores[code] >= reference_threshold
        ]

        def sort_key(code: str) -> tuple[bool, float, str]:
            return code not in scores, -scores.get(code, 0.0), code

        ranked = sorted(eligible, key=sort_key)
        scores = {code: scores[code] for code in eligible}
        # Report P1-3: sticky candidates for large pools. The daily ranking is
        # noisy, and rotating a held name out because its percentile dipped
        # slightly is pure churn (fee/slippage + selling a winner). We therefore
        # retain every eligible held symbol (incumbent bonus) and only rotate
        # the weakest NON-CORE held name when a NEW candidate CLEARLY beats it
        # by ``MIN_SCORE_GAP`` for ``STICKY_CONFIRM_DAYS`` consecutive days and
        # at most ``MAX_NEW_PER_CYCLE`` per ``STICKY_CYCLE_DAYS`` cycle. The
        # strongest ``STICKY_CORE_LOCK`` held names (core) are never rotated by
        # short-term noise. When the book is under-deployed (spare slots above
        # the current holdings) new names are admitted freely from the top of
        # the ranking, preserving bull growth.
        sticky_enabled = bool(self.cfg.get("sticky_candidates", True))
        if not sticky_enabled:
            return set(ranked[:maximum])
        self._sticky_eval_pos = getattr(self, "_sticky_eval_pos", 0) + 1
        held = {
            code
            for code, positions in self.positions.items()
            if any(
                getattr(position, "shares", 0) > 0
                for position in positions.values()
            )
        }
        eligible_held = set(eligible) & held
        # Incumbent bonus: every eligible held name is retained by default.
        selected = set(eligible_held)
        # Expire rotated-out names whose cooldown has elapsed so a finite pool
        # never dead-locks rotation (report P1-3 "recently rotated"). This prune
        # must run BEFORE both the spare-slot fill and the rotation branch so a
        # freshly rotated-out name is never re-admitted into a spare slot within
        # its cooldown window.
        cooldown = int(
            self.cfg.get(
                "sticky_rotated_cooldown_days",
                self.STICKY_ROTATED_COOLDOWN_DAYS,
            )
        )
        self._sticky_rotated = {
            code: pos
            for code, pos in self._sticky_rotated.items()
            if self._sticky_eval_pos - pos < cooldown
        }
        # Under-deployed book: fill spare slots freely from the top of the
        # ranking so a fresh bull / post-rotation book can deploy capital.
        spare = maximum - len(selected)
        if spare > 0:
            for code in ranked:
                if len(selected) >= maximum:
                    break
                # A recently rotated-out name stays on cooldown even when the
                # book is under-deployed, so a weak name cannot be re-admitted
                # into a spare slot the same cycle it was rotated out.
                if code in selected or code in self._sticky_rotated:
                    continue
                selected.add(code)
        # Full book: rotate the weakest non-core held name when a clearly better
        # new candidate persists. Core (strongest STICKY_CORE_LOCK) names are
        # locked against short-term ranking noise (report P1-3 "core lock").
        if len(selected) >= maximum and eligible_held:
            score_dispersion = (
                max(scores.values()) - min(scores.values()) if scores else 0.0
            )
            gap = float(self.cfg.get("sticky_min_score_gap", self.MIN_SCORE_GAP))
            confirmations = int(
                self.cfg.get("sticky_confirm_days", self.STICKY_CONFIRM_DAYS)
            )
            cycle_days = int(
                self.cfg.get("sticky_cycle_days", self.STICKY_CYCLE_DAYS)
            )
            if (
                bool(self.cfg.get("adaptive_sticky_candidates", True))
                and self._external_risk_level >= 1
                and score_dispersion < 0.20
            ):
                gap = min(1.0, gap * 1.5)
                confirmations += 2
            if self._regime_state == "TRANSITION" and self._external_risk_level >= 1:
                gap = min(1.0, gap * 1.25)
                confirmations += 2
                cycle_days += 2
            core = set(
                sorted(
                    eligible_held,
                    key=lambda code: (-scores.get(code, 0.0), code),
                )[
                    : self.STICKY_CORE_LOCK
                ]
            )
            replaceable = sorted(
                eligible_held - core,
                key=lambda code: (scores.get(code, 0.0), code),
            )
            if (
                replaceable
                and self._sticky_eval_pos - self._sticky_last_rotation_pos
                >= cycle_days
            ):
                weakest = replaceable[0]
                weakest_score = scores.get(weakest, 0.0)
                # Best new candidate (not held, not under cooldown) that beats
                # the weakest held name by the minimum score gap.
                best_new = None
                for cand in ranked:
                    if cand in selected or cand in held or cand in self._sticky_rotated:
                        continue
                    if scores[cand] >= weakest_score + gap:
                        best_new = cand
                        break
                # Consecutive-day confirmation (report P1-3 "consecutive days"):
                # the beat counter advances only while the SAME candidate keeps
                # being the best new option. A switch of leader (or the absence
                # of any qualifying leader) resets the previous leader's count,
                # so a candidate's qualifying days must be contiguous.
                if best_new is not None and best_new == self._sticky_leader:
                    self._sticky_beat_days[best_new] = (
                        self._sticky_beat_days.get(best_new, 0) + 1
                    )
                else:
                    if self._sticky_leader is not None:
                        self._sticky_beat_days.pop(self._sticky_leader, None)
                    self._sticky_leader = best_new
                    if best_new is not None:
                        self._sticky_beat_days[best_new] = 1
                if (
                    best_new is not None
                    and self._sticky_beat_days.get(best_new, 0)
                    >= confirmations
                ):
                    selected.discard(weakest)
                    selected.add(best_new)
                    self._sticky_last_rotation_pos = self._sticky_eval_pos
                    self._sticky_rotated[weakest] = self._sticky_eval_pos
                    self._sticky_beat_days.pop(best_new, None)
                    self._sticky_leader = None
        return selected

    def _candidate_reference_scores(
        self, date: pd.Timestamp, symbols: list[str] | set[str]
    ) -> dict[str, float]:
        """Score symbols against the fixed basket, independent of pool makeup."""
        requested = sorted(symbols)
        totals = {code: 0.0 for code in requested}
        observations = {code: 0 for code in requested}
        for window in self.policy.candidate_lookbacks:
            reference_values: list[float] = []
            for code in self.policy.regime_symbols:
                series = self._candidate_score_series.get(code, {}).get(window)
                if series is None or date not in series.index:
                    continue
                value = float(series.loc[date])
                if math.isfinite(value):
                    reference_values.append(value)
            if not reference_values:
                continue

            for code in requested:
                series = self._candidate_score_series.get(code, {}).get(window)
                if series is None or date not in series.index:
                    continue
                value = float(series.loc[date])
                if not math.isfinite(value):
                    continue
                percentile = sum(
                    reference <= value for reference in reference_values
                ) / len(reference_values)
                totals[code] += percentile
                observations[code] += 1
        return {
            code: totals[code] / observations[code]
            for code in requested
            if observations[code]
        }
