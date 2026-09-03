"""Validated portfolio allocation and drawdown policy."""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from quantfusion.domain.rules import (
    require_bool,
    require_finite,
    require_int,
    require_positive,
)

_require_bool = require_bool
_require_finite = require_finite
_require_int = require_int
_require_positive = require_positive


def _require_positive_ratio(
    name: str, value: object, *, inclusive_max: bool = False
) -> float:
    """Reject booleans and return a validated ratio in the interval (0, 1]."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a numeric ratio, not a Boolean")
    return _require_positive(
        name,
        value,
        max_value=1.0,
        inclusive_max=inclusive_max,
    )


@dataclass(frozen=True)
class _PortfolioPolicyBase:
    """Define ensemble controls independently from the core signal parameters."""

    allocation_mode: str = "ensemble"
    single_lookbacks: tuple[int, ...] = (5, 10, 20)
    allocation_horizons: tuple[tuple[int, ...], ...] = (
        (3, 5, 10),
        (5, 10, 20),
        (10, 20, 40),
    )
    drawdown_alert: float = 0.14
    confirmed_drawdown: float = 0.15
    drawdown_confirmations: int = 2
    emergency_drawdown: float = 0.175
    adv_lookback: int = 20
    max_order_adv_ratio: float = 0.005

    def __post_init__(self) -> None:
        """Validate thresholds, horizons, and liquidity controls eagerly."""
        mode = str(self.allocation_mode).lower()
        if mode not in {"single", "ensemble"}:
            raise ValueError("allocation_mode must be 'single' or 'ensemble'")
        object.__setattr__(self, "allocation_mode", mode)
        alert = _require_positive_ratio("drawdown_alert", self.drawdown_alert)
        confirmed = _require_positive_ratio(
            "confirmed_drawdown", self.confirmed_drawdown
        )
        emergency = _require_positive_ratio(
            "emergency_drawdown", self.emergency_drawdown
        )
        if not alert < confirmed < emergency:
            raise ValueError(
                "drawdown thresholds must satisfy alert < confirmed < emergency"
            )
        object.__setattr__(self, "drawdown_alert", alert)
        object.__setattr__(self, "confirmed_drawdown", confirmed)
        object.__setattr__(self, "emergency_drawdown", emergency)
        confirmations = _require_int(
            "drawdown_confirmations", self.drawdown_confirmations, min_value=1
        )
        adv_lookback = _require_int("adv_lookback", self.adv_lookback, min_value=1)
        ratio = _require_positive_ratio(
            "max_order_adv_ratio",
            self.max_order_adv_ratio,
            inclusive_max=True,
        )
        object.__setattr__(self, "drawdown_confirmations", confirmations)
        object.__setattr__(self, "adv_lookback", adv_lookback)
        object.__setattr__(self, "max_order_adv_ratio", ratio)
        object.__setattr__(
            self,
            "single_lookbacks",
            self._validate_lookbacks("single_lookbacks", self.single_lookbacks),
        )
        horizons = tuple(
            self._validate_lookbacks(f"allocation_horizons[{index}]", values)
            for index, values in enumerate(self.allocation_horizons)
        )
        if not horizons:
            raise ValueError("allocation_horizons must contain at least one sleeve")
        if len(set(horizons)) != len(horizons):
            raise ValueError("allocation_horizons must not contain duplicate sleeves")
        object.__setattr__(self, "allocation_horizons", horizons)

    @staticmethod
    def _validate_lookbacks(name: str, values: object) -> tuple[int, ...]:
        """Return one strictly increasing tuple of positive integer lookbacks."""
        if isinstance(values, (str, bytes)) or not isinstance(values, (tuple, list)):
            raise ValueError(f"{name} must be a sequence of positive integers")
        normalized = tuple(
            _require_int(f"{name}[{index}]", value, min_value=1)
            for index, value in enumerate(values)
        )
        if not normalized or any(right <= left for left, right in pairwise(normalized)):
            raise ValueError(f"{name} must be strictly increasing and non-empty")
        return normalized

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly policy snapshot for result auditing."""
        return {
            "allocation_mode": self.allocation_mode,
            "single_lookbacks": list(self.single_lookbacks),
            "allocation_horizons": [
                list(values) for values in self.allocation_horizons
            ],
            "drawdown_alert": self.drawdown_alert,
            "confirmed_drawdown": self.confirmed_drawdown,
            "drawdown_confirmations": self.drawdown_confirmations,
            "emergency_drawdown": self.emergency_drawdown,
            "adv_lookback": self.adv_lookback,
            "max_order_adv_ratio": self.max_order_adv_ratio,
        }


