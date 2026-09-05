"""Cross-market evidence loading and synchronized ensemble replay."""

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
from quantfusion.domain.models import MarketRegimeObservation, Signal
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
from quantfusion.risk.overlay.adapter import apply_risk_actions
from quantfusion.strategy.trend import BaseStrategy

_CoreBacktestEngine = CoreBacktestEngine
_ESTABLISHED_EXPANSION_CORE = ESTABLISHED_EXPANSION_CORE
_EnsembleBacktestEngine = EnsembleBacktestEngine
_EnsembleSleeveBacktestEngine = EnsembleSleeveBacktestEngine
_PreparedSleeveRun = PreparedSleeveRun
_RunRequest = RunRequest
_floor_to_lot = floor_to_lot
_require_int = require_int


class EnsembleOrchestrationMixin:
    """Cross-market evidence loading and synchronized ensemble replay."""

    @staticmethod
    def _reference_evidence_complete(
        states: list[_PreparedSleeveRun], regime_symbols: tuple[str, ...]
    ) -> bool:
        """Require every sleeve to load the fixed signal-only reference basket."""
        expected = set(regime_symbols)
        return bool(states) and all(
            expected.issubset(state.data_map) for state in states
        )

    def _load_overlay_risk_frames(self, request: _RunRequest) -> dict[str, pd.DataFrame]:
        """Load a point-in-time AI risk basket independently of the trade pool."""
        if (
            not bool(self.cfg.get("cm_independent_risk_basket", True))
            or not request.data_dir
        ):
            return {}
        from quantfusion.config.overlay import RISK_BASKET

        warm_start = str(
            (
                pd.Timestamp(request.start_date)
                - pd.Timedelta(days=int(request.warmup_calendar_days))
            ).date()
        )
        frames: dict[str, pd.DataFrame] = {}
        for symbol in RISK_BASKET:
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    frames[symbol] = DataFetcher.load_stock_data(
                        symbol,
                        warm_start,
                        request.end_date,
                        data_dir=request.data_dir,
                    )
            except (OSError, KeyError, RuntimeError, ValueError):
                # Missing reference evidence never creates a risk-on signal.
                # The overlay's minimum-industry gate fails toward warning-only.
                continue
        return frames

    def _run_ensemble(self, request: _RunRequest) -> dict:
        """Replay fixed-capital sleeves on one synchronized portfolio calendar."""
        self._sleeve_weight_events = []
        self._last_sleeve_weight_regime = None
        tradable_count = len(request.symbols_dict)
        self._runtime_tradable_count = tradable_count
        # MarketRegimeMixin loads the fixed signal-only reference basket beside
        # the tradable pool and fails closed on missing data. Tradable membership
        # therefore must not be used as a proxy for reference-data completeness.
        self._runtime_reference_complete = True
        self._new_candidate_intent_streak = {}
        self._tail_guard_active = False
        self._tail_guard_policies = {}
        diagnostic = getattr(self, "_c6_diagnostic_request", None)
        self._c6_score_trace = [] if diagnostic is not None and diagnostic["recording_mode"] != "OFF" else None
        effective_policy = self._effective_policy(tradable_count)
        states = self._prepare_ensemble_sleeves(request, effective_policy)
        if not self._reference_evidence_complete(
            states, self.policy.regime_symbols
        ):
            raise RuntimeError(
                "fixed signal-only regime reference evidence is incomplete"
            )
        reference_dates = states[0].all_dates
        if any(state.all_dates != reference_dates for state in states[1:]):
            raise RuntimeError("ensemble sleeves produced different trading calendars")

        account_risk_policy = self._effective_account_risk_policy(
            effective_policy,
            tradable_count,
            reference_complete=self._runtime_reference_complete,
        )
        portfolio_risk = RecoverableDrawdownRiskManager(
            {"max_drawdown": account_risk_policy.confirmed_drawdown},
            account_risk_policy,
        )
        # Restore previous risk state when explicitly provided by the caller.
        # Note: quantfusion.application.daily_scan does NOT use this feature —
        # it replays the full history each time to avoid time-direction errors.
        if request.risk_state:
            if request.risk_state.get("terminal_risk_lock", False):
                portfolio_risk.terminal_lock = True
                portfolio_risk.persistent_lock = True
            portfolio_risk.cycle_lock_count = request.risk_state.get(
                "cycle_lock_count", 0
            )
            if request.risk_state.get("sector_guard_active", False):
                for state in states:
                    state.sleeve.sector_guard_active = True
        portfolio_risk_events: list[dict[str, Any]] = []
        symbol_count_curve: list[dict[str, Any]] = []
        # 穿越牛熊 overlay: bull-silent defensive layer on top of the ensemble.
        # Default ON, only fires on genuine risk (catastrophe drop / structural
        # shock + drawdown), so a clean bull run is left untouched.
        from quantfusion.risk.overlay.policy import CrossMarketOverlay
        overlay_risk_frames = self._load_overlay_risk_frames(request)
        cm_overlay = CrossMarketOverlay(
            enable_shock_trim=bool(self.cfg.get("cm_overlay_shock_trim", False)),
            risk_frames=overlay_risk_frames,
            enable_trend_health=bool(
                self.cfg.get("cm_trend_health_protection", True)
            ),
            continuous_confirm_days=int(
                self.cfg.get("cm_risk_continuous_confirm_days", 3)
            ),
            level2_drawdown=float(self.cfg.get("cm_risk_level2_drawdown", 0.08)),
            level3_drawdown=float(self.cfg.get("cm_risk_level3_drawdown", 0.12)),
            severe_direct_return=float(
                self.cfg.get("cm_risk_severe_direct_return", -0.10)
            ),
        ) if self.cfg.get("enable_cm_overlay", True) else None
        if cm_overlay is not None:
            setattr(
                cm_overlay,
                "_c6_s_enabled",
                self._c6_feature_enabled("S")
                or bool(getattr(cm_overlay, "C6_S_PRODUCTION", False)),
            )
            setattr(
                cm_overlay,
                "_c6_diagnostic_evidence_enabled",
                self._c6_intervention_id() in {"C6_BASE", "C6_BASE_PLUS_S"},
            )
            cm_overlay.events.append(
                {
                    "date": request.start_date,
                    "event": "independent_risk_basket_loaded",
                    "observed_symbols": len(overlay_risk_frames),
                }
            )
        cm_overlay_peak = 0.0
        # ── 风险治理观测层（2026-08-16 报告 P0-1/P0-2/P0-3/P1-1/P1-2）───
        # 纯观测与输出：warmup 健康契约、逐日袖套共识、风险篮覆盖置信度、
        # 独立风险意见与事后风险事件校准。只读取、不修改、不参与任何交易
        # 决策状态，因此对既有回测路径是零行为漂移的（golden 指标不变）。
        # 注：本段 P0/P1 编号指 2026-08-16 报告，与旧注释中 2026-08-07
        # 报告的编号体系（如 P0-4 灾变冷却、P1-2 子行业收缩）不同。
        import quantfusion.risk.governance as rg
        from quantfusion.config.overlay import SYMBOL_SUB_INDUSTRY

        warmup_health = self._assess_run_warmup_health(
            request, states, overlay_risk_frames
        )
        governance_days: list[dict[str, Any]] = []
        risk_level_curve: list[int] = []
        last_opinion: rg.RiskOpinion | None = None
        last_agreement: rg.SleeveAgreementSnapshot | None = None
        prev_consensus: float | None = None
        prev_decline_streak = 0
        for idx, date in enumerate(reference_dates):
            # P0-4: pass the overlay so it can hard-block re-entry buys
            # for any symbol still in catastrophe cooldown (report P0-4).
            self._execute_ensemble_open(states, date, idx, cm_overlay)
            for state in states:
                state.pending = state.sleeve._evaluate_trading_day(
                    request.symbols_dict,
                    state.data_map,
                    state.indicator_map,
                    state.all_dates,
                    state.date_to_pos,
                    date,
                    state.pending,
                )
                if self._c6_intervention_id() in {
                    "W1_DATA_MAP_ONLY",
                    "W2_POOL_DENOMINATOR_ONLY",
                }:
                    state.pending = [
                        item for item in state.pending if item[0].symbol != "601869"
                    ]
            if request.route_controller is not None:
                request.route_controller.after_close(
                    states,
                    date,
                    request.symbols_dict,
                )
                if cm_overlay is not None and hasattr(
                    request.route_controller, "current_route"
                ):
                    cm_overlay.set_outer_route(
                        request.route_controller.current_route,
                        date,
                    )
            state_assets = [
                state.sleeve._total_assets(state.data_map, date) for state in states
            ]
            assets = sum(state_assets)
            if (
                self._c6_intervention_id()
                != "W5_FULL_BASE_PRODUCTION_POOL_RELATIVE_NO_LOCK"
            ):
                status = portfolio_risk.check_portfolio_risk(
                    assets,
                    date.strftime("%Y-%m-%d"),
                    trading_dates=reference_dates,
                    date_to_pos=states[0].date_to_pos,
                )
                portfolio_risk_events.extend(portfolio_risk.drain_audit_events())
                if status:
                    self._apply_global_risk_lock(states, date)
            self._update_tail_sleeve_guard(
                states,
                date,
                assets,
                float(portfolio_risk.lifetime_peak_assets),
                portfolio_risk_events,
            )
            # 穿越牛熊 policy emits immutable actions; the engine-owned
            # adapter appends the resulting T+1 sells and resolves priorities.
            cm_overlay_peak = max(cm_overlay_peak, assets)
            if cm_overlay is not None and cm_overlay_peak > 0:
                actions = cm_overlay.evaluate(
                    states, date, idx, assets, cm_overlay_peak,
                    self._overlay_allocation_score(states, date),
                )
                apply_risk_actions(
                    actions,
                    states,
                    date_str=date.strftime("%Y-%m-%d"),
                    events=cm_overlay.events,
                    state_local_books=self._c6_feature_enabled("F0"),
                )
            held = self._held_portfolio_symbols(states)
            symbol_count_curve.append(
                {"date": date.strftime("%Y-%m-%d"), "symbol_count": len(held)}
            )
            # ── 每日治理采样（当日交易与 overlay 分级已完成）──────────
            risk_level_curve.append(
                int(cm_overlay.risk_level) if cm_overlay is not None else 0
            )
            agreement = rg.compute_sleeve_agreement(
                date.strftime("%Y-%m-%d"),
                [state.sleeve.sleeve_name for state in states],
                [
                    {
                        symbol
                        for symbol, positions in state.sleeve.positions.items()
                        if positions
                    }
                    for state in states
                ],
                state_assets,
                [float(state.sleeve.cash) for state in states],
                previous_consensus=prev_consensus,
                previous_streak=prev_decline_streak,
            )
            prev_consensus = agreement.mean_consensus
            prev_decline_streak = agreement.decline_streak
            last_agreement = agreement
            cov = (
                cm_overlay.coverage_metrics() if cm_overlay is not None else {}
            )
            coverage = rg.basket_coverage_confidence(
                int(cov.get("observed", 0)),
                int(cov.get("total_basket", 0)),
                int(cov.get("observed_industries", 0)),
                int(cov.get("total_industries", 0)),
                held,
                SYMBOL_SUB_INDUSTRY,
            )
            overlay_snapshot = (
                cm_overlay.state_snapshot() if cm_overlay is not None else {}
            )
            outer_route = (
                request.route_controller.current_route
                if request.route_controller is not None
                and hasattr(request.route_controller, "current_route")
                else None
            )
            last_opinion = rg.build_risk_opinion(
                date.strftime("%Y-%m-%d"),
                int(cm_overlay.risk_level) if cm_overlay is not None else 0,
                coverage,
                regime=str(
                    getattr(states[0].sleeve, "_regime_state", "TREND")
                ).lower(),
                stressed_sub_industry=overlay_snapshot.get("stressed_subindustry"),
                catastrophe_cooldown_active=(
                    cm_overlay.has_active_catastrophe_cooldown(idx)
                    if cm_overlay is not None
                    else False
                ),
                outer_route=outer_route,
                sleeve_consensus=agreement.mean_consensus,
                sleeve_consensus_decline_streak=agreement.decline_streak,
            )
            governance_days.append(
                {
                    "date": date.strftime("%Y-%m-%d"),
                    "risk_level": last_opinion.risk_level,
                    "risk_confidence": round(last_opinion.risk_confidence, 4),
                    "regime": last_opinion.regime,
                    "bull_silent": last_opinion.bull_silent,
                    "block_new_entries": last_opinion.block_new_entries,
                    "block_pyramids": last_opinion.block_pyramids,
                    "sleeve_consensus": round(agreement.mean_consensus, 4),
                    "sleeve_decline_streak": agreement.decline_streak,
                    "weakest_sleeve": agreement.weakest_sleeve,
                }
            )

        results = self._finalize_ensemble_sleeves(states)
        combined = self._aggregate_sleeve_results(results)
        combined_risk_events = list(combined["risk_events"])
        combined_risk_events.extend(
            {"sleeve": "portfolio", **event} for event in portfolio_risk_events
        )
        if cm_overlay is not None:
            combined_risk_events.extend(
                {"sleeve": "overlay", **event} for event in cm_overlay.events
            )
        combined.update(
            {
                "portfolio_policy": self.policy.as_dict(),
                "effective_portfolio_policy": effective_policy.as_dict(),
                "effective_account_risk_policy": account_risk_policy.as_dict(),
                "terminal_risk_lock": bool(
                    portfolio_risk.terminal_lock
                    or any(
                        result.get("terminal_risk_lock", False) for result in results
                    )
                ),
                "cycle_lock_count": int(
                    portfolio_risk.cycle_lock_count
                    + sum(int(result.get("cycle_lock_count", 0)) for result in results)
                ),
                "portfolio_cycle_lock_count": int(portfolio_risk.cycle_lock_count),
                "persistent_risk_lock": bool(
                    portfolio_risk.persistent_lock or combined["persistent_risk_lock"]
                ),
                "all_sleeves_locked": bool(
                    portfolio_risk.persistent_lock or combined["all_sleeves_locked"]
                ),
                "locked_sleeves": (
                    [state.sleeve.sleeve_name for state in states]
                    if portfolio_risk.persistent_lock
                    else combined["locked_sleeves"]
                ),
                "guard_scope_mode": "fixed_signal_only_regime_basket",
                "portfolio_cash_model": (
                    "independent_sleeves_dynamic_idle_cash"
                    if bool(self.cfg.get("dynamic_sleeve_weights", True))
                    else "fixed_virtual_subaccounts"
                ),
                "portfolio_max_positions": int(self.cfg["max_positions"]),
                "max_concurrent_symbols": max(
                    item["symbol_count"] for item in symbol_count_curve
                ),
                "portfolio_symbol_count_curve": symbol_count_curve,
                "sleeve_weight_events": list(self._sleeve_weight_events),
                "cm_risk_level": cm_overlay.risk_level if cm_overlay else 0,
                "cm_overlay_state": (
                    cm_overlay.state_snapshot() if cm_overlay else None
                ),
                "risk_events": self._sort_events(combined_risk_events),
                "regime_state_series": (
                    list(results[0].get("regime_state_series", []))
                    if results
                    else []
                ),
                "regime_final_state": (
                    results[0].get("regime_final_state", "TREND")
                    if results
                    else "TREND"
                ),
                "safe_mode_active": any(
                    result.get("safe_mode_active", False) for result in results
                ) if results else False,
                "production_replay": (
                    request.route_controller.result_snapshot()
                    if request.route_controller is not None
                    else None
                ),
                "tail_sleeve_guard_active": self._tail_guard_active,
                "c6_s_evidence": getattr(cm_overlay, "c6_s_evidence", None) if cm_overlay is not None else None,
            }
        )
        # ── 风险治理输出（2026-08-16 报告 P0-1/P0-2/P0-3/P1-1/P1-2）──────
        # 全部为附加字段：不进入任何决策路径，仅随结果自动输出，供生产
        # 契约（warmup 分级）、独立风险意见消费方与事后校准使用。
        combined["warmup_health"] = warmup_health.as_dict()
        combined["risk_opinion"] = (
            last_opinion.as_dict() if last_opinion is not None else None
        )
        combined["sleeve_agreement"] = (
            last_agreement.as_dict() if last_agreement is not None else None
        )
        combined["risk_governance_series"] = governance_days
        combined["risk_event_calibration"] = self._calibrate_run_risk_events(
            combined, risk_level_curve, overlay_risk_frames
        )
        request_data = getattr(self, "_c6_diagnostic_request", None)
        if request_data is not None:
            combined["_c6_sleeve_results"] = results
            combined["_c6_states"] = states
            combined["_c6_score_trace"] = self._c6_score_trace
        self.last_result = combined
        return combined
