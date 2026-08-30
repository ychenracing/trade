"""Sleeve preparation, buy authorization, execution, and finalization."""

from __future__ import annotations

# pyright: reportAttributeAccessIssue=false

# ruff: noqa: F401

import contextlib
import io
import math
from dataclasses import replace
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from quantfusion.config.universe import ESTABLISHED_EXPANSION_CORE
from quantfusion.data.providers import DataFetcher
from quantfusion.domain.models import AccountState, MarketRegimeObservation, Signal
from quantfusion.domain.rules import floor_to_lot, require_int
from quantfusion.engine.core import CoreBacktestEngine
from quantfusion.engine.ensemble import (
    EnsembleBacktestEngine,
    EnsembleSleeveBacktestEngine,
    PreparedSleeveRun,
    RunRequest,
)
from quantfusion.execution.priorities import EXECUTION_PRIORITY
from quantfusion.indicators.technical import Indicators
from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.risk.managers import RecoverableDrawdownRiskManager, RiskManager
from quantfusion.risk.overlay.adapter import (
    apply_cooldown_buy_gate,
    apply_risk_buy_gate,
)
from quantfusion.strategy.trend import BaseStrategy

_CoreBacktestEngine = CoreBacktestEngine
_ESTABLISHED_EXPANSION_CORE = ESTABLISHED_EXPANSION_CORE
_EnsembleBacktestEngine = EnsembleBacktestEngine
_EnsembleSleeveBacktestEngine = EnsembleSleeveBacktestEngine
_PreparedSleeveRun = PreparedSleeveRun
_RunRequest = RunRequest
_floor_to_lot = floor_to_lot
_require_int = require_int


