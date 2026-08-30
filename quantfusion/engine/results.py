"""Backtest metrics and immutable result assembly."""

from __future__ import annotations

# pyright: reportAttributeAccessIssue=false

# The same stable domain vocabulary is intentionally available to each mixin;
# responsibility is split by behavior, not by duplicating implementations.
# ruff: noqa: F401

import math
from dataclasses import replace
from typing import Any, Callable, ClassVar

import numpy as np
import pandas as pd

from quantfusion.config.engine import default_engine_config
from quantfusion.data.providers import DataFetcher
from quantfusion.domain.models import (
    AccountState,
    BarContext,
    Position,
    SectorObservation,
    Signal,
    TradeRecord,
    date_symbol_side_count,
)
from quantfusion.domain.rules import (
    SYMBOL_RE,
    floor_to_lot,
    is_finite_number,
    require_bool,
    require_finite,
    require_int,
    require_positive,
)
from quantfusion.indicators.technical import Indicators
from quantfusion.engine.configuration import EngineConfigurationMixin
from quantfusion.risk.managers import RiskManager
from quantfusion.strategy.trend import (
    ATRChannelStrategy,
    BaseStrategy,
    DualMAStrategy,
    TurtleBreakoutStrategy,
)

_SYMBOL_RE = SYMBOL_RE
_date_symbol_side_count = date_symbol_side_count
_floor_to_lot = floor_to_lot
_is_finite_number = is_finite_number
_require_bool = require_bool
_require_finite = require_finite
_require_int = require_int
_require_positive = require_positive


class CoreResultsMixin:
    """Backtest metrics and immutable result assembly."""

    def _build_result(self, final_assets: float, all_dates: list[pd.Timestamp]) -> dict:
        """Build performance metrics and audited output objects from the equity curve."""
        eq = pd.DataFrame(self.equity_curve)
        if eq.empty:
            return {"error": "No equity data"}
        eq["date"] = pd.to_datetime(eq["date"])
        eq["assets"] = eq["assets"].astype(float)
        eq = eq.set_index("date")
        total_return = (final_assets - self.initial_capital) / self.initial_capital
        n_trading_days = len(all_dates)
        # Annualization and Sharpe consistently use 252 trading days.
        annual_return = (
            (1 + total_return) ** (252 / max(n_trading_days, 1)) - 1
            if total_return > -1
            else -1.0
        )
        # Drawdown is computed from the marked-to-market portfolio equity curve.
        peak = eq["assets"].cummax().replace(0, np.nan)
        drawdown = (eq["assets"] - peak) / peak
        max_drawdown = drawdown.min()
        calmar = annual_return / abs(max_drawdown) if max_drawdown < 0 else 0.0
        daily_returns = (
            eq["assets"]
            .pct_change()
            .replace([np.inf, -np.inf], np.nan)
            .dropna()
        )
        sharpe = 0.0
        if daily_returns.std() > 0:
            rf_annual = float(self.cfg.get("risk_free_rate", 0.0))
            rf_daily = (1 + rf_annual) ** (1 / 252) - 1 if rf_annual > -1 else 0.0
            sharpe = (
                (daily_returns - rf_daily).mean() / daily_returns.std() * math.sqrt(252)
            )
        sell_trades = [t for t in self.trades if t.direction == "sell"]
        exit_givebacks = [
            float(t.exit_from_peak_pct)
            for t in sell_trades
            if _is_finite_number(t.exit_from_peak_pct)
        ]
        wins = [t for t in sell_trades if t.pnl > 0]
        losses = [t for t in sell_trades if t.pnl < 0]
        decisive_trades = len(wins) + len(losses)
        win_rate = len(wins) / decisive_trades if decisive_trades else 0
        total_win = sum((t.pnl for t in wins)) if wins else 0
        total_loss = abs(sum((t.pnl for t in losses))) if losses else 0
        profit_factor = total_win / total_loss if total_loss > 0 else float("inf")
        open_positions = sum(
            (len(sym_positions) for sym_positions in self.positions.values())
        )
        open_position_value = max(float(final_assets - self.cash), 0.0)
        return {
            "initial_capital": self.initial_capital,
            "final_assets": final_assets,
            "total_return": total_return,
            "annual_return": annual_return,
            "max_drawdown": max_drawdown,
            "sharpe": sharpe,
            "calmar": calmar,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "total_trades": len(self.trades),
            "sleeve_fill_count": len(self.trades),
            "sell_trades": len(sell_trades),
            "sleeve_sell_fill_count": len(sell_trades),
            "date_symbol_side_count": _date_symbol_side_count(self.trades),
            "date_symbol_sell_side_count": _date_symbol_side_count(
                self.trades, direction="sell"
            ),
            "avg_exit_from_peak": float(np.mean(exit_givebacks))
            if exit_givebacks
            else 0.0,
            "worst_exit_from_peak": float(min(exit_givebacks))
            if exit_givebacks
            else 0.0,
            "open_positions": int(open_positions),
            "open_position_value": open_position_value,
            "period_end_valuation": "mark_to_market",
            "equity_curve": eq,
            "trades": self.trades,
            "drawdown_series": drawdown,
            "pending_signals": [signal for signal, _ in self.pending_signals],
            "parameter_routes": {
                code: EngineConfigurationMixin._SYMBOL_PROFILE.get(
                    code,
                    EngineConfigurationMixin.classify_symbol(
                        code, name=self.symbol_names.get(code, "")
                    ),
                )
                for code in self.symbol_names
            },
            "unmapped_symbols": sorted(
                code
                for code, name in self.symbol_names.items()
                if EngineConfigurationMixin._uses_unmapped_auto_route(code, name)
            ),
            "fusion_events": list(self.fusion_events),
            "risk_events": list(self.risk_events),
            "sector_guard_active": bool(self.sector_guard_active),
            "safe_mode_active": bool(getattr(self, "_safe_mode_active", False)),
            "reversal_exit_trades": sum(
                (
                    1
                    for trade in self.trades
                    if trade.direction == "sell" and "reversal" in str(trade.reason)
                )
            ),
        }
