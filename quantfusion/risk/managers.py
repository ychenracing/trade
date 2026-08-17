"""Portfolio and position risk managers."""

from __future__ import annotations

from typing import Any

import pandas as pd

from quantfusion.domain.models import Position
from quantfusion.domain.rules import is_finite_number
from quantfusion.config.portfolio import PortfolioPolicy, PortfolioPolicyBase

_is_finite_number = is_finite_number
_PortfolioPolicyBase = PortfolioPolicyBase


class RiskManager:
    """Enforce portfolio drawdown, daily-loss, sector, and position limits."""

    def __init__(self, cfg: dict) -> None:
        """Initialize shared daily-loss and exposure controls."""
        self.cfg = cfg
        self.peak_assets: float = 0.0
        self.daily_start_assets: float = 0.0
        self.symbol_groups: dict[str, str] = {}
        self.group_weight_limits: dict[str, float] = {}

    def configure_groups(self, symbol_groups: dict[str, str]) -> None:
        """Enable sector caps only when multiple groups are tradable."""
        self.symbol_groups = dict(symbol_groups)
        active = set(self.symbol_groups.values())
        if len(active) > 1:
            self.group_weight_limits = dict(
                self.cfg.get("combined_group_weight_limits", {})
            )
        else:
            self.group_weight_limits = {group: 1.0 for group in active}

    def check_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        trading_dates: list[pd.Timestamp] | None = None,
        date_to_pos: dict[pd.Timestamp, int] | None = None,
    ) -> str | None:
        """Require a concrete persistent or recoverable drawdown policy."""
        del current_assets, date_str, trading_dates, date_to_pos
        raise NotImplementedError("RiskManager requires a concrete drawdown policy")

    def check_daily_loss(self, current_assets: float) -> bool:
        """Return whether close-to-close portfolio loss reached its limit."""
        if self.daily_start_assets > 0:
            daily_loss = (
                self.daily_start_assets - current_assets
            ) / self.daily_start_assets
            return daily_loss >= self.cfg.get("daily_loss_limit", 0.06)
        return False

    def check_position_limits(
        self,
        symbol: str,
        positions: dict,
        current_assets: float,
        buy_value: float,
        current_prices: dict | None = None,
        position_cfg: dict | None = None,
    ) -> bool:
        """Check symbol, sector, and total exposure before a buy."""
        if current_assets <= 0:
            return False
        if current_prices is not None:
            for sym in positions:
                price = current_prices.get(sym)
                if price is None or not _is_finite_number(price) or price <= 0:
                    return False

        def _mark(sym: str, pos: Position) -> float:
            if current_prices is None:
                return pos.entry_price
            return float(current_prices[sym])

        symbol_value = sum(
            (p.shares * _mark(symbol, p) for p in positions.get(symbol, {}).values())
        )
        symbol_cap = (position_cfg or self.cfg).get(
            "max_symbol_weight", self.cfg.get("max_symbol_weight", 0.5)
        )
        if (symbol_value + buy_value) / current_assets > symbol_cap:
            return False
        target_group = self.symbol_groups.get(symbol)
        group_cap = (
            self.group_weight_limits.get(target_group, 1.0)
            if target_group is not None
            else 1.0
        )
        if target_group:
            group_value = sum(
                (
                    p.shares * _mark(sym, p)
                    for sym, sym_positions in positions.items()
                    if self.symbol_groups.get(sym) == target_group
                    for p in sym_positions.values()
                )
            )
            if (group_value + buy_value) / current_assets > group_cap:
                return False
        total_position_value = sum(
            (
                p.shares * _mark(sym, p)
                for sym, sym_positions in positions.items()
                for p in sym_positions.values()
            )
        )
        # All three strategy sub-positions for a symbol share one symbol cap.
        return (total_position_value + buy_value) / current_assets <= self.cfg.get(
            "max_total_weight", 0.95
        )


