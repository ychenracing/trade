"""Validated daily replay orchestration."""

from __future__ import annotations

from quantfusion.execution.c6_receipts import reconcile_close_queue

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


class CoreReplayLoopMixin:
    """Validated daily replay orchestration."""

    @staticmethod
    def _validate_run_request(
        symbols_dict: dict[str, str],
        start_date: str,
        end_date: str,
        profile: str | None,
        config_route: str,
    ) -> tuple[str | None, str, pd.Timestamp, pd.Timestamp]:
        """Validate public run inputs before mutating any engine state."""
        if profile is not None:
            profile = str(profile).lower()
            allowed_profiles = {
                "default",
                "semiconductor",
                "semiconductor_heavy",
                "aggressive",
            }
            if profile not in allowed_profiles:
                raise ValueError(
                    "profile must be one of 'default', 'semiconductor', "
                    f"'semiconductor_heavy', or 'aggressive'; received {profile!r}"
                )
        config_route = str(config_route).lower()
        if config_route not in {"auto", "none"}:
            raise ValueError(
                "config_route must be either 'auto' or 'none'; "
                f"received {config_route!r}"
            )
        if not symbols_dict:
            raise ValueError("symbols_dict must not be empty")
        bad_codes = [
            code
            for code in symbols_dict
            if not isinstance(code, str) or not _SYMBOL_RE.match(code)
        ]
        if bad_codes:
            raise ValueError(
                f"symbols_dict contains an invalid stock code: {bad_codes}"
            )
        start_ts = pd.Timestamp(start_date)
        end_ts = pd.Timestamp(end_date)
        if start_ts > end_ts:
            raise ValueError("start_date must not be later than end_date")
        return profile, config_route, start_ts, end_ts

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
        cache_dir: str | None,
    ) -> tuple[
        dict[str, pd.DataFrame],
        dict[str, dict[str, pd.Series]],
        list[pd.Timestamp],
        dict[pd.Timestamp, int],
    ]:
        """Reset state, load data, compute indicators, and instantiate strategies."""
        self._reset_run_state(symbols_dict)
        display_start, display_end = self._display_run_period(start_date, end_date)
        print(f"\n{'=' * 60}")
        print(f"{self.ENGINE_LABEL} backtest")
        print(f"  Capital: {self.initial_capital:,.0f}")
        print(f"  Symbols: {symbols_dict}")
        print(f"  Period: {display_start} ~ {display_end}")
        print(f"{'=' * 60}\n")
        self._apply_global_profile(profile)
        symbol_configs = self._resolve_symbol_configs(
            symbols_dict, per_symbol_config, config_route
        )
        self.symbol_configs = symbol_configs
        data_map, indicator_map = self._load_market_data(
            symbols_dict,
            symbol_configs,
            start_date,
            end_date,
            start_ts,
            end_ts,
            config_route,
            profile,
            data_dir,
            cache_dir,
        )
        all_dates = sorted(
            {date for frame in data_map.values() for date in frame.index}
        )
        date_to_pos = {pd.Timestamp(date): i for i, date in enumerate(all_dates)}
        self.global_last_date = pd.Timestamp(all_dates[-1])
        self.symbol_last_dates = {
            code: pd.Timestamp(frame.index[-1]) for code, frame in data_map.items()
        }
        print(f"\n  Trading days: {len(all_dates)}")
        self._validate_strategy_templates()
        self.strategy_instances = {
            code: [cls(symbol_configs[code]) for cls in self.strategy_templates]
            for code in symbols_dict
        }
        return data_map, indicator_map, all_dates, date_to_pos

    def _apply_sector_guard_actions(
        self,
        pending: list[tuple[Signal, BaseStrategy]],
        guard_state: str | None,
        date_str: str,
        risk_blocked: bool,
        liquidate: bool,
    ) -> tuple[list[tuple[Signal, BaseStrategy]], bool, bool]:
        """Convert guard state into pending T+1 liquidations and entry blocking."""
        if self.sector_guard_active:
            if guard_state == "triggered":
                print(
                    f"  WARNING [{date_str}] sector breadth deteriorated repeatedly; "
                    "generate T+1 liquidation signals"
                )
            guard_liquidations = self._generate_liquidation_signals(
                date_str, reason="sector breadth risk liquidation"
            )
            pending = self._dedupe_pending_signals(
                [item for item in pending if item[0].direction == "sell"]
                + guard_liquidations
            )
            return pending, True, True
        if guard_state == "recovered":
            print(
                f"  RECOVERED [{date_str}] sector breadth recovered repeatedly; "
                "new entries are allowed from the next trading day"
            )
        return pending, risk_blocked, liquidate

    def _merge_unblocked_daily_signals(
        self,
        symbols_dict: dict[str, str],
        data_map: dict[str, pd.DataFrame],
        indicator_map: dict[str, dict[str, pd.Series]],
        date: pd.Timestamp,
        date_str: str,
        current_assets: float,
        pending: list[tuple[Signal, BaseStrategy]],
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Rank candidates, fuse new signals, and remove conflicting pending buys."""
        # Momentum ranks only allocate scarce slots; they do not create a buy
        # unless an underlying strategy independently emits an entry signal.
        top_symbols = self._select_momentum_candidates(data_map, symbols_dict, date)
        daily_signals = self._collect_strategy_signals(
            symbols_dict,
            data_map,
            indicator_map,
            date,
            date_str,
            current_assets,
            pending,
            allow_buys=True,
            top_symbols=top_symbols,
        )
        fused_daily = self._fuse_daily_signals(daily_signals, date_str)
        sells = {
            (signal.symbol, signal.strategy_name)
            for signal, _ in fused_daily
            if signal.direction == "sell"
        }
        if sells:
            sell_symbols = {symbol for symbol, _ in sells}
            symbol_veto = bool(self.cfg["symbol_level_sell_veto"])
            pending = [
                item
                for item in pending
                if not (
                    item[0].direction == "buy"
                    and (
                        item[0].symbol in sell_symbols
                        if symbol_veto
                        else (item[0].symbol, item[0].strategy_name) in sells
                    )
                )
            ]
        pending.extend(fused_daily)
        return pending

    def _finish_trading_day(
        self,
        pending: list[tuple[Signal, BaseStrategy]],
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
        date_str: str,
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Deduplicate pending instructions and mark the closing portfolio equity."""
        before = list(pending)
        pending = self._dedupe_pending_signals(pending)
        reconcile_close_queue(self, before, pending, date_str, "close_pending_deduplication")
        self._record_equity(data_map, date, date_str)
        return pending

    def _start_trading_day(self) -> None:
        """Freeze the prior-close equity used by the daily loss guard."""
        self.risk.daily_start_assets = (
            self.equity_curve[-1]["assets"]
            if self.equity_curve
            else self.initial_capital
        )

    def _evaluate_trading_day(
        self,
        symbols_dict: dict[str, str],
        data_map: dict[str, pd.DataFrame],
        indicator_map: dict[str, dict[str, pd.Series]],
        all_dates: list[pd.Timestamp],
        date_to_pos: dict[pd.Timestamp, int],
        date: pd.Timestamp,
        pending: list[tuple[Signal, BaseStrategy]],
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Evaluate close-based risk and signals after the opening execution phase."""
        if getattr(self, "_c6_intervention", None) in {"W1_DATA_MAP_ONLY", "W2_POOL_DENOMINATOR_ONLY"}:
            symbols_dict = {code: name for code, name in symbols_dict.items() if code != "601869"}
        before = list(pending)
        date_str = date.strftime("%Y-%m-%d")
        current_assets = self._total_assets(data_map, date)
        pending, risk_blocked, liquidate = self._apply_portfolio_risk(
            current_assets, date_str, all_dates, date_to_pos, pending
        )
        # Portfolio risk and breadth state use the current close, then create
        # orders that cannot execute until a later tradable open.
        guard_state = self._update_sector_guard(data_map, date, all_dates, date_to_pos)
        pending, risk_blocked, liquidate = self._apply_sector_guard_actions(
            pending, guard_state, date_str, risk_blocked, liquidate
        )
        if risk_blocked and not liquidate:
            pending.extend(
                self._collect_strategy_signals(
                    symbols_dict,
                    data_map,
                    indicator_map,
                    date,
                    date_str,
                    current_assets,
                    pending,
                    allow_buys=False,
                )
            )
        elif not risk_blocked:
            pending = self._merge_unblocked_daily_signals(
                symbols_dict,
                data_map,
                indicator_map,
                date,
                date_str,
                current_assets,
                pending,
            )
        reconcile_close_queue(self, before, pending, date_str, "close_risk_or_signal_merge")
        return self._finish_trading_day(pending, data_map, date, date_str)

    def _process_trading_day(
        self,
        symbols_dict: dict[str, str],
        data_map: dict[str, pd.DataFrame],
        indicator_map: dict[str, dict[str, pd.Series]],
        all_dates: list[pd.Timestamp],
        date_to_pos: dict[pd.Timestamp, int],
        date: pd.Timestamp,
        pending: list[tuple[Signal, BaseStrategy]],
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Execute prior-close orders, then evaluate today's close."""
        self._start_trading_day()
        if pending:
            pending = self._execute_pending_signals(
                pending, data_map, date, date_to_pos
            )
        return self._evaluate_trading_day(
            symbols_dict,
            data_map,
            indicator_map,
            all_dates,
            date_to_pos,
            date,
            pending,
        )

    def run(
        self,
        symbols_dict: dict[str, str],
        start_date: str,
        end_date: str,
        per_symbol_config: dict[str, dict] | None = None,
        profile: str | None = None,
        config_route: str = "auto",
        data_dir: str | None = None,
        cache_dir: str | None = None,
    ) -> dict:
        """Run a deterministic backtest over the requested inclusive date range."""
        profile, config_route, start_ts, end_ts = self._validate_run_request(
            symbols_dict, start_date, end_date, profile, config_route
        )
        data_map, indicator_map, all_dates, date_to_pos = self._prepare_run(
            symbols_dict,
            start_date,
            end_date,
            start_ts,
            end_ts,
            per_symbol_config,
            profile,
            config_route,
            data_dir,
            cache_dir,
        )
        # Apply initial risk state when explicitly provided by the caller.
        # Note: quantfusion.application.daily_scan does NOT use this feature —
        # it replays the full history each time to avoid time-direction errors.
        initial_risk = self.cfg.get("_initial_risk_state")
        if initial_risk and initial_risk.get("sector_guard_active", False):
            self.sector_guard_active = True
        pending_signals: list[tuple[Signal, BaseStrategy]] = []
        for date in all_dates:
            pending_signals = self._process_trading_day(
                symbols_dict,
                data_map,
                indicator_map,
                all_dates,
                date_to_pos,
                date,
                pending_signals,
            )
        last_date = all_dates[-1]
        final_assets = self._total_assets(data_map, last_date)
        self.pending_signals = self._dedupe_pending_signals(pending_signals)
        print(
            f"\n  Backtest completed: initial {self.initial_capital:,.0f} -> final assets {final_assets:,.0f}"
        )
        return self._build_result(final_assets, all_dates)

    def _execute_pending_signals(
        self,
        pending: list[tuple[Signal, BaseStrategy]],
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
        date_to_pos: dict[pd.Timestamp, int],
        directions: frozenset[str] | None = None,
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Execute pending orders in a concrete causal execution model."""
        del pending, data_map, date, date_to_pos, directions
        raise NotImplementedError
