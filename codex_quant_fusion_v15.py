#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex Quant Fusion v15: performance-preserving execution and risk upgrades.

Version 15 keeps the reviewed v14 signal engine and its default cold-start behavior,
but replaces the arbitrary symbol execution list with a causal risk-adjusted momentum
ensemble. It also fixes routed-configuration precedence, clips oversized orders to
remaining exposure capacity, disables non-causal end-of-data settlement by default,
and turns a portfolio drawdown breach into an auditable persistent risk lock.

The optional warm indicator state exists for robustness analysis. Cold and warm runs
answer different questions and must never be presented as interchangeable results.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from typing import Any

import numpy as np
import pandas as pd

import codex_quant_fusion_v14 as v14


DEFAULT_SYMBOLS = v14.DEFAULT_SYMBOLS
PerformanceReport = v14.PerformanceReport
Signal = v14.Signal
BaseStrategy = v14.BaseStrategy


class PersistentRiskManager(v14.RiskManager):
    """Keep a breached portfolio locked until an explicit operator reset.

    A fixed backtest cannot model an investment committee or operator decision. The
    conservative default is therefore to stay in cash after the lifetime drawdown
    threshold is crossed. A new engine instance is the explicit reset boundary.
    """

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.persistent_lock = False
        self.lock_date: str | None = None
        self.lock_drawdown = 0.0

    def check_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        trading_dates: list[pd.Timestamp] | None = None,
        date_to_pos: dict[pd.Timestamp, int] | None = None,
    ) -> str | None:
        """Trigger once at the lifetime high-water drawdown and then block entries."""
        del trading_dates, date_to_pos
        self.peak_assets = max(self.peak_assets, float(current_assets))
        if self.persistent_lock:
            return "persistent portfolio risk lock"
        if self.peak_assets <= 0:
            return None
        drawdown = (self.peak_assets - current_assets) / self.peak_assets
        if drawdown < float(self.cfg.get("max_drawdown", 0.2)):
            return None
        self.persistent_lock = True
        self.lock_date = date_str
        self.lock_drawdown = float(drawdown)
        return "portfolio drawdown circuit breaker"


