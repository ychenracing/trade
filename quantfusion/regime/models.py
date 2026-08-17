"""Immutable regime observations, routes, and deployment decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

@dataclass(frozen=True, slots=True)
class IndexTrend:
    """One index observation available at the deployment boundary."""

    code: str
    observed_date: str
    close: float
    ma20: float
    ma60: float
    trending: bool


class RegimeRoute(Enum):
    """Daily dynamic outer route (report 3.3).

    The route is a state machine that persists across trading days and only
    switches on confirmed, causally-available evidence so a clean bull stays in
    TREND (frozen trend engine) and a confirmed bear drifts to WEAK, with
    explicit TRANSITION states that need consecutive-day confirmation.
    """

    TREND = "trend"
    WEAK = "weak"
    CASH = "cash"
    TRANSITION_TO_TREND = "transition_to_trend"
    TRANSITION_TO_WEAK = "transition_to_weak"


@dataclass(frozen=True, slots=True)
class DailyRouteStep:
    """One auditable row of the daily route sequence."""

    date: str
    route: str


@dataclass(frozen=True, slots=True)
class RegimeEvidence:
    """Fixed-index regime evidence with explicit failure-closed coverage."""

    as_of: str
    regime: str
    observations: tuple[IndexTrend, ...]


@dataclass(frozen=True, slots=True)
class LeaderSelection:
    """Positive 240-session leaders observable before deployment."""

    as_of: str
    requested_symbols: tuple[str, ...]
    observed_symbols: int
    selected_symbols: tuple[str, ...]
    selected_returns: tuple[float, ...]
    unavailable_symbols: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DeploymentDecision:
    """Auditable choice between the frozen trend and weak-regime policies."""

    name: str
    boundary: str
    reason: str
    regime: RegimeEvidence
    leaders: LeaderSelection | None
