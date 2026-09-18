"""Equity tracking, sector defense, and strategy observation."""

from __future__ import annotations

# pyright: reportAttributeAccessIssue=false


import numpy as np
import pandas as pd

from quantfusion.domain.models import BarContext, SectorObservation, Signal
from quantfusion.strategy.trend import BaseStrategy, EARLY_DUAL_TRANSITION_RSI_MAX


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
    def _drop_worst_indices(values: list[float], drop: int = 1) -> list[int]:
        """Return keep-indices after dropping the ``drop`` worst (minimum) values.

        Used so equal-weight return and breadth share the same membership when
        hardening the sector observation against a single-name blowup.
        """
        if drop <= 0 or len(values) <= drop:
            return list(range(len(values)))
        order = sorted(range(len(values)), key=lambda i: values[i])
        drop_set = set(order[:drop])
        return [i for i in range(len(values)) if i not in drop_set]

    @staticmethod
    def _build_sector_observation(
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
        max_ma: int,
        shock_ma: int,
        recovery_ma: int,
    ) -> SectorObservation | None:
        """Build one robust equal-weight breadth snapshot without looking past date.

        Exact robust rule (Experiment D)
        --------------------------------
        1. Observe every fully-populated regime name as before (quorum
           ``symbol_count`` still counts all of them).
        2. Identify the single worst daily return (minimum ``close_t/close_{t-1}-1``).
        3. Drop that one name before aggregating:
           - ``equal_return`` = mean of remaining daily returns
           - ``shock_breadth`` / ``recovery_breadth`` = mean of remaining MA flags
           - ``normalized_series`` = remaining series only (recovery sector-MA
             therefore uses the same robust membership)
        4. If fewer than 2 names are observed, no trim is applied (plain mean).
        5. Shock/recovery confirmation counts and numeric thresholds are unchanged;
           a single-name blowup can no longer alone push equal-weight return /
           breadth through the shock gates.
        """
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
        keep = CoreSectorRiskMixin._drop_worst_indices(daily_returns, drop=1)
        kept_returns = [daily_returns[i] for i in keep]
        kept_shock = [above_shock_ma[i] for i in keep]
        kept_recovery = [above_recovery_ma[i] for i in keep]
        kept_series = tuple(normalized_series[i] for i in keep)
        return SectorObservation(
            symbol_count=len(daily_returns),
            equal_return=float(np.mean(kept_returns)),
            shock_breadth=float(np.mean(kept_shock)),
            recovery_breadth=float(np.mean(kept_recovery)),
            normalized_series=kept_series,
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
                    rsi = ind_map[code].get("rsi")
                    signal_day_rsi = (
                        float(rsi.iloc[i])
                        if rsi is not None and not pd.isna(rsi.iloc[i])
                        else None
                    )
                    early_dual_transition = (
                        len(self._tradable_symbol_codes)
                        > int(
                            getattr(
                                self,
                                "_portfolio_max_positions",
                                self.cfg.get("max_positions", 6),
                            )
                        )
                        and strategy.name == "dual_ma"
                        and signal_day_rsi is not None
                        and np.isfinite(signal_day_rsi)
                        and signal_day_rsi <= EARLY_DUAL_TRANSITION_RSI_MAX
                    )
                    if (
                        top_symbols is not None
                        and code not in top_symbols
                        and (code not in held_symbols)
                        and not early_dual_transition
                    ):
                        continue
                elif signal.direction == "sell":
                    if self._pending_has_sell(pending, code, strategy.name):
                        continue
                else:
                    continue
                daily.append((signal, strategy))
        return daily
