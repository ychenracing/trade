#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex Quant Fusion v16: confirmed defense and horizon diversification.

Version 16 preserves the reviewed v15 signal engine while adding four bounded
upgrades: a two-close confirmed portfolio lock, an emergency one-close lock, a
shadow drawdown alert, and a causal average-daily-volume participation limit.
The default portfolio runs three independent allocation-horizon sleeves without
adding leverage. A single-sleeve compatibility mode remains available.

All risk and liquidity decisions use information available no later than the
decision date. Orders generated from a close still execute no earlier than a later
tradable open under the inherited T+1 contract.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import math
from dataclasses import dataclass, replace
from itertools import pairwise
from typing import Any, cast

import numpy as np
import pandas as pd

import codex_quant_fusion_v14 as v14
import codex_quant_fusion_v15 as v15


DEFAULT_SYMBOLS = v15.DEFAULT_SYMBOLS
PerformanceReport = v15.PerformanceReport
Signal = v15.Signal
BaseStrategy = v15.BaseStrategy


def _require_positive_ratio(
    name: str, value: object, *, inclusive_max: bool = False
) -> float:
    """Reject booleans and return a validated ratio in the interval (0, 1]."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a numeric ratio, not a Boolean")
    return v14._require_positive(
        name,
        value,
        max_value=1.0,
        inclusive_max=inclusive_max,
    )


@dataclass(frozen=True)
class V16Policy:
    """Define version 16 controls independently from the v15 signal parameters."""

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
            raise ValueError("allocation_mode must be either 'single' or 'ensemble'")
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
        confirmations = v14._require_int(
            "drawdown_confirmations", self.drawdown_confirmations, min_value=1
        )
        adv_lookback = v14._require_int("adv_lookback", self.adv_lookback, min_value=1)
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
            v14._require_int(f"{name}[{index}]", value, min_value=1)
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


class ConfirmedPersistentRiskManager(v15.PersistentRiskManager):
    """Require sustained stress unless an emergency threshold is breached."""

    def __init__(self, cfg: dict, policy: V16Policy) -> None:
        super().__init__(cfg)
        self.policy = policy
        self.breach_streak = 0
        self.alert_active = False
        self.audit_events: list[dict[str, Any]] = []

    def _record_alert_state(self, date_str: str, drawdown: float, active: bool) -> None:
        """Record threshold crossings without changing portfolio exposure."""
        event = (
            "portfolio_drawdown_alert_on" if active else "portfolio_drawdown_alert_off"
        )
        self.audit_events.append(
            {
                "date": date_str,
                "event": event,
                "drawdown": float(drawdown),
                "threshold": self.policy.drawdown_alert,
            }
        )

    def _activate_lock(self, date_str: str, drawdown: float, trigger: str) -> str:
        """Persist the hard lock and expose its exact trigger for the audit trail."""
        self.persistent_lock = True
        self.lock_date = date_str
        self.lock_drawdown = float(drawdown)
        self.audit_events.append(
            {
                "date": date_str,
                "event": trigger,
                "drawdown": float(drawdown),
                "breach_streak": int(self.breach_streak),
            }
        )
        return "portfolio drawdown circuit breaker"

    def check_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        trading_dates: list[pd.Timestamp] | None = None,
        date_to_pos: dict[pd.Timestamp, int] | None = None,
    ) -> str | None:
        """Apply shadow alert, sustained confirmation, and emergency lock rules."""
        del trading_dates, date_to_pos
        self.peak_assets = max(self.peak_assets, float(current_assets))
        if self.persistent_lock:
            return "persistent portfolio risk lock"
        if self.peak_assets <= 0:
            return None
        drawdown = (self.peak_assets - current_assets) / self.peak_assets
        above_alert = drawdown >= self.policy.drawdown_alert
        if above_alert != self.alert_active:
            self.alert_active = above_alert
            self._record_alert_state(date_str, drawdown, above_alert)
        if drawdown >= self.policy.emergency_drawdown:
            return self._activate_lock(
                date_str, drawdown, "emergency_portfolio_drawdown_lock"
            )
        self.breach_streak = (
            self.breach_streak + 1 if drawdown >= self.policy.confirmed_drawdown else 0
        )
        if self.breach_streak < self.policy.drawdown_confirmations:
            return None
        return self._activate_lock(
            date_str, drawdown, "confirmed_portfolio_drawdown_lock"
        )

    def drain_audit_events(self) -> list[dict[str, Any]]:
        """Move newly generated manager events into the engine-level audit log."""
        events = list(self.audit_events)
        self.audit_events = []
        return events


class SleeveBacktestEngine(v15.BacktestEngine):
    """Run one independently funded v16 horizon sleeve."""

    ENGINE_LABEL = "Codex Quant v16"

    def __init__(
        self,
        initial_capital: float,
        *,
        cfg: dict | None,
        policy: V16Policy,
        allocation_lookbacks: tuple[int, ...],
        sleeve_name: str,
    ) -> None:
        self.policy = policy
        self.sleeve_name = sleeve_name
        self.ALLOCATION_LOOKBACKS = tuple(allocation_lookbacks)
        self._execution_data_map: dict[str, pd.DataFrame] | None = None
        self._execution_date: pd.Timestamp | None = None
        normalized_cfg = dict(cfg or {})
        normalized_cfg["max_drawdown"] = policy.confirmed_drawdown
        super().__init__(initial_capital=initial_capital, cfg=normalized_cfg)

    def _resolve_symbol_configs(
        self,
        symbols_dict: dict[str, str],
        per_symbol_config: dict[str, dict] | None,
        config_route: str,
    ) -> dict[str, dict]:
        """Install the confirmed manager after inherited route resolution."""
        resolved = super()._resolve_symbol_configs(
            symbols_dict, per_symbol_config, config_route
        )
        symbol_groups = dict(self.risk.symbol_groups)
        self.risk = ConfirmedPersistentRiskManager(self.cfg, self.policy)
        self.risk.configure_groups(symbol_groups)
        return resolved

    def _apply_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        all_dates: list[pd.Timestamp],
        date_to_pos: dict[pd.Timestamp, int],
        pending: list[tuple[Signal, BaseStrategy]],
    ) -> tuple[list[tuple[Signal, BaseStrategy]], bool, bool]:
        """Apply inherited liquidation handling and retain v16 manager events."""
        outcome = super()._apply_portfolio_risk(
            current_assets, date_str, all_dates, date_to_pos, pending
        )
        if isinstance(self.risk, ConfirmedPersistentRiskManager):
            self.risk_events.extend(self.risk.drain_audit_events())
        return outcome

    def _adv_capacity(
        self,
        symbol: str,
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
    ) -> tuple[int, float]:
        """Return a causal lot-rounded capacity from prior daily volumes only."""
        frame = data_map.get(symbol)
        if frame is None or "volume" not in frame.columns:
            return 0, 0.0
        raw_volume = cast(pd.Series, frame.loc[frame.index < date, "volume"])
        history = cast(pd.Series, pd.to_numeric(raw_volume, errors="coerce")).astype(
            "float64"
        )
        history = history.dropna()
        history = history.loc[history.gt(0)].tail(self.policy.adv_lookback)
        if history.empty:
            return 0, 0.0
        adv = float(history.mean())
        capacity = v14._floor_to_lot(adv * self.policy.max_order_adv_ratio)
        return capacity, adv

    def _execute_buy(
        self,
        signal: Signal,
        strategy: BaseStrategy,
        date_str: str,
        data_map: dict[str, pd.DataFrame] | None = None,
        date: pd.Timestamp | None = None,
    ) -> bool:
        """Apply the causal ADV cap before v15 exposure and cash checks."""
        adjusted_signal = signal
        if data_map is not None and date is not None:
            capacity, adv = self._adv_capacity(signal.symbol, data_map, date)
            if capacity <= 0:
                self._record_order_event(
                    date=date_str,
                    signal=signal,
                    event="rejected_no_prior_adv_capacity",
                )
                return False
            if capacity < signal.target_shares:
                adjusted_signal = replace(signal, target_shares=capacity)
                self._record_order_event(
                    date=date_str,
                    signal=signal,
                    event="clipped_to_adv_capacity",
                    requested_shares=int(signal.target_shares),
                    adjusted_shares=int(capacity),
                    prior_adv=adv,
                )
        return super()._execute_buy(adjusted_signal, strategy, date_str, data_map, date)

    def _execute_pending_signals(
        self,
        pending: list[tuple[Signal, BaseStrategy]],
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
        date_to_pos: dict[pd.Timestamp, int],
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Expose execution context so inherited sell calls can use prior ADV."""
        self._execution_data_map = data_map
        self._execution_date = date
        try:
            return super()._execute_pending_signals(
                pending, data_map, date, date_to_pos
            )
        finally:
            self._execution_data_map = None
            self._execution_date = None

    def _execute_sell(
        self, signal: Signal, strategy: BaseStrategy, date_str: str
    ) -> bool:
        """Partially fill an oversized sell and preserve its pending remainder."""
        data_map = self._execution_data_map
        date = self._execution_date
        if data_map is None or date is None:
            return super()._execute_sell(signal, strategy, date_str)
        capacity, adv = self._adv_capacity(signal.symbol, data_map, date)
        if capacity <= 0:
            self._record_order_event(
                date=date_str,
                signal=signal,
                event="deferred_sell_no_prior_adv_capacity",
            )
            return False
        position = self.positions.get(signal.symbol, {}).get(strategy.name)
        shares_before = position.shares if position is not None else 0
        adjusted_signal = signal
        partial = capacity < signal.target_shares
        if partial:
            adjusted_signal = replace(signal, target_shares=capacity)
            self._record_order_event(
                date=date_str,
                signal=signal,
                event="clipped_to_adv_capacity",
                requested_shares=int(signal.target_shares),
                adjusted_shares=int(capacity),
                prior_adv=adv,
            )
        executed = super()._execute_sell(adjusted_signal, strategy, date_str)
        if partial and executed and strategy.position is not None:
            shares_sold = max(shares_before - strategy.position.shares, 0)
            remaining_target = max(signal.target_shares - shares_sold, 0)
            if remaining_target > 0:
                signal.target_shares = remaining_target
                return False
        return executed

    def _build_result(self, final_assets: float, all_dates: list[pd.Timestamp]) -> dict:
        """Add sleeve, risk, and liquidity metadata to the inherited result."""
        result = super()._build_result(final_assets, all_dates)
        result.update(
            {
                "engine_version": "16.0",
                "allocation_mode": "single",
                "sleeve_name": self.sleeve_name,
                "allocation_lookbacks": list(self.ALLOCATION_LOOKBACKS),
                "v16_policy": self._policy_snapshot("single"),
            }
        )
        return result

    def _policy_snapshot(self, allocation_mode: str) -> dict[str, Any]:
        """Report the effective runtime mode instead of only the policy default."""
        snapshot = self.policy.as_dict()
        snapshot["allocation_mode"] = allocation_mode
        return snapshot