class EnsembleAllocationMixin:
    """Sleeve preparation, buy authorization, execution, and finalization."""

    def _assess_run_warmup_health(
        self,
        request: _RunRequest,
        states: list[_PreparedSleeveRun],
        overlay_frames: dict[str, pd.DataFrame],
    ) -> Any:
        """2026-08-16 报告 P0-1：评估本次运行的预热健康契约（READY/DEGRADED/NOT_READY）。

        - 指标就绪度按交易池逐股统计（cold 运行历史为 0 → NOT_READY）；
        - 参考篮就绪度按独立 23 股风险篮实际可观察帧统计；
        - regime 证据按袖套 regime 参考篮在场成员的新鲜度统计。
        """
        import quantfusion.risk.governance as rg
        from quantfusion.config.overlay import RISK_BASKET

        data_map = states[0].data_map if states else {}
        regime_frames = {
            symbol: data_map[symbol]
            for symbol in self.policy.regime_symbols
            if symbol in data_map
        }
        return rg.assess_warmup_health(
            data_map,
            request.start_date,
            request.end_date,
            reference_symbols=RISK_BASKET,
            reference_frames=overlay_frames,
            regime_index_frames=regime_frames,
        )

    @staticmethod
    def _basket_daily_returns_series(
        overlay_frames: dict[str, pd.DataFrame],
        calendar: Any,
    ) -> list[float] | None:
        """等权风险篮逐日收益（对齐回测日历），供事件校准使用。"""
        frames = {
            symbol: frame
            for symbol, frame in overlay_frames.items()
            if frame is not None and len(frame.index)
        }
        if not frames or len(calendar) == 0:
            return None
        closes = pd.DataFrame(
            {
                symbol: pd.to_numeric(frame["close"], errors="coerce")
                .reindex(pd.DatetimeIndex(calendar))
                .ffill()
                for symbol, frame in frames.items()
            }
        )
        returns = closes.pct_change().fillna(0.0)
        return [float(v) for v in returns.mean(axis=1).tolist()]

    def _calibrate_run_risk_events(
        self,
        combined: dict[str, Any],
        risk_level_curve: list[int],
        overlay_frames: dict[str, pd.DataFrame],
    ) -> dict[str, Any]:
        """2026-08-16 报告 P0-2：事后校准本次运行的风险事件分类器。

        组合逐日资产来自聚合权益曲线；风险等级来自 overlay 逐日采样；
        风险篮逐日收益按等权篮计算。日历长度不一致时显式返回
        ``calendar_mismatch`` 而不是输出错误指标。
        """
        import quantfusion.risk.governance as rg

        equity = combined.get("equity_curve")
        if equity is None or len(equity.index) == 0:
            return {"status": "insufficient_data", "events": [], "metrics": {}}
        assets = [float(v) for v in equity["assets"].tolist()]
        if len(assets) != len(risk_level_curve):
            return {
                "status": "calendar_mismatch",
                "equity_days": len(assets),
                "risk_level_days": len(risk_level_curve),
                "events": [],
                "metrics": {},
            }
        dates = [d.strftime("%Y-%m-%d") for d in equity.index]
        basket = self._basket_daily_returns_series(overlay_frames, equity.index)
        return rg.calibrate_risk_events(
            dates,
            assets,
            risk_level_curve,
            basket_daily_returns=basket,
        )

    def _prepare_ensemble_sleeves(
        self, request: _RunRequest, effective_policy: PortfolioPolicy
    ) -> list[_PreparedSleeveRun]:
        """Create funded sleeves and prepare their data without running ahead."""
        tradable_count = len(request.symbols_dict)
        indicator_state = str(request.indicator_state).lower()
        if indicator_state not in {"cold", "warm"}:
            raise ValueError("indicator_state must be either 'cold' or 'warm'")
        warmup_days = _require_int(
            "warmup_calendar_days", request.warmup_calendar_days, min_value=120
        )
        horizons = effective_policy.allocation_horizons
        sleeve_capital = self.initial_capital / len(horizons)
        self.sleeves = []
        states: list[_PreparedSleeveRun] = []
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
            sleeve = self.SLEEVE_ENGINE_CLASS(
                capital,
                cfg=sleeve_cfg,
                policy=sleeve_policy,
                allocation_lookbacks=lookbacks,
                sleeve_name=name,
            )
            sleeve._indicator_state = indicator_state
            sleeve._warmup_calendar_days = warmup_days
            sleeve._requested_start_date = request.start_date
            sleeve._requested_end_date = request.end_date
            profile, route, start_ts, end_ts = sleeve._validate_run_request(
                request.symbols_dict,
                request.start_date,
                request.end_date,
                request.profile,
                request.config_route,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                prepared = sleeve._prepare_run(
                    request.symbols_dict,
                    request.start_date,
                    request.end_date,
                    start_ts,
                    end_ts,
                    request.per_symbol_config,
                    profile,
                    route,
                    request.data_dir,
                    request.cache_dir,
                )
            self.sleeves.append(sleeve)
            states.append(
                _PreparedSleeveRun(
                    sleeve=sleeve,
                    data_map=prepared[0],
                    indicator_map=prepared[1],
                    all_dates=prepared[2],
                    date_to_pos=prepared[3],
                )
            )
        return states

    @staticmethod
    def _held_portfolio_symbols(states: list[_PreparedSleeveRun]) -> set[str]:
        """Return the distinct symbols held by any virtual subaccount."""
        return {
            symbol
            for state in states
            for symbol, positions in state.sleeve.positions.items()
            if positions
        }

    def _current_position_limit(
        self, states: list[_PreparedSleeveRun], external_risk_level: int = 0
    ) -> int:
        """Return the causal three-to-six position limit for the current regime."""
        hard_limit = int(self.cfg["max_positions"])
        if (
            not bool(self.cfg.get("adaptive_max_positions", True))
            or not states
            or external_risk_level < 1
        ):
            return hard_limit
        regime = str(getattr(states[0].sleeve, "_regime_state", "TREND"))
        if regime == "CHOPPY":
            return min(hard_limit, int(self.cfg.get("choppy_max_positions", 3)))
        if regime == "TRANSITION":
            return min(
                hard_limit, int(self.cfg.get("transition_max_positions", 4))
            )
        return hard_limit

    def _rebalance_free_sleeve_cash(
        self, states: list[_PreparedSleeveRun], date: pd.Timestamp
    ) -> None:
        """Shift idle cash without merging positions, strategies, or pending orders."""
        if (
            not bool(self.cfg.get("dynamic_sleeve_weights", True))
            or len(states) != 3
        ):
            return
        regime = str(getattr(states[0].sleeve, "_regime_state", "TREND"))
        if self._last_sleeve_weight_regime is None:
            self._last_sleeve_weight_regime = regime
            return
        if regime == self._last_sleeve_weight_regime:
            return
        self._last_sleeve_weight_regime = regime
        prefix = regime.lower() if regime in {"TRANSITION", "CHOPPY"} else None
        weights = (
            [1.0 / 3.0] * 3
            if prefix is None
            else [
                float(self.cfg[f"{prefix}_{name}_weight"])
                for name in ("fast", "base", "slow")
            ]
        )
        total_cash = sum(float(state.sleeve.cash) for state in states)
        if total_cash <= 0:
            return
        before = [float(state.sleeve.cash) for state in states]
        targets = [total_cash * weight for weight in weights]
        targets[-1] = total_cash - sum(targets[:-1])
        if all(
            math.isclose(old, new, rel_tol=0.0, abs_tol=0.01)
            for old, new in zip(before, targets, strict=True)
        ):
            return
        for state, old, target in zip(states, before, targets, strict=True):
            state.sleeve.cash = target
            cash_flow = target - old
            risk = state.sleeve.risk
            for attribute in (
                "peak_assets",
                "lifetime_peak_assets",
                "daily_start_assets",
            ):
                if hasattr(risk, attribute):
                    adjusted = max(
                        0.0, float(getattr(risk, attribute, 0.0)) + cash_flow
                    )
                    setattr(risk, attribute, adjusted)
        self._sleeve_weight_events.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "event": "free_cash_sleeve_reweight",
                "regime": regime,
                "weights": dict(
                    zip(("fast", "base", "slow"), weights, strict=True)
                ),
                "cash_before": before,
                "cash_after": targets,
            }
        )

    @staticmethod
    def _overlay_allocation_score(states: list[_PreparedSleeveRun], date: pd.Timestamp):
        """Mean allocation score across sleeves, used to rank laggards for trim."""
        def _score(symbol: str) -> float:
            samples = []
            for state in states:
                try:
                    scores = state.sleeve._allocation_scores(state.data_map, date)
                except Exception:
                    scores = {}
                samples.append(float(scores.get(symbol, 0.0)))
            return float(np.mean(samples)) if samples else 0.0
        return _score

    def _authorize_portfolio_buys(
        self,
        states: list[_PreparedSleeveRun],
        date: pd.Timestamp,
        external_risk_level: int = 0,
    ) -> None:
        """Admit symbols by the mean of comparable percentile ranks (Borda score)."""
        held = self._held_portfolio_symbols(states)
        hard_limit = int(self.cfg["max_positions"])
        maximum = self._current_position_limit(states, external_risk_level)
        if len(held) > hard_limit:
            raise RuntimeError("portfolio symbol limit was already exceeded")
        candidate_symbols: set[str] = set()
        for state in states:
            candidates = {
                signal.symbol
                for signal, _ in state.pending
                if signal.direction == "buy"
                and signal.symbol not in held
                and signal.symbol in state.data_map
                and date in state.data_map[signal.symbol].index
            }
            candidate_symbols.update(candidates)
        score_samples = {
            symbol: [] for symbol in candidate_symbols
        }
        for state in states:
            scores = state.sleeve._allocation_scores(state.data_map, date)
            candidates = {
                signal.symbol
                for signal, _ in state.pending
                if signal.direction == "buy"
                and signal.symbol not in held
                and signal.symbol in state.data_map
                and date in state.data_map[signal.symbol].index
            }
            for symbol in candidates:
                score_samples[symbol].append(scores.get(symbol, 0.0))
        date_str = date.strftime("%Y-%m-%d")
        route_migrations = {
            signal.symbol
            for state in states
            for signal, strategy in state.pending
            if signal.direction == "buy"
            and signal.symbol not in held
            and getattr(strategy, "name", "") == "positive_momentum_hold"
        }
        admission_scores = {
            symbol: float(np.mean(samples))
            for symbol, samples in score_samples.items()
        }
        # A six-to-twelve-name expansion keeps the fixed five-name production
        # basket on its established path; only additional names must earn
        # new-candidate evidence.  Reclassifying the same core as "new" at
        # seven names creates an artificial 6 -> 7 discontinuity.
        reference_core = set(self.policy.regime_symbols)
        tradable_symbols = (
            set(states[0].sleeve._tradable_symbol_codes) if states else set()
        )
        fixed_core = (
            reference_core
            if 6 <= self._runtime_tradable_count <= 12
            and reference_core.issubset(tradable_symbols)
            else set()
        )
        if self._runtime_tradable_count >= 6:
            score_eligible = fixed_core | {
                symbol
                for symbol, score in admission_scores.items()
                if score >= 0.50
            }
        else:
            score_eligible = set(admission_scores)

        # Expanded pools are sensitive to a single noisy add-one candidate.
        # Preserve the five-name core through 6-12 names. Once the established
        # 13-name production pool is present, preserve its existing admission
        # path too, while requiring only symbols outside it to sustain four
        # executable intent days. Interrupted evidence resets. Existing
        # holdings and outer-route migrations bypass this new-entry gate.
        established_expansion = (
            self._runtime_tradable_count == 14
            and _ESTABLISHED_EXPANSION_CORE.issubset(tradable_symbols)
        )
        confirmation_core = (
            _ESTABLISHED_EXPANSION_CORE if established_expansion else fixed_core
        )
        confirmation_required = (
            6 <= self._runtime_tradable_count <= 12 or established_expansion
        )
        if confirmation_required:
            required_confirmation_days = (
                2
                if established_expansion
                else (4 if self._runtime_tradable_count >= 9 else 2)
            )
            current_intent = (
                score_eligible & set(score_samples)
            ) - confirmation_core
            if established_expansion:
                expansion_min_score = float(
                    self.cfg.get("established_expansion_min_score", 0.80)
                )
                current_intent = {
                    symbol
                    for symbol in current_intent
                    if admission_scores.get(symbol, 0.0) >= expansion_min_score
                }
            previous = self._new_candidate_intent_streak
            self._new_candidate_intent_streak = {
                symbol: previous.get(symbol, 0) + 1
                for symbol in current_intent
            }
            confirmation_eligible = confirmation_core | {
                symbol
                for symbol, streak in self._new_candidate_intent_streak.items()
                if streak >= required_confirmation_days
            }
        else:
            required_confirmation_days = 1
            self._new_candidate_intent_streak = {}
            confirmation_eligible = set(score_samples)

        eligible_new = set(score_samples) & score_eligible & confirmation_eligible
        ranked = sorted(
            eligible_new,
            key=lambda symbol: (
                -admission_scores[symbol],
                EXECUTION_PRIORITY.get(symbol, 9999),
                symbol,
            ),
        )
        migration_capacity = max(maximum - len(held), 0)
        admitted_migrations = set(
            sorted(
                route_migrations,
                key=lambda symbol: (EXECUTION_PRIORITY.get(symbol, 9999), symbol),
            )[:migration_capacity]
        )
        candidate_capacity = max(
            maximum - len(held) - len(admitted_migrations), 0
        )
        allowed = held | admitted_migrations | set(ranked[:candidate_capacity])
        for state in states:
            retained: list[tuple[Signal, BaseStrategy]] = []
            for signal, strategy in state.pending:
                if signal.direction == "buy" and signal.symbol not in allowed:
                    if signal.symbol in route_migrations:
                        event = "rejected_portfolio_symbol_limit"
                    elif (
                        signal.symbol in score_samples
                        and signal.symbol not in score_eligible
                    ):
                        event = "rejected_new_candidate_allocation_score"
                    elif (
                        confirmation_required
                        and signal.symbol in score_samples
                        and signal.symbol not in confirmation_eligible
                    ):
                        event = "rejected_new_candidate_confirmation"
                    else:
                        event = "rejected_portfolio_symbol_limit"
                    state.sleeve._record_order_event(
                        date=date_str,
                        signal=signal,
                        event=event,
                        portfolio_max_positions=maximum,
                        allocation_score=admission_scores.get(signal.symbol),
                        confirmation_days=self._new_candidate_intent_streak.get(
                            signal.symbol, 0
                        ),
                        required_confirmation_days=required_confirmation_days,
                    )
                    continue
                retained.append((signal, strategy))
            state.pending = retained

    def _update_tail_sleeve_guard(
        self,
        states: list[_PreparedSleeveRun],
        date: pd.Timestamp,
        assets: float,
        peak_assets: float,
        events: list[dict[str, Any]],
    ) -> None:
        """Temporarily tighten sleeve tails after account-level stress.

        Only policy references are switched: sleeve positions, peaks, locks,
        pending orders and cooldowns remain intact.  The guard is hysteretic so
        it cannot chatter around the activation boundary.
        """
        if self._runtime_tradable_count < 9 or peak_assets <= 0:
            return
        drawdown = max(0.0, (peak_assets - float(assets)) / peak_assets)
        date_str = date.strftime("%Y-%m-%d")
        activation_drawdown = 0.18
        if not self._tail_guard_active and drawdown >= activation_drawdown:
            tail_rearm_days = int(self.policy.rearm_trading_days)
            alert_drawdown = 0.14
            emergency_drawdown = 0.22
            terminal_drawdown = 0.24
            policies: dict[str, PortfolioPolicy] = {}
            for state in states:
                manager = state.sleeve.risk
                if not isinstance(manager, RecoverableDrawdownRiskManager):
                    continue
                policies[state.sleeve.sleeve_name] = manager.policy
                manager.policy = replace(
                    manager.policy,
                    drawdown_alert=alert_drawdown,
                    confirmed_drawdown=activation_drawdown,
                    emergency_drawdown=emergency_drawdown,
                    terminal_drawdown=terminal_drawdown,
                    rearm_trading_days=tail_rearm_days,
                )
            if policies:
                self._tail_guard_policies = policies
                self._tail_guard_active = True
                events.append(
                    {
                        "date": date_str,
                        "event": "tail_sleeve_guard_on",
                        "drawdown": drawdown,
                        "activation_drawdown": activation_drawdown,
                        "sleeve_thresholds": {
                            "drawdown_alert": alert_drawdown,
                            "confirmed_drawdown": activation_drawdown,
                            "emergency_drawdown": emergency_drawdown,
                            "terminal_drawdown": terminal_drawdown,
                            "rearm_trading_days": tail_rearm_days,
                        },
                    }
                )
        elif self._tail_guard_active and drawdown <= 0.10:
            for state in states:
                policy = self._tail_guard_policies.get(state.sleeve.sleeve_name)
                manager = state.sleeve.risk
                if policy is not None and isinstance(
                    manager, RecoverableDrawdownRiskManager
                ):
                    manager.policy = policy
            self._tail_guard_policies = {}
            self._tail_guard_active = False
            events.append(
                {
                    "date": date_str,
                    "event": "tail_sleeve_guard_off",
                    "drawdown": drawdown,
                    "recovery_drawdown": 0.10,
                }
            )

    def _execute_ensemble_open(
        self,
        states: list[_PreparedSleeveRun],
        date: pd.Timestamp,
        date_pos: int = 0,
        cm_overlay=None,
    ) -> None:
        """Execute every sleeve's sells before globally admitting and filling buys.

        ``cm_overlay`` (the cross-market overlay) is passed so that its
        catastrophe-cooldown table can hard-block any pending buy for a symbol
        that just exited via a layered/catastrophe stop (report P0-4). The block
        runs after sells are executed and before buys are authorized, so re-entry
        across all three trend sleeves is suppressed for the full cooldown.
        """
        # Sleeves own independent internal positions, so their pending signals
        # must remain independent too. Broker-level netting would require an
        # internal-transfer or fill-allocation ledger that this model does not
        # have. Opposite same-day fills therefore execute on both sides and pay
        # their respective modeled costs; sells still execute before buys.
        for state in states:
            state.sleeve._start_trading_day()
            state.pending = state.sleeve._execute_pending_signals(
                state.pending,
                state.data_map,
                date,
                state.date_to_pos,
                frozenset({"sell"}),
            )
        self._rebalance_free_sleeve_cash(states, date)
        if cm_overlay is not None:
            apply_cooldown_buy_gate(cm_overlay, states, date, date_pos)
            for state in states:
                state.sleeve._external_risk_level = cm_overlay.risk_level
            apply_risk_buy_gate(
                cm_overlay,
                states, date, self._held_portfolio_symbols(states)
            )
        self._authorize_portfolio_buys(
            states,
            date,
            cm_overlay.risk_level if cm_overlay is not None else 0,
        )
        for state in states:
            state.pending = state.sleeve._execute_pending_signals(
                state.pending,
                state.data_map,
                date,
                state.date_to_pos,
                frozenset({"buy"}),
            )
        if len(self._held_portfolio_symbols(states)) > int(self.cfg["max_positions"]):
            raise RuntimeError("portfolio symbol limit exceeded after buy execution")

    @staticmethod
    def _apply_global_risk_lock(
        states: list[_PreparedSleeveRun], date: pd.Timestamp
    ) -> None:
        """Cancel buys and queue T+1 liquidations in every funded sleeve."""
        date_str = date.strftime("%Y-%m-%d")
        for state in states:
            pending_sells = {
                state.sleeve._signal_key(signal): signal
                for signal, _ in state.pending
                if signal.direction == "sell"
            }
            liquidations = state.sleeve._generate_liquidation_signals(
                date_str, reason="portfolio-level drawdown liquidation"
            )
            for signal, _ in liquidations:
                previous = pending_sells.get(state.sleeve._signal_key(signal))
                if previous is None:
                    continue
                state.sleeve._record_order_event(
                    date=date_str,
                    signal=signal,
                    event="pending_sell_superseded_by_portfolio_liquidation",
                    previous_reason=previous.reason,
                    previous_target_shares=int(previous.target_shares),
                    liquidation_target_shares=int(signal.target_shares),
                )
            state.pending = state.sleeve._dedupe_pending_signals(
                [item for item in state.pending if item[0].direction == "sell"]
                + liquidations
            )

    @staticmethod
    def _finalize_ensemble_sleeves(
        states: list[_PreparedSleeveRun],
    ) -> list[dict]:
        """Mark open positions at the final close and build sleeve reports."""
        results: list[dict] = []
        for state in states:
            last_date = state.all_dates[-1]
            final_assets = state.sleeve._total_assets(state.data_map, last_date)
            state.sleeve.pending_signals = state.sleeve._dedupe_pending_signals(
                state.pending
            )
            results.append(state.sleeve._build_result(final_assets, state.all_dates))
        return results
