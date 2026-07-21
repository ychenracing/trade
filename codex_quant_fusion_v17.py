#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex Quant Fusion v17: universe-size-independent risk coordination.

Version 17 keeps the reviewed v16 execution contract and signal engines while
removing three sources of universe-size sensitivity:

* the breadth guard uses a fixed signal-only regime basket, independent of the
  number of tradable symbols;
* candidate strength is measured against the same signal-only reference basket,
  so adding a tradable symbol cannot rescale existing scores;
* one- and two-symbol portfolios switch from degenerate cross-sectional ranks to
  a time-series trend contract;
* ordinary drawdown locks can rearm after a cash cooldown, while a separate
  lifetime drawdown threshold remains a terminal safety boundary.

The strategy uses one parameter set for every universe size. Adding a symbol may
change causal rankings, but it does not silently change the risk model.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import math
import re
from dataclasses import dataclass, replace
from typing import Any

import pandas as pd

import codex_quant_fusion_v14 as v14
import codex_quant_fusion_v15 as v15
import codex_quant_fusion_v16 as v16


DEFAULT_SYMBOLS = v16.DEFAULT_SYMBOLS
PerformanceReport = v16.PerformanceReport
Signal = v16.Signal
BaseStrategy = v16.BaseStrategy


@dataclass(frozen=True)
class V17Policy(v16.V16Policy):
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

    def __post_init__(self) -> None:
        """Validate inherited controls and the v17 recovery constraints."""
        super().__post_init__()
        rearm_days = v14._require_int(
            "rearm_trading_days", self.rearm_trading_days, min_value=1
        )
        terminal = v14._require_positive(
            "terminal_drawdown", self.terminal_drawdown, max_value=1.0
        )
        if terminal < self.confirmed_drawdown:
            raise ValueError("terminal_drawdown must not be below confirmed_drawdown")
        concentration_adjustment = v14._require_finite(
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
        reference_percentile = v14._require_finite(
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

    def as_dict(self) -> dict[str, Any]:
        """Return a complete JSON-friendly v17 policy snapshot."""
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
            }
        )
        return snapshot


