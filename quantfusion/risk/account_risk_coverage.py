"""Unified dual-peak account-risk coverage orchestration."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import replace
from typing import Any

import pandas as pd

from quantfusion.domain.models import Signal
from quantfusion.domain.rules import floor_to_lot, require_int
from quantfusion.risk import account_budget_legacy as _legacy
from quantfusion.risk.account_risk_capacity import (
    AccountRiskSnapshot,
    _allocate_buys,
    _validated_snapshot,
    account_budget_capacity,
)
from quantfusion.risk.account_risk_reallocation import (
    ExecutionContext,
    _plan_capacity_reallocation,
)
from quantfusion.risk.overlay.models import RiskAction

_LEGACY_PLAN = _legacy.plan_account_risk_budget
_EPSILON = 1e-8

observed_direct_losses = _legacy.observed_direct_losses
observed_shock_stress = _legacy.observed_shock_stress

_ACTIVE_SNAPSHOT: ContextVar[AccountRiskSnapshot | None] = ContextVar(
    "account_risk_snapshot", default=None
)
_ACTIVE_EXECUTION: ContextVar[ExecutionContext | None] = ContextVar(
    "account_risk_execution", default=None
)


def plan_account_risk_budget(
    equity: float,
    peak: float,
    cfg: Mapping[str, Any],
    books: Sequence[tuple[int, str, str, int, float]],
    buys: Sequence[tuple[int, Signal, float]],
    score: Callable[[str], float],
    *,
    date_str: str,
    stress_by_symbol: Mapping[str, float] | None = None,
    direct_loss_by_symbol: Mapping[str, float] | None = None,
    shock_episode_active: bool = False,
    shock_floor: float = 0.0,
    preserve_strategy_valid_holdings: bool = False,
    risk_alert_active: bool = False,
    weak_book_ids: set[tuple[int, str, str]] | None = None,
    strategy_handoff_symbols: set[str] | None = None,
    repeated_proven_reentry_symbols: set[str] | None = None,
    proven_early_dual_book_ids: set[tuple[int, str, str]] | None = None,
    shock_reduced_book_ids: set[tuple[int, str, str]] | None = None,
    confirmed_shock_reduced_book_ids: set[tuple[int, str, str]] | None = None,
    crowded_shock_reduced_book_ids: set[tuple[int, str, str]] | None = None,
    cycle_peak_assets: float | None = None,
    lifetime_peak_assets: float | None = None,
    terminal_drawdown: float | None = None,
    terminal_lock_active: bool = False,
    cycle_lock_active: bool = False,
    risk_state_complete: bool = True,
    sellable_shares_by_book: Mapping[tuple[int, str, str], int] | None = None,
    queued_sell_book_ids: set[tuple[int, str, str]] | None = None,
) -> tuple[dict[str, Any], list[RiskAction]]:
    """Plan strong risk actions, causal buy admission and minimal reallocation."""
    active = _ACTIVE_SNAPSHOT.get()
    if active is None:
        snapshot = _validated_snapshot(
            equity,
            peak,
            cfg,
            cycle_peak_assets=cycle_peak_assets,
            lifetime_peak_assets=lifetime_peak_assets,
            terminal_drawdown=terminal_drawdown,
            shock_floor=shock_floor,
            risk_alert_active=risk_alert_active,
            terminal_lock_active=terminal_lock_active,
            cycle_lock_active=cycle_lock_active,
            state_complete=risk_state_complete,
        )
    else:
        snapshot = replace(
            active,
            shock_floor=max(active.shock_floor, float(shock_floor)),
            risk_alert_active=bool(active.risk_alert_active or risk_alert_active),
        )
    legacy_floor = max(
        snapshot.shock_floor,
        (1.0 - snapshot.terminal_drawdown) * snapshot.lifetime_peak_assets,
    )
    legacy_receipt, legacy_actions = _LEGACY_PLAN(
        equity,
        snapshot.lifetime_peak_assets,
        cfg,
        books,
        buys,
        score,
        date_str=date_str,
        stress_by_symbol=stress_by_symbol,
        direct_loss_by_symbol=direct_loss_by_symbol,
        shock_episode_active=shock_episode_active,
        shock_floor=legacy_floor,
        preserve_strategy_valid_holdings=preserve_strategy_valid_holdings,
        risk_alert_active=snapshot.risk_alert_active,
        weak_book_ids=weak_book_ids,
        strategy_handoff_symbols=strategy_handoff_symbols,
        repeated_proven_reentry_symbols=repeated_proven_reentry_symbols,
        proven_early_dual_book_ids=proven_early_dual_book_ids,
        shock_reduced_book_ids=shock_reduced_book_ids,
        confirmed_shock_reduced_book_ids=confirmed_shock_reduced_book_ids,
        crowded_shock_reduced_book_ids=crowded_shock_reduced_book_ids,
    )
    shock_confirmed = bool(legacy_receipt["observed_shock_confirmed"])
    shock_active = bool(legacy_receipt["shock_episode_active"])
    effective_shock_floor = (
        legacy_floor if shock_confirmed or snapshot.risk_alert_active else 0.0
    )
    execution = _ACTIVE_EXECUTION.get()
    if execution is None and (
        sellable_shares_by_book is not None or queued_sell_book_ids is not None
    ):
        execution = ExecutionContext(
            sellable_shares=dict(sellable_shares_by_book or {}),
            queued_sell_books=frozenset(queued_sell_book_ids or ()),
            rearm_consumption_ready=True,
            rearm_pending_validation=False,
        )
    state_complete = bool(
        snapshot.state_complete
        and (execution is None or execution.rearm_consumption_ready)
    )
    receipt = account_budget_capacity(
        equity,
        snapshot.lifetime_peak_assets,
        cfg,
        len({
            (state, symbol, strategy)
            for state, symbol, strategy, shares, _ in books
            if shares
        } | {
            (state, signal.symbol, signal.strategy_name)
            for state, signal, _ in buys
            if signal.target_shares
        }),
        cycle_peak_assets=snapshot.cycle_peak_assets,
        lifetime_peak_assets=snapshot.lifetime_peak_assets,
        terminal_drawdown=snapshot.terminal_drawdown,
        shock_floor=effective_shock_floor,
        risk_alert_active=bool(shock_confirmed or snapshot.risk_alert_active),
        terminal_lock_active=snapshot.terminal_lock_active,
        cycle_lock_active=snapshot.cycle_lock_active,
        state_complete=state_complete,
        stress_fraction=(
            float(legacy_receipt["systemic_stress_fraction"])
            - float(legacy_receipt["cost_rate"])
        ),
    )
    blocked_indexes = set(legacy_receipt.get("shock_reduced_pyramid_buy_indexes", ()))
    scales, current_gap, requested_gap = _allocate_buys(
        books,
        buys,
        score,
        cfg,
        receipt,
        block_all_buys=bool(
            shock_active
            or snapshot.terminal_lock_active
            or snapshot.cycle_lock_active
            or not state_complete
        ),
        blocked_indexes=blocked_indexes,
    )
    strong_actions = list(legacy_actions) if (
        shock_confirmed or snapshot.risk_alert_active
    ) else []
    capacity_actions: list[RiskAction] = []
    capacity_plans: list[dict[str, Any]] = []
    if not (
        shock_active
        or snapshot.risk_alert_active
        or snapshot.terminal_lock_active
        or snapshot.cycle_lock_active
        or not state_complete
    ):
        capacity_actions, capacity_plans = _plan_capacity_reallocation(
            books,
            buys,
            scales,
            score,
            cfg,
            receipt,
            date_str=date_str,
            execution=execution,
        )
    gross = sum(shares * price for _, _, _, shares, price in books)
    receipt.update(
        {
            **{
                key: value
                for key, value in legacy_receipt.items()
                if key not in {
                    "equity",
                    "lifetime_peak",
                    "floor",
                    "effective_policy_floor",
                    "remaining_loss_budget",
                    "ordinary_gross_cap",
                    "gross_cap",
                    "buy_scales",
                    "buy_scale",
                    "buy_gross_scale",
                    "buy_gap_scale",
                    "current_gap_debit",
                    "requested_buy_gap_debit",
                }
            },
            "gross_before": gross,
            "buy_scales": scales,
            "buy_scale": min(scales, default=1.0),
            "buy_envelope_binding": any(scale < 1.0 - _EPSILON for scale in scales),
            "buy_gross_scale": min(scales, default=1.0),
            "buy_gap_scale": min(scales, default=1.0),
            "current_gap_debit": current_gap,
            "requested_buy_gap_debit": requested_gap,
            "normal_budget_trim_suppressed": True,
            "capacity_reallocation_triggered": bool(capacity_actions),
            "capacity_reallocation_plans": capacity_plans,
            "rearm_consumption_ready": (
                True if execution is None else execution.rearm_consumption_ready
            ),
            "rearm_pending_validation": (
                False if execution is None else execution.rearm_pending_validation
            ),
            "policy_mode": "ACCOUNT_RISK_COVERAGE",
            "health_status": "EVALUATED" if state_complete else "NOT_READY",
        }
    )
    return receipt, [*strong_actions, *capacity_actions]


def _latest_snapshot(
    events: Sequence[Mapping[str, Any]],
    *,
    date_str: str,
    assets: float,
    peak: float,
    cfg: Mapping[str, Any],
    shock_floor: float,
    risk_alert_active: bool | None,
) -> AccountRiskSnapshot:
    event = next(
        (
            item
            for item in reversed(events)
            if item.get("event") == "account_risk_epoch_snapshot"
            and str(item.get("date", "")) <= date_str
        ),
        None,
    )
    if event is None:
        # Direct unit/research callers keep an explicit single-peak fallback.
        return _validated_snapshot(
            assets,
            peak,
            cfg,
            shock_floor=shock_floor,
            risk_alert_active=bool(risk_alert_active),
            source="legacy_explicit_fallback",
        )
    alert = bool(event.get("risk_alert_active", False))
    if risk_alert_active is not None:
        alert = bool(risk_alert_active)
    return _validated_snapshot(
        assets,
        peak,
        cfg,
        cycle_peak_assets=float(event["cycle_peak_assets"]),
        lifetime_peak_assets=float(event["lifetime_peak_assets"]),
        terminal_drawdown=float(event["terminal_drawdown"]),
        shock_floor=shock_floor,
        risk_alert_active=alert,
        terminal_lock_active=bool(event.get("terminal_lock_active", False)),
        cycle_lock_active=bool(event.get("cycle_lock_active", False)),
        state_complete=bool(event.get("risk_state_complete", True)),
        source="risk_manager_snapshot",
    )


def _execution_context(
    states: Sequence[Any],
    date_str: str,
    events: Sequence[Mapping[str, Any]],
) -> ExecutionContext:
    queued = frozenset(
        (state_index, signal.symbol, signal.strategy_name)
        for state_index, state in enumerate(states)
        for signal, _ in state.pending
        if signal.direction == "sell"
    )
    sellable: dict[tuple[int, str, str], int] = {}
    has_positions = False
    has_executable_pending = any(
        signal.direction == "sell"
        or (signal.direction == "buy" and str(signal.signal_date) < date_str)
        for state in states
        for signal, _ in state.pending
    )
    for state_index, state in enumerate(states):
        for symbol, positions in state.sleeve.positions.items():
            for strategy, position in positions.items():
                shares = require_int("held shares", position.shares, min_value=0)
                if not shares:
                    continue
                has_positions = True
                book = (state_index, symbol, strategy)
                entry_date = str(getattr(position, "entry_date", ""))
                last_buy_date = str(getattr(position, "last_buy_date", ""))
                if book in queued or entry_date >= date_str or last_buy_date >= date_str:
                    sellable[book] = 0
                else:
                    sellable[book] = floor_to_lot(shares)
    last_rearm = next(
        (
            str(event.get("date"))
            for event in reversed(events)
            if event.get("event") == "portfolio_drawdown_rearmed"
            and event.get("date")
        ),
        None,
    )
    validation = next(
        (
            event
            for event in reversed(events)
            if event.get("event") == "account_budget_envelope"
            and event.get("risk_epoch_consumption_ready") is True
            and (
                last_rearm is None
                or str(event.get("date", "")) >= last_rearm
            )
        ),
        None,
    )
    pending_validation = last_rearm is not None and validation is None
    ready = not pending_validation or (
        not has_positions and not has_executable_pending
    )
    return ExecutionContext(
        sellable_shares=sellable,
        queued_sell_books=queued,
        rearm_consumption_ready=ready,
        rearm_pending_validation=pending_validation,
    )


def apply_account_risk_budget(
    states: Sequence[Any],
    date: pd.Timestamp,
    assets: float,
    peak: float,
    cfg: Mapping[str, Any],
    score: Callable[[str], float],
    events: list[dict[str, Any]],
    *,
    shock_floor: float = 0.0,
    preserve_strategy_valid_holdings: bool = False,
    risk_alert_active: bool | None = None,
    portfolio_evidence_buy_symbols: set[str] | None = None,
) -> None:
    """Apply the unified plan through the existing queue and fill adapter."""
    date_str = date.strftime("%Y-%m-%d")
    snapshot = _latest_snapshot(
        events,
        date_str=date_str,
        assets=assets,
        peak=peak,
        cfg=cfg,
        shock_floor=shock_floor,
        risk_alert_active=risk_alert_active,
    )
    execution = _execution_context(states, date_str, events)
    snapshot_token = _ACTIVE_SNAPSHOT.set(snapshot)
    execution_token = _ACTIVE_EXECUTION.set(execution)
    try:
        _legacy.apply_account_risk_budget(
            states,
            date,
            assets,
            peak,
            cfg,
            score,
            events,
            shock_floor=shock_floor,
            preserve_strategy_valid_holdings=preserve_strategy_valid_holdings,
            risk_alert_active=risk_alert_active,
            portfolio_evidence_buy_symbols=portfolio_evidence_buy_symbols,
        )
    finally:
        _ACTIVE_EXECUTION.reset(execution_token)
        _ACTIVE_SNAPSHOT.reset(snapshot_token)
    latest = next(
        (
            event
            for event in reversed(events)
            if event.get("event") == "account_budget_envelope"
            and event.get("date") == date_str
        ),
        None,
    )
    if latest is not None:
        latest["production_mechanism"] = "account_risk_coverage"
        latest["risk_epoch_consumption_ready"] = execution.rearm_consumption_ready
        latest["risk_epoch"] = {
            "cycle_peak_assets": snapshot.cycle_peak_assets,
            "lifetime_peak_assets": snapshot.lifetime_peak_assets,
            "terminal_drawdown": snapshot.terminal_drawdown,
            "terminal_lock_active": snapshot.terminal_lock_active,
            "cycle_lock_active": snapshot.cycle_lock_active,
            "source": snapshot.source,
        }


def account_budget_status(
    events: Sequence[Mapping[str, Any]], enabled: bool
) -> dict[str, Any]:
    """Report the evaluated account-risk-coverage state."""
    last = next(
        (
            dict(event)
            for event in reversed(events)
            if event.get("event") == "account_budget_envelope"
        ),
        None,
    )
    return {
        "enabled": enabled,
        # Historical consumers remain readable during the preregistered gate.
        "mechanism": "AB5",
        "production_mechanism": "account_risk_coverage",
        "status": (
            "APPLIED"
            if enabled and last is not None and last.get("health_status") != "NOT_READY"
            else "NOT_READY"
            if enabled and last is not None
            else "NOT_EVALUATED"
            if enabled
            else "DISABLED_DIAGNOSTIC"
        ),
        "latest": last,
    }


# Reuse the mature state/queue adapter while replacing its planning kernel.
_legacy.plan_account_risk_budget = plan_account_risk_budget
