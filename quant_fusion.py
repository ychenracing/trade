#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility facade for the canonical :mod:`quantfusion` package."""

from __future__ import annotations

from quantfusion.application.backtest_cli import (
    DEFAULT_SYMBOLS as DEFAULT_SYMBOLS,
    DEFAULT_SYMBOL_NAMES as DEFAULT_SYMBOL_NAMES,
    SYMBOL_NAME_TABLE as SYMBOL_NAME_TABLE,
    build_argument_parser,
    main,
    parse_symbols,
)
from quantfusion.application.reporting import PerformanceReport
from quantfusion.config.universe import ESTABLISHED_EXPANSION_CORE
from quantfusion.data.providers import DataFetcher
from quantfusion.domain.models import (
    AccountState,
    BarContext,
    EngineState,
    MarketRegimeObservation,
    Position,
    SectorObservation,
    Signal,
    TradeRecord,
    account_order_count,
)
from quantfusion.domain.rules import (
    A_SHARE_LOT_SIZE as A_SHARE_LOT_SIZE,
    SYMBOL_RE,
    floor_to_lot,
    is_finite_number,
    limit_pct_for_code,
    require_bool,
    require_finite,
    require_int,
    require_positive,
)
from quantfusion.engine.causal import CausalBacktestEngine
from quantfusion.engine.core import CoreBacktestEngine
from quantfusion.engine.ensemble import (
    EnsembleBacktestEngine,
    EnsembleSleeveBacktestEngine,
    PreparedSleeveRun,
    RunRequest,
)
from quantfusion.engine.universe import BacktestEngine, SleeveBacktestEngine
from quantfusion.execution.priorities import EXECUTION_PRIORITY
from quantfusion.indicators.technical import Indicators
from quantfusion.portfolio.policy import (
    PortfolioPolicy,
    PortfolioPolicyBase,
    require_positive_ratio,
)
from quantfusion.risk.managers import (
    ConfirmedDrawdownRiskManager,
    PersistentRiskManager,
    RecoverableDrawdownRiskManager,
    RiskManager,
)
from quantfusion.strategy.trend import (
    ATRChannelStrategy,
    BaseStrategy,
    DualMAStrategy,
    TurtleBreakoutStrategy,
)

_SYMBOL_RE = SYMBOL_RE
_ESTABLISHED_EXPANSION_CORE = ESTABLISHED_EXPANSION_CORE
_account_order_count = account_order_count
_floor_to_lot = floor_to_lot
_is_finite_number = is_finite_number
_limit_pct_for_code = limit_pct_for_code
_require_bool = require_bool
_require_finite = require_finite
_require_int = require_int
_require_positive = require_positive
_require_positive_ratio = require_positive_ratio
_CoreBacktestEngine = CoreBacktestEngine
_CausalBacktestEngine = CausalBacktestEngine
_PortfolioPolicyBase = PortfolioPolicyBase
_ConfirmedDrawdownRiskManager = ConfirmedDrawdownRiskManager
_EnsembleSleeveBacktestEngine = EnsembleSleeveBacktestEngine
_RunRequest = RunRequest
_PreparedSleeveRun = PreparedSleeveRun
_EnsembleBacktestEngine = EnsembleBacktestEngine

__all__ = [
    "ATRChannelStrategy",
    "AccountState",
    "BacktestEngine",
    "BarContext",
    "BaseStrategy",
    "DataFetcher",
    "DualMAStrategy",
    "EngineState",
    "EXECUTION_PRIORITY",
    "Indicators",
    "MarketRegimeObservation",
    "PerformanceReport",
    "PersistentRiskManager",
    "PortfolioPolicy",
    "Position",
    "RecoverableDrawdownRiskManager",
    "RiskManager",
    "SectorObservation",
    "Signal",
    "SleeveBacktestEngine",
    "TradeRecord",
    "TurtleBreakoutStrategy",
    "build_argument_parser",
    "main",
    "parse_symbols",
]


if __name__ == "__main__":
    main()
