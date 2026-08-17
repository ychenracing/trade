"""Universe-aware sector and portfolio risk result decoration."""

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
from quantfusion.domain.models import AccountState, MarketRegimeObservation, Signal
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


class UniverseRiskMixin:
    """Universe-aware sector and portfolio risk result decoration."""

    def _update_sector_guard(
        self,
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
        all_dates: list[pd.Timestamp],
        date_to_pos: dict[pd.Timestamp, int],
    ) -> str | None:
        """Update breadth risk, then advance the market-regime state machine.

        The regime update runs after the sector guard so entries respect the
        freshly scored regime, and before signal generation because
        ``_evaluate_trading_day`` continues only after this method returns.
        """
        scoped_data = {
            code: data_map[code]
            for code in self.policy.regime_symbols
            if code in data_map
        }
        guard_state = super()._update_sector_guard(  # pyright: ignore[reportAttributeAccessIssue]
            scoped_data,
            date,
            all_dates,
            date_to_pos,
        )
        self._update_market_regime(data_map, date, all_dates, date_to_pos)
        return guard_state

    def _apply_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        all_dates: list[pd.Timestamp],
        date_to_pos: dict[pd.Timestamp, int],
        pending: list[tuple[Signal, BaseStrategy]],
    ) -> tuple[list[tuple[Signal, BaseStrategy]], bool, bool]:
        """Reset inherited one-shot logging whenever a temporary lock rearms."""
        before = len(self.risk_events)
        outcome = super()._apply_portfolio_risk(  # pyright: ignore[reportAttributeAccessIssue]
            current_assets, date_str, all_dates, date_to_pos, pending
        )
        if any(
            event.get("event") == "portfolio_drawdown_rearmed"
            for event in self.risk_events[before:]
        ):
            self._risk_lock_logged = False
        return outcome

    def _build_result(self, final_assets: float, all_dates: list[pd.Timestamp]) -> dict:
        """Expose temporary and terminal lock state plus regime history."""
        result = super()._build_result(  # pyright: ignore[reportAttributeAccessIssue]
            final_assets,
            all_dates,
        )
        manager = self.risk
        result.update(
            {
                "portfolio_policy": self.policy.as_dict(),
                "safe_mode_active": bool(getattr(self, "_safe_mode_active", False)),
                "terminal_risk_lock": bool(
                    isinstance(manager, RecoverableDrawdownRiskManager)
                    and manager.terminal_lock
                ),
                "cycle_lock_count": int(
                    manager.cycle_lock_count
                    if isinstance(manager, RecoverableDrawdownRiskManager)
                    else 0
                ),
                "guard_scope_mode": "fixed_signal_only_regime_basket",
                "tradable_symbols": sorted(self._tradable_symbol_codes),
                "regime_state_series": list(self._regime_state_series),
                "regime_final_state": self._regime_state,
            }
        )
        return result