class BacktestEngine(v14.BacktestEngine):
    """Run v14 signals with causal allocation, explicit state, and durable defense."""

    ENGINE_LABEL = "Codex Quant v15"
    ALLOCATION_LOOKBACKS = (5, 10, 20)

    def __init__(
        self, initial_capital: float = 2_000_000, cfg: dict | None = None
    ) -> None:
        super().__init__(initial_capital=initial_capital, cfg=cfg)
        self.order_events: list[dict[str, Any]] = []
        self._profile_strategy_overrides: dict[str, Any] = {}
        self._indicator_state = "cold"
        self._warmup_calendar_days = 365
        self._requested_start_date = ""
        self._requested_end_date = ""
        self._risk_lock_logged = False

    def _display_run_period(self, start_date: str, end_date: str) -> tuple[str, str]:
        """Show the requested trading window, not the optional warmup window."""
        if self._requested_start_date and self._requested_end_date:
            return self._requested_start_date, self._requested_end_date
        return start_date, end_date

    @staticmethod
    def _default_config() -> dict:
        """Return v14 defaults with non-causal data-end settlement disabled."""
        cfg = v14.BacktestEngine._default_config()
        cfg["close_position_on_data_end"] = False
        return cfg

    @staticmethod
    def _validate_config(cfg: dict) -> dict:
        """Normalize legacy data-end settlement off before inherited validation."""
        normalized = dict(cfg)
        normalized["close_position_on_data_end"] = False
        return v14.BacktestEngine._validate_config(normalized)

    def _reset_run_state(self, symbols_dict: dict[str, str]) -> None:
        """Reset v15 audit and profile state together with the inherited ledger."""
        super()._reset_run_state(symbols_dict)
        self.order_events = []
        self._profile_strategy_overrides = {}
        self._risk_lock_logged = False

    def _apply_global_profile(self, profile: str | None) -> None:
        """Apply a profile and remember its strategy-level routed overrides."""
        super()._apply_global_profile(profile)
        factories = {
            "semiconductor": self.semiconductor_config,
            "semiconductor_heavy": self.semiconductor_heavy_config,
            "aggressive": self.optimized_aggressive_config,
        }
        factory = factories.get(profile)
        if factory is None:
            self._profile_strategy_overrides = {}
            return
        defaults = self._default_config()
        profile_cfg = factory()
        self._profile_strategy_overrides = {
            key: profile_cfg[key]
            for key in self._PER_SYMBOL_OVERRIDE_KEYS
            if key in profile_cfg and profile_cfg[key] != defaults.get(key)
        }

    def _resolve_symbol_configs(
        self,
        symbols_dict: dict[str, str],
        per_symbol_config: dict[str, dict] | None,
        config_route: str,
    ) -> dict[str, dict]:
        """Resolve explicit precedence and install the persistent risk manager."""
        resolved = super()._resolve_symbol_configs(
            symbols_dict, per_symbol_config, config_route
        )
        symbol_groups = dict(self.risk.symbol_groups)
        self.risk = PersistentRiskManager(self.cfg)
        self.risk.configure_groups(symbol_groups)

        explicit_global = {
            key: value
            for key, value in self._user_cfg.items()
            if key in self._PER_SYMBOL_OVERRIDE_KEYS
        }
        route_overrides = {
            **self._profile_strategy_overrides,
            **explicit_global,
        }
        per_symbol = per_symbol_config or {}
        final: dict[str, dict] = {}
        for code, route_cfg in resolved.items():
            final[code] = self._validate_config(
                {
                    **route_cfg,
                    **route_overrides,
                    **per_symbol.get(code, {}),
                    "close_position_on_data_end": False,
                }
            )
        return final

    def _allocation_scores(
        self, data_map: dict[str, pd.DataFrame], date: pd.Timestamp
    ) -> dict[str, float]:
        """Average cross-sectional ranks of causal risk-adjusted momentum signals."""
        raw: dict[int, dict[str, float]] = {
            window: {} for window in self.ALLOCATION_LOOKBACKS
        }
        for code, frame in data_map.items():
            history = frame.loc[frame.index < date, "close"].dropna().astype(float)
            for window in self.ALLOCATION_LOOKBACKS:
                if len(history) <= window:
                    continue
                momentum = float(history.iloc[-1] / history.iloc[-1 - window] - 1.0)
                volatility = float(history.pct_change().tail(window).std())
                score = momentum / volatility if volatility > 0 else momentum
                if np.isfinite(score):
                    raw[window][code] = score

        scores = {code: 0.0 for code in data_map}
        observations = {code: 0 for code in data_map}
        for values in raw.values():
            if not values:
                continue
            ranks = pd.Series(values, dtype="float64").rank(pct=True)
            for code, rank in ranks.items():
                scores[code] += float(rank)
                observations[code] += 1
        return {
            code: scores[code] / observations[code] if observations[code] else 0.0
            for code in scores
        }

    def _record_order_event(
        self,
        *,
        date: str,
        signal: Signal,
        event: str,
        **details: Any,
    ) -> None:
        """Append a compact, serializable order decision to the audit trail."""
        self.order_events.append(
            {
                "date": date,
                "symbol": signal.symbol,
                "strategy": signal.strategy_name,
                "direction": signal.direction,
                "event": event,
                "signal_date": signal.signal_date,
                **details,
            }
        )

    def _remaining_buy_capacity(
        self,
        signal: Signal,
        strategy: BaseStrategy,
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
    ) -> tuple[float, float]:
        """Return execution-day equity and remaining currency exposure capacity."""
        prices = self._execution_mark_prices(data_map, date)
        current_assets = self._total_assets_at_prices(prices)
        if current_assets <= 0 or signal.symbol not in prices:
            return current_assets, 0.0

        def position_value(code: str) -> float:
            price = prices.get(code)
            if price is None or price <= 0:
                return 0.0
            return sum(
                position.shares * price
                for position in self.positions.get(code, {}).values()
            )

        symbol_value = position_value(signal.symbol)
        total_value = sum(position_value(code) for code in self.positions)
        symbol_cap = float(strategy.cfg.get("max_symbol_weight", 1.0))
        capacities = [
            current_assets * symbol_cap - symbol_value,
            current_assets * float(self.cfg.get("max_total_weight", 1.0)) - total_value,
        ]
        target_group = self.risk.symbol_groups.get(signal.symbol)
        if target_group:
            group_value = sum(
                position_value(code)
                for code in self.positions
                if self.risk.symbol_groups.get(code) == target_group
            )
            group_cap = float(self.risk.group_weight_limits.get(target_group, 1.0))
            capacities.append(current_assets * group_cap - group_value)
        return current_assets, max(min(capacities), 0.0)

    def _execute_buy(
        self,
        signal: Signal,
        strategy: BaseStrategy,
        date_str: str,
        data_map: dict[str, pd.DataFrame] | None = None,
        date: pd.Timestamp | None = None,
    ) -> bool:
        """Clip an oversized buy to available capacity before inherited checks."""
        adjusted_signal = signal
        if data_map is not None and date is not None and signal.price > 0:
            _, capacity = self._remaining_buy_capacity(signal, strategy, data_map, date)
            execution_price = float(signal.price) * (
                1.0 + float(self.cfg.get("slippage", 0.001))
            )
            capacity_shares = v14._floor_to_lot(capacity / execution_price)
            if capacity_shares < signal.target_shares:
                if capacity_shares <= 0:
                    self._record_order_event(
                        date=date_str,
                        signal=signal,
                        event="rejected_no_exposure_capacity",
                    )
                    return False
                adjusted_signal = replace(signal, target_shares=capacity_shares)
                self._record_order_event(
                    date=date_str,
                    signal=signal,
                    event="clipped_to_exposure_capacity",
                    requested_shares=int(signal.target_shares),
                    adjusted_shares=int(capacity_shares),
                )
        executed = super()._execute_buy(
            adjusted_signal, strategy, date_str, data_map, date
        )
        if not executed:
            self._record_order_event(
                date=date_str,
                signal=adjusted_signal,
                event="rejected_by_execution_checks",
            )
        return executed

    def _execute_pending_signals(
        self,
        pending: list[tuple[Signal, BaseStrategy]],
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
        date_to_pos: dict[pd.Timestamp, int],
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Execute sells first, then rank buys by prior-close allocation scores."""
        date_str = date.strftime("%Y-%m-%d")
        strategy_rank = {"turtle_breakout": 0, "dual_ma": 1, "atr_channel": 2}
        allocation_scores = self._allocation_scores(data_map, date)
        sorted_pending = sorted(
            pending,
            key=lambda item: (
                0 if item[0].direction == "sell" else 1,
                -allocation_scores.get(item[0].symbol, 0.0),
                item[0].symbol,
                strategy_rank.get(item[0].strategy_name, 99),
            ),
        )
        unexecuted: list[tuple[Signal, BaseStrategy]] = []
        for signal, strategy in sorted_pending:
            code = signal.symbol
            if self._buy_signal_expired(signal, date, date_to_pos):
                self._record_order_event(
                    date=date_str, signal=signal, event="expired_pending_buy"
                )
                continue
            if code not in data_map or date not in data_map[code].index:
                unexecuted.append((signal, strategy))
                continue
            open_price = data_map[code].loc[date, "open"]
            if pd.isna(open_price) or open_price <= 0:
                unexecuted.append((signal, strategy))
                continue
            limit_state = self._opening_limit_state(
                signal, data_map[code], date, float(open_price)
            )
            if limit_state == "buy_blocked":
                self._record_order_event(
                    date=date_str, signal=signal, event="rejected_limit_up_open"
                )
                continue
            if limit_state == "sell_blocked":
                unexecuted.append((signal, strategy))
                continue
            executable_signal = replace(signal, price=float(open_price))
            if executable_signal.direction == "buy":
                self._execute_buy(executable_signal, strategy, date_str, data_map, date)
            elif executable_signal.direction == "sell":
                executed = self._execute_sell(executable_signal, strategy, date_str)
                if not executed and strategy.position is not None:
                    unexecuted.append((executable_signal, strategy))
        return self._dedupe_pending_signals(unexecuted)

    def _opening_limit_state(
        self,
        signal: Signal,
        frame: pd.DataFrame,
        date: pd.Timestamp,
        open_price: float,
    ) -> str | None:
        """Classify an opening board limit without consuming a pending sell."""
        location = frame.index.get_loc(date)
        if location <= 0:
            return None
        previous_close = float(frame.iloc[location - 1]["close"])
        if previous_close <= 0:
            return None
        change = (open_price - previous_close) / previous_close
        limit_up = v14._limit_pct_for_code(
            signal.symbol, self.cfg, self.symbol_names.get(signal.symbol, "")
        )
        epsilon = float(self.cfg.get("limit_price_epsilon", 0.001))
        if signal.direction == "buy" and change >= limit_up - epsilon:
            return "buy_blocked"
        if signal.direction == "sell" and change <= -limit_up + epsilon:
            return "sell_blocked"
        return None

    def _close_positions_on_data_end(
        self, data_map: dict[str, pd.DataFrame], date: pd.Timestamp
    ) -> set[tuple[str, str]]:
        """Never infer a tradable liquidation event from missing future rows."""
        del data_map, date
        return set()

    def _apply_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        all_dates: list[pd.Timestamp],
        date_to_pos: dict[pd.Timestamp, int],
        pending: list[tuple[Signal, BaseStrategy]],
    ) -> tuple[list[tuple[Signal, BaseStrategy]], bool, bool]:
        """Apply T+1 liquidation and accurately report the durable risk lock."""
        risk_status = self.risk.check_portfolio_risk(
            current_assets,
            date_str,
            trading_dates=all_dates,
            date_to_pos=date_to_pos,
        )
        if risk_status is None and self.risk.check_daily_loss(current_assets):
            risk_status = "daily loss limit"
        risk_blocked = self._has_pending_liquidation(pending)
        if risk_blocked:
            risk_status = risk_status or "circuit breaker liquidation pending"
        if not risk_status:
            return pending, risk_blocked, False

        liquidate = False
        if risk_status == "portfolio drawdown circuit breaker":
            liquidate = bool(self.cfg.get("liquidate_on_circuit_breaker", True))
            if liquidate:
                print(
                    f"  WARNING [{date_str}] {risk_status}: generate T+1 "
                    "liquidation signals and enter a persistent risk lock"
                )
                liquidation_signals = self._generate_liquidation_signals(date_str)
                pending = self._dedupe_pending_signals(
                    [item for item in pending if item[0].direction == "sell"]
                    + liquidation_signals
                )
            else:
                print(
                    f"  WARNING [{date_str}] {risk_status}: block new entries "
                    "under a persistent risk lock"
                )
        if (
            isinstance(self.risk, PersistentRiskManager)
            and self.risk.persistent_lock
            and not self._risk_lock_logged
        ):
            self.risk_events.append(
                {
                    "date": self.risk.lock_date or date_str,
                    "event": "persistent_portfolio_risk_lock",
                    "drawdown": self.risk.lock_drawdown,
                }
            )
            self._risk_lock_logged = True
        return pending, True, liquidate

    def _prepare_run(
        self,
        symbols_dict: dict[str, str],
        start_date: str,
        end_date: str,
        start_ts: pd.Timestamp,
        end_ts: pd.Timestamp,
        per_symbol_config: dict[str, dict] | None,
        profile: str | None,
        config_route: str,
        data_dir: str | None,
    ) -> tuple[
        dict[str, pd.DataFrame],
        dict[str, dict[str, pd.Series]],
        list[pd.Timestamp],
        dict[pd.Timestamp, int],
    ]:
        """Optionally compute indicators on pre-start history while trading flat."""
        if self._indicator_state == "cold":
            return super()._prepare_run(
                symbols_dict,
                start_date,
                end_date,
                start_ts,
                end_ts,
                per_symbol_config,
                profile,
                config_route,
                data_dir,
            )
        warm_start = start_ts - pd.Timedelta(days=self._warmup_calendar_days)
        data_map, indicator_map, _, _ = super()._prepare_run(
            symbols_dict,
            warm_start.strftime("%Y-%m-%d"),
            end_date,
            warm_start,
            end_ts,
            per_symbol_config,
            profile,
            config_route,
            data_dir,
        )
        trading_dates = sorted(
            {
                date
                for frame in data_map.values()
                for date in frame.index
                if start_ts <= date <= end_ts
            }
        )
        date_to_pos = {
            pd.Timestamp(date): index for index, date in enumerate(trading_dates)
        }
        return data_map, indicator_map, trading_dates, date_to_pos

    def run(
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
    ) -> dict:
        """Run v15 with an explicit cold or warm indicator-state contract."""
        indicator_state = str(indicator_state).lower()
        if indicator_state not in {"cold", "warm"}:
            raise ValueError("indicator_state must be either 'cold' or 'warm'")
        warmup_calendar_days = v14._require_int(
            "warmup_calendar_days", warmup_calendar_days, min_value=120
        )
        self._indicator_state = indicator_state
        self._warmup_calendar_days = warmup_calendar_days
        self._requested_start_date = start_date
        self._requested_end_date = end_date
        return super().run(
            symbols_dict,
            start_date,
            end_date,
            per_symbol_config=per_symbol_config,
            profile=profile,
            config_route=config_route,
            data_dir=data_dir,
        )

    def _build_result(self, final_assets: float, all_dates: list[pd.Timestamp]) -> dict:
        """Extend the inherited report with allocation and resolved-config audits."""
        result = super()._build_result(final_assets, all_dates)
        result.update(
            {
                "engine_version": "15.0",
                "indicator_state": self._indicator_state,
                "allocation_lookbacks": list(self.ALLOCATION_LOOKBACKS),
                "order_events": list(self.order_events),
                "resolved_symbol_configs": {
                    code: dict(config) for code, config in self.symbol_configs.items()
                },
                "persistent_risk_lock": bool(
                    isinstance(self.risk, PersistentRiskManager)
                    and self.risk.persistent_lock
                ),
            }
        )
        return result


def main() -> dict | None:
    """Run a v15 backtest from the command line."""
    parser = argparse.ArgumentParser(description="Codex Quant Fusion v15 backtester")
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
        "--profile",
        choices=["default", "semiconductor", "semiconductor_heavy", "aggressive"],
        default="default",
    )
    parser.add_argument("--config-route", choices=["auto", "none"], default="auto")
    parser.add_argument("--save-dir", default="")
    parser.add_argument("--no-plot", action="store_true")
    args = parser.parse_args()
    symbols = v14.parse_symbols(args.symbol)
    engine = BacktestEngine(args.capital)
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
            f"equity_curve_v15_{args.indicator_state}_{args.config_route}.png",
        )
    return result


if __name__ == "__main__":
    main()
