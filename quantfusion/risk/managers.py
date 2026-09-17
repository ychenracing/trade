"""Portfolio risk managers with explicit cycle and lifetime epoch snapshots."""
from __future__ import annotations

import math
from typing import Any

from quantfusion.risk.managers_legacy import (
    ConfirmedDrawdownRiskManager as _LegacyConfirmedDrawdownRiskManager,
)
from quantfusion.risk.managers_legacy import (
    PersistentRiskManager,
    RecoverableDrawdownRiskManager as _LegacyRecoverableDrawdownRiskManager,
    RiskManager,
)


def _append_epoch_snapshot(manager: Any, current_assets: float, date_str: str) -> None:
    """Emit one close-known snapshot without transferring state ownership."""
    assets = float(current_assets)
    cycle_peak = float(manager.peak_assets)
    lifetime_peak = float(getattr(manager, "lifetime_peak_assets", cycle_peak))
    terminal_drawdown = float(getattr(manager.policy, "terminal_drawdown", 0.28))
    values = (assets, cycle_peak, lifetime_peak, terminal_drawdown)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("account risk epoch snapshot requires finite values")
    if assets < 0.0 or cycle_peak <= 0.0 or lifetime_peak <= 0.0:
        raise ValueError("account risk epoch snapshot requires non-negative equity and peaks")
    if assets > cycle_peak + 1e-8:
        raise ValueError("cycle peak must include current account equity")
    if cycle_peak > lifetime_peak + 1e-8:
        raise ValueError("cycle peak cannot exceed lifetime peak")
    manager.audit_events.append(
        {
            "date": date_str,
            "event": "account_risk_epoch_snapshot",
            "equity": assets,
            "cycle_peak_assets": cycle_peak,
            "lifetime_peak_assets": lifetime_peak,
            "terminal_drawdown": terminal_drawdown,
            "risk_alert_active": bool(getattr(manager, "alert_active", False)),
            "terminal_lock_active": bool(getattr(manager, "terminal_lock", False)),
            "cycle_lock_active": bool(
                getattr(manager, "persistent_lock", False)
                and not getattr(manager, "terminal_lock", False)
            ),
            "risk_state_complete": True,
            "peak_owner": "risk_manager",
        }
    )


class ConfirmedDrawdownRiskManager(_LegacyConfirmedDrawdownRiskManager):
    """Retain confirmed-lock behavior while exposing both explicit peaks."""

    def __init__(self, cfg: dict, policy: Any) -> None:
        super().__init__(cfg, policy)
        self.lifetime_peak_assets = 0.0
        self.terminal_lock = False
        self.cycle_lock_count = 0

    def check_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        *,
        trading_dates: list[Any] | None = None,
        date_to_pos: dict[Any, int] | None = None,
    ) -> str | None:
        self.lifetime_peak_assets = max(
            float(self.lifetime_peak_assets), float(current_assets)
        )
        outcome = super().check_portfolio_risk(
            current_assets,
            date_str,
            trading_dates=trading_dates,
            date_to_pos=date_to_pos,
        )
        # A confirmed manager has no automatic rearm. Its persistent lock is
        # therefore terminal for ordinary account-risk admission.
        self.terminal_lock = bool(self.persistent_lock)
        _append_epoch_snapshot(self, current_assets, date_str)
        return outcome


class RecoverableDrawdownRiskManager(_LegacyRecoverableDrawdownRiskManager):
    """Own risk epochs and publish their validated close-known snapshot."""

    def check_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        *,
        trading_dates: list[Any] | None = None,
        date_to_pos: dict[Any, int] | None = None,
    ) -> str | None:
        outcome = super().check_portfolio_risk(
            current_assets,
            date_str,
            trading_dates=trading_dates,
            date_to_pos=date_to_pos,
        )
        _append_epoch_snapshot(self, current_assets, date_str)
        return outcome


__all__ = [
    "RiskManager",
    "PersistentRiskManager",
    "ConfirmedDrawdownRiskManager",
    "RecoverableDrawdownRiskManager",
]