class PersistentRiskManager(RiskManager):
    """Keep a breached portfolio locked until an explicit operator reset.

    A fixed backtest cannot model an investment committee or operator decision. The
    conservative default is therefore to stay in cash after the lifetime drawdown
    threshold is crossed. A new engine instance is the explicit reset boundary.
    """

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.persistent_lock = False
        self.lock_date: str | None = None
        self.lock_drawdown = 0.0

    def check_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        trading_dates: list[pd.Timestamp] | None = None,
        date_to_pos: dict[pd.Timestamp, int] | None = None,
    ) -> str | None:
        """Trigger once at the lifetime high-water drawdown and then block entries."""
        del trading_dates, date_to_pos
        self.peak_assets = max(self.peak_assets, float(current_assets))
        if self.persistent_lock:
            return "persistent portfolio risk lock"
        if self.peak_assets <= 0:
            return None
        drawdown = (self.peak_assets - current_assets) / self.peak_assets
        if drawdown < float(self.cfg.get("max_drawdown", 0.2)):
            return None
        self.persistent_lock = True
        self.lock_date = date_str
        self.lock_drawdown = float(drawdown)
        return "portfolio drawdown circuit breaker"


class _ConfirmedDrawdownRiskManager(PersistentRiskManager):
    """Require sustained stress unless an emergency threshold is breached."""

    def __init__(self, cfg: dict, policy: _PortfolioPolicyBase) -> None:
        super().__init__(cfg)
        self.policy = policy
        self.breach_streak = 0
        self.alert_active = False
        self.audit_events: list[dict[str, Any]] = []

    def _record_alert_state(self, date_str: str, drawdown: float, active: bool) -> None:
        """Record threshold crossings without changing portfolio exposure."""
        event = (
            "portfolio_drawdown_alert_on" if active else "portfolio_drawdown_alert_off"
        )
        self.audit_events.append(
            {
                "date": date_str,
                "event": event,
                "drawdown": float(drawdown),
                "threshold": self.policy.drawdown_alert,
            }
        )

    def _activate_lock(self, date_str: str, drawdown: float, trigger: str) -> str:
        """Persist the hard lock and expose its exact trigger for the audit trail."""
        self.persistent_lock = True
        self.lock_date = date_str
        self.lock_drawdown = float(drawdown)
        self.audit_events.append(
            {
                "date": date_str,
                "event": trigger,
                "drawdown": float(drawdown),
                "breach_streak": int(self.breach_streak),
            }
        )
        return "portfolio drawdown circuit breaker"

    def check_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        trading_dates: list[pd.Timestamp] | None = None,
        date_to_pos: dict[pd.Timestamp, int] | None = None,
    ) -> str | None:
        """Apply shadow alert, sustained confirmation, and emergency lock rules."""
        del trading_dates, date_to_pos
        self.peak_assets = max(self.peak_assets, float(current_assets))
        if self.persistent_lock:
            return "persistent portfolio risk lock"
        if self.peak_assets <= 0:
            return None
        drawdown = (self.peak_assets - current_assets) / self.peak_assets
        above_alert = drawdown >= self.policy.drawdown_alert
        if above_alert != self.alert_active:
            self.alert_active = above_alert
            self._record_alert_state(date_str, drawdown, above_alert)
        if drawdown >= self.policy.emergency_drawdown:
            return self._activate_lock(
                date_str, drawdown, "emergency_portfolio_drawdown_lock"
            )
        self.breach_streak = (
            self.breach_streak + 1 if drawdown >= self.policy.confirmed_drawdown else 0
        )
        if self.breach_streak < self.policy.drawdown_confirmations:
            return None
        return self._activate_lock(
            date_str, drawdown, "confirmed_portfolio_drawdown_lock"
        )

    def drain_audit_events(self) -> list[dict[str, Any]]:
        """Move newly generated manager events into the engine-level audit log."""
        events = list(self.audit_events)
        self.audit_events = []
        return events