class RecoverableDrawdownRiskManager(v16.ConfirmedPersistentRiskManager):
    """Rearm cycle locks after a cooldown but preserve a lifetime hard stop."""

    def __init__(self, cfg: dict, policy: V17Policy) -> None:
        super().__init__(cfg, policy)
        self.policy = policy
        self.lifetime_peak_assets = 0.0
        self.lock_start_position: int | None = None
        self.terminal_lock = False
        self.cycle_lock_count = 0

    @staticmethod
    def _date_position(
        date_str: str,
        trading_dates: list[pd.Timestamp] | None,
        date_to_pos: dict[pd.Timestamp, int] | None,
    ) -> int | None:
        """Resolve the current trading position without calendar-day assumptions."""
        try:
            timestamp = pd.Timestamp(date_str)
        except (TypeError, ValueError):
            return None
        if not isinstance(timestamp, pd.Timestamp):
            return None
        if date_to_pos is not None:
            return date_to_pos.get(timestamp)
        if trading_dates is None:
            return None
        try:
            return trading_dates.index(timestamp)
        except ValueError:
            return None

    def _activate_cycle_lock(
        self, date_str: str, drawdown: float, position: int | None, trigger: str
    ) -> str:
        """Enter a temporary cash lock and record its exact causal trigger."""
        self.persistent_lock = True
        self.terminal_lock = False
        self.lock_date = date_str
        self.lock_drawdown = float(drawdown)
        self.lock_start_position = position
        self.cycle_lock_count += 1
        self.audit_events.append(
            {
                "date": date_str,
                "event": trigger,
                "drawdown": float(drawdown),
                "breach_streak": int(self.breach_streak),
                "cycle_lock_count": int(self.cycle_lock_count),
            }
        )
        return "portfolio drawdown circuit breaker"

    def _activate_terminal_lock(
        self, date_str: str, drawdown: float, position: int | None
    ) -> str:
        """Enter the non-rearming lifetime safety lock."""
        self.persistent_lock = True
        self.terminal_lock = True
        self.lock_date = date_str
        self.lock_drawdown = float(drawdown)
        self.lock_start_position = position
        self.audit_events.append(
            {
                "date": date_str,
                "event": "terminal_portfolio_drawdown_lock",
                "drawdown": float(drawdown),
                "threshold": self.policy.terminal_drawdown,
            }
        )
        return "portfolio drawdown circuit breaker"

    def _try_rearm(
        self, date_str: str, current_assets: float, position: int | None
    ) -> bool:
        """Reset the cycle high-water mark after the required cash cooldown."""
        if self.terminal_lock or self.lock_start_position is None or position is None:
            return False
        elapsed = position - self.lock_start_position
        if elapsed < self.policy.rearm_trading_days:
            return False
        self.persistent_lock = False
        self.lock_date = None
        self.lock_drawdown = 0.0
        self.lock_start_position = None
        self.peak_assets = float(current_assets)
        self.breach_streak = 0
        self.alert_active = False
        self.audit_events.append(
            {
                "date": date_str,
                "event": "portfolio_drawdown_rearmed",
                "cooldown_trading_days": int(elapsed),
                "cycle_lock_count": int(self.cycle_lock_count),
            }
        )
        return True

    def check_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        trading_dates: list[pd.Timestamp] | None = None,
        date_to_pos: dict[pd.Timestamp, int] | None = None,
    ) -> str | None:
        """Apply temporary cycle defense before the lifetime terminal boundary."""
        assets = float(current_assets)
        self.lifetime_peak_assets = max(self.lifetime_peak_assets, assets)
        position = self._date_position(date_str, trading_dates, date_to_pos)
        if self.persistent_lock:
            if self._try_rearm(date_str, assets, position):
                return None
            return "persistent portfolio risk lock"

        self.peak_assets = max(self.peak_assets, assets)
        lifetime_drawdown = (
            (self.lifetime_peak_assets - assets) / self.lifetime_peak_assets
            if self.lifetime_peak_assets > 0
            else 0.0
        )
        if lifetime_drawdown >= self.policy.terminal_drawdown:
            return self._activate_terminal_lock(date_str, lifetime_drawdown, position)
        if self.peak_assets <= 0:
            return None
        cycle_drawdown = (self.peak_assets - assets) / self.peak_assets
        above_alert = cycle_drawdown >= self.policy.drawdown_alert
        if above_alert != self.alert_active:
            self.alert_active = above_alert
            self._record_alert_state(date_str, cycle_drawdown, above_alert)
        if cycle_drawdown >= self.policy.emergency_drawdown:
            return self._activate_cycle_lock(
                date_str,
                cycle_drawdown,
                position,
                "emergency_cycle_drawdown_lock",
            )
        self.breach_streak = (
            self.breach_streak + 1
            if cycle_drawdown >= self.policy.confirmed_drawdown
            else 0
        )
        if self.breach_streak < self.policy.drawdown_confirmations:
            return None
        return self._activate_cycle_lock(
            date_str,
            cycle_drawdown,
            position,
            "confirmed_cycle_drawdown_lock",
        )


