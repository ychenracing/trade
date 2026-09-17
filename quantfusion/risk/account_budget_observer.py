"""Read-only account risk-budget observation for the current production policy.

AB5 remains an account-level risk measurement and evidence mechanism.  This
module deliberately has no execution adapter: it may inspect qualified close
state and compute the existing budget model, but it cannot alter pending
orders, cash, holdings, or risk-manager state.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from quantfusion.domain.rules import require_finite, require_int
from quantfusion.risk.account_budget import (
    observed_direct_losses,
    observed_shock_stress,
    plan_account_risk_budget,
)

POLICY_MODE = "OBSERVE_ONLY"
HEALTH_EVALUATED = "EVALUATED"


def _latest_risk_alert(events: Sequence[Mapping[str, Any]]) -> bool:
    latest = next(
        (
            event
            for event in reversed(events)
            if event.get("event")
            in {"portfolio_drawdown_alert_on", "portfolio_drawdown_alert_off"}
            and event.get("sleeve") in {None, "portfolio"}
        ),
        None,
    )
    return bool(latest and latest.get("event") == "portfolio_drawdown_alert_on")


def observe_account_risk_budget(
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
) -> dict[str, Any]:
    """Evaluate the canonical AB5 budget without changing execution state.

    The existing pure planner remains the single budget formula.  Its action
    suggestions are treated as counterfactual calculation detail only and are
    never forwarded to an execution adapter or written into a pending queue.
    """
    if risk_alert_active is None:
        risk_alert_active = _latest_risk_alert(events)

    evidence_symbols = (
        None
        if portfolio_evidence_buy_symbols is None
        else set(portfolio_evidence_buy_symbols)
    )
    if evidence_symbols is not None and any(
        not isinstance(symbol, str) or not symbol for symbol in evidence_symbols
    ):
        raise ValueError("portfolio evidence buy symbols must be non-empty strings")

    date_str = date.strftime("%Y-%m-%d")
    books: list[tuple[int, str, str, int, float]] = []
    buys: list[tuple[int, Any, float]] = []
    excluded_buy_slots: list[list[int]] = []
    costs = dict(cfg)

    for state_index, state in enumerate(states):
        sleeve = state.sleeve
        for key in ("slippage", "commission_rate", "stamp_duty", "min_commission"):
            costs[key] = max(float(costs[key]), float(sleeve.cfg[key]))

        for symbol, positions in sorted(sleeve.positions.items()):
            for strategy, position in sorted(positions.items()):
                shares = require_int("held shares", position.shares, min_value=0)
                if not shares:
                    continue
                frame = state.data_map.get(symbol)
                if frame is None:
                    raise ValueError("account budget requires every held mark")
                price = require_finite(
                    "held close",
                    sleeve._latest_close_on_or_before(frame, date),
                    min_value=0.000001,
                )
                books.append((state_index, symbol, strategy, shares, price))

        for pending_index, (signal, _strategy) in enumerate(state.pending):
            if signal.direction != "buy":
                continue
            if evidence_symbols is not None and signal.symbol not in evidence_symbols:
                excluded_buy_slots.append([state_index, pending_index])
                continue
            shares = require_int("pending buy shares", signal.target_shares, min_value=0)
            price = require_finite("pending buy price", signal.price, min_value=0.000001)
            if signal.signal_date is None or pd.Timestamp(signal.signal_date) > date:
                raise ValueError("account budget requires close-known buy intents")
            buys.append((state_index, signal, shares * price))

    shock_frames = {
        symbol: frame
        for state in states
        for symbol, frame in state.data_map.items()
    }
    previous_episode = bool(
        next(
            (
                event.get("shock_episode_active", False)
                for event in reversed(events)
                if event.get("event") == "account_budget_envelope"
                and event.get("mechanism") == "AB5"
            ),
            False,
        )
    )
    receipt, _counterfactual_actions = plan_account_risk_budget(
        assets,
        peak,
        costs,
        books,
        buys,
        score,
        date_str=date_str,
        stress_by_symbol=observed_shock_stress(shock_frames, date, costs),
        direct_loss_by_symbol=observed_direct_losses(shock_frames, date, costs),
        shock_episode_active=previous_episode,
        shock_floor=shock_floor,
        preserve_strategy_valid_holdings=preserve_strategy_valid_holdings,
        risk_alert_active=risk_alert_active,
    )

    observed_buy_scales = list(receipt.pop("buy_scales", []))
    observed_buy_scale = float(receipt.pop("buy_scale", 1.0))
    if len(observed_buy_scales) != len(buys):
        raise RuntimeError("account budget observation lost buy alignment")

    observation = {
        "date": date_str,
        "event": "account_budget_envelope",
        "mechanism": "AB5",
        "policy_mode": POLICY_MODE,
        "health_status": HEALTH_EVALUATED,
        "trade_intervention_allowed": False,
        **receipt,
        "portfolio_evidence_buy_symbols": (
            None if evidence_symbols is None else sorted(evidence_symbols)
        ),
        "portfolio_excluded_buy_slots": excluded_buy_slots,
        "observed_counterfactual_buy_scales": observed_buy_scales,
        "observed_counterfactual_buy_scale": observed_buy_scale,
        "buy_shares_removed": 0,
        "new_reduction_orders": 0,
    }
    events.append(observation)
    return observation


def account_budget_observer_status(
    events: Sequence[Mapping[str, Any]], enabled: bool
) -> dict[str, Any]:
    """Expose policy and evaluation health as separate fail-closed facts."""
    last = next(
        (
            dict(event)
            for event in reversed(events)
            if event.get("event") == "account_budget_envelope"
            and event.get("mechanism") == "AB5"
        ),
        None,
    )
    if not enabled:
        return {
            "enabled": False,
            "mechanism": "AB5",
            "policy_mode": "DISABLED_DIAGNOSTIC",
            "health_status": "NOT_EVALUATED",
            "status": "DISABLED_DIAGNOSTIC",
            "latest": last,
        }
    if last is None:
        return {
            "enabled": True,
            "mechanism": "AB5",
            "policy_mode": POLICY_MODE,
            "health_status": "NOT_EVALUATED",
            "status": "NOT_EVALUATED",
            "latest": None,
        }

    policy_mode = last.get("policy_mode")
    health_status = last.get("health_status")
    observed = policy_mode == POLICY_MODE and health_status == HEALTH_EVALUATED
    return {
        "enabled": True,
        "mechanism": "AB5",
        "policy_mode": policy_mode,
        "health_status": health_status,
        "status": "OBSERVED" if observed else "INVALID_OBSERVATION",
        "latest": last,
    }
