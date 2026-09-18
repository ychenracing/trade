"""Universe-aware sector and portfolio risk result decoration."""

from __future__ import annotations

# pyright: reportAttributeAccessIssue=false


import pandas as pd

from quantfusion.domain.models import Signal
from quantfusion.risk.managers import RecoverableDrawdownRiskManager
from quantfusion.strategy.trend import BaseStrategy

# Result/audit label: sector guard observes the run's tradable pool, while the
# market-regime thermometer stays on the fixed policy.regime_symbols basket.
GUARD_SCOPE_MODE = "tradable_pool_breadth"


class UniverseRiskMixin:
    """Universe-aware sector and portfolio risk result decoration."""

    def _sector_guard_observation_data(
        self, data_map: dict[str, pd.DataFrame]
    ) -> dict[str, pd.DataFrame]:
        """Return trade-pool frames for breadth/shock/recovery (not regime_symbols).

        The fixed ``policy.regime_symbols`` basket remains the cross-pool
        regime thermometer via ``_update_market_regime``; guard must not pull
        out-of-pool names (e.g. 688008) into held-pool risk measurement.

        Quorum (``sector_guard_min_symbols``) is scaled to the trade pool in
        ``BacktestEngine._runtime_sleeve_cfg`` as ``ceil(0.8 * n_trade)``,
        matching the former ratio but keyed to the guard observation set.
        """
        tradable = getattr(self, "_tradable_symbol_codes", None) or set()
        return {
            code: data_map[code] for code in tradable if code in data_map
        }

    def _update_sector_guard(
        self,
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
        all_dates: list[pd.Timestamp],
        date_to_pos: dict[pd.Timestamp, int],
    ) -> str | None:
        """Update breadth risk from the trade pool, then advance market regime.

        The regime update runs after the sector guard so entries respect the
        freshly scored regime, and before signal generation because
        ``_evaluate_trading_day`` continues only after this method returns.
        Regime membership stays on ``policy.regime_symbols`` (unchanged).
        """
        scoped_data = self._sector_guard_observation_data(data_map)
        guard_state = super()._update_sector_guard(  # pyright: ignore[reportAttributeAccessIssue]
            scoped_data,
            date,
            all_dates,
            date_to_pos,
        )
        self._update_market_regime(data_map, date, all_dates, date_to_pos)
        return guard_state

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
        """Expose temporary and terminal lock state plus regime history."""
        result = super()._build_result(  # pyright: ignore[reportAttributeAccessIssue]
            final_assets,
            all_dates,
        )
        manager = self.risk
        result.update(
            {
                "portfolio_policy": self.policy.as_dict(),
                "safe_mode_active": bool(getattr(self, "_safe_mode_active", False)),
                "terminal_risk_lock": bool(
                    isinstance(manager, RecoverableDrawdownRiskManager)
                    and manager.terminal_lock
                ),
                "cycle_lock_count": int(
                    manager.cycle_lock_count
                    if isinstance(manager, RecoverableDrawdownRiskManager)
                    else 0
                ),
                "guard_scope_mode": GUARD_SCOPE_MODE,
                "tradable_symbols": sorted(self._tradable_symbol_codes),
                "regime_state_series": list(self._regime_state_series),
                "regime_final_state": self._regime_state,
            }
        )
        return result
