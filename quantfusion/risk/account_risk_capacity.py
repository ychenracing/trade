"""Dual-peak account-risk capacity and causal buy admission."""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from quantfusion.config.overlay import SYMBOL_SUB_INDUSTRY
from quantfusion.domain.models import Signal
from quantfusion.domain.rules import limit_pct_for_code, require_finite, require_int

_EPSILON = 1e-8


@dataclass(frozen=True, slots=True)
class AccountRiskSnapshot:
    """One close-known risk-manager snapshot consumed by account coverage."""

    cycle_peak_assets: float
    lifetime_peak_assets: float
    terminal_drawdown: float
    shock_floor: float = 0.0
    risk_alert_active: bool = False
    terminal_lock_active: bool = False
    cycle_lock_active: bool = False
    state_complete: bool = True
    source: str = "explicit"


def _finite_score(score: Callable[[str], float], symbol: str) -> float:
    value = float(score(symbol))
    if not math.isfinite(value):
        raise ValueError(f"allocation score must be finite for {symbol}")
    return value


def _validated_snapshot(
    equity: float,
    peak: float,
    cfg: Mapping[str, Any],
    *,
    cycle_peak_assets: float | None = None,
    lifetime_peak_assets: float | None = None,
    terminal_drawdown: float | None = None,
    shock_floor: float = 0.0,
    risk_alert_active: bool = False,
    terminal_lock_active: bool = False,
    cycle_lock_active: bool = False,
    state_complete: bool = True,
    source: str = "explicit",
) -> AccountRiskSnapshot:
    equity = require_finite("account equity", equity, min_value=0.0)
    fallback_peak = require_finite("account peak", peak, min_value=0.01)
    cycle_peak = require_finite(
        "account cycle peak",
        fallback_peak if cycle_peak_assets is None else cycle_peak_assets,
        min_value=0.01,
    )
    lifetime_peak = require_finite(
        "account lifetime peak",
        fallback_peak if lifetime_peak_assets is None else lifetime_peak_assets,
        min_value=0.01,
    )
    if equity > lifetime_peak + _EPSILON:
        raise ValueError("account lifetime peak must include current equity")
    if equity > cycle_peak + _EPSILON:
        raise ValueError("account cycle peak must include current equity")
    if cycle_peak > lifetime_peak + _EPSILON:
        raise ValueError("account cycle peak cannot exceed lifetime peak")
    resolved_terminal = require_finite(
        "terminal_drawdown",
        cfg.get("terminal_drawdown", 0.28)
        if terminal_drawdown is None
        else terminal_drawdown,
        min_value=0.0,
        max_value=1.0,
        inclusive_max=False,
    )
    resolved_shock_floor = require_finite(
        "shock equity floor", shock_floor, min_value=0.0, max_value=lifetime_peak
    )
    for label, value in (
        ("risk_alert_active", risk_alert_active),
        ("terminal_lock_active", terminal_lock_active),
        ("cycle_lock_active", cycle_lock_active),
        ("state_complete", state_complete),
    ):
        if type(value) is not bool:
            raise ValueError(f"{label} must be boolean")
    return AccountRiskSnapshot(
        cycle_peak_assets=cycle_peak,
        lifetime_peak_assets=lifetime_peak,
        terminal_drawdown=resolved_terminal,
        shock_floor=resolved_shock_floor,
        risk_alert_active=risk_alert_active,
        terminal_lock_active=terminal_lock_active,
        cycle_lock_active=cycle_lock_active,
        state_complete=state_complete,
        source=source,
    )


def account_budget_capacity(
    equity: float,
    peak: float,
    cfg: Mapping[str, Any],
    book_count: int,
    *,
    cycle_peak_assets: float | None = None,
    lifetime_peak_assets: float | None = None,
    terminal_drawdown: float | None = None,
    shock_floor: float = 0.0,
    risk_alert_active: bool = False,
    terminal_lock_active: bool = False,
    cycle_lock_active: bool = False,
    state_complete: bool = True,
    stress_fraction: float | None = None,
) -> dict[str, float | bool | str]:
    """Calculate deployable risk from cycle and lifetime responsibilities."""
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
        state_complete=state_complete,
    )
    book_count = require_int("book_count", book_count, min_value=0)
    daily = require_finite(
        "daily_loss_limit",
        cfg["daily_loss_limit"],
        min_value=0.000001,
        max_value=1.0,
        inclusive_max=False,
    )
    costs = {
        key: require_finite(key, cfg[key], min_value=0.0)
        for key in ("slippage", "commission_rate", "stamp_duty", "min_commission")
    }
    maximum = require_finite(
        "max_total_weight", cfg["max_total_weight"], min_value=0.0, max_value=1.0
    )
    cycle_floor = 0.82 * snapshot.cycle_peak_assets
    lifetime_terminal_floor = (
        1.0 - snapshot.terminal_drawdown
    ) * snapshot.lifetime_peak_assets
    base_floor = max(cycle_floor, lifetime_terminal_floor)
    effective_floor = max(
        base_floor,
        snapshot.shock_floor if snapshot.risk_alert_active else 0.0,
    )
    base_stress = 1.0 - (1.0 - daily) ** 2
    resolved_stress = require_finite(
        "account stress fraction",
        base_stress if stress_fraction is None else stress_fraction,
        min_value=0.000001,
        max_value=1.0,
        inclusive_max=False,
    )
    cost_rate = (
        2.0 * costs["slippage"]
        + 2.0 * costs["commission_rate"]
        + costs["stamp_duty"]
    )
    fixed = 2.0 * book_count * costs["min_commission"]
    remaining = max(0.0, equity - effective_floor - fixed)
    ordinary_cap = maximum * equity
    locked = bool(
        snapshot.terminal_lock_active
        or snapshot.cycle_lock_active
        or not snapshot.state_complete
    )
    gross_cap = 0.0 if locked else min(
        ordinary_cap, remaining / (resolved_stress + cost_rate)
    )
    return {
        "equity": equity,
        "cycle_peak": snapshot.cycle_peak_assets,
        "lifetime_peak": snapshot.lifetime_peak_assets,
        "cycle_peak_assets": snapshot.cycle_peak_assets,
        "lifetime_peak_assets": snapshot.lifetime_peak_assets,
        "cycle_floor": cycle_floor,
        "lifetime_terminal_floor": lifetime_terminal_floor,
        "base_effective_floor": base_floor,
        "floor": base_floor,
        "effective_policy_floor": effective_floor,
        "terminal_drawdown": snapshot.terminal_drawdown,
        "terminal_lock_active": snapshot.terminal_lock_active,
        "cycle_lock_active": snapshot.cycle_lock_active,
        "risk_state_complete": snapshot.state_complete,
        "stress_fraction": base_stress,
        "systemic_stress_fraction": resolved_stress + cost_rate,
        "cost_rate": cost_rate,
        "fixed_cost_reserve": fixed,
        "remaining_loss_budget": remaining,
        "ordinary_gross_cap": ordinary_cap,
        "gross_cap": gross_cap,
        "risk_epoch_source": snapshot.source,
    }


