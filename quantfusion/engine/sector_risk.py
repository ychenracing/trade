"""Equity tracking, sector defense, and strategy observation."""

from __future__ import annotations

# pyright: reportAttributeAccessIssue=false

# The same stable domain vocabulary is intentionally available to each mixin;
# responsibility is split by behavior, not by duplicating implementations.
# ruff: noqa: F401

import math
from dataclasses import replace
from typing import Any, Callable, ClassVar

import numpy as np
import pandas as pd

from quantfusion.config.engine import default_engine_config
from quantfusion.data.providers import DataFetcher
from quantfusion.domain.models import (
    AccountState,
    BarContext,
    Position,
    SectorObservation,
    Signal,
    TradeRecord,
)
from quantfusion.domain.rules import (
    SYMBOL_RE,
    floor_to_lot,
    is_finite_number,
    require_bool,
    require_finite,
    require_int,
    require_positive,
)
from quantfusion.indicators.technical import Indicators
from quantfusion.risk.managers import RiskManager
from quantfusion.strategy.trend import (
    ATRChannelStrategy,
    BaseStrategy,
    DualMAStrategy,
    TurtleBreakoutStrategy,
)

_SYMBOL_RE = SYMBOL_RE
_floor_to_lot = floor_to_lot
_is_finite_number = is_finite_number
_require_bool = require_bool
_require_finite = require_finite
_require_int = require_int
_require_positive = require_positive


