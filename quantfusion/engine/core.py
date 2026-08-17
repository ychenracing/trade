"""Composed core deterministic signal, execution, and accounting engine."""

from __future__ import annotations

# ruff: noqa: F401

from typing import Any

import pandas as pd

from quantfusion.domain.models import Position, Signal, TradeRecord
from quantfusion.domain.rules import require_finite
from quantfusion.engine.configuration import EngineConfigurationMixin
from quantfusion.engine.data_flow import CoreDataFlowMixin
from quantfusion.execution.flow import CoreExecutionMixin
from quantfusion.engine.replay_loop import CoreReplayLoopMixin
from quantfusion.engine.results import CoreResultsMixin
from quantfusion.engine.sector_risk import CoreSectorRiskMixin
from quantfusion.engine.signals import CoreSignalMixin
from quantfusion.risk.managers import RiskManager
from quantfusion.strategy.trend import (
    ATRChannelStrategy,
    BaseStrategy,
    DualMAStrategy,
    TurtleBreakoutStrategy,
)

_require_finite = require_finite


class _CoreBacktestEngine(
    EngineConfigurationMixin,
    CoreSignalMixin,
    CoreDataFlowMixin,
    CoreSectorRiskMixin,
    CoreReplayLoopMixin,
    CoreExecutionMixin,
    CoreResultsMixin,
):
    """Run the deterministic strategy, execution, and accounting pipeline."""

    ENGINE_LABEL = "Quant Fusion"

    def _display_run_period(self, start_date: str, end_date: str) -> tuple[str, str]:
        """Return the user-facing trading period shown in the run header."""
        return start_date, end_date

    def __init__(
        self, initial_capital: float = 2000000, cfg: dict | None = None
    ) -> None:
        """Initialize a reusable engine with validated immutable inputs."""
        self.initial_capital = _require_finite(
            "initial_capital", initial_capital, min_value=0.01
        )
        self._user_cfg = dict(cfg or {})
        self.cfg = self._validate_config({**self._default_config(), **self._user_cfg})
        self.cash = self.initial_capital
        self.positions: dict[str, dict[str, Position]] = {}
        self._initial_positions: dict[str, dict[str, Position]] = {}
        self._initial_cash: float | None = None
        self.trades: list[TradeRecord] = []
        self.equity_curve: list[dict] = []
        self.risk = RiskManager(self.cfg)
        self.strategy_instances: dict[str, list[BaseStrategy]] = {}
        # Strategies driven by an outer controller still own live positions and
        # must be discoverable by liquidation/reduction code, but they must not
        # be evaluated again by the core daily signal loop.
        self.external_strategy_instances: dict[str, list[BaseStrategy]] = {}
        self.symbol_names: dict[str, str] = {}
        self.symbol_last_dates: dict[str, pd.Timestamp] = {}
        self.global_last_date: pd.Timestamp | None = None
        self.symbol_configs: dict[str, dict] = {}
        self.pending_signals: list[tuple[Signal, BaseStrategy]] = []
        self.fusion_events: list[dict] = []
        self.risk_events: list[dict] = []
        self.sector_guard_active = False
        self._safe_mode_active: bool = False  # audit-only flag; no dynamic parameter changes
        self._sector_shock_positions: list[int] = []
        self._sector_recovery_streak = 0
        self.strategy_templates: list[type[BaseStrategy]] = [
            TurtleBreakoutStrategy,
            DualMAStrategy,
            ATRChannelStrategy,
        ]


CoreBacktestEngine = _CoreBacktestEngine

__all__ = ["CoreBacktestEngine"]