@dataclass(frozen=True)
class PortfolioPolicy(_PortfolioPolicyBase):
    """Define recoverable cycle risk and a separate terminal loss boundary."""

    allocation_horizons: tuple[tuple[int, ...], ...] = (
        (3, 5, 10),
        (5, 10, 20),
        (5, 20, 60),
    )
    candidate_lookbacks: tuple[int, ...] = (10, 20, 40)
    candidate_horizons: tuple[tuple[int, ...], ...] = (
        (10, 20, 40),
        (10, 20, 40),
        (10, 40, 80),
    )
    drawdown_alert: float = 0.18
    confirmed_drawdown: float = 0.23
    emergency_drawdown: float = 0.27
    rearm_trading_days: int = 10
    terminal_drawdown: float = 0.28
    concentration_drawdown_adjustment: float = 0.02
    candidate_reference_percentile: float = 0.50
    regime_symbols: tuple[str, ...] = (
        "300308",
        "300502",
        "300394",
        "688008",
        "603986",
    )
    # Market regime recognition controls (propagated to cfg at runtime so the
    # mixin reads them via self.cfg.get(...); the policy snapshot stays auditable).
    # Enabled by default — see the canonical engine defaults for rationale.
    market_regime_enabled: bool = True
    regime_ewi_lookback: int = 20
    regime_breadth_ma_long: int = 20
    regime_adx_trend: float = 25
    regime_adx_choppy: float = 20
    regime_hurst_window: int = 100
    regime_hurst_trend: float = 0.55
    regime_hurst_choppy: float = 0.45
    regime_vol_lookback: int = 60
    regime_vol_extreme_pct: float = 0.9
    regime_ewi_slope_trend: float = 0.02
    regime_ewi_slope_choppy: float = -0.02
    regime_score_trend: int = 2
    regime_score_choppy: int = -3
    regime_choppy_confirmations: int = 2
    regime_trend_confirmations: int = 3
    regime_recovery_confirmations: int = 3
    regime_min_state_hold: int = 3
    regime_transition_scale: float = 1.0
    regime_trend_to_transition_confirmations: int = 3
    regime_choppy_exit_ratio: float = 0.3
    regime_transition_exit_ratio: float = 0.0

    def __post_init__(self) -> None:
        """Validate inherited controls and the portfolio recovery constraints."""
        super().__post_init__()
        rearm_days = _require_int(
            "rearm_trading_days", self.rearm_trading_days, min_value=1
        )
        terminal = _require_positive(
            "terminal_drawdown", self.terminal_drawdown, max_value=1.0
        )
        if terminal < self.confirmed_drawdown:
            raise ValueError("terminal_drawdown must not be below confirmed_drawdown")
        concentration_adjustment = _require_finite(
            "concentration_drawdown_adjustment",
            self.concentration_drawdown_adjustment,
            min_value=0.0,
            max_value=self.confirmed_drawdown,
        )
        if self.confirmed_drawdown - concentration_adjustment <= self.drawdown_alert:
            raise ValueError(
                "concentration_drawdown_adjustment leaves no room above "
                "drawdown_alert for a one-symbol portfolio"
            )
        reference_percentile = _require_finite(
            "candidate_reference_percentile",
            self.candidate_reference_percentile,
            min_value=0.0,
            max_value=1.0,
        )
        regime_symbols = tuple(str(symbol) for symbol in self.regime_symbols)
        if not regime_symbols:
            raise ValueError("regime_symbols must contain at least one symbol")
        if len(set(regime_symbols)) != len(regime_symbols):
            raise ValueError("regime_symbols must not contain duplicates")
        if any(re.fullmatch(r"\d{6}", symbol) is None for symbol in regime_symbols):
            raise ValueError("every regime symbol must be a six-digit code")
        object.__setattr__(self, "rearm_trading_days", rearm_days)
        object.__setattr__(self, "terminal_drawdown", terminal)
        object.__setattr__(
            self,
            "candidate_lookbacks",
            self._validate_lookbacks("candidate_lookbacks", self.candidate_lookbacks),
        )
        candidate_horizons = tuple(
            self._validate_lookbacks(f"candidate_horizons[{index}]", values)
            for index, values in enumerate(self.candidate_horizons)
        )
        if len(candidate_horizons) != len(self.allocation_horizons):
            raise ValueError(
                "candidate_horizons must align one-for-one with allocation_horizons"
            )
        object.__setattr__(self, "candidate_horizons", candidate_horizons)
        object.__setattr__(
            self, "concentration_drawdown_adjustment", concentration_adjustment
        )
        object.__setattr__(self, "candidate_reference_percentile", reference_percentile)
        object.__setattr__(self, "regime_symbols", regime_symbols)
        self._validate_market_regime_fields()

    def _validate_market_regime_fields(self) -> None:
        """Validate the market-regime controls and freeze normalized values."""
        market_regime_enabled = _require_bool(
            "market_regime_enabled", self.market_regime_enabled
        )
        object.__setattr__(self, "market_regime_enabled", market_regime_enabled)
        ewi_lookback = _require_int(
            "regime_ewi_lookback", self.regime_ewi_lookback, min_value=2
        )
        breadth_ma_long = _require_int(
            "regime_breadth_ma_long", self.regime_breadth_ma_long, min_value=1
        )
        hurst_window = _require_int(
            "regime_hurst_window", self.regime_hurst_window, min_value=10
        )
        vol_lookback = _require_int(
            "regime_vol_lookback", self.regime_vol_lookback, min_value=2
        )
        score_trend = _require_int(
            "regime_score_trend", self.regime_score_trend, min_value=-10
        )
        score_choppy = _require_int(
            "regime_score_choppy", self.regime_score_choppy, min_value=-10
        )
        choppy_confirmations = _require_int(
            "regime_choppy_confirmations",
            self.regime_choppy_confirmations,
            min_value=1,
        )
        trend_confirmations = _require_int(
            "regime_trend_confirmations",
            self.regime_trend_confirmations,
            min_value=1,
        )
        recovery_confirmations = _require_int(
            "regime_recovery_confirmations",
            self.regime_recovery_confirmations,
            min_value=1,
        )
        min_state_hold = _require_int(
            "regime_min_state_hold", self.regime_min_state_hold, min_value=1
        )
        adx_trend = _require_finite(
            "regime_adx_trend", self.regime_adx_trend, min_value=0.0
        )
        adx_choppy = _require_finite(
            "regime_adx_choppy", self.regime_adx_choppy, min_value=0.0
        )
        hurst_trend = _require_finite(
            "regime_hurst_trend",
            self.regime_hurst_trend,
            min_value=0.0,
            max_value=1.0,
        )
        hurst_choppy = _require_finite(
            "regime_hurst_choppy",
            self.regime_hurst_choppy,
            min_value=0.0,
            max_value=1.0,
        )
        vol_extreme_pct = _require_positive(
            "regime_vol_extreme_pct",
            self.regime_vol_extreme_pct,
            max_value=1.0,
            inclusive_max=True,
        )
        ewi_slope_trend = _require_finite(
            "regime_ewi_slope_trend",
            self.regime_ewi_slope_trend,
            min_value=-1.0,
            max_value=1.0,
        )
        ewi_slope_choppy = _require_finite(
            "regime_ewi_slope_choppy",
            self.regime_ewi_slope_choppy,
            min_value=-1.0,
            max_value=1.0,
        )
        transition_scale = _require_finite(
            "regime_transition_scale",
            self.regime_transition_scale,
            min_value=0.0,
            max_value=1.0,
        )
        trend_to_transition = _require_int(
            "regime_trend_to_transition_confirmations",
            self.regime_trend_to_transition_confirmations,
            min_value=1,
        )
        choppy_exit_ratio = _require_finite(
            "regime_choppy_exit_ratio",
            self.regime_choppy_exit_ratio,
            min_value=0.0,
            max_value=1.0,
        )
        transition_exit_ratio = _require_finite(
            "regime_transition_exit_ratio",
            self.regime_transition_exit_ratio,
            min_value=0.0,
            max_value=1.0,
        )
        if adx_trend <= adx_choppy:
            raise ValueError("regime_adx_trend must be greater than regime_adx_choppy")
        if hurst_trend <= hurst_choppy:
            raise ValueError(
                "regime_hurst_trend must be greater than regime_hurst_choppy"
            )
        if ewi_slope_trend <= ewi_slope_choppy:
            raise ValueError(
                "regime_ewi_slope_trend must be greater than regime_ewi_slope_choppy"
            )
        object.__setattr__(self, "regime_ewi_lookback", ewi_lookback)
        object.__setattr__(self, "regime_breadth_ma_long", breadth_ma_long)
        object.__setattr__(self, "regime_hurst_window", hurst_window)
        object.__setattr__(self, "regime_vol_lookback", vol_lookback)
        object.__setattr__(self, "regime_score_trend", score_trend)
        object.__setattr__(self, "regime_score_choppy", score_choppy)
        object.__setattr__(self, "regime_choppy_confirmations", choppy_confirmations)
        object.__setattr__(self, "regime_trend_confirmations", trend_confirmations)
        object.__setattr__(self, "regime_recovery_confirmations", recovery_confirmations)
        object.__setattr__(self, "regime_min_state_hold", min_state_hold)
        object.__setattr__(self, "regime_adx_trend", adx_trend)
        object.__setattr__(self, "regime_adx_choppy", adx_choppy)
        object.__setattr__(self, "regime_hurst_trend", hurst_trend)
        object.__setattr__(self, "regime_hurst_choppy", hurst_choppy)
        object.__setattr__(self, "regime_vol_extreme_pct", vol_extreme_pct)
        object.__setattr__(self, "regime_ewi_slope_trend", ewi_slope_trend)
        object.__setattr__(self, "regime_ewi_slope_choppy", ewi_slope_choppy)
        object.__setattr__(self, "regime_transition_scale", transition_scale)
        object.__setattr__(
            self, "regime_trend_to_transition_confirmations", trend_to_transition
        )
        object.__setattr__(self, "regime_choppy_exit_ratio", choppy_exit_ratio)
        object.__setattr__(
            self, "regime_transition_exit_ratio", transition_exit_ratio
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a complete JSON-friendly portfolio policy snapshot."""
        snapshot = super().as_dict()
        snapshot.update(
            {
                "rearm_trading_days": self.rearm_trading_days,
                "terminal_drawdown": self.terminal_drawdown,
                "candidate_lookbacks": list(self.candidate_lookbacks),
                "candidate_horizons": [
                    list(values) for values in self.candidate_horizons
                ],
                "concentration_drawdown_adjustment": (
                    self.concentration_drawdown_adjustment
                ),
                "candidate_reference_percentile": (self.candidate_reference_percentile),
                "regime_symbols": list(self.regime_symbols),
                "market_regime_enabled": self.market_regime_enabled,
                "regime_ewi_lookback": self.regime_ewi_lookback,
                "regime_breadth_ma_long": self.regime_breadth_ma_long,
                "regime_adx_trend": self.regime_adx_trend,
                "regime_adx_choppy": self.regime_adx_choppy,
                "regime_hurst_window": self.regime_hurst_window,
                "regime_hurst_trend": self.regime_hurst_trend,
                "regime_hurst_choppy": self.regime_hurst_choppy,
                "regime_vol_lookback": self.regime_vol_lookback,
                "regime_vol_extreme_pct": self.regime_vol_extreme_pct,
                "regime_ewi_slope_trend": self.regime_ewi_slope_trend,
                "regime_ewi_slope_choppy": self.regime_ewi_slope_choppy,
                "regime_score_trend": self.regime_score_trend,
                "regime_score_choppy": self.regime_score_choppy,
                "regime_choppy_confirmations": self.regime_choppy_confirmations,
                "regime_trend_confirmations": self.regime_trend_confirmations,
                "regime_recovery_confirmations": self.regime_recovery_confirmations,
                "regime_min_state_hold": self.regime_min_state_hold,
                "regime_transition_scale": self.regime_transition_scale,
                "regime_trend_to_transition_confirmations": (
                    self.regime_trend_to_transition_confirmations
                ),
                "regime_choppy_exit_ratio": self.regime_choppy_exit_ratio,
                "regime_transition_exit_ratio": self.regime_transition_exit_ratio,
            }
        )
        return snapshot


require_positive_ratio = _require_positive_ratio
PortfolioPolicyBase = _PortfolioPolicyBase

__all__ = ["PortfolioPolicy", "PortfolioPolicyBase", "require_positive_ratio"]