def _group_debit(
    books: Sequence[tuple[int, str, str, int, float]],
    buy_values: Sequence[tuple[str, float]],
    cfg: Mapping[str, Any],
    *,
    cost_rate: float,
    stress_fraction: float,
    sold_shares: Mapping[tuple[int, str, str], int] | None = None,
) -> float:
    sold = sold_shares or {}
    base_rate = stress_fraction + cost_rate
    group_gap: dict[str, float] = {}
    group_base: dict[str, float] = {}
    gross = 0.0
    for state, symbol, strategy, shares, price in books:
        remaining = max(0, shares - int(sold.get((state, symbol, strategy), 0)))
        value = remaining * price
        gross += value
        group = SYMBOL_SUB_INDUSTRY.get(symbol, symbol)
        group_gap[group] = group_gap.get(group, 0.0) + value * (
            limit_pct_for_code(symbol, cfg) + cost_rate
        )
        group_base[group] = group_base.get(group, 0.0) + value * base_rate
    for symbol, value in buy_values:
        if value <= 0.0:
            continue
        gross += value
        group = SYMBOL_SUB_INDUSTRY.get(symbol, symbol)
        group_gap[group] = group_gap.get(group, 0.0) + value * (
            limit_pct_for_code(symbol, cfg) + cost_rate
        )
        group_base[group] = group_base.get(group, 0.0) + value * base_rate
    excess = max(
        (
            group_gap.get(group, 0.0) - group_base.get(group, 0.0)
            for group in set(group_gap) | set(group_base)
        ),
        default=0.0,
    )
    return base_rate * gross + excess


def _allocate_buys(
    books: Sequence[tuple[int, str, str, int, float]],
    buys: Sequence[tuple[int, Signal, float]],
    score: Callable[[str], float],
    cfg: Mapping[str, Any],
    receipt: Mapping[str, Any],
    *,
    block_all_buys: bool,
    blocked_indexes: set[int],
) -> tuple[list[float], float, float]:
    if not buys:
        current = _group_debit(
            books,
            (),
            cfg,
            cost_rate=float(receipt["cost_rate"]),
            stress_fraction=float(receipt["stress_fraction"]),
        )
        return [], current, 0.0
    current_gross = sum(shares * price for _, _, _, shares, price in books)
    accepted: list[tuple[str, float]] = []
    scales = [0.0] * len(buys)
    order = sorted(
        range(len(buys)),
        key=lambda index: (
            -_finite_score(score, buys[index][1].symbol),
            buys[index][1].symbol,
            buys[index][0],
            buys[index][1].strategy_name,
        ),
    )
    current_debit = _group_debit(
        books,
        (),
        cfg,
        cost_rate=float(receipt["cost_rate"]),
        stress_fraction=float(receipt["stress_fraction"]),
    )
    for index in order:
        state, signal, requested = buys[index]
        del state
        requested = require_finite("requested buy value", requested, min_value=0.0)
        if requested <= 0.0 or block_all_buys or index in blocked_indexes:
            continue
        accepted_total = sum(value for _, value in accepted)
        gross_room = max(
            0.0,
            float(receipt["gross_cap"]) - current_gross - accepted_total,
        )
        upper = min(requested, gross_room)
        if upper <= _EPSILON:
            continue

        def fits(value: float) -> bool:
            debit = _group_debit(
                books,
                [*accepted, (signal.symbol, value)],
                cfg,
                cost_rate=float(receipt["cost_rate"]),
                stress_fraction=float(receipt["stress_fraction"]),
            )
            return debit <= float(receipt["remaining_loss_budget"]) + _EPSILON

        if fits(upper):
            approved = upper
        else:
            low, high = 0.0, upper
            for _ in range(60):
                middle = (low + high) / 2.0
                if fits(middle):
                    low = middle
                else:
                    high = middle
            approved = low
        scales[index] = min(1.0, max(0.0, approved / requested))
        accepted.append((signal.symbol, approved))
    full_debit = _group_debit(
        books,
        [(signal.symbol, value) for _, signal, value in buys],
        cfg,
        cost_rate=float(receipt["cost_rate"]),
        stress_fraction=float(receipt["stress_fraction"]),
    )
    return scales, current_debit, max(0.0, full_debit - current_debit)
