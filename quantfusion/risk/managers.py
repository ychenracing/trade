"""Portfolio risk managers with manager-owned cycle and lifetime epochs."""
from __future__ import annotations

import pandas as pd

from quantfusion.risk.account_risk_epoch import publish_account_risk_epoch
from quantfusion.risk.managers_legacy import (
    ConfirmedDrawdownRiskManager,
    PersistentRiskManager,
    RecoverableDrawdownRiskManager as _LegacyRecoverableDrawdownRiskManager,
    RiskManager,
)


class RecoverableDrawdownRiskManager(_LegacyRecoverableDrawdownRiskManager):
    """Publish the canonical close-known epoch without changing audit events."""

    def check_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        trading_dates: list[pd.Timestamp] | None = None,
        date_to_pos: dict[pd.Timestamp, int] | None = None,
    ) -> str | None:
        outcome = super().check_portfolio_risk(
            current_assets,
            date_str,
            trading_dates=trading_dates,
            date_to_pos=date_to_pos,
        )
        publish_account_risk_epoch(self, current_assets, date_str)
        return outcome


__all__ = [
    "RiskManager",
    "PersistentRiskManager",
    "ConfirmedDrawdownRiskManager",
    "RecoverableDrawdownRiskManager",
]