class CoreSectorRiskMixin:
    """Equity tracking, sector defense, and strategy observation."""

    def _record_equity(
        self, data_map: dict[str, pd.DataFrame], date: pd.Timestamp, date_str: str
    ) -> None:
        """Append one closing mark-to-market portfolio snapshot."""
        assets = self._total_assets(data_map, date)
        self.equity_curve.append(
            {
                "date": date_str,
                "assets": assets,
                "cash": self.cash,
                "position_value": assets - self.cash,
            }
        )

    def _apply_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        all_dates: list[pd.Timestamp],
        date_to_pos: dict[pd.Timestamp, int],
        pending: list[tuple[Signal, BaseStrategy]],
    ) -> tuple[list[tuple[Signal, BaseStrategy]], bool, bool]:
        """Apply a concrete persistent or recoverable portfolio risk policy."""
        del current_assets, date_str, all_dates, date_to_pos, pending
        raise NotImplementedError

    def _update_sector_guard(
        self,
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
        all_dates: list[pd.Timestamp],
        date_to_pos: dict[pd.Timestamp, int],
    ) -> str | None:
        """Update the portfolio breadth risk state using only data visible at the current close."""
        if not bool(self.cfg.get("sector_guard_enabled", True)):
            self.sector_guard_active = False
            return None
        pos = date_to_pos[pd.Timestamp(date)]
        shock_ma = int(self.cfg["sector_shock_ma"])
        recovery_ma = int(self.cfg["sector_recovery_ma"])
        max_ma = max(shock_ma, recovery_ma)
        if pos < max_ma:
            return self._current_sector_guard_state()
        # The breadth guard is a portfolio signal, not a disguised single-stock
        # stop. It remains inactive unless enough symbols have complete data.
        observation = self._build_sector_observation(
            data_map, date, max_ma, shock_ma, recovery_ma
        )
        required = int(self.cfg["sector_guard_min_symbols"])
        observed = observation.symbol_count if observation is not None else 0
        if observation is None or observed < required:
            # Missing one regime constituent must not erase earlier causal
            # confirmations or release an active defense. Old shocks still age
            # out normally; recovery simply pauses until quorum returns.
            self._trim_sector_shock_window(pos)
            self.risk_events.append(
                {
                    "date": date.strftime("%Y-%m-%d"),
                    "event": "sector_guard_data_insufficient",
                    "observed_symbols": observed,
                    "required_symbols": required,
                    "guard_active": bool(self.sector_guard_active),
                }
            )
            return self._current_sector_guard_state()
        shock = self._is_sector_shock(observation)
        if shock:
            self._record_sector_shock(date, pos, observation)
        self._trim_sector_shock_window(pos)
        if not self.sector_guard_active:
            return self._try_activate_sector_guard(date, observation)
        recovery = self._is_sector_recovery(
            observation, shock, all_dates, pos, recovery_ma
        )
        self._sector_recovery_streak = (
            self._sector_recovery_streak + 1 if recovery else 0
        )
        if self._sector_recovery_streak < int(
            self.cfg["sector_recovery_confirmations"]
        ):
            return "active"
        self.sector_guard_active = False
        self._sector_recovery_streak = 0
        self._sector_shock_positions = []
        self.risk_events.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "event": "sector_guard_off",
                "equal_weight_return": observation.equal_return,
                "breadth": observation.recovery_breadth,
            }
        )
        return "recovered"

    def _current_sector_guard_state(self) -> str | None:
        """Translate the current guard flag into the run-loop state contract."""
        return "active" if self.sector_guard_active else None

    @staticmethod
    def _build_sector_observation(
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
        max_ma: int,
        shock_ma: int,
        recovery_ma: int,
    ) -> SectorObservation | None:
        """Build one equal-weight breadth snapshot without looking past date."""
        daily_returns: list[float] = []
        above_shock_ma: list[bool] = []
        above_recovery_ma: list[bool] = []
        normalized_series: list[pd.Series] = []
        for df in data_map.values():
            history = df.loc[df.index <= date, "close"].dropna().astype(float)
            if len(history) <= max_ma or date not in history.index:
                continue
            current = float(history.iloc[-1])
            previous = float(history.iloc[-2])
            if current <= 0 or previous <= 0:
                continue
            daily_returns.append(current / previous - 1.0)
            above_shock_ma.append(current > float(history.tail(shock_ma).mean()))
            above_recovery_ma.append(current > float(history.tail(recovery_ma).mean()))
            normalized_series.append(history / float(history.iloc[0]))
        if not daily_returns:
            return None
        return SectorObservation(
            symbol_count=len(daily_returns),
            equal_return=float(np.mean(daily_returns)),
            shock_breadth=float(np.mean(above_shock_ma)),
            recovery_breadth=float(np.mean(above_recovery_ma)),
            normalized_series=tuple(normalized_series),
        )

    def _is_sector_shock(self, observation: SectorObservation) -> bool:
        """Require both a severe equal-weight loss and collapsed breadth."""
        # A shock requires both a large equal-weight loss and collapsed breadth.
        return observation.equal_return <= float(
            self.cfg["sector_shock_return"]
        ) and observation.shock_breadth <= float(self.cfg["sector_shock_breadth"])

    def _record_sector_shock(
        self, date: pd.Timestamp, pos: int, observation: SectorObservation
    ) -> None:
        """Append one shock occurrence to the rolling window and audit log."""
        self._sector_shock_positions.append(pos)
        self.risk_events.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "event": "sector_shock",
                "equal_weight_return": observation.equal_return,
                "breadth": observation.shock_breadth,
            }
        )

    def _trim_sector_shock_window(self, pos: int) -> None:
        """Discard shock confirmations older than the configured trading window."""
        window = int(self.cfg["sector_shock_window"])
        self._sector_shock_positions = [
            p for p in self._sector_shock_positions if p >= pos - window + 1
        ]

    def _try_activate_sector_guard(
        self, date: pd.Timestamp, observation: SectorObservation
    ) -> str | None:
        """Activate defense only after the configured shock count is confirmed."""
        # Multiple shocks inside a rolling trading-day window reduce the chance
        # that an isolated correction forces a full portfolio liquidation.
        if len(self._sector_shock_positions) < int(
            self.cfg["sector_shock_confirmations"]
        ):
            return None
        self.sector_guard_active = True
        self._sector_recovery_streak = 0
        self.risk_events.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "event": "sector_guard_on",
                "shock_count": len(self._sector_shock_positions),
                "equal_weight_return": observation.equal_return,
                "breadth": observation.shock_breadth,
            }
        )
        return "triggered"

    def _is_sector_recovery(
        self,
        observation: SectorObservation,
        shock: bool,
        all_dates: list[pd.Timestamp],
        pos: int,
        recovery_ma: int,
    ) -> bool:
        """Require positive return, broad participation, and sector trend repair."""
        # Recovery is deliberately asymmetric and slower than entry into defense.
        # It requires a positive day, broad participation, and sector trend repair.
        recent_dates = all_dates[max(0, pos - recovery_ma + 1) : pos + 1]
        sector_levels: list[float] = []
        for d in recent_dates:
            values = [
                float(series.loc[d])
                for series in observation.normalized_series
                if d in series.index
            ]
            if values:
                sector_levels.append(float(np.mean(values)))
        sector_above_ma = len(sector_levels) >= recovery_ma and sector_levels[
            -1
        ] > float(np.mean(sector_levels))
        return (
            not shock
            and observation.equal_return > 0
            and (
                observation.recovery_breadth
                >= float(self.cfg["sector_recovery_breadth"])
            )
            and sector_above_ma
        )

    def _collect_strategy_signals(
        self,
        symbols_dict: dict[str, str],
        data_map: dict[str, pd.DataFrame],
        ind_map: dict[str, dict[str, pd.Series]],
        date: pd.Timestamp,
        date_str: str,
        current_assets: float,
        pending: list[tuple[Signal, BaseStrategy]],
        allow_buys: bool,
        top_symbols: set[str] | None = None,
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Collect one close-generated instruction per eligible strategy."""
        held_symbols = set(self.positions)
        daily: list[tuple[Signal, BaseStrategy]] = []
        for code in symbols_dict:
            df = data_map[code]
            if date not in df.index:
                continue
            i = df.index.get_loc(date)
            for strategy in self.strategy_instances[code]:
                ctx = BarContext(
                    i=i,
                    df=df,
                    current_assets=current_assets,
                    indicators=ind_map[code],
                    symbol=code,
                    date=date_str,
                )
                signal = strategy.on_bar(ctx)
                if signal is None:
                    continue
                if signal.direction == "buy":
                    if not allow_buys or self._pending_has_buy(
                        pending, code, strategy.name
                    ):
                        continue
                    if (
                        top_symbols is not None
                        and code not in top_symbols
                        and (code not in held_symbols)
                    ):
                        continue
                elif signal.direction == "sell":
                    if self._pending_has_sell(pending, code, strategy.name):
                        continue
                else:
                    continue
                daily.append((signal, strategy))
        return daily
