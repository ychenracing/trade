"""Close-known account risk epochs published by the canonical risk manager."""
from __future__ import annotations

import math
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

_EPSILON = 1e-8


@dataclass(frozen=True, slots=True)
class PublishedRiskEpoch:
    """Validated cycle/lifetime state for one account close."""

    date: str
    equity: float
    cycle_peak_assets: float
    lifetime_peak_assets: float
    terminal_drawdown: float
    risk_alert_active: bool
    terminal_lock_active: bool
    cycle_lock_active: bool


_PUBLISHED_RISK_EPOCHS: ContextVar[tuple[PublishedRiskEpoch, ...]] = ContextVar(
    "published_account_risk_epochs",
    default=(),
)


def publish_account_risk_epoch(manager: Any, equity: float, date: str) -> None:
    """Publish manager-owned state without adding a new audit-event schema."""
    assets = float(equity)
    cycle_peak = float(getattr(manager, "peak_assets", 0.0))
    lifetime_peak = float(getattr(manager, "lifetime_peak_assets", cycle_peak))
    terminal_drawdown = float(getattr(manager.policy, "terminal_drawdown", 0.28))
    values = (assets, cycle_peak, lifetime_peak, terminal_drawdown)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("account risk epoch requires finite manager state")
    if assets < 0.0 or cycle_peak < 0.0 or lifetime_peak < 0.0:
        raise ValueError("account risk epoch requires nonnegative manager state")
    # Zero-valued startup is not yet an account-risk identity. The ordinary
    # engine can continue initialization without publishing an unusable epoch.
    if cycle_peak <= 0.0 or lifetime_peak <= 0.0:
        return
    if assets > cycle_peak + _EPSILON:
        raise ValueError("cycle peak must include current account equity")
    if cycle_peak > lifetime_peak + _EPSILON:
        raise ValueError("cycle peak cannot exceed lifetime peak")
    snapshot = PublishedRiskEpoch(
        date=str(date),
        equity=assets,
        cycle_peak_assets=cycle_peak,
        lifetime_peak_assets=lifetime_peak,
        terminal_drawdown=terminal_drawdown,
        risk_alert_active=bool(getattr(manager, "alert_active", False)),
        terminal_lock_active=bool(getattr(manager, "terminal_lock", False)),
        cycle_lock_active=bool(
            getattr(manager, "persistent_lock", False)
            and not getattr(manager, "terminal_lock", False)
        ),
    )
    published = _PUBLISHED_RISK_EPOCHS.get()
    _PUBLISHED_RISK_EPOCHS.set((*published[-31:], snapshot))


def consume_account_risk_epoch(
    *,
    date: str,
    equity: float,
    lifetime_peak_assets: float,
) -> PublishedRiskEpoch | None:
    """Consume the matching portfolio epoch and discard stale thread-local state."""
    published = _PUBLISHED_RISK_EPOCHS.get()
    _PUBLISHED_RISK_EPOCHS.set(())
    assets = float(equity)
    lifetime_peak = float(lifetime_peak_assets)
    for snapshot in reversed(published):
        if snapshot.date != str(date):
            continue
        if not math.isclose(snapshot.equity, assets, rel_tol=0.0, abs_tol=1e-6):
            continue
        if not math.isclose(
            snapshot.lifetime_peak_assets,
            lifetime_peak,
            rel_tol=0.0,
            abs_tol=1e-6,
        ):
            continue
        return snapshot
    return None


__all__ = [
    "PublishedRiskEpoch",
    "publish_account_risk_epoch",
    "consume_account_risk_epoch",
]
