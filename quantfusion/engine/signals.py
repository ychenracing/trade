"""Signal normalization, fusion, and per-symbol setup."""

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
    account_order_count,
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
from quantfusion.engine.configuration import EngineConfigurationMixin
from quantfusion.risk.managers import RiskManager
from quantfusion.strategy.trend import (
    ATRChannelStrategy,
    BaseStrategy,
    DualMAStrategy,
    TurtleBreakoutStrategy,
)

_SYMBOL_RE = SYMBOL_RE
_account_order_count = account_order_count
_floor_to_lot = floor_to_lot
_is_finite_number = is_finite_number
_require_bool = require_bool
_require_finite = require_finite
_require_int = require_int
_require_positive = require_positive


class CoreSignalMixin:
    """Signal normalization, fusion, and per-symbol setup."""

    @staticmethod
    def _signal_key(signal: Signal) -> tuple[str, str, str]:
        """Return the identity used to deduplicate pending instructions."""
        return (signal.symbol, signal.strategy_name, signal.direction)

    def _pending_has_buy(
        self, pending: list[tuple[Signal, BaseStrategy]], code: str, strategy_name: str
    ) -> bool:
        """Check for a pending buy from the same symbol and strategy."""
        return any(
            (
                sig.symbol == code
                and sig.strategy_name == strategy_name
                and (sig.direction == "buy")
                for sig, _ in pending
            )
        )

    def _pending_has_sell(
        self, pending: list[tuple[Signal, BaseStrategy]], code: str, strategy_name: str
    ) -> bool:
        """Check for a pending sell from the same symbol and strategy."""
        return any(
            (
                sig.symbol == code
                and sig.strategy_name == strategy_name
                and (sig.direction == "sell")
                for sig, _ in pending
            )
        )

    @staticmethod
    def _dedupe_pending_signals(
        pending: list[tuple[Signal, BaseStrategy]],
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Keep the newest instruction per symbol, strategy, and side."""
        result: dict[tuple[str, str, str], tuple[Signal, BaseStrategy]] = {}
        for signal, strategy in pending:
            if signal.direction not in {"buy", "sell"}:
                continue
            key = CoreSignalMixin._signal_key(signal)
            result[key] = (signal, strategy)
        sell_keys = {
            (s.symbol, s.strategy_name)
            for s, _ in result.values()
            if s.direction == "sell"
        }
        filtered = []
        for key, item in result.items():
            sig, _ = item
            if sig.direction == "buy" and (sig.symbol, sig.strategy_name) in sell_keys:
                continue
            filtered.append(item)
        return filtered

    def _fuse_daily_signals(
        self, daily: list[tuple[Signal, BaseStrategy]], date_str: str
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Resolve buy/sell conflicts and scale same-symbol strategy confirmations."""
        # Strategy positions remain independent for exits and audit trails; only
        # same-day target sizes and conflicting directions are fused here.
        by_symbol: dict[str, list[tuple[Signal, BaseStrategy]]] = {}
        for item in daily:
            by_symbol.setdefault(item[0].symbol, []).append(item)
        fused: list[tuple[Signal, BaseStrategy]] = []
        for symbol, items in by_symbol.items():
            sells = [item for item in items if item[0].direction == "sell"]
            buys = [item for item in items if item[0].direction == "buy"]
            if sells:
                conflict = bool(buys)
                for signal, strategy in sells:
                    label = (
                        "conflict: sell takes priority" if conflict else "exit signal"
                    )
                    reason = f"[{label}] {signal.reason}" if conflict else signal.reason
                    fused.append(
                        (
                            replace(
                                signal,
                                fusion_votes=len(sells),
                                fusion_label=label,
                                reason=reason,
                            ),
                            strategy,
                        )
                    )
                self.fusion_events.append(
                    {
                        "date": date_str,
                        "symbol": symbol,
                        "state": "conflict_sell_first" if conflict else "sell",
                        "buy_votes": len(buys),
                        "sell_votes": len(sells),
                    }
                )
                if bool(self.cfg["symbol_level_sell_veto"]):
                    continue
                selling_strategies = {signal.strategy_name for signal, _ in sells}
                buys = [
                    item
                    for item in buys
                    if item[0].strategy_name not in selling_strategies
                ]
            if not buys:
                continue
            votes = len(buys)
            if votes >= 3:
                label, scale = (
                    "three-strategy confirmation",
                    float(self.cfg["fusion_triple_scale"]),
                )
            elif votes == 2:
                label, scale = (
                    "two-strategy confirmation",
                    float(self.cfg["fusion_double_scale"]),
                )
            else:
                label, scale = (
                    "single-strategy probe",
                    float(self.cfg["fusion_single_scale"]),
                )
            for signal, strategy in buys:
                target_shares = _floor_to_lot(signal.target_shares * scale)
                if target_shares > 0:
                    fused.append(
                        (
                            replace(
                                signal,
                                fusion_votes=votes,
                                fusion_label=label,
                                target_shares=target_shares,
                                reason=f"[{label}] {signal.reason}",
                            ),
                            strategy,
                        )
                    )
            self.fusion_events.append(
                {
                    "date": date_str,
                    "symbol": symbol,
                    "state": label,
                    "buy_votes": votes,
                    "sell_votes": 0,
                    "scale": scale,
                }
            )
        return fused

    def _buy_signal_expired(
        self, signal: Signal, date: pd.Timestamp, date_to_pos: dict[pd.Timestamp, int]
    ) -> bool:
        """Expire an unfilled buy after its trading-day lifetime."""
        if signal.direction != "buy" or not signal.signal_date:
            return False
        signal_ts = pd.Timestamp(signal.signal_date)
        if signal_ts in date_to_pos and date in date_to_pos:
            waited = date_to_pos[date] - date_to_pos[signal_ts]
            return waited > int(self.cfg.get("max_pending_buy_days", 5))
        return False

    @staticmethod
    def _has_pending_liquidation(pending: list[tuple[Signal, BaseStrategy]]) -> bool:
        """Detect risk-generated sells that must keep entries blocked."""
        return any(
            (
                sig.direction == "sell"
                and str(sig.reason)
                in {"circuit breaker liquidation", "sector breadth risk liquidation"}
                for sig, _ in pending
            )
        )

    def _validate_strategy_templates(self) -> None:
        """Reject unnamed or duplicate strategy templates before a run."""
        names: list[str] = []
        for cls in self.strategy_templates:
            name = getattr(cls, "name", "")
            if not isinstance(name, str) or not name:
                raise ValueError(f"Strategy {cls!r} Missing a valid name")
            names.append(name)
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(
                f"strategy_templates contains duplicate strategy names: {duplicates}"
            )

    def _reset_run_state(self, symbols_dict: dict[str, str]) -> None:
        """Reset every mutable ledger and state machine for a fresh run.

        If account-state injection populated ``_initial_positions`` and
        ``_initial_cash`` before this method is called (single-sleeve mode),
        those values are restored after the reset so the engine replays from
        the real portfolio state instead of a zero-position, full-capital start.
        """
        self.cash = (
            self._initial_cash
            if getattr(self, "_initial_cash", None) is not None
            else self.initial_capital
        )
        self.positions = (
            dict(self._initial_positions)
            if getattr(self, "_initial_positions", None)
            else {}
        )
        self.trades = []
        self.equity_curve = []
        self.strategy_instances = {}
        self.external_strategy_instances = {}
        self.symbol_names = dict(symbols_dict)
        self.symbol_last_dates = {}
        self.global_last_date = None
        self.symbol_configs = {}
        self.pending_signals = []
        self.fusion_events = []
        self.risk_events = []
        self.sector_guard_active = False
        self._safe_mode_active = False
        self._sector_shock_positions = []
        self._sector_recovery_streak = 0
        self.cfg = self._validate_config({**self._default_config(), **self._user_cfg})

    def _apply_global_profile(self, profile: str | None) -> None:
        """Layer an optional profile beneath explicit user overrides."""
        factories = {
            "semiconductor": self.semiconductor_config,
            "semiconductor_heavy": self.semiconductor_heavy_config,
            "aggressive": self.optimized_aggressive_config,
        }
        factory = factories.get(profile) if profile is not None else None
        if factory is not None:
            self.cfg = self._validate_config(
                {**self._default_config(), **factory(), **self._user_cfg}
            )

    def _resolve_symbol_configs(
        self,
        symbols_dict: dict[str, str],
        per_symbol_config: dict[str, dict] | None,
        config_route: str,
    ) -> dict[str, dict]:
        """Resolve and validate one effective strategy config per symbol."""
        overrides = per_symbol_config or {}
        if not isinstance(overrides, dict):
            raise ValueError("per_symbol_config must be a dict")
        unknown_overrides = sorted(set(overrides) - set(symbols_dict))
        if unknown_overrides:
            raise ValueError(
                f"per_symbol_configcontains a symbol outside the backtest universe: {unknown_overrides}"
            )
        for code, values in overrides.items():
            if not isinstance(values, dict):
                raise ValueError(f"per_symbol_config[{code}] must be a dict")
            ignored_keys = sorted(set(values) - self._PER_SYMBOL_OVERRIDE_KEYS)
            if ignored_keys:
                raise ValueError(
                    f"per_symbol_config[{code}] contains global-only or unknown keys: {ignored_keys}; set global values through _CoreBacktestEngine(cfg=...)"
                )
        self.risk = RiskManager(self.cfg)
        self.risk.configure_groups(
            {
                code: EngineConfigurationMixin._SYMBOL_GROUP.get(
                    code,
                    "domestic_semiconductor"
                    if EngineConfigurationMixin.classify_symbol(
                        code, name=symbols_dict.get(code, "")
                    )
                    == "semiconductor"
                    else "overseas_compute",
                )
                for code in symbols_dict
            }
        )

        def _base_for(code: str) -> dict:
            if config_route == "auto":
                return EngineConfigurationMixin.config_for_symbol(
                    code,
                    name=symbols_dict.get(code, ""),
                    shrinkage=self.cfg.get("subindustry_shrinkage"),
                )
            return self.cfg

        return {
            code: self._validate_config({**_base_for(code), **overrides.get(code, {})})
            for code in symbols_dict
        }