class _UniverseInvariantSleeveMixin:
    """Share v17 risk and breadth behavior across coordinator and child sleeves."""

    policy: V17Policy
    cfg: dict[str, Any]
    risk: v14.RiskManager
    risk_events: list[dict[str, Any]]
    _risk_lock_logged: bool

    @staticmethod
    def _validate_config(cfg: dict) -> dict:
        """Permit a one-symbol risk scope while retaining every other v15 check."""
        normalized = dict(cfg)
        requested_minimum = normalized.get("sector_guard_min_symbols")
        if requested_minimum == 1:
            normalized["sector_guard_min_symbols"] = 2
            validated = v15.BacktestEngine._validate_config(normalized)
            validated["sector_guard_min_symbols"] = 1
            return validated
        return v15.BacktestEngine._validate_config(normalized)

    def _reset_run_state(self, symbols_dict: dict[str, str]) -> None:
        """Reset tradable and regime metadata at every independent run."""
        # The concrete classes place this cooperative mixin before a v16 engine.
        super()._reset_run_state(  # pyright: ignore[reportAttributeAccessIssue]
            symbols_dict
        )
        self._tradable_symbol_codes: set[str] = set(symbols_dict)
        self._candidate_score_series: dict[str, dict[int, pd.Series]] = {}

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
        """Load a fixed signal-only regime basket beside tradable symbols."""
        self._tradable_symbol_codes = set(symbols_dict)
        combined = dict(symbols_dict)
        for code in self.policy.regime_symbols:
            combined.setdefault(code, code)
        prepared = super()._prepare_run(  # pyright: ignore[reportAttributeAccessIssue]
            combined,
            start_date,
            end_date,
            start_ts,
            end_ts,
            per_symbol_config,
            profile,
            config_route,
            data_dir,
        )
        self._tradable_symbol_codes = set(symbols_dict)
        self._candidate_score_series = self._build_candidate_score_series(prepared[0])
        return prepared

    def _build_candidate_score_series(
        self, data_map: dict[str, pd.DataFrame]
    ) -> dict[str, dict[int, pd.Series]]:
        """Precompute causal multi-horizon risk-adjusted momentum series."""
        cache: dict[str, dict[int, pd.Series]] = {}
        for code, frame in data_map.items():
            close = frame["close"].astype(float)
            daily_returns = close.pct_change()
            cache[code] = {}
            for window in self.policy.candidate_lookbacks:
                volatility = daily_returns.rolling(window, min_periods=window).std()
                cache[code][window] = close.pct_change(window) / volatility
        return cache

    def _resolve_symbol_configs(
        self,
        symbols_dict: dict[str, str],
        per_symbol_config: dict[str, dict] | None,
        config_route: str,
    ) -> dict[str, dict]:
        """Install the recoverable manager after inherited parameter routing."""
        resolved = super()._resolve_symbol_configs(  # pyright: ignore[reportAttributeAccessIssue]
            symbols_dict, per_symbol_config, config_route
        )
        symbol_groups = dict(self.risk.symbol_groups)
        self.risk = RecoverableDrawdownRiskManager(self.cfg, self.policy)
        self.risk.configure_groups(symbol_groups)
        return resolved

    def _select_momentum_candidates(
        self,
        data_map: dict[str, pd.DataFrame],
        symbols_dict: dict[str, str],
        date: pd.Timestamp,
    ) -> set[str]:
        """Rank tradable symbols with stable multi-horizon momentum evidence."""
        del data_map
        tradable = sorted(
            code for code in symbols_dict if code in self._tradable_symbol_codes
        )
        if not tradable:
            return set()
        maximum = int(self.cfg.get("max_positions", 6))
        if len(tradable) <= maximum:
            return set(tradable)

        totals = {code: 0.0 for code in tradable}
        observations = {code: 0 for code in tradable}
        for window in self.policy.candidate_lookbacks:
            reference_values: list[float] = []
            for code in self.policy.regime_symbols:
                series = self._candidate_score_series.get(code, {}).get(window)
                if series is None or date not in series.index:
                    continue
                value = float(series.loc[date])
                if math.isfinite(value):
                    reference_values.append(value)
            if not reference_values:
                continue

            for code in tradable:
                series = self._candidate_score_series.get(code, {}).get(window)
                if series is None or date not in series.index:
                    continue
                value = float(series.loc[date])
                if not math.isfinite(value):
                    continue
                percentile = sum(
                    reference <= value for reference in reference_values
                ) / len(reference_values)
                totals[code] += percentile
                observations[code] += 1

        def sort_key(code: str) -> tuple[bool, float, str]:
            count = observations[code]
            score = totals[code] / count if count else 0.0
            return count == 0, -score, code

        eligible = [
            code
            for code in tradable
            if observations[code]
            and totals[code] / observations[code]
            >= self.policy.candidate_reference_percentile
        ]
        ranked = sorted(eligible, key=sort_key)
        return set(ranked[:maximum])

    def _update_sector_guard(
        self,
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
        all_dates: list[pd.Timestamp],
        date_to_pos: dict[pd.Timestamp, int],
    ) -> str | None:
        """Update breadth risk from the fixed signal-only basket."""
        scoped_data = {
            code: data_map[code]
            for code in self.policy.regime_symbols
            if code in data_map
        }
        return super()._update_sector_guard(  # pyright: ignore[reportAttributeAccessIssue]
            scoped_data,
            date,
            all_dates,
            date_to_pos,
        )

    def _apply_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        all_dates: list[pd.Timestamp],
        date_to_pos: dict[pd.Timestamp, int],
        pending: list[tuple[Signal, BaseStrategy]],
    ) -> tuple[list[tuple[Signal, BaseStrategy]], bool, bool]:
        """Reset inherited one-shot logging whenever a temporary lock rearms."""
        before = len(self.risk_events)
        outcome = super()._apply_portfolio_risk(  # pyright: ignore[reportAttributeAccessIssue]
            current_assets, date_str, all_dates, date_to_pos, pending
        )
        if any(
            event.get("event") == "portfolio_drawdown_rearmed"
            for event in self.risk_events[before:]
        ):
            self._risk_lock_logged = False
        return outcome

    def _build_result(self, final_assets: float, all_dates: list[pd.Timestamp]) -> dict:
        """Expose temporary and terminal lock state in the standard result."""
        result = super()._build_result(  # pyright: ignore[reportAttributeAccessIssue]
            final_assets,
            all_dates,
        )
        manager = self.risk
        result.update(
            {
                "engine_version": "17.0",
                "v17_policy": self.policy.as_dict(),
                "terminal_risk_lock": bool(
                    isinstance(manager, RecoverableDrawdownRiskManager)
                    and manager.terminal_lock
                ),
                "cycle_lock_count": int(
                    manager.cycle_lock_count
                    if isinstance(manager, RecoverableDrawdownRiskManager)
                    else 0
                ),
                "guard_scope_mode": "fixed_signal_only_regime_basket",
                "tradable_symbols": sorted(self._tradable_symbol_codes),
            }
        )
        return result


