"""Universe-invariant portfolio coordination and public engines."""

from __future__ import annotations

# ruff: noqa: F401
import contextlib
import io
import math
from dataclasses import replace
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from quantfusion.config.portfolio import PortfolioPolicy
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
from quantfusion.risk.managers import (
    RecoverableDrawdownRiskManager,
    RiskManager,
)
from quantfusion.strategy.trend import BaseStrategy

_CoreBacktestEngine = CoreBacktestEngine
_ESTABLISHED_EXPANSION_CORE = ESTABLISHED_EXPANSION_CORE
_EnsembleBacktestEngine = EnsembleBacktestEngine
_EnsembleSleeveBacktestEngine = EnsembleSleeveBacktestEngine
_PreparedSleeveRun = PreparedSleeveRun
_RunRequest = RunRequest
_floor_to_lot = floor_to_lot
_require_int = require_int
from quantfusion.engine.ensemble_allocation import EnsembleAllocationMixin
from quantfusion.engine.ensemble_orchestration import EnsembleOrchestrationMixin
from quantfusion.engine.market_regime import MarketRegimeMixin
from quantfusion.engine.universe_risk import UniverseRiskMixin
from quantfusion.engine.universe_selection import UniverseSelectionMixin


class _UniverseInvariantSleeveMixin(
    MarketRegimeMixin, UniverseSelectionMixin, UniverseRiskMixin
):
    """Share portfolio risk and breadth behavior across coordinator and child sleeves."""

    policy: PortfolioPolicy
    cfg: dict[str, Any]
    risk: RiskManager
    risk_events: list[dict[str, Any]]
    _risk_lock_logged: bool

    # Report P1-3: sticky candidates reduce large-pool churn. A held name is
    # retained (incumbent bonus) unless it stops qualifying, and a weak held
    # non-core name is replaced only when a new candidate CLEARLY beats it by
    # ``MIN_SCORE_GAP`` for ``STICKY_CONFIRM_DAYS`` consecutive days, at most
    # ``MAX_NEW_PER_CYCLE`` per ``STICKY_CYCLE_DAYS`` cycle. The strongest
    # ``STICKY_CORE_LOCK`` held names (core) are never replaced by short-term
    # ranking noise. Enabled by default via the cfg flag ``sticky_candidates``.
    MIN_SCORE_GAP = 0.15
    MAX_NEW_PER_CYCLE = 1
    STICKY_CONFIRM_DAYS = 4
    STICKY_CYCLE_DAYS = 5
    STICKY_CORE_LOCK = 2
    # A rotated-out name is blocked from re-entry for this many trading days
    # (measured in evaluation positions) before it can compete again. Without
    # this cooldown the ``_sticky_rotated`` set grows without bound and, once it
    # covers every non-held eligible name in a finite pool, rotation silently
    # dead-locks (no new candidate can ever qualify).
    STICKY_ROTATED_COOLDOWN_DAYS = 20

    pass


class SleeveBacktestEngine(
    _UniverseInvariantSleeveMixin, _EnsembleSleeveBacktestEngine
):
    """Run one portfolio sleeve with adaptive breadth and recoverable drawdown defense."""

    ENGINE_LABEL = "Quant Fusion"


