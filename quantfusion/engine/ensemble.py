"""Independent-sleeve ensemble allocation coordinator."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, cast

import numpy as np
import pandas as pd

from quantfusion.domain.models import (
    Signal,
    TradeRecord,
    date_symbol_side_count,
)
from quantfusion.domain.rules import floor_to_lot, is_finite_number
from quantfusion.engine.causal import CausalBacktestEngine
from quantfusion.config.portfolio import (
    PortfolioPolicyBase,
    require_positive_ratio,
)
from quantfusion.risk.managers import ConfirmedDrawdownRiskManager
from quantfusion.strategy.trend import BaseStrategy

_CausalBacktestEngine = CausalBacktestEngine
_ConfirmedDrawdownRiskManager = ConfirmedDrawdownRiskManager
_PortfolioPolicyBase = PortfolioPolicyBase
_date_symbol_side_count = date_symbol_side_count
_floor_to_lot = floor_to_lot
_is_finite_number = is_finite_number
_require_positive_ratio = require_positive_ratio


class _EnsembleSleeveBacktestEngine(_CausalBacktestEngine):
    """Run one independently funded horizon sleeve."""

    ENGINE_LABEL = "Quant Fusion"

    def __init__(
        self,
        initial_capital: float,
        *,
        cfg: dict | None,
        policy: _PortfolioPolicyBase,
        allocation_lookbacks: tuple[int, ...],
        sleeve_name: str,
    ) -> None:
        self.policy = policy
        self.sleeve_name = sleeve_name
        self.ALLOCATION_LOOKBACKS = tuple(allocation_lookbacks)
        self._execution_data_map: dict[str, pd.DataFrame] | None = None
        self._execution_date: pd.Timestamp | None = None
        self._adv_used: dict[tuple[str, str, str], int] = {}
        normalized_cfg = dict(cfg or {})
        normalized_cfg["max_drawdown"] = policy.confirmed_drawdown
        super().__init__(initial_capital=initial_capital, cfg=normalized_cfg)

    def _reset_run_state(self, symbols_dict: dict[str, str]) -> None:
        """Reset the ledger and all per-run liquidity reservations."""
        super()._reset_run_state(symbols_dict)
        self._adv_used = {}

    def _resolve_symbol_configs(
        self,
        symbols_dict: dict[str, str],
        per_symbol_config: dict[str, dict] | None,
        config_route: str,
    ) -> dict[str, dict]:
        """Install the confirmed manager after inherited route resolution."""
        resolved = super()._resolve_symbol_configs(
            symbols_dict, per_symbol_config, config_route
        )
        symbol_groups = dict(self.risk.symbol_groups)
        self.risk = _ConfirmedDrawdownRiskManager(self.cfg, self.policy)
        self.risk.configure_groups(symbol_groups)
        return resolved

    def _apply_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        all_dates: list[pd.Timestamp],
        date_to_pos: dict[pd.Timestamp, int],
        pending: list[tuple[Signal, BaseStrategy]],
    ) -> tuple[list[tuple[Signal, BaseStrategy]], bool, bool]:
        """Apply inherited liquidation handling and retain manager events."""
        outcome = super()._apply_portfolio_risk(
            current_assets, date_str, all_dates, date_to_pos, pending
        )
        if isinstance(self.risk, _ConfirmedDrawdownRiskManager):
            self.risk_events.extend(self.risk.drain_audit_events())
        return outcome

    def _adv_capacity(
        self,
        symbol: str,
        direction: str,
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
    ) -> tuple[int, float]:
        """Return a causal lot-rounded capacity from prior daily volumes only."""
        frame = data_map.get(symbol)
        if frame is None or "volume" not in frame.columns:
            return 0, 0.0
        raw_volume = cast(pd.Series, frame.loc[frame.index < date, "volume"])
        history = cast(pd.Series, pd.to_numeric(raw_volume, errors="coerce")).astype(
            "float64"
        )
        history = history.dropna()
        history = history.loc[history.gt(0)].tail(self.policy.adv_lookback)
        if history.empty:
            return 0, 0.0
        adv = float(history.mean())
        daily_capacity = _floor_to_lot(adv * self.policy.max_order_adv_ratio)
        key = (date.strftime("%Y-%m-%d"), symbol, direction)
        return max(daily_capacity - self._adv_used.get(key, 0), 0), adv

    def _remaining_adv_capacity(
        self,
        symbol: str,
        direction: str,
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
    ) -> int | None:
        """Return the unconsumed daily side-specific ADV budget."""
        capacity, _ = self._adv_capacity(symbol, direction, data_map, date)
        return capacity

    def _consume_adv(
        self, date_str: str, symbol: str, direction: str, shares: int
    ) -> None:
        """Reserve actual fills so later same-day orders cannot reuse capacity."""
        key = (date_str, symbol, direction)
        self._adv_used[key] = self._adv_used.get(key, 0) + int(shares)

    def _execute_buy(
        self,
        signal: Signal,
        strategy: BaseStrategy,
        date_str: str,
        data_map: dict[str, pd.DataFrame] | None = None,
        date: pd.Timestamp | None = None,
    ) -> bool:
        """Apply the causal ADV cap before exposure and cash checks."""
        adjusted_signal = signal
        if data_map is not None and date is not None:
            capacity, adv = self._adv_capacity(signal.symbol, "buy", data_map, date)
            if capacity <= 0:
                self._record_order_event(
                    date=date_str,
                    signal=signal,
                    event="rejected_no_prior_adv_capacity",
                )
                return False
            if capacity < signal.target_shares:
                adjusted_signal = replace(signal, target_shares=capacity)
                self._record_order_event(
                    date=date_str,
                    signal=signal,
                    event="clipped_to_adv_capacity",
                    requested_shares=int(signal.target_shares),
                    adjusted_shares=int(capacity),
                    prior_adv=adv,
                )
        before = len(self.trades)
        executed = super()._execute_buy(
            adjusted_signal, strategy, date_str, data_map, date
        )
        if executed and len(self.trades) > before:
            self._consume_adv(date_str, signal.symbol, "buy", self.trades[-1].shares)
        return executed

    def _execute_pending_signals(
        self,
        pending: list[tuple[Signal, BaseStrategy]],
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
        date_to_pos: dict[pd.Timestamp, int],
        directions: frozenset[str] | None = None,
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Expose execution context so inherited sell calls can use prior ADV."""
        self._execution_data_map = data_map
        self._execution_date = date
        try:
            return super()._execute_pending_signals(
                pending, data_map, date, date_to_pos, directions
            )
        finally:
            self._execution_data_map = None
            self._execution_date = None

    def _execute_sell(
        self, signal: Signal, strategy: BaseStrategy | None, date_str: str
    ) -> int:
        """Fill a sell from the remaining shared ADV budget."""
        data_map = self._execution_data_map
        date = self._execution_date
        if data_map is None or date is None:
            return super()._execute_sell(signal, strategy, date_str)
        capacity, adv = self._adv_capacity(signal.symbol, "sell", data_map, date)
        if capacity <= 0:
            self._record_order_event(
                date=date_str,
                signal=signal,
                event="deferred_sell_no_prior_adv_capacity",
            )
            return 0
        adjusted_signal = signal
        if capacity < signal.target_shares:
            adjusted_signal = replace(signal, target_shares=capacity)
            self._record_order_event(
                date=date_str,
                signal=signal,
                event="clipped_to_adv_capacity",
                requested_shares=int(signal.target_shares),
                adjusted_shares=int(capacity),
                prior_adv=adv,
            )
        shares_sold = super()._execute_sell(adjusted_signal, strategy, date_str)
        if shares_sold > 0:
            self._consume_adv(date_str, signal.symbol, "sell", shares_sold)
        return shares_sold

    def _build_result(self, final_assets: float, all_dates: list[pd.Timestamp]) -> dict:
        """Add sleeve, risk, and liquidity metadata to the inherited result."""
        result = super()._build_result(final_assets, all_dates)
        result.update(
            {
                "allocation_mode": "single",
                "sleeve_name": self.sleeve_name,
                "allocation_lookbacks": list(self.ALLOCATION_LOOKBACKS),
                "sleeve_policy": self._policy_snapshot("single"),
            }
        )
        return result

    def _policy_snapshot(self, allocation_mode: str) -> dict[str, Any]:
        """Report the effective runtime mode instead of only the policy default."""
        snapshot = self.policy.as_dict()
        snapshot["allocation_mode"] = allocation_mode
        return snapshot


