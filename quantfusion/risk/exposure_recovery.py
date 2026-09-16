"""Causal recovery hysteresis for AB5 account buy headroom."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

import pandas as pd

from quantfusion.risk.account_budget import (
    apply_account_risk_budget as _apply_account_risk_budget,
)


class AB5RecoveryState(str, Enum):
    """Track whether AB5-reduced exposure is ready to re-expand."""

    NORMAL = "NORMAL"
    AB5_REDUCED = "AB5_REDUCED"
    AB5_RECOVERY_PENDING = "AB5_RECOVERY_PENDING"


@dataclass(frozen=True)
class AB5RecoveryDecision:
    """One close-known recovery state and its optional buy-cap ceiling."""

    state: AB5RecoveryState
    buy_gross_cap_ceiling: float | None


def _finite_cap(label: str, value: Any) -> float:
    try:
        cap = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be finite and non-negative") from exc
    if not math.isfinite(cap) or cap < 0.0:
        raise ValueError(f"{label} must be finite and non-negative")
    return cap


def _latest_account_budget_envelope(
    events: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    return next(
        (
            event
            for event in reversed(events)
            if event.get("event") == "account_budget_envelope"
        ),
        None,
    )


def _envelope_for_date(
    events: Sequence[Mapping[str, Any]], date_str: str
) -> Mapping[str, Any] | None:
    return next(
        (
            event
            for event in reversed(events)
            if event.get("event") == "account_budget_envelope"
            and event.get("date") == date_str
        ),
        None,
    )


def filled_ab5_reduction_caps(
    states: Sequence[Any],
    date_str: str,
    events: Sequence[Mapping[str, Any]],
) -> list[float]:
    """Return source-close canonical gross caps for AB5 reductions filled today."""
    caps: list[float] = []
    for state in states:
        sleeve = getattr(state, "sleeve", None)
        for trade in getattr(sleeve, "trades", ()):
            if str(getattr(trade, "date", "")) != date_str:
                continue
            if str(getattr(trade, "direction", "")) != "sell":
                continue
            if (
                str(getattr(trade, "reason", "")).split(":", 1)[0]
                != "account_budget_trim"
            ):
                continue
            signal_date = str(getattr(trade, "signal_date", ""))
            if not signal_date:
                raise ValueError("filled AB5 reduction requires its source signal date")
            source = _envelope_for_date(events, signal_date)
            if source is None:
                raise ValueError(
                    "filled AB5 reduction requires its source account budget envelope"
                )
            caps.append(_finite_cap("source AB5 gross cap", source.get("gross_cap")))
    return caps


def _active_recovery_cap(envelope: Mapping[str, Any] | None) -> float | None:
    if envelope is None:
        return None
    state = AB5RecoveryState(
        envelope.get("ab5_recovery_state", AB5RecoveryState.NORMAL.value)
    )
    if state is AB5RecoveryState.NORMAL:
        return None
    if "ab5_recovery_buy_gross_cap" not in envelope:
        raise ValueError("active AB5 recovery requires its retained buy gross cap")
    return _finite_cap(
        "AB5 recovery buy gross cap",
        envelope.get("ab5_recovery_buy_gross_cap"),
    )


def _recovery_safe(envelope: Mapping[str, Any] | None) -> bool:
    """Advance only after canonical AB5 requests no new reduction at the close."""
    if not envelope or envelope.get("event") != "account_budget_envelope":
        return False
    if envelope.get("shock_episode_active"):
        return False
    orders = envelope.get("new_reduction_orders")
    if isinstance(orders, bool) or not isinstance(orders, int) or orders < 0:
        return False
    return orders == 0


def next_ab5_recovery_decision(
    *,
    previous_envelope: Mapping[str, Any] | None,
    new_reduction_caps: Sequence[float],
) -> AB5RecoveryDecision:
    """Advance the fixed recovery lifecycle without changing AB5 thresholds."""
    previous_state = AB5RecoveryState(
        previous_envelope.get("ab5_recovery_state", AB5RecoveryState.NORMAL.value)
        if previous_envelope is not None
        else AB5RecoveryState.NORMAL.value
    )
    carried_cap = _active_recovery_cap(previous_envelope)
    if new_reduction_caps:
        source_cap = min(
            _finite_cap("source AB5 gross cap", cap) for cap in new_reduction_caps
        )
        ceiling = source_cap if carried_cap is None else min(carried_cap, source_cap)
        return AB5RecoveryDecision(AB5RecoveryState.AB5_REDUCED, ceiling)
    if previous_state is AB5RecoveryState.NORMAL:
        return AB5RecoveryDecision(AB5RecoveryState.NORMAL, None)
    if carried_cap is None:
        raise ValueError("active AB5 recovery requires a retained buy gross cap")
    if not _recovery_safe(previous_envelope):
        return AB5RecoveryDecision(AB5RecoveryState.AB5_REDUCED, carried_cap)
    if previous_state is AB5RecoveryState.AB5_REDUCED:
        return AB5RecoveryDecision(AB5RecoveryState.AB5_RECOVERY_PENDING, carried_cap)
    return AB5RecoveryDecision(AB5RecoveryState.NORMAL, None)


def apply_account_risk_budget_with_recovery(
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
    """Apply canonical AB5 while delaying re-expansion of account buy headroom."""
    date_str = date.strftime("%Y-%m-%d")
    previous = _latest_account_budget_envelope(events)
    source_caps = filled_ab5_reduction_caps(states, date_str, events)
    decision = next_ab5_recovery_decision(
        previous_envelope=previous,
        new_reduction_caps=source_caps,
    )

    event_start = len(events)
    options: dict[str, Any] = {}
    if shock_floor != 0.0:
        options["shock_floor"] = shock_floor
    if preserve_strategy_valid_holdings:
        options["preserve_strategy_valid_holdings"] = True
    if risk_alert_active is not None:
        options["risk_alert_active"] = risk_alert_active
    if portfolio_evidence_buy_symbols is not None:
        options["portfolio_evidence_buy_symbols"] = portfolio_evidence_buy_symbols
    if decision.buy_gross_cap_ceiling is not None:
        options["buy_gross_cap_ceiling"] = decision.buy_gross_cap_ceiling

    _apply_account_risk_budget(
        states,
        date,
        assets,
        peak,
        cfg,
        score,
        events,
        **options,
    )
    envelope = next(
        (
            event
            for event in reversed(events[event_start:])
            if event.get("event") == "account_budget_envelope"
            and event.get("date") == date_str
        ),
        None,
    )
    if envelope is None:
        raise RuntimeError("canonical AB5 did not publish its account budget envelope")
    if decision.buy_gross_cap_ceiling is not None:
        canonical_cap = _finite_cap("canonical AB5 gross cap", envelope.get("gross_cap"))
        envelope.update(
            ab5_recovery_state=decision.state.value,
            ab5_recovery_buy_gross_cap=min(
                decision.buy_gross_cap_ceiling,
                canonical_cap,
            ),
        )