class RecoverableDrawdownRiskManager(_ConfirmedDrawdownRiskManager):
    """Rearm cycle locks after a cooldown but preserve a lifetime hard stop."""

    def __init__(self, cfg: dict, policy: PortfolioPolicy) -> None:
        super().__init__(cfg, policy)
        self.policy = policy
        self.lifetime_peak_assets = 0.0
        self.lock_start_position: int | None = None
        self.terminal_lock = False
        self.cycle_lock_count = 0

    @staticmethod
    def _date_position(
        date_str: str,
        trading_dates: list[pd.Timestamp] | None,
        date_to_pos: dict[pd.Timestamp, int] | None,
    ) -> int | None:
        """Resolve the current trading position without calendar-day assumptions."""
        try:
            timestamp = pd.Timestamp(date_str)
        except (TypeError, ValueError):
            return None
        if not isinstance(timestamp, pd.Timestamp):
            return None
        if date_to_pos is not None:
            return date_to_pos.get(timestamp)
        if trading_dates is None:
            return None
        try:
            return trading_dates.index(timestamp)
        except ValueError:
            return None

    def _activate_cycle_lock(
        self, date_str: str, drawdown: float, position: int | None, trigger: str
    ) -> str:
        """Enter a temporary cash lock and record its exact causal trigger."""
        self.persistent_lock = True
        self.terminal_lock = False
        self.lock_date = date_str
        self.lock_drawdown = float(drawdown)
        self.lock_start_position = position
        self.cycle_lock_count += 1
        self.audit_events.append(
            {
                "date": date_str,
                "event": trigger,
                "drawdown": float(drawdown),
                "breach_streak": int(self.breach_streak),
                "cycle_lock_count": int(self.cycle_lock_count),
            }
        )
        return "portfolio drawdown circuit breaker"

    def _activate_terminal_lock(
        self, date_str: str, drawdown: float, position: int | None
    ) -> str:
        """Enter the non-rearming lifetime safety lock."""
        self.persistent_lock = True
        self.terminal_lock = True
        self.lock_date = date_str
        self.lock_drawdown = float(drawdown)
        self.lock_start_position = position
        self.audit_events.append(
            {
                "date": date_str,
                "event": "terminal_portfolio_drawdown_lock",
                "drawdown": float(drawdown),
                "threshold": self.policy.terminal_drawdown,
            }
        )
        return "portfolio drawdown circuit breaker"

    def _try_rearm(
        self, date_str: str, current_assets: float, position: int | None
    ) -> bool:
        """Reset the cycle high-water mark after the required cash cooldown."""
        if self.terminal_lock or self.lock_start_position is None or position is None:
            return False
        elapsed = position - self.lock_start_position
        if elapsed < self.policy.rearm_trading_days:
            return False
        self.persistent_lock = False
        self.lock_date = None
        self.lock_drawdown = 0.0
        self.lock_start_position = None
        self.peak_assets = float(current_assets)
        self.breach_streak = 0
        self.alert_active = False
        self.audit_events.append(
            {
                "date": date_str,
                "event": "portfolio_drawdown_rearmed",
                "cooldown_trading_days": int(elapsed),
                "cycle_lock_count": int(self.cycle_lock_count),
            }
        )
        return True

    def check_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        trading_dates: list[pd.Timestamp] | None = None,
        date_to_pos: dict[pd.Timestamp, int] | None = None,
    ) -> str | None:
        """Apply temporary cycle defense before the lifetime terminal boundary."""
        assets = float(current_assets)
        self.lifetime_peak_assets = max(self.lifetime_peak_assets, assets)
        position = self._date_position(date_str, trading_dates, date_to_pos)
        if self.persistent_lock:
            if self._try_rearm(date_str, assets, position):
                return None
            return "persistent portfolio risk lock"

        self.peak_assets = max(self.peak_assets, assets)
        lifetime_drawdown = (
            (self.lifetime_peak_assets - assets) / self.lifetime_peak_assets
            if self.lifetime_peak_assets > 0
            else 0.0
        )
        if lifetime_drawdown >= self.policy.terminal_drawdown:
            return self._activate_terminal_lock(date_str, lifetime_drawdown, position)
        if self.peak_assets <= 0:
            return None
        cycle_drawdown = (self.peak_assets - assets) / self.peak_assets
        above_alert = cycle_drawdown >= self.policy.drawdown_alert
        if above_alert != self.alert_active:
            self.alert_active = above_alert
            self._record_alert_state(date_str, cycle_drawdown, above_alert)
        if cycle_drawdown >= self.policy.emergency_drawdown:
            return self._activate_cycle_lock(
                date_str,
                cycle_drawdown,
                position,
                "emergency_cycle_drawdown_lock",
            )
        self.breach_streak = (
            self.breach_streak + 1
            if cycle_drawdown >= self.policy.confirmed_drawdown
            else 0
        )
        if self.breach_streak < self.policy.drawdown_confirmations:
            return None
        return self._activate_cycle_lock(
            date_str,
            cycle_drawdown,
            position,
            "confirmed_cycle_drawdown_lock",
        )


ConfirmedDrawdownRiskManager = _ConfirmedDrawdownRiskManager

__all__ = [
    "ConfirmedDrawdownRiskManager",
    "PersistentRiskManager",
    "RecoverableDrawdownRiskManager",
    "RiskManager",
]
