"""Compatibility facade for canonical regime and replay modules."""

# ruff: noqa: F401

import quant_fusion as qf

from quantfusion.config.regime import (
    REGIME_INDEX_FILES,
    LEADER_LOOKBACK,
    MAX_LEADERS,
    MAX_SYMBOL_WEIGHT,
    EMERGING_MIN_DAYS,
    MAX_EMERGING_LEADERS,
    PROFIT_ACTIVATION,
    TRAILING_ATR_MULTIPLIER,
    WEAK_ENTRY_ATR_MULTIPLIER,
    WEAK_HARD_STOP,
    WEAK_TIME_STOP_DAYS,
    WEAK_TIME_STOP_RETURN,
    MAX_EVIDENCE_STALENESS_DAYS,
    WEAK_DRAWDOWN_ALERT,
    WEAK_CONFIRMED_DRAWDOWN,
    WEAK_EMERGENCY_DRAWDOWN,
    WEAK_TERMINAL_DRAWDOWN,
    WEAK_DAILY_LOSS_LIMIT,
    WEAK_EXIT_COOLDOWN,
    DEFAULT_EXIT_COOLDOWN,
    WEAK_PROBE_WEIGHT_RATIO,
    WEAK_PROBE_CONFIRM_DAYS,
    WEAK_REENTRY_FAIL_LIMIT,
    WEAK_REENTRY_MAX_DRAWDOWN,
    ROUTE_TREND_FAST_MA,
    ROUTE_TREND_SLOW_MA,
    ROUTE_MIN_HOLD_DAYS,
    ROUTE_CONFIRM_DAYS,
    ROUTE_RECOVERY_CONFIRM_DAYS,
)
from quantfusion.engine.replay import (
    ProductionReplayEngine,
    ProductionRouteController,
    RegimeAdaptiveBacktestEngine,
)
from quantfusion.config.weak import weak_regime_config, weak_regime_policy
from quantfusion.regime.evidence import (
    detect_regime,
    local_frame,
    normalized_timestamp,
    select_positive_momentum_leaders,
    timestamp,
)
from quantfusion.regime.models import (
    DailyRouteStep,
    DeploymentDecision,
    IndexTrend,
    LeaderSelection,
    RegimeEvidence,
    RegimeRoute,
)
from quantfusion.regime.state_machine import boundary_route, simulate_route_sequence
from quantfusion.strategy.weak import (
    CashPreservationStrategy,
    PositiveMomentumHoldStrategy,
)

_timestamp = timestamp
_normalized_timestamp = normalized_timestamp
_local_frame = local_frame
_weak_regime_policy = weak_regime_policy
_weak_regime_config = weak_regime_config
