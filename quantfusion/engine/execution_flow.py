"""Causal matching, cash accounting, and liquidation orders."""

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
    BarContext,
    Position,
    SectorObservation,
    Signal,
    TradeRecord,
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
from quantfusion.risk.managers import RiskManager
from quantfusion.strategy.trend import (
    ATRChannelStrategy,
    BaseStrategy,
    DualMAStrategy,
    TurtleBreakoutStrategy,
)

_SYMBOL_RE = SYMBOL_RE
_floor_to_lot = floor_to_lot
_is_finite_number = is_finite_number
_require_bool = require_bool
_require_finite = require_finite
_require_int = require_int
_require_positive = require_positive


class CoreExecutionMixin:
    """Causal matching, cash accounting, and liquidation orders."""

    def _latest_close_on_or_before(self, df: pd.DataFrame, date: pd.Timestamp) -> float:
        """Return the latest valid closing mark known by date."""
        if date in df.index:
            price = df.loc[date, "close"]
            if _is_finite_number(price) and price > 0:
                return float(price)
        mask = df.index <= date
        if not mask.any():
            return 0.0
        closes = pd.to_numeric(df.loc[mask, "close"], errors="coerce")
        closes = closes[closes > 0]
        return float(closes.iloc[-1]) if not closes.empty else 0.0

    def _latest_close_before(self, df: pd.DataFrame, date: pd.Timestamp) -> float:
        """Return the latest valid closing mark strictly before date."""
        mask = df.index < date
        if not mask.any():
            return 0.0
        closes = pd.to_numeric(df.loc[mask, "close"], errors="coerce")
        closes = closes[closes > 0]
        return float(closes.iloc[-1]) if not closes.empty else 0.0

    def _execution_mark_prices(
        self, data_map: dict[str, pd.DataFrame], date: pd.Timestamp
    ) -> dict[str, float]:
        """Build opening marks, falling back only to prior known closes."""
        prices: dict[str, float] = {}
        for code, df in data_map.items():
            price = 0.0
            if date in df.index:
                open_price = df.loc[date, "open"]
                if _is_finite_number(open_price) and open_price > 0:
                    price = float(open_price)
            if price <= 0:
                price = self._latest_close_before(df, date)
            if price > 0:
                prices[code] = price
        return prices

    def _total_assets_at_prices(self, prices: dict[str, float]) -> float:
        """Mark cash and every position with an explicit price map."""
        total = self.cash
        for code, positions in self.positions.items():
            price = prices.get(code)
            for pos in positions.values():
                mark = price if price is not None and price > 0 else pos.entry_price
                total += pos.market_value_at(mark)
        return float(total)

    def _total_assets(
        self, data_map: dict[str, pd.DataFrame], date: pd.Timestamp
    ) -> float:
        """Mark total assets with the latest close known by date."""
        total = self.cash
        for code, positions in self.positions.items():
            if code not in data_map:
                continue
            price = self._latest_close_on_or_before(data_map[code], date)
            for pos in positions.values():
                mark = price if price > 0 else pos.entry_price
                total += pos.market_value_at(mark)
        return float(total)

    def _fit_buy_to_cash(
        self,
        requested_shares: float,
        exec_price: float,
        commission_rate: float,
        min_commission: float,
    ) -> tuple[int, float, float, float]:
        """Fit a board-lot buy to cash in constant time, including minimum fees."""
        requested = _floor_to_lot(requested_shares)
        if requested <= 0 or exec_price <= 0 or self.cash <= min_commission:
            return (0, 0.0, 0.0, 0.0)
        by_rate = _floor_to_lot(self.cash / (exec_price * (1.0 + commission_rate)))
        by_minimum_fee = _floor_to_lot(
            max(self.cash - min_commission, 0.0) / exec_price
        )
        shares = min(requested, by_rate, by_minimum_fee)
        if shares <= 0:
            return (0, 0.0, 0.0, 0.0)
        buy_value = shares * exec_price
        commission = max(buy_value * commission_rate, min_commission)
        total_cost = buy_value + commission
        return (shares, buy_value, commission, total_cost)

    def _apply_buy_to_position(
        self,
        signal: Signal,
        strategy: BaseStrategy,
        date_str: str,
        shares: int,
        exec_price: float,
        total_cost: float,
    ) -> None:
        """Apply one filled buy to its strategy sub-position."""
        strategy_cfg = strategy.cfg
        effective_entry = total_cost / shares
        # Anchor the stop to the actual slipped execution price, while never
        # loosening a stricter stop already generated at the signal close.
        exec_based_stop = (
            exec_price - float(strategy_cfg.get("atr_multiplier", 2.0)) * signal.atr
            if signal.atr > 0
            else signal.stop_loss
        )
        symbol_positions = self.positions.setdefault(signal.symbol, {})
        if strategy.name in symbol_positions:
            pos = symbol_positions[strategy.name]
            total_cost_basis = pos.cost + total_cost
            total_shares = pos.shares + shares
            pos.entry_price = total_cost_basis / total_shares
            pos.shares = total_shares
            pos.units += 1
            stop_candidates = [pos.stop_loss, exec_based_stop]
            if signal.stop_loss:
                stop_candidates.append(signal.stop_loss)
            pos.stop_loss = max(stop_candidates)
            pos.highest_since_entry = max(pos.highest_since_entry, exec_price)
            pos.highest_close_since_entry = max(
                pos.highest_close_since_entry, exec_price
            )
            pos.last_buy_date = date_str
            pos.last_add_price = exec_price
        else:
            symbol_positions[strategy.name] = Position(
                symbol=signal.symbol,
                strategy_name=strategy.name,
                shares=shares,
                entry_price=effective_entry,
                entry_date=date_str,
                stop_loss=exec_based_stop,
                highest_since_entry=exec_price,
                highest_close_since_entry=exec_price,
                units=1,
                last_buy_date=date_str,
                last_add_price=exec_price,
            )
        strategy.position = symbol_positions[strategy.name]

    def _record_buy_rejection(
        self,
        *,
        date: str,
        signal: Signal,
        event: str,
        **details: Any,
    ) -> None:
        """Allow audit-capable engines to explain a rejected buy."""
        del date, signal, event, details

    def _execute_buy(
        self,
        signal: Signal,
        strategy: BaseStrategy,
        date_str: str,
        data_map: dict[str, pd.DataFrame] | None = None,
        date: pd.Timestamp | None = None,
    ) -> bool:
        """Execute a buy after cash, risk, and exposure checks."""
        if signal.target_shares <= 0 or signal.price <= 0:
            self._record_buy_rejection(
                date=date_str,
                signal=signal,
                event="rejected_invalid_buy_order",
                requested_shares=int(signal.target_shares),
                signal_price=float(signal.price),
            )
            return False
        global_cfg = self.cfg
        strategy_cfg = strategy.cfg
        slippage = float(global_cfg.get("slippage", 0.001))
        commission_rate = float(global_cfg.get("commission_rate", 0.00025))
        min_commission = float(global_cfg.get("min_commission", 0.0))
        # Buy slippage worsens the opening price; sell slippage is applied in the
        # opposite direction by _execute_sell.
        exec_price = float(signal.price) * (1 + slippage)
        shares, buy_value, commission, total_cost = self._fit_buy_to_cash(
            signal.target_shares, exec_price, commission_rate, min_commission
        )
        if shares <= 0:
            self._record_buy_rejection(
                date=date_str,
                signal=signal,
                event="rejected_insufficient_cash",
                requested_shares=int(signal.target_shares),
                execution_price=exec_price,
                available_cash=float(self.cash),
            )
            return False
        if data_map is not None and date is not None:
            current_prices = self._execution_mark_prices(data_map, date)
            current_assets = self._total_assets_at_prices(current_prices)
        else:
            current_assets = self.initial_capital
            current_prices = None
        if signal.symbol not in self.positions and len(self.positions) >= int(
            global_cfg.get("max_positions", 6)
        ):
            self._record_buy_rejection(
                date=date_str,
                signal=signal,
                event="rejected_max_positions",
                current_positions=len(self.positions),
                max_positions=int(global_cfg.get("max_positions", 6)),
            )
            return False
        if signal.atr > 0:
            # Recompute only the size against execution-day equity. The ATR itself
            # is frozen on the signal date to avoid future-data leakage.
            existing_pos = self.positions.get(signal.symbol, {}).get(strategy.name)
            unit_num = existing_pos.units + 1 if existing_pos is not None else 1
            risk_limited_shares = strategy._calc_shares(
                current_assets * float(strategy_cfg.get("strategy_weight", 1.0)),
                exec_price,
                signal.atr,
                unit_number=unit_num,
            )
            shares = min(shares, risk_limited_shares)
            if shares <= 0:
                self._record_buy_rejection(
                    date=date_str,
                    signal=signal,
                    event="rejected_risk_sizing_zero",
                    current_assets=float(current_assets),
                    signal_atr=float(signal.atr),
                )
                return False
            shares, buy_value, commission, total_cost = self._fit_buy_to_cash(
                shares, exec_price, commission_rate, min_commission
            )
            if shares <= 0:
                self._record_buy_rejection(
                    date=date_str,
                    signal=signal,
                    event="rejected_insufficient_cash_after_risk_sizing",
                    risk_limited_shares=int(risk_limited_shares),
                    available_cash=float(self.cash),
                )
                return False
        if not self.risk.check_position_limits(
            signal.symbol,
            self.positions,
            current_assets,
            buy_value,
            current_prices,
            position_cfg=strategy_cfg,
        ):
            self._record_buy_rejection(
                date=date_str,
                signal=signal,
                event="rejected_position_limit",
                proposed_buy_value=float(buy_value),
                current_assets=float(current_assets),
            )
            return False
        if self.risk.check_daily_loss(current_assets):
            self._record_buy_rejection(
                date=date_str,
                signal=signal,
                event="rejected_daily_loss_limit",
                current_assets=float(current_assets),
            )
            return False
        self.cash -= total_cost
        self._apply_buy_to_position(
            signal, strategy, date_str, shares, exec_price, total_cost
        )
        self.trades.append(
            TradeRecord(
                symbol=signal.symbol,
                strategy_name=signal.strategy_name,
                direction="buy",
                shares=shares,
                price=exec_price,
                date=date_str,
                reason=signal.reason,
                signal_date=signal.signal_date,
                gross_value=buy_value,
                commission=commission,
                stamp_duty_cost=0.0,
                net_cash_flow=-total_cost,
                cash_after=self.cash,
            )
        )
        return True

    def _execute_sell(
        self, signal: Signal, strategy: BaseStrategy | None, date_str: str
    ) -> int:
        """Execute a strategy or risk-adapter sell and return its filled shares."""
        if signal.target_shares <= 0 or signal.price <= 0:
            return 0
        strat_name = strategy.name if strategy is not None else signal.strategy_name
        pos = None
        if signal.symbol in self.positions:
            pos = self.positions[signal.symbol].get(strat_name)
        if pos is None:
            if strategy is not None:
                strategy.position = None
            return 0
        cfg = self.cfg
        slippage = float(cfg.get("slippage", 0.001))
        commission_rate = float(cfg.get("commission_rate", 0.00025))
        min_commission = float(cfg.get("min_commission", 0.0))
        stamp_duty = float(cfg.get("stamp_duty", 0.0005))
        exec_price = float(signal.price) * (1 - slippage)
        if signal.target_shares >= pos.shares:
            # Full liquidation: sell every share, including any odd lot, so an
            # odd-lot remainder can be fully cleared. A-share sells allow odd
            # lots; the 100-share lot constraint applies only to buys.
            sell_shares = pos.shares
        else:
            # Partial reduction: floor to a board lot. This is the behavior the
            # validated backtest metrics depend on (a sub-lot target floors to 0
            # and is dropped by the caller rather than re-queued forever).
            sell_shares = _floor_to_lot(min(signal.target_shares, pos.shares))
        if sell_shares <= 0:
            return 0
        sell_value = sell_shares * exec_price
        commission = (
            max(sell_value * commission_rate, min_commission) if sell_value > 0 else 0.0
        )
        stamp_duty_cost = sell_value * stamp_duty
        # PnL uses net proceeds after commission and sell-side stamp duty.
        net_proceeds = sell_value - commission - stamp_duty_cost
        cost_basis = sell_shares * pos.entry_price
        pnl = net_proceeds - cost_basis
        pnl_pct = pnl / cost_basis if cost_basis > 0 else 0.0
        peak_close = max(float(pos.highest_close_since_entry), float(pos.entry_price))
        exit_from_peak_pct = exec_price / peak_close - 1 if peak_close > 0 else 0.0
        self.cash += net_proceeds
        pos.shares -= sell_shares
        if pos.shares <= 0:
            del self.positions[signal.symbol][strat_name]
            if not self.positions[signal.symbol]:
                del self.positions[signal.symbol]
            if strategy is not None:
                strategy.position = None
        else:
            if strategy is not None:
                strategy.position = pos
        self.trades.append(
            TradeRecord(
                symbol=signal.symbol,
                strategy_name=signal.strategy_name,
                direction="sell",
                shares=sell_shares,
                price=exec_price,
                date=date_str,
                reason=signal.reason,
                pnl=pnl,
                pnl_pct=pnl_pct,
                signal_date=signal.signal_date,
                gross_value=sell_value,
                commission=commission,
                stamp_duty_cost=stamp_duty_cost,
                net_cash_flow=net_proceeds,
                cash_after=self.cash,
                peak_close=peak_close,
                exit_from_peak_pct=exit_from_peak_pct,
            )
        )
        return sell_shares

    def _generate_liquidation_signals(
        self, date_str: str, reason: str = "circuit breaker liquidation"
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Queue full-position sells for execution at a later tradable open."""
        # These are ordinary pending sell signals. The placeholder price is always
        # replaced by a later tradable opening price before execution.
        signals = []
        for code, positions in self.positions.items():
            strategies = {
                strategy.name: strategy
                for strategy in (
                    self.strategy_instances.get(code, [])
                    + self.external_strategy_instances.get(code, [])
                )
            }
            for strat_name, pos in positions.items():
                strategy = strategies.get(strat_name)
                if strategy is None:
                    continue
                sig = Signal(
                    symbol=code,
                    strategy_name=strat_name,
                    direction="sell",
                    target_shares=pos.shares,
                    price=pos.entry_price,
                    reason=reason,
                    signal_date=date_str,
                )
                signals.append((sig, strategy))
        return signals

    def _generate_regime_reduction_signals(
        self, date_str: str, exit_ratio: float
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Queue partial-position sells when the regime enters CHOPPY.

        Unlike full liquidation, only *exit_ratio* of each position is sold,
        reducing exposure without abandoning all trend-following entries.
        """
        signals = []
        for code, positions in self.positions.items():
            strategies = {
                strategy.name: strategy
                for strategy in (
                    self.strategy_instances.get(code, [])
                    + self.external_strategy_instances.get(code, [])
                )
            }
            for strat_name, pos in positions.items():
                strategy = strategies.get(strat_name)
                if strategy is None:
                    continue
                # A-share sells may be any share count (odd lots allowed); the
                # 100-share lot constraint applies only to buys. The execution
                # path already floors the fill to a board lot, so keep the raw
                # proportional reduction here to avoid over-trimming positions
                # in choppy regimes (flooring early would sell fewer shares and
                # leave larger exposure, dragging returns down).
                sell_shares = max(0, int(pos.shares * exit_ratio))
                if sell_shares <= 0:
                    continue
                sig = Signal(
                    symbol=code,
                    strategy_name=strat_name,
                    direction="sell",
                    target_shares=sell_shares,
                    price=pos.entry_price,
                    reason="regime choppy reduction",
                    signal_date=date_str,
                )
                signals.append((sig, strategy))
        return signals