@dataclass(frozen=True)
class _RunRequest:
    """Carry an inherited run contract through the ensemble coordinator."""

    symbols_dict: dict[str, str]
    start_date: str
    end_date: str
    per_symbol_config: dict[str, dict] | None
    profile: str | None
    config_route: str
    data_dir: str | None
    indicator_state: str
    warmup_calendar_days: int


class BacktestEngine(SleeveBacktestEngine):
    """Run either one v16 sleeve or an equal-capital ensemble of sleeves."""

    def __init__(
        self,
        initial_capital: float = 2_000_000,
        cfg: dict | None = None,
        policy: V16Policy | None = None,
    ) -> None:
        supplied_policy = policy is not None
        resolved_policy = policy or V16Policy()
        raw_cfg = dict(cfg or {})
        if "max_drawdown" in raw_cfg:
            configured_drawdown = _require_positive_ratio(
                "max_drawdown", raw_cfg["max_drawdown"]
            )
            if supplied_policy and not math.isclose(
                configured_drawdown, resolved_policy.confirmed_drawdown
            ):
                raise ValueError(
                    "cfg['max_drawdown'] conflicts with policy.confirmed_drawdown"
                )
            resolved_policy = replace(
                resolved_policy, confirmed_drawdown=configured_drawdown
            )
        raw_cfg["max_drawdown"] = resolved_policy.confirmed_drawdown
        self._v16_user_cfg = raw_cfg
        self.sleeves: list[SleeveBacktestEngine] = []
        self.last_result: dict | None = None
        super().__init__(
            initial_capital,
            cfg=raw_cfg,
            policy=resolved_policy,
            allocation_lookbacks=resolved_policy.single_lookbacks,
            sleeve_name="single",
        )

    @staticmethod
    def _sleeve_name(index: int, total: int) -> str:
        """Return stable human-readable names for the default three sleeves."""
        if total == 3:
            return ("fast", "base", "slow")[index]
        return f"sleeve_{index + 1}"

    def _run_ensemble(self, request: _RunRequest) -> dict:
        """Run independent ledgers and combine their marked-to-market equity."""
        horizons = self.policy.allocation_horizons
        sleeve_capital = self.initial_capital / len(horizons)
        results: list[dict] = []
        self.sleeves = []
        print(f"\n{'=' * 60}")
        print("Codex Quant v16 ensemble backtest")
        print(f"  Capital: {self.initial_capital:,.0f}")
        print(f"  Sleeves: {len(horizons)}")
        print(f"  Period: {request.start_date} ~ {request.end_date}")
        print(f"{'=' * 60}\n")
        sleeve_policy = replace(
            self.policy,
            allocation_mode="single",
            max_order_adv_ratio=self.policy.max_order_adv_ratio / len(horizons),
        )
        for index, lookbacks in enumerate(horizons):
            capital = (
                sleeve_capital
                if index < len(horizons) - 1
                else self.initial_capital - sleeve_capital * (len(horizons) - 1)
            )
            name = self._sleeve_name(index, len(horizons))
            sleeve = SleeveBacktestEngine(
                capital,
                cfg=self._v16_user_cfg,
                policy=sleeve_policy,
                allocation_lookbacks=lookbacks,
                sleeve_name=name,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                result = sleeve.run(
                    request.symbols_dict,
                    request.start_date,
                    request.end_date,
                    per_symbol_config=request.per_symbol_config,
                    profile=request.profile,
                    config_route=request.config_route,
                    data_dir=request.data_dir,
                    indicator_state=request.indicator_state,
                    warmup_calendar_days=request.warmup_calendar_days,
                )
            self.sleeves.append(sleeve)
            results.append(result)
            print(
                f"  {name:<5} {lookbacks}: final {result['final_assets']:,.0f}, "
                f"return {result['total_return']:.2%}"
            )
            lock_dates = [
                event["date"]
                for event in result.get("risk_events", [])
                if event.get("event") == "persistent_portfolio_risk_lock"
            ]
            if lock_dates:
                print(f"        persistent risk lock: {lock_dates[-1]}")
        combined = self._aggregate_sleeve_results(results)
        self.last_result = combined
        print(
            f"\n  Ensemble completed: initial {self.initial_capital:,.0f} -> "
            f"final assets {combined['final_assets']:,.0f}"
        )
        return combined

    @staticmethod
    def _decorate_trade(trade: v14.TradeRecord, sleeve: str) -> v14.TradeRecord:
        """Preserve the base trade schema while making sleeve ownership explicit."""
        return replace(trade, strategy_name=f"{sleeve}:{trade.strategy_name}")

    @staticmethod
    def _decorate_signal(signal: Signal, sleeve: str) -> Signal:
        """Preserve pending-signal compatibility while adding the sleeve prefix."""
        return replace(signal, strategy_name=f"{sleeve}:{signal.strategy_name}")

    @staticmethod
    def _decorate_events(events: list[dict], sleeve: str) -> list[dict]:
        """Copy audit events and attach their source sleeve."""
        return [{"sleeve": sleeve, **dict(event)} for event in events]

    @staticmethod
    def _sort_events(events: list[dict]) -> list[dict]:
        """Return a chronological deterministic audit trail across all sleeves."""
        return sorted(
            events,
            key=lambda event: (
                str(event.get("date", "")),
                str(event.get("sleeve", "")),
                str(event.get("event", "")),
            ),
        )

    def _aggregate_sleeve_results(self, results: list[dict]) -> dict:
        """Build standard portfolio metrics from independent sleeve ledgers."""
        if not results:
            raise RuntimeError("ensemble produced no sleeve results")
        reference_index = results[0]["equity_curve"].index
        if any(
            not result["equity_curve"].index.equals(reference_index)
            for result in results
        ):
            raise RuntimeError("ensemble sleeves produced different equity calendars")
        equity = pd.DataFrame(index=reference_index)
        for column in ("assets", "cash", "position_value"):
            equity[column] = sum(
                result["equity_curve"][column].astype(float) for result in results
            )
        final_assets = float(equity["assets"].iloc[-1])
        total_return = final_assets / self.initial_capital - 1.0
        trading_days = len(equity)
        annual_return = (
            (1.0 + total_return) ** (252 / max(trading_days, 1)) - 1.0
            if total_return > -1.0
            else -1.0
        )
        peak = equity["assets"].cummax()
        drawdown = (equity["assets"] - peak) / peak
        daily_returns = equity["assets"].pct_change().dropna()
        sharpe = 0.0
        if daily_returns.std() > 0:
            risk_free = float(self.cfg.get("risk_free_rate", 0.0))
            daily_rf = (1.0 + risk_free) ** (1 / 252) - 1.0
            sharpe = float(
                (daily_returns - daily_rf).mean() / daily_returns.std() * math.sqrt(252)
            )
        names = [result["sleeve_name"] for result in results]
        trades = [
            self._decorate_trade(trade, name)
            for name, result in zip(names, results, strict=True)
            for trade in result["trades"]
        ]
        trades.sort(key=lambda trade: (trade.date, trade.symbol, trade.strategy_name))
        sell_trades = [trade for trade in trades if trade.direction == "sell"]
        wins = [trade for trade in sell_trades if trade.pnl > 0]
        losses = [trade for trade in sell_trades if trade.pnl < 0]
        decisive = len(wins) + len(losses)
        total_win = sum(trade.pnl for trade in wins)
        total_loss = abs(sum(trade.pnl for trade in losses))
        givebacks = [
            float(trade.exit_from_peak_pct)
            for trade in sell_trades
            if v14._is_finite_number(trade.exit_from_peak_pct)
        ]
        pending_signals = [
            self._decorate_signal(signal, name)
            for name, result in zip(names, results, strict=True)
            for signal in result.get("pending_signals", [])
        ]
        risk_events = [
            event
            for name, result in zip(names, results, strict=True)
            for event in self._decorate_events(result.get("risk_events", []), name)
        ]
        order_events = [
            event
            for name, result in zip(names, results, strict=True)
            for event in self._decorate_events(result.get("order_events", []), name)
        ]
        fusion_events = [
            event
            for name, result in zip(names, results, strict=True)
            for event in self._decorate_events(result.get("fusion_events", []), name)
        ]
        risk_events = self._sort_events(risk_events)
        order_events = self._sort_events(order_events)
        fusion_events = self._sort_events(fusion_events)
        locked_sleeves = [
            name
            for name, result in zip(names, results, strict=True)
            if result.get("persistent_risk_lock", False)
        ]
        return {
            "engine_version": "16.0",
            "allocation_mode": "ensemble",
            "allocation_lookbacks": [
                list(values) for values in self.policy.allocation_horizons
            ],
            "v16_policy": self._policy_snapshot("ensemble"),
            "portfolio_max_order_adv_ratio": self.policy.max_order_adv_ratio,
            "per_sleeve_max_order_adv_ratio": (
                self.policy.max_order_adv_ratio / len(results)
            ),
            "indicator_state": results[0]["indicator_state"],
            "initial_capital": self.initial_capital,
            "final_assets": final_assets,
            "total_return": total_return,
            "annual_return": annual_return,
            "max_drawdown": float(drawdown.min()),
            "sharpe": sharpe,
            "win_rate": len(wins) / decisive if decisive else 0.0,
            "profit_factor": total_win / total_loss if total_loss > 0 else float("inf"),
            "total_trades": len(trades),
            "sell_trades": len(sell_trades),
            "avg_exit_from_peak": float(np.mean(givebacks)) if givebacks else 0.0,
            "worst_exit_from_peak": float(min(givebacks)) if givebacks else 0.0,
            "open_positions": sum(result["open_positions"] for result in results),
            "open_position_value": sum(
                float(result["open_position_value"]) for result in results
            ),
            "force_close_on_end": all(
                result["force_close_on_end"] for result in results
            ),
            "equity_curve": equity,
            "drawdown_series": drawdown,
            "trades": trades,
            "pending_signals": pending_signals,
            "trade_cash_scope": "sleeve",
            "parameter_routes": dict(results[0].get("parameter_routes", {})),
            "fusion_events": fusion_events,
            "risk_events": risk_events,
            "order_events": order_events,
            "sector_guard_active": any(
                result.get("sector_guard_active", False) for result in results
            ),
            "persistent_risk_lock": any(
                result.get("persistent_risk_lock", False) for result in results
            ),
            "all_sleeves_locked": len(locked_sleeves) == len(results),
            "locked_sleeves": locked_sleeves,
            "reversal_exit_trades": sum(
                int(result.get("reversal_exit_trades", 0)) for result in results
            ),
            "resolved_symbol_configs": {
                name: result.get("resolved_symbol_configs", {})
                for name, result in zip(names, results, strict=True)
            },
            "sleeve_summaries": [
                {
                    "name": name,
                    "allocation_lookbacks": result["allocation_lookbacks"],
                    "initial_capital": result["initial_capital"],
                    "final_assets": result["final_assets"],
                    "total_return": result["total_return"],
                    "max_drawdown": result["max_drawdown"],
                    "persistent_risk_lock": result["persistent_risk_lock"],
                    "max_order_adv_ratio": result["v16_policy"]["max_order_adv_ratio"],
                }
                for name, result in zip(names, results, strict=True)
            ],
        }

    def run(  # noqa: PLR0913 - Keep the inherited public API compatible.
        self,
        symbols_dict: dict[str, str],
        start_date: str,
        end_date: str,
        per_symbol_config: dict[str, dict] | None = None,
        profile: str | None = None,
        config_route: str = "auto",
        data_dir: str | None = None,
        *,
        indicator_state: str = "cold",
        warmup_calendar_days: int = 365,
        allocation_mode: str | None = None,
    ) -> dict:
        """Run the configured single sleeve or the default three-sleeve ensemble."""
        mode = str(allocation_mode or self.policy.allocation_mode).lower()
        if mode not in {"single", "ensemble"}:
            raise ValueError("allocation_mode must be either 'single' or 'ensemble'")
        if mode == "single":
            self.sleeves = [self]
            result = super().run(
                symbols_dict,
                start_date,
                end_date,
                per_symbol_config=per_symbol_config,
                profile=profile,
                config_route=config_route,
                data_dir=data_dir,
                indicator_state=indicator_state,
                warmup_calendar_days=warmup_calendar_days,
            )
            result["allocation_mode"] = "single"
            result["v16_policy"] = self._policy_snapshot("single")
            self.last_result = result
            return result
        return self._run_ensemble(
            _RunRequest(
                symbols_dict=symbols_dict,
                start_date=start_date,
                end_date=end_date,
                per_symbol_config=per_symbol_config,
                profile=profile,
                config_route=config_route,
                data_dir=data_dir,
                indicator_state=indicator_state,
                warmup_calendar_days=warmup_calendar_days,
            )
        )


def main() -> dict | None:
    """Run a version 16 single-sleeve or ensemble backtest from the command line."""
    parser = argparse.ArgumentParser(description="Codex Quant Fusion v16 backtester")
    parser.add_argument(
        "--symbol",
        "-s",
        default=",".join(DEFAULT_SYMBOLS),
        help="Comma-separated six-digit codes or preset stock names",
    )
    parser.add_argument("--start", default="2025-04-01")
    parser.add_argument("--end", default="2026-07-20")
    parser.add_argument("--capital", type=float, default=2_000_000)
    parser.add_argument("--data-dir", default="market_data_qfq")
    parser.add_argument("--indicator-state", choices=["cold", "warm"], default="cold")
    parser.add_argument("--warmup-calendar-days", type=int, default=365)
    parser.add_argument(
        "--allocation-mode", choices=["single", "ensemble"], default="ensemble"
    )
    parser.add_argument("--drawdown-alert", type=float, default=0.14)
    parser.add_argument("--confirmed-drawdown", type=float, default=0.15)
    parser.add_argument("--drawdown-confirmations", type=int, default=2)
    parser.add_argument("--emergency-drawdown", type=float, default=0.175)
    parser.add_argument("--adv-lookback", type=int, default=20)
    parser.add_argument("--max-order-adv-ratio", type=float, default=0.005)
    parser.add_argument("--slippage", type=float, default=0.001)
    parser.add_argument(
        "--profile",
        choices=["default", "semiconductor", "semiconductor_heavy", "aggressive"],
        default="default",
    )
    parser.add_argument("--config-route", choices=["auto", "none"], default="auto")
    parser.add_argument("--save-dir", default="")
    parser.add_argument("--no-plot", action="store_true")
    args = parser.parse_args()
    policy = V16Policy(
        allocation_mode=args.allocation_mode,
        drawdown_alert=args.drawdown_alert,
        confirmed_drawdown=args.confirmed_drawdown,
        drawdown_confirmations=args.drawdown_confirmations,
        emergency_drawdown=args.emergency_drawdown,
        adv_lookback=args.adv_lookback,
        max_order_adv_ratio=args.max_order_adv_ratio,
    )
    symbols = v14.parse_symbols(args.symbol)
    engine = BacktestEngine(
        args.capital,
        cfg={"slippage": args.slippage},
        policy=policy,
    )
    result = engine.run(
        symbols,
        args.start,
        args.end,
        data_dir=args.data_dir or None,
        indicator_state=args.indicator_state,
        warmup_calendar_days=args.warmup_calendar_days,
        profile=args.profile,
        config_route=args.config_route,
    )
    PerformanceReport.print_report(result, symbols)
    if args.save_dir:
        PerformanceReport.save_result(result, args.save_dir)
    if not args.no_plot:
        PerformanceReport.plot_equity_curve(
            result,
            f"equity_curve_v16_{args.indicator_state}_{args.allocation_mode}.png",
        )
    return result


if __name__ == "__main__":
    main()