class BacktestEngine(
    EnsembleOrchestrationMixin,
    EnsembleAllocationMixin,
    _UniverseInvariantSleeveMixin,
    _EnsembleBacktestEngine,
):
    """Coordinate equal-capital portfolio sleeves under one universe-invariant policy."""

    ENGINE_LABEL = "Quant Fusion"
    SLEEVE_ENGINE_CLASS = SleeveBacktestEngine

    _SINGLE_ASSET_TREND_OVERRIDES: ClassVar[dict[str, Any]] = {
        "entry_period": 30,
        "exit_period": 20,
        "trail_atr_mult": 10.0,
        "profit_lock_giveback": 0.40,
        "reversal_break_giveback": 0.40,
        "reversal_exit_period": 20,
        "hard_stop": 0.25,
    }

    # Policy field names that are mirrored into cfg so a custom PortfolioPolicy can
    # drive the regime state machine read by the mixin (self.cfg.get(...)).
    _REGIME_CFG_KEYS: ClassVar[tuple[str, ...]] = (
        "market_regime_enabled",
        "regime_ewi_lookback",
        "regime_breadth_ma_long",
        "regime_adx_trend",
        "regime_adx_choppy",
        "regime_hurst_window",
        "regime_hurst_trend",
        "regime_hurst_choppy",
        "regime_vol_lookback",
        "regime_vol_extreme_pct",
        "regime_ewi_slope_trend",
        "regime_ewi_slope_choppy",
        "regime_score_trend",
        "regime_score_choppy",
        "regime_choppy_confirmations",
        "regime_trend_confirmations",
        "regime_recovery_confirmations",
        "regime_min_state_hold",
        "regime_transition_scale",
        "regime_trend_to_transition_confirmations",
        "regime_choppy_exit_ratio",
        "regime_transition_exit_ratio",
    )

    def __init__(
        self,
        initial_capital: float = 2_000_000,
        cfg: dict | None = None,
        policy: PortfolioPolicy | None = None,
    ) -> None:
        resolved_policy = policy or PortfolioPolicy()
        regime_cfg = {
            key: getattr(resolved_policy, key) for key in self._REGIME_CFG_KEYS
        }
        normalized_cfg = {
            "sector_guard_min_symbols": max(
                1, math.ceil(len(resolved_policy.regime_symbols) * 0.8)
            ),
            "group_min_slots": 0,
            # Policy regime values override the canonical defaults; explicit
            # user cfg still wins because it is spread last.
            **regime_cfg,
            **dict(cfg or {}),
        }
        super().__init__(
            initial_capital=initial_capital,
            cfg=normalized_cfg,
            policy=resolved_policy,
        )
        self._sleeve_weight_events: list[dict[str, Any]] = []
        self._last_sleeve_weight_regime: str | None = None
        self._runtime_tradable_count = 0
        self._runtime_reference_complete = True
        self._new_candidate_intent_streak: dict[str, int] = {}
        self._tail_guard_active = False
        self._tail_guard_policies: dict[str, PortfolioPolicy] = {}
        self._drawdown_budget_controller = None

    def _effective_policy(self, tradable_count: int) -> PortfolioPolicy:
        """Tighten drawdown gates smoothly as diversification approaches one."""
        count = _require_int("tradable_count", tradable_count, min_value=1)
        adjustment = self.policy.concentration_drawdown_adjustment / count
        return replace(
            self.policy,
            confirmed_drawdown=self.policy.confirmed_drawdown - adjustment,
            emergency_drawdown=self.policy.emergency_drawdown - adjustment,
        )

    def _effective_account_risk_policy(
        self,
        sleeve_policy: PortfolioPolicy,
        tradable_count: int,
        *,
        reference_complete: bool = True,
    ) -> PortfolioPolicy:
        """Tighten only the merged account for small correlated baskets."""
        if 3 <= tradable_count <= 6:
            if tradable_count >= 5 and not reference_complete:
                return replace(
                    sleeve_policy,
                    drawdown_alert=0.10,
                    confirmed_drawdown=0.11,
                    emergency_drawdown=0.13,
                    terminal_drawdown=0.20,
                    concentration_drawdown_adjustment=0.0,
                    rearm_trading_days=int(
                        self.cfg.get("concentrated_account_rearm_days", 252)
                    ),
                )
            return replace(
                sleeve_policy,
                drawdown_alert=0.14,
                confirmed_drawdown=0.18,
                emergency_drawdown=0.20,
                rearm_trading_days=int(
                    self.cfg.get("concentrated_account_rearm_days", 252)
                ),
            )
        if 7 <= tradable_count <= 8:
            if not reference_complete:
                return replace(
                    sleeve_policy,
                    drawdown_alert=0.10,
                    confirmed_drawdown=0.11,
                    emergency_drawdown=0.13,
                    terminal_drawdown=0.20,
                    concentration_drawdown_adjustment=0.0,
                    rearm_trading_days=int(
                        self.cfg.get("concentrated_account_rearm_days", 252)
                    ),
                )
            return replace(
                sleeve_policy,
                drawdown_alert=0.12,
                confirmed_drawdown=0.14,
                emergency_drawdown=0.18,
                rearm_trading_days=int(
                    self.cfg.get("concentrated_account_rearm_days", 252)
                ),
            )
        if tradable_count >= 9:
            if not reference_complete:
                return replace(
                    sleeve_policy,
                    drawdown_alert=0.10,
                    confirmed_drawdown=0.11,
                    emergency_drawdown=0.13,
                    terminal_drawdown=0.20,
                    concentration_drawdown_adjustment=0.0,
                    rearm_trading_days=int(
                        self.cfg.get("concentrated_account_rearm_days", 252)
                    ),
                )
            return replace(
                sleeve_policy,
                drawdown_alert=0.14,
                confirmed_drawdown=0.175,
                emergency_drawdown=0.18,
                terminal_drawdown=0.22,
                rearm_trading_days=int(
                    self.cfg.get("concentrated_account_rearm_days", 252)
                ),
            )
        return sleeve_policy

    def _runtime_sleeve_cfg(self, tradable_count: int) -> dict[str, Any]:
        """Return shared overrides with one fixed parameter set for all sizes.

        The per-sleeve ``max_positions`` is set to 10 so each sleeve has a
        wide candidate pool for momentum ranking. The portfolio-level
        ``self.cfg["max_positions"]`` (default 6) limits the total unique
        symbols held across all sleeves. These are two separate limits:
        per-sleeve=10 (candidate breadth), portfolio=6 (concentration).

        Small universes are naturally bounded by their own symbol count.
        Very small universes (<=2) still use the slower time-series
        trend contract, which is a strategy-logic switch (no cross-sectional
        information) rather than a parameter change.
        """
        sleeve_cfg = dict(self._ensemble_user_cfg)
        if tradable_count <= 2:
            sleeve_cfg.update(self._SINGLE_ASSET_TREND_OVERRIDES)
        sleeve_cfg["max_positions"] = 10
        if tradable_count >= 5 and not self._runtime_reference_complete:
            exposure_cap = float(
                self.cfg.get("incomplete_reference_max_total_weight", 0.85)
            )
            sleeve_cfg["max_total_weight"] = min(
                float(sleeve_cfg.get("max_total_weight", 1.0)), exposure_cap
            )
            sleeve_cfg["strategy_weight"] = min(
                float(sleeve_cfg.get("strategy_weight", 0.98)), exposure_cap
            )
        return sleeve_cfg

    def run(  # noqa: PLR0913 - Preserve the inherited public API.
        self,
        symbols_dict: dict[str, str],
        start_date: str,
        end_date: str,
        per_symbol_config: dict[str, dict] | None = None,
        profile: str | None = None,
        config_route: str = "auto",
        data_dir: str | None = None,
        *,
        cache_dir: str | None = None,
        indicator_state: str = "cold",
        warmup_calendar_days: int = 365,
        allocation_mode: str | None = None,
        risk_state: dict | None = None,
        route_controller: Any | None = None,
    ) -> dict:
        """Run one or several portfolio sleeves under the same effective policy formula."""
        mode = str(allocation_mode or self.policy.allocation_mode).lower()
        if mode == "ensemble":
            return super().run(
                symbols_dict,
                start_date,
                end_date,
                per_symbol_config=per_symbol_config,
                profile=profile,
                config_route=config_route,
                data_dir=data_dir,
                cache_dir=cache_dir,
                indicator_state=indicator_state,
                warmup_calendar_days=warmup_calendar_days,
                allocation_mode="ensemble",
                risk_state=risk_state,
                route_controller=route_controller,
            )
        if mode != "single":
            raise ValueError("allocation_mode must be 'single' or 'ensemble'")
        if route_controller is not None:
            raise ValueError("route_controller requires allocation_mode='ensemble'")
        count = len(symbols_dict)
        effective_policy = replace(
            self._effective_policy(count), allocation_mode="single"
        )
        sleeve = SleeveBacktestEngine(
            self.initial_capital,
            cfg=self._runtime_sleeve_cfg(count),
            policy=effective_policy,
            allocation_lookbacks=effective_policy.single_lookbacks,
            sleeve_name="single",
        )
        if risk_state:
            sleeve.cfg = dict(sleeve.cfg)
            sleeve.cfg["_initial_risk_state"] = risk_state
        result = sleeve.run(
            symbols_dict,
            start_date,
            end_date,
            per_symbol_config=per_symbol_config,
            profile=profile,
            config_route=config_route,
            data_dir=data_dir,
            cache_dir=cache_dir,
            indicator_state=indicator_state,
            warmup_calendar_days=warmup_calendar_days,
        )
        result["effective_portfolio_policy"] = effective_policy.as_dict()
        self.sleeves = [sleeve]
        self.last_result = result
        return result


UniverseInvariantSleeveMixin = _UniverseInvariantSleeveMixin

__all__ = ["BacktestEngine", "SleeveBacktestEngine", "UniverseInvariantSleeveMixin"]