@dataclass(frozen=True)
class _RunRequest:
    """Carry an inherited run contract through the ensemble coordinator."""

    symbols_dict: dict[str, str]
    start_date: str
    end_date: str
    per_symbol_config: dict[str, dict] | None
    profile: str | None
    config_route: str
    data_dir: str | None
    cache_dir: str | None
    indicator_state: str
    warmup_calendar_days: int
    risk_state: dict | None = None
    route_controller: Any | None = None


@dataclass
class _PreparedSleeveRun:
    """Hold one funded sleeve's state during synchronized portfolio replay."""

    sleeve: _EnsembleSleeveBacktestEngine
    data_map: dict[str, pd.DataFrame]
    indicator_map: dict[str, dict[str, pd.Series]]
    all_dates: list[pd.Timestamp]
    date_to_pos: dict[pd.Timestamp, int]
    pending: list[tuple[Signal, BaseStrategy]] = field(default_factory=list)


class _EnsembleBacktestEngine(_EnsembleSleeveBacktestEngine):
    """Coordinate one sleeve or an equal-capital ensemble of sleeves."""

    def __init__(
        self,
        initial_capital: float = 2_000_000,
        cfg: dict | None = None,
        policy: _PortfolioPolicyBase | None = None,
    ) -> None:
        supplied_policy = policy is not None
        resolved_policy = policy or _PortfolioPolicyBase()
        raw_cfg = dict(cfg or {})
        if "max_drawdown" in raw_cfg:
            configured_drawdown = _require_positive_ratio(
                "max_drawdown", raw_cfg["max_drawdown"]
            )
            if supplied_policy and not math.isclose(
                configured_drawdown, resolved_policy.confirmed_drawdown
            ):
                raise ValueError(
                    "cfg['max_drawdown'] conflicts with policy.confirmed_drawdown"
                )
            resolved_policy = replace(
                resolved_policy, confirmed_drawdown=configured_drawdown
            )
        raw_cfg["max_drawdown"] = resolved_policy.confirmed_drawdown
        self._ensemble_user_cfg = raw_cfg
        self.sleeves: list[_EnsembleSleeveBacktestEngine] = []
        self.last_result: dict | None = None
        super().__init__(
            initial_capital,
            cfg=raw_cfg,
            policy=resolved_policy,
            allocation_lookbacks=resolved_policy.single_lookbacks,
            sleeve_name="single",
        )

    @staticmethod
    def _sleeve_name(index: int, total: int) -> str:
        """Return stable human-readable names for the default three sleeves."""
        if total == 3:
            return ("fast", "base", "slow")[index]
        return f"sleeve_{index + 1}"

    def _run_ensemble(self, request: _RunRequest) -> dict:
        """Require the concrete portfolio coordinator to build its sleeves."""
        del request
        raise NotImplementedError

    @staticmethod
    def _decorate_trade(trade: TradeRecord, sleeve: str) -> TradeRecord:
        """Preserve the base trade schema while making sleeve ownership explicit."""
        return replace(trade, strategy_name=f"{sleeve}:{trade.strategy_name}")

    @staticmethod
    def _decorate_signal(signal: Signal, sleeve: str) -> Signal:
        """Preserve pending-signal compatibility while adding the sleeve prefix."""
        return replace(signal, strategy_name=f"{sleeve}:{signal.strategy_name}")

    @staticmethod
    def _decorate_events(events: list[dict], sleeve: str) -> list[dict]:
        """Copy audit events and attach their source sleeve."""
        return [{"sleeve": sleeve, **dict(event)} for event in events]

    @staticmethod
    def _sort_events(events: list[dict]) -> list[dict]:
        """Return a chronological deterministic audit trail across all sleeves."""
        return sorted(
            events,
            key=lambda event: (
                str(event.get("date", "")),
                str(event.get("sleeve", "")),
                str(event.get("event", "")),
            ),
        )

    def _aggregate_sleeve_results(self, results: list[dict]) -> dict:
        """Build standard portfolio metrics from independent sleeve ledgers."""
        if not results:
            raise RuntimeError("ensemble produced no sleeve results")
        reference_index = results[0]["equity_curve"].index
        if any(
            not result["equity_curve"].index.equals(reference_index)
            for result in results
        ):
            raise RuntimeError("ensemble sleeves produced different equity calendars")
        equity = pd.DataFrame(index=reference_index)
        for column in ("assets", "cash", "position_value"):
            equity[column] = sum(
                result["equity_curve"][column].astype(float) for result in results
            )
        final_assets = float(equity["assets"].iloc[-1])
        total_return = final_assets / self.initial_capital - 1.0
        trading_days = len(equity)
        annual_return = (
            (1.0 + total_return) ** (252 / max(trading_days, 1)) - 1.0
            if total_return > -1.0
            else -1.0
        )
        peak = equity["assets"].cummax().replace(0, np.nan)
        drawdown = (equity["assets"] - peak) / peak
        daily_returns = (
            equity["assets"]
            .pct_change()
            .replace([np.inf, -np.inf], np.nan)
            .dropna()
        )
        sharpe = 0.0
        if daily_returns.std() > 0:
            risk_free = float(self.cfg.get("risk_free_rate", 0.0))
            daily_rf = (1.0 + risk_free) ** (1 / 252) - 1.0
            sharpe = float(
                (daily_returns - daily_rf).mean() / daily_returns.std() * math.sqrt(252)
            )
        max_drawdown = float(drawdown.min())
        calmar = annual_return / abs(max_drawdown) if max_drawdown < 0 else 0.0
        names = [result["sleeve_name"] for result in results]
        trades = [
            self._decorate_trade(trade, name)
            for name, result in zip(names, results, strict=True)
            for trade in result["trades"]
        ]
        trades.sort(key=lambda trade: (trade.date, trade.symbol, trade.strategy_name))
        sell_trades = [trade for trade in trades if trade.direction == "sell"]
        wins = [trade for trade in sell_trades if trade.pnl > 0]
        losses = [trade for trade in sell_trades if trade.pnl < 0]
        decisive = len(wins) + len(losses)
        total_win = sum(trade.pnl for trade in wins)
        total_loss = abs(sum(trade.pnl for trade in losses))
        givebacks = [
            float(trade.exit_from_peak_pct)
            for trade in sell_trades
            if _is_finite_number(trade.exit_from_peak_pct)
        ]
        pending_signals = [
            self._decorate_signal(signal, name)
            for name, result in zip(names, results, strict=True)
            for signal in result.get("pending_signals", [])
        ]
        risk_events = [
            event
            for name, result in zip(names, results, strict=True)
            for event in self._decorate_events(result.get("risk_events", []), name)
        ]
        order_events = [
            event
            for name, result in zip(names, results, strict=True)
            for event in self._decorate_events(result.get("order_events", []), name)
        ]
        fusion_events = [
            event
            for name, result in zip(names, results, strict=True)
            for event in self._decorate_events(result.get("fusion_events", []), name)
        ]
        risk_events = self._sort_events(risk_events)
        order_events = self._sort_events(order_events)
        fusion_events = self._sort_events(fusion_events)
        locked_sleeves = [
            name
            for name, result in zip(names, results, strict=True)
            if result.get("persistent_risk_lock", False)
        ]
        return {
            "allocation_mode": "ensemble",
            "allocation_lookbacks": [
                list(values) for values in self.policy.allocation_horizons
            ],
            "sleeve_policy": self._policy_snapshot("ensemble"),
            "portfolio_max_order_adv_ratio": self.policy.max_order_adv_ratio,
            "per_sleeve_max_order_adv_ratio": (
                self.policy.max_order_adv_ratio / len(results)
            ),
            "indicator_state": results[0]["indicator_state"],
            "initial_capital": self.initial_capital,
            "final_assets": final_assets,
            "total_return": total_return,
            "annual_return": annual_return,
            "max_drawdown": max_drawdown,
            "sharpe": sharpe,
            "calmar": calmar,
            "win_rate": len(wins) / decisive if decisive else 0.0,
            "profit_factor": total_win / total_loss if total_loss > 0 else float("inf"),
            "total_trades": len(trades),
            "sleeve_fill_count": len(trades),
            "sell_trades": len(sell_trades),
            "sleeve_sell_fill_count": len(sell_trades),
            "date_symbol_side_count": _date_symbol_side_count(trades),
            "date_symbol_sell_side_count": _date_symbol_side_count(
                trades, direction="sell"
            ),
            "avg_exit_from_peak": float(np.mean(givebacks)) if givebacks else 0.0,
            "worst_exit_from_peak": float(min(givebacks)) if givebacks else 0.0,
            "open_positions": sum(result["open_positions"] for result in results),
            "open_position_value": sum(
                float(result["open_position_value"]) for result in results
            ),
            "period_end_valuation": "mark_to_market",
            "equity_curve": equity,
            "drawdown_series": drawdown,
            "trades": trades,
            "pending_signals": pending_signals,
            "trade_cash_scope": "sleeve",
            "parameter_routes": dict(results[0].get("parameter_routes", {})),
            "unmapped_symbols": sorted(
                {
                    code
                    for result in results
                    for code in result.get("unmapped_symbols", [])
                }
            ),
            "fusion_events": fusion_events,
            "risk_events": risk_events,
            "order_events": order_events,
            "sector_guard_active": any(
                result.get("sector_guard_active", False) for result in results
            ),
            "safe_mode_active": any(
                result.get("safe_mode_active", False) for result in results
            ),
            "persistent_risk_lock": any(
                result.get("persistent_risk_lock", False) for result in results
            ),
            "all_sleeves_locked": len(locked_sleeves) == len(results),
            "locked_sleeves": locked_sleeves,
            "reversal_exit_trades": sum(
                int(result.get("reversal_exit_trades", 0)) for result in results
            ),
            "resolved_symbol_configs": {
                name: result.get("resolved_symbol_configs", {})
                for name, result in zip(names, results, strict=True)
            },
            "sleeve_summaries": [
                {
                    "name": name,
                    "allocation_lookbacks": result["allocation_lookbacks"],
                    "initial_capital": result["initial_capital"],
                    "final_assets": result["final_assets"],
                    "total_return": result["total_return"],
                    "max_drawdown": result["max_drawdown"],
                    "persistent_risk_lock": result["persistent_risk_lock"],
                    "max_order_adv_ratio": result["sleeve_policy"][
                        "max_order_adv_ratio"
                    ],
                }
                for name, result in zip(names, results, strict=True)
            ],
        }

    def run(  # noqa: PLR0913 - Keep the inherited public API compatible.
        self,
        symbols_dict: dict[str, str],
        start_date: str,
        end_date: str,
        per_symbol_config: dict[str, dict] | None = None,
        profile: str | None = None,
        config_route: str = "auto",
        data_dir: str | None = None,
        *,
        cache_dir: str | None = None,
        indicator_state: str = "cold",
        warmup_calendar_days: int = 365,
        allocation_mode: str | None = None,
        risk_state: dict | None = None,
        route_controller: Any | None = None,
    ) -> dict:
        """Run the configured single sleeve or the default three-sleeve ensemble."""
        mode = str(allocation_mode or self.policy.allocation_mode).lower()
        if mode not in {"single", "ensemble"}:
            raise ValueError("allocation_mode must be 'single' or 'ensemble'")
        if mode == "single":
            if route_controller is not None:
                raise ValueError("route_controller requires allocation_mode='ensemble'")
            self.sleeves = [self]
            if risk_state:
                self.cfg = dict(self.cfg)
                self.cfg["_initial_risk_state"] = risk_state
            result = super().run(
                symbols_dict,
                start_date,
                end_date,
                per_symbol_config=per_symbol_config,
                profile=profile,
                config_route=config_route,
                data_dir=data_dir,
                cache_dir=cache_dir,
                indicator_state=indicator_state,
                warmup_calendar_days=warmup_calendar_days,
            )
            result["allocation_mode"] = "single"
            result["sleeve_policy"] = self._policy_snapshot("single")
            self.last_result = result
            return result
        return self._run_ensemble(
            _RunRequest(
                symbols_dict=symbols_dict,
                start_date=start_date,
                end_date=end_date,
                per_symbol_config=per_symbol_config,
                profile=profile,
                config_route=config_route,
                data_dir=data_dir,
                cache_dir=cache_dir,
                indicator_state=indicator_state,
                warmup_calendar_days=warmup_calendar_days,
                risk_state=risk_state,
                route_controller=route_controller,
            )
        )


EnsembleSleeveBacktestEngine = _EnsembleSleeveBacktestEngine
RunRequest = _RunRequest
PreparedSleeveRun = _PreparedSleeveRun
EnsembleBacktestEngine = _EnsembleBacktestEngine

__all__ = [
    "EnsembleBacktestEngine",
    "EnsembleSleeveBacktestEngine",
    "PreparedSleeveRun",
    "RunRequest",
]