class SleeveBacktestEngine(_UniverseInvariantSleeveMixin, v16.SleeveBacktestEngine):
    """Run one v17 sleeve with adaptive breadth and recoverable drawdown defense."""

    ENGINE_LABEL = "Codex Quant v17"


class BacktestEngine(_UniverseInvariantSleeveMixin, v16.BacktestEngine):
    """Coordinate equal-capital v17 sleeves under one universe-invariant policy."""

    ENGINE_LABEL = "Codex Quant v17"

    _SINGLE_ASSET_TREND_OVERRIDES: dict[str, Any] = {
        "entry_period": 30,
        "exit_period": 20,
        "trail_atr_mult": 10.0,
        "profit_lock_giveback": 0.40,
        "reversal_break_giveback": 0.40,
        "reversal_exit_period": 20,
        "hard_stop": 0.25,
    }

    def __init__(
        self,
        initial_capital: float = 2_000_000,
        cfg: dict | None = None,
        policy: V17Policy | None = None,
    ) -> None:
        resolved_policy = policy or V17Policy()
        normalized_cfg = {
            "sector_guard_min_symbols": len(resolved_policy.regime_symbols),
            "group_min_slots": 0,
            **dict(cfg or {}),
        }
        super().__init__(
            initial_capital=initial_capital,
            cfg=normalized_cfg,
            policy=resolved_policy,
        )

    def _effective_policy(self, tradable_count: int) -> V17Policy:
        """Tighten drawdown gates smoothly as diversification approaches one."""
        count = v14._require_int("tradable_count", tradable_count, min_value=1)
        adjustment = self.policy.concentration_drawdown_adjustment / count
        return replace(
            self.policy,
            confirmed_drawdown=self.policy.confirmed_drawdown - adjustment,
            emergency_drawdown=self.policy.emergency_drawdown - adjustment,
        )

    def _runtime_sleeve_cfg(self, tradable_count: int) -> dict[str, Any]:
        """Return shared overrides, with a time-series fallback for one asset."""
        sleeve_cfg = dict(self._v16_user_cfg)
        if tradable_count <= 2:
            sleeve_cfg.update(self._SINGLE_ASSET_TREND_OVERRIDES)
        return sleeve_cfg

    def run(  # noqa: PLR0913 - Preserve the inherited public API.
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
        """Run one or several v17 sleeves under the same effective policy formula."""
        mode = str(allocation_mode or self.policy.allocation_mode).lower()
        if mode == "ensemble":
            return super().run(
                symbols_dict,
                start_date,
                end_date,
                per_symbol_config=per_symbol_config,
                profile=profile,
                config_route=config_route,
                data_dir=data_dir,
                indicator_state=indicator_state,
                warmup_calendar_days=warmup_calendar_days,
                allocation_mode="ensemble",
            )
        if mode != "single":
            raise ValueError("allocation_mode must be either 'single' or 'ensemble'")
        count = len(symbols_dict)
        effective_policy = replace(
            self._effective_policy(count), allocation_mode="single"
        )
        sleeve = SleeveBacktestEngine(
            self.initial_capital,
            cfg=self._runtime_sleeve_cfg(count),
            policy=effective_policy,
            allocation_lookbacks=effective_policy.single_lookbacks,
            sleeve_name="single",
        )
        result = sleeve.run(
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
        result["effective_v17_policy"] = effective_policy.as_dict()
        self.sleeves = [sleeve]
        self.last_result = result
        return result

    def _run_ensemble(self, request: v16._RunRequest) -> dict:
        """Run v17 child sleeves and aggregate their independent ledgers."""
        tradable_count = len(request.symbols_dict)
        effective_policy = self._effective_policy(tradable_count)
        horizons = effective_policy.allocation_horizons
        sleeve_capital = self.initial_capital / len(horizons)
        results: list[dict] = []
        self.sleeves = []
        base_sleeve_policy = replace(
            effective_policy,
            allocation_mode="single",
            max_order_adv_ratio=effective_policy.max_order_adv_ratio / len(horizons),
        )
        for index, lookbacks in enumerate(horizons):
            capital = (
                sleeve_capital
                if index < len(horizons) - 1
                else self.initial_capital - sleeve_capital * (len(horizons) - 1)
            )
            name = self._sleeve_name(index, len(horizons))
            sleeve_policy = replace(
                base_sleeve_policy,
                candidate_lookbacks=effective_policy.candidate_horizons[index],
            )
            # Cross-sectional ranks contain no information with one asset. The
            # fallback preserves the same 60% symbol exposure ceiling.
            sleeve_cfg = self._runtime_sleeve_cfg(tradable_count)
            sleeve = SleeveBacktestEngine(
                capital,
                cfg=sleeve_cfg,
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
        combined = self._aggregate_sleeve_results(results)
        combined.update(
            {
                "engine_version": "17.0",
                "v17_policy": self.policy.as_dict(),
                "effective_v17_policy": effective_policy.as_dict(),
                "terminal_risk_lock": any(
                    result.get("terminal_risk_lock", False) for result in results
                ),
                "cycle_lock_count": sum(
                    int(result.get("cycle_lock_count", 0)) for result in results
                ),
                "guard_scope_mode": "fixed_signal_only_regime_basket",
            }
        )
        self.last_result = combined
        return combined


def main() -> dict | None:
    """Run a v17 backtest from the command line."""
    parser = argparse.ArgumentParser(description="Codex Quant Fusion v17 backtester")
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
    parser.add_argument("--indicator-state", choices=["cold", "warm"], default="warm")
    parser.add_argument("--warmup-calendar-days", type=int, default=365)
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
    )
    PerformanceReport.print_report(result, symbols)
    if args.save_dir:
        PerformanceReport.save_result(result, args.save_dir)
    if not args.no_plot:
        PerformanceReport.plot_equity_curve(
            result,
            f"equity_curve_v17_{args.indicator_state}.png",
        )
    return result


if __name__ == "__main__":
    main()
