"""Causal allocation and persistent-risk refinement."""

from __future__ import annotations

from quantfusion.execution.c6_receipts import (
    begin_action_batch, finish_action_batch, begin_order,
    link_order, note_order, order_receipt,
)

import math
from dataclasses import replace
from typing import Any

import pandas as pd

from quantfusion.config.engine import (
    PER_SYMBOL_OVERRIDE_KEYS,
    default_engine_config,
    validate_engine_config,
)
from quantfusion.config.profiles import (
    optimized_aggressive_config,
    semiconductor_config,
    semiconductor_heavy_config,
)
from quantfusion.domain.models import Signal
from quantfusion.domain.rules import (
    A_SHARE_LOT_SIZE,
    floor_to_lot,
    limit_pct_for_code,
    require_int,
)
from quantfusion.engine.core import CoreBacktestEngine
from quantfusion.risk.managers import PersistentRiskManager
from quantfusion.strategy.trend import BaseStrategy

_CoreBacktestEngine = CoreBacktestEngine
_floor_to_lot = floor_to_lot
_limit_pct_for_code = limit_pct_for_code
_require_int = require_int


class _CausalBacktestEngine(_CoreBacktestEngine):
    """Run core signals with causal allocation, explicit state, and durable defense."""

    ENGINE_LABEL = "Quant Fusion"
    ALLOCATION_LOOKBACKS = (5, 10, 20)
    policy: Any
    _candidate_score_series: dict[str, dict[int, pd.Series]]

    def __init__(
        self, initial_capital: float = 2_000_000, cfg: dict | None = None
    ) -> None:
        super().__init__(initial_capital=initial_capital, cfg=cfg)
        self.order_events: list[dict[str, Any]] = []
        self._profile_strategy_overrides: dict[str, Any] = {}
        self._indicator_state = "cold"
        self._warmup_calendar_days = 365
        self._requested_start_date = ""
        self._requested_end_date = ""
        self._risk_lock_logged = False
        self._allocation_raw_series: dict[str, dict[int, pd.Series]] = {}
        self._allocation_score_cache: dict[pd.Timestamp, dict[str, float]] = {}

    def _display_run_period(self, start_date: str, end_date: str) -> tuple[str, str]:
        """Show the requested trading window, not the optional warmup window."""
        if self._requested_start_date and self._requested_end_date:
            return self._requested_start_date, self._requested_end_date
        return start_date, end_date

    def _reset_run_state(self, symbols_dict: dict[str, str]) -> None:
        """Reset causal audit and profile state with the inherited ledger."""
        super()._reset_run_state(symbols_dict)
        self.order_events = []
        self._profile_strategy_overrides = {}
        self._risk_lock_logged = False
        self._allocation_raw_series = {}
        self._allocation_score_cache = {}

    def _apply_global_profile(self, profile: str | None) -> None:
        """Apply a profile and remember its strategy-level routed overrides."""
        super()._apply_global_profile(profile)
        factories = {
            "semiconductor": semiconductor_config,
            "semiconductor_heavy": semiconductor_heavy_config,
            "aggressive": optimized_aggressive_config,
        }
        factory = factories.get(profile) if profile is not None else None
        if factory is None:
            self._profile_strategy_overrides = {}
            return
        defaults = default_engine_config()
        profile_cfg = factory()
        self._profile_strategy_overrides = {
            key: profile_cfg[key]
            for key in PER_SYMBOL_OVERRIDE_KEYS
            if key in profile_cfg and profile_cfg[key] != defaults.get(key)
        }

    def _resolve_symbol_configs(
        self,
        symbols_dict: dict[str, str],
        per_symbol_config: dict[str, dict] | None,
        config_route: str,
    ) -> dict[str, dict]:
        """Resolve explicit precedence and install the persistent risk manager."""
        resolved = super()._resolve_symbol_configs(
            symbols_dict, per_symbol_config, config_route
        )
        symbol_groups = dict(self.risk.symbol_groups)
        self.risk = PersistentRiskManager(self.cfg)
        self.risk.configure_groups(symbol_groups)

        explicit_global = {
            key: value
            for key, value in self._user_cfg.items()
            if key in PER_SYMBOL_OVERRIDE_KEYS
        }
        route_overrides = {
            **self._profile_strategy_overrides,
            **explicit_global,
        }
        per_symbol = per_symbol_config or {}
        final: dict[str, dict] = {}
        for code, route_cfg in resolved.items():
            final[code] = validate_engine_config(
                {
                    **route_cfg,
                    **route_overrides,
                    **per_symbol.get(code, {}),
                }
            )
        return final

    def _allocation_scores(
        self, data_map: dict[str, pd.DataFrame], date: pd.Timestamp
    ) -> dict[str, float]:
        """Average cached cross-sectional ranks of causal momentum signals."""
        if getattr(self, "_c6_intervention", None) == "W1_DATA_MAP_ONLY":
            data_map = {code: frame for code, frame in data_map.items() if code != "601869"}
        date = pd.Timestamp(date)
        cached = self._allocation_score_cache.get(date)
        if cached is not None:
            return cached
        if not self._allocation_raw_series:
            self._allocation_raw_series = self._build_allocation_raw_series(data_map)
        raw: dict[int, dict[str, float]] = {
            window: {} for window in self.ALLOCATION_LOOKBACKS
        }
        for code in data_map:
            for window in self.ALLOCATION_LOOKBACKS:
                series = self._allocation_raw_series.get(code, {}).get(window)
                if series is None or series.empty:
                    continue
                position = int(series.index.searchsorted(date, side="left")) - 1
                if position < 0:
                    continue
                score = float(series.iloc[position])
                if math.isfinite(score):
                    raw[window][code] = score

        scores = {code: 0.0 for code in data_map}
        observations = {code: 0 for code in data_map}
        for values in raw.values():
            if not values:
                continue
            ranks = pd.Series(values, dtype="float64").rank(pct=True)
            for code, rank in ranks.items():
                scores[code] += float(rank)
                observations[code] += 1
        result = {
            code: scores[code] / observations[code] if observations[code] else 0.0
            for code in scores
        }
        self._allocation_score_cache[date] = result
        return result

    def _build_allocation_raw_series(
        self, data_map: dict[str, pd.DataFrame]
    ) -> dict[str, dict[int, pd.Series]]:
        """Precompute the lag-safe inputs used by daily allocation rankings."""
        raw: dict[str, dict[int, pd.Series]] = {}
        for code, frame in data_map.items():
            close = pd.to_numeric(frame["close"], errors="coerce")
            daily_returns = close.pct_change()
            raw[code] = {}
            for window in self.ALLOCATION_LOOKBACKS:
                volatility = daily_returns.rolling(
                    window, min_periods=window
                ).std()
                raw[code][window] = close.pct_change(window) / volatility.where(
                    volatility > 0
                )
        return raw

    def _fixed_reference_scores(
        self, date: pd.Timestamp, symbols: set[str] | list[str]
    ) -> dict[str, float]:
        """Score symbols at the latest prior close against the fixed basket."""
        date = pd.Timestamp(date)
        score_series = getattr(self, "_candidate_score_series", {})

        def prior_value(code: str, window: int) -> float | None:
            series = score_series.get(code, {}).get(window)
            if series is None or series.empty:
                return None
            position = int(series.index.searchsorted(date, side="left")) - 1
            if position < 0:
                return None
            value = float(series.iloc[position])
            return value if math.isfinite(value) else None

        result: dict[str, float] = {}
        for code in sorted(symbols):
            percentiles: list[float] = []
            for window in self.policy.candidate_lookbacks:
                candidate = prior_value(code, window)
                references = [
                    prior_value(reference, window)
                    for reference in self.policy.regime_symbols
                ]
                valid_references = [
                    value for value in references if value is not None
                ]
                if candidate is None or len(valid_references) != len(references):
                    break
                percentiles.append(
                    sum(value <= candidate for value in valid_references)
                    / len(self.policy.regime_symbols)
                )
            if len(percentiles) == len(self.policy.candidate_lookbacks):
                result[code] = sum(percentiles) / len(percentiles)
        return result

    def _record_order_event(
        self,
        *,
        date: str,
        signal: Signal,
        event: str,
        **details: Any,
    ) -> None:
        """Append a compact, serializable order decision to the audit trail."""
        note_order(self, signal, date, event, **details)
        self.order_events.append(
            {
                "date": date,
                "symbol": signal.symbol,
                "strategy": signal.strategy_name,
                "direction": signal.direction,
                "event": event,
                "signal_date": signal.signal_date,
                **details,
            }
        )

    def _record_buy_rejection(
        self,
        *,
        date: str,
        signal: Signal,
        event: str,
        **details: Any,
    ) -> None:
        """Record the concrete execution check that rejected a buy."""
        self._record_order_event(
            date=date,
            signal=signal,
            event=event,
            **details,
        )

    def _remaining_buy_capacity(
        self,
        signal: Signal,
        strategy: BaseStrategy,
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
    ) -> tuple[float, float]:
        """Return execution-day equity and remaining currency exposure capacity."""
        prices = self._execution_mark_prices(data_map, date)
        current_assets = self._total_assets_at_prices(prices)
        if current_assets <= 0 or signal.symbol not in prices:
            return current_assets, 0.0

        def position_value(code: str) -> float:
            price = prices.get(code)
            if price is None or price <= 0:
                return 0.0
            return sum(
                position.shares * price
                for position in self.positions.get(code, {}).values()
            )

        symbol_value = position_value(signal.symbol)
        total_value = sum(position_value(code) for code in self.positions)
        symbol_cap = float(strategy.cfg.get("max_symbol_weight", 1.0))
        capacities = [
            current_assets * symbol_cap - symbol_value,
            current_assets * float(self.cfg.get("max_total_weight", 1.0)) - total_value,
        ]
        target_group = self.risk.symbol_groups.get(signal.symbol)
        if target_group:
            group_value = sum(
                position_value(code)
                for code in self.positions
                if self.risk.symbol_groups.get(code) == target_group
            )
            group_cap = float(self.risk.group_weight_limits.get(target_group, 1.0))
            capacities.append(current_assets * group_cap - group_value)
        return current_assets, max(min(capacities), 0.0)

    def _execute_buy(
        self,
        signal: Signal,
        strategy: BaseStrategy,
        date_str: str,
        data_map: dict[str, pd.DataFrame] | None = None,
        date: pd.Timestamp | None = None,
    ) -> bool:
        """Clip an oversized buy to available capacity before inherited checks."""
        adjusted_signal = signal
        if data_map is not None and date is not None and signal.price > 0:
            _, capacity = self._remaining_buy_capacity(signal, strategy, data_map, date)
            execution_price = float(signal.price) * (
                1.0 + float(self.cfg.get("slippage", 0.001))
            )
            capacity_shares = _floor_to_lot(capacity / execution_price)
            if capacity_shares < signal.target_shares:
                if capacity_shares <= 0:
                    self._record_order_event(
                        date=date_str,
                        signal=signal,
                        event="rejected_no_exposure_capacity",
                    )
                    return False
                adjusted_signal = replace(signal, target_shares=capacity_shares)
                link_order(self, signal, adjusted_signal)
                self._record_order_event(
                    date=date_str,
                    signal=signal,
                    event="clipped_to_exposure_capacity",
                    requested_shares=int(signal.target_shares),
                    adjusted_shares=int(capacity_shares),
                )
        events_before = len(self.order_events)
        executed = super()._execute_buy(
            adjusted_signal, strategy, date_str, data_map, date
        )
        if not executed and len(self.order_events) == events_before:
            self._record_order_event(
                date=date_str,
                signal=adjusted_signal,
                event="rejected_by_execution_checks",
            )
        return executed

    @staticmethod
    def _allocate_lots_pro_rata(
        items: list[tuple[Signal, BaseStrategy]], capacity: int
    ) -> list[int]:
        """Split a same-symbol capacity proportionally, with at most one-lot skew."""
        targets = [_floor_to_lot(signal.target_shares) for signal, _ in items]
        total = sum(targets)
        capacity = min(_floor_to_lot(capacity), total)
        if capacity <= 0:
            return [0] * len(items)
        if capacity >= total:
            return targets

        exact = [capacity * target / total for target in targets]
        allocated = [_floor_to_lot(value) for value in exact]
        remaining_lots = (capacity - sum(allocated)) // A_SHARE_LOT_SIZE
        order = sorted(
            range(len(items)),
            key=lambda index: (
                -(exact[index] - allocated[index]),
                items[index][0].strategy_name,
            ),
        )
        while remaining_lots > 0:
            progressed = False
            for index in order:
                if allocated[index] >= targets[index]:
                    continue
                allocated[index] += A_SHARE_LOT_SIZE
                remaining_lots -= 1
                progressed = True
                if remaining_lots == 0:
                    break
            if not progressed:
                break
        return allocated

    def _remaining_adv_capacity(
        self,
        symbol: str,
        direction: str,
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
    ) -> int | None:
        """Return a liquidity cap when the concrete engine enables ADV limits."""
        del symbol, direction, data_map, date
        return None

    def _buy_batch_capacity(
        self,
        items: list[tuple[Signal, BaseStrategy]],
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
    ) -> int:
        """Return the shared cash, exposure, and liquidity capacity for one symbol."""
        if not items:
            return 0
        signal = items[0][0]
        if any(item[0].symbol != signal.symbol for item in items[1:]):
            raise ValueError("A buy batch must contain exactly one symbol")
        if any(
            not math.isclose(float(item[0].price), float(signal.price))
            for item in items[1:]
        ):
            raise ValueError("A buy batch must use one execution price")
        requested = sum(_floor_to_lot(item[0].target_shares) for item in items)
        execution_price = float(signal.price) * (
            1.0 + float(self.cfg.get("slippage", 0.001))
        )
        if requested <= 0 or execution_price <= 0:
            return 0
        if signal.symbol not in self.positions and len(self.positions) >= int(
            self.cfg.get("max_positions", 6)
        ):
            return 0
        # All production strategies for one symbol currently share a validated
        # config. Taking the minimum still preserves safety if a future caller
        # supplies heterogeneous strategy-level exposure limits.
        exposure_value = min(
            self._remaining_buy_capacity(item_signal, strategy, data_map, date)[1]
            for item_signal, strategy in items
        )
        exposure_shares = _floor_to_lot(exposure_value / execution_price)
        cash_shares = self._cash_affordable_batch_capacity(
            items, requested, execution_price
        )
        capacities = [requested, exposure_shares, cash_shares]
        adv_capacity = self._remaining_adv_capacity(
            signal.symbol, "buy", data_map, date
        )
        if adv_capacity is not None:
            capacities.append(adv_capacity)
        return max(min(capacities), 0)

    def _cash_affordable_batch_capacity(
        self,
        items: list[tuple[Signal, BaseStrategy]],
        requested: int,
        execution_price: float,
    ) -> int:
        """Find the largest proportional batch whose separate fees fit cash."""
        commission_rate = float(self.cfg.get("commission_rate", 0.00025))
        min_commission = float(self.cfg.get("min_commission", 0.0))

        def total_cost(capacity: int) -> float:
            allocations = self._allocate_lots_pro_rata(items, capacity)
            return sum(
                shares * execution_price
                + max(shares * execution_price * commission_rate, min_commission)
                for shares in allocations
                if shares > 0
            )

        # Largest-remainder allocation can exhibit the Alabama paradox: adding
        # one lot may remove a small strategy's allocation and one minimum fee.
        # Binary search is safe for normal A-share prices, where one extra lot
        # costs more than every possible fee-floor drop. Use an exact descending
        # scan only for pathological low adjusted prices where that proof fails.
        lot_cost = A_SHARE_LOT_SIZE * execution_price * (1.0 + commission_rate)
        if lot_cost < len(items) * min_commission:
            for capacity in range(_floor_to_lot(requested), -1, -A_SHARE_LOT_SIZE):
                if total_cost(capacity) <= self.cash:
                    return capacity
            return 0

        low = 0
        high = requested // A_SHARE_LOT_SIZE
        while low < high:
            midpoint = (low + high + 1) // 2
            capacity = midpoint * A_SHARE_LOT_SIZE
            if total_cost(capacity) <= self.cash:
                low = midpoint
            else:
                high = midpoint - 1
        return low * A_SHARE_LOT_SIZE

    def _execute_buy_batch(
        self,
        items: list[tuple[Signal, BaseStrategy]],
        date_str: str,
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
    ) -> None:
        """Execute same-symbol confirmations from one shared proportional budget."""
        capacity = self._buy_batch_capacity(items, data_map, date)
        allocations = self._allocate_lots_pro_rata(items, capacity)
        for (signal, strategy), allocated in zip(items, allocations, strict=True):
            if allocated <= 0:
                self._record_order_event(
                    date=date_str,
                    signal=signal,
                    event="rejected_no_shared_batch_capacity",
                )
                continue
            adjusted = replace(signal, target_shares=allocated)
            link_order(self, signal, adjusted)
            if allocated < signal.target_shares:
                self._record_order_event(
                    date=date_str,
                    signal=signal,
                    event="scaled_for_fair_batch_allocation",
                    requested_shares=int(signal.target_shares),
                    adjusted_shares=int(allocated),
                )
            self._execute_buy(adjusted, strategy, date_str, data_map, date)

    def _prepare_open_signal(
        self,
        signal: Signal,
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
        date_to_pos: dict[pd.Timestamp, int],
    ) -> tuple[Signal | None, bool]:
        """Return an executable open order and whether a blocked order must persist."""
        date_str = date.strftime("%Y-%m-%d")
        if self._buy_signal_expired(signal, date, date_to_pos):
            self._record_order_event(
                date=date_str, signal=signal, event="expired_pending_buy"
            )
            return None, False
        frame = data_map.get(signal.symbol)
        if frame is None or date not in frame.index:
            note_order(self, signal, date_str, "blocked_missing_open")
            return None, True
        open_price = frame.loc[date, "open"]
        if pd.isna(open_price) or float(open_price) <= 0:
            note_order(self, signal, date_str, "blocked_missing_open")
            return None, True
        executable = replace(signal, price=float(open_price))
        link_order(self, signal, executable)
        limit_state = self._opening_limit_state(
            executable, frame, date, float(open_price)
        )
        if limit_state == "buy_blocked":
            self._record_order_event(
                date=date_str, signal=signal, event="rejected_limit_up_open"
            )
            return None, False
        if limit_state == "sell_blocked":
            note_order(self, signal, date_str, "blocked_limit_down_open")
            return None, True
        return executable, False

    def _execute_pending_signals(
        self,
        pending: list[tuple[Signal, BaseStrategy]],
        data_map: dict[str, pd.DataFrame],
        date: pd.Timestamp,
        date_to_pos: dict[pd.Timestamp, int],
        directions: frozenset[str] | None = None,
        buy_scores: dict[str, float] | None = None,
    ) -> list[tuple[Signal, BaseStrategy]]:
        """Execute selected sides, batching same-symbol buys before any fill."""
        if buy_scores is None:
            buy_scores = getattr(self, "_c6_buy_scores", None)
        date_str = date.strftime("%Y-%m-%d")
        strategy_rank = {"turtle_breakout": 0, "dual_ma": 1, "atr_channel": 2}
        allocation_scores = self._allocation_scores(data_map, date)
        sorted_pending = sorted(
            pending,
            key=lambda item: (
                0 if item[0].direction == "sell" else 1,
                -(
                    buy_scores
                    if item[0].direction == "buy" and buy_scores is not None and item[0].symbol in buy_scores
                    else allocation_scores
                ).get(item[0].symbol, 0.0),
                item[0].symbol,
                strategy_rank.get(item[0].strategy_name, 99),
            ),
        )
        allowed = directions or frozenset({"buy", "sell"})
        unexecuted: list[tuple[Signal, BaseStrategy]] = []
        buy_batches: dict[str, list[tuple[Signal, BaseStrategy]]] = {}
        for signal, strategy in sorted_pending:
            if signal.direction not in allowed:
                unexecuted.append((signal, strategy))
                continue
            receipt = begin_action_batch(self, signal, date_str)
            order = begin_order(self, signal, date_str, defensive=strategy is None)
            if receipt is not None and order is not None:
                receipt["winner_order_ordinal"] = order["order_ordinal"]
            executable_signal, keep_pending = self._prepare_open_signal(
                signal, data_map, date, date_to_pos
            )
            if keep_pending:
                unexecuted.append((signal, strategy))
                finish_action_batch(receipt, remainder=signal.target_shares,
                                    carry=True, release="STILL_LIVE")
            if executable_signal is None:
                if not keep_pending:
                    finish_action_batch(receipt, release="CANCELLED")
                continue
            if order is not None:
                order["authorized_shares"] = int(executable_signal.target_shares)
            code = executable_signal.symbol
            if executable_signal.direction == "buy":
                buy_batches.setdefault(code, []).append((executable_signal, strategy))
            elif executable_signal.direction == "sell":
                sold = self._execute_sell(executable_signal, strategy, date_str)
                remaining = max(executable_signal.target_shares - sold, 0)
                strat_name = (
                    strategy.name if strategy is not None else signal.strategy_name
                )
                current = self.positions.get(signal.symbol, {}).get(strat_name)
                current_shares = int(current.shares) if current is not None else 0
                release_sublot = (
                    0 < remaining < A_SHARE_LOT_SIZE
                    and remaining < current_shares
                )
                if release_sublot:
                    self._record_order_event(
                        date=date_str,
                        signal=signal,
                        event="released_unexecutable_sublot_sell",
                        remaining_shares=int(remaining),
                        current_shares=current_shares,
                    )
                    finish_action_batch(receipt, filled=sold, release="UNEXECUTABLE_SUBLOT")
                elif remaining > 0 and current_shares > 0:
                    residual = replace(signal, target_shares=remaining)
                    link_order(self, signal, residual)
                    unexecuted.append(
                        (residual, strategy)
                    )
                    finish_action_batch(receipt, filled=sold, remainder=remaining,
                                        carry=True, release="STILL_LIVE")
                else:
                    finish_action_batch(receipt, filled=sold,
                                        release="FILLED" if remaining == 0 else "POSITION_ABSENT")
                if order is not None and sold == 0 and order["status"] == "pending":
                    order.update(status="cancelled" if release_sublot or current_shares == 0 else "blocked",
                                 blocked_reason="unexecutable_sublot" if release_sublot else "position_absent" if current_shares == 0 else "execution_checks")
        for items in buy_batches.values():
            self._execute_buy_batch(items, date_str, data_map, date)
        retained = self._dedupe_pending_signals(unexecuted)
        retained_ids = {id(signal) for signal, _ in retained}
        for signal, _ in unexecuted:
            if id(signal) not in retained_ids:
                record = order_receipt(self, signal, date_str)
                if record is not None:
                    record.update(status="suppressed", blocked_reason="pending_deduplication_or_sell_conflict")
        return retained

    def _opening_limit_state(
        self,
        signal: Signal,
        frame: pd.DataFrame,
        date: pd.Timestamp,
        open_price: float,
    ) -> str | None:
        """Classify an opening board limit without consuming a pending sell."""
        location = frame.index.get_loc(date)
        if location <= 0:
            return None
        previous_close = float(frame.iloc[location - 1]["close"])
        if previous_close <= 0:
            return None
        change = (open_price - previous_close) / previous_close
        limit_up = _limit_pct_for_code(
            signal.symbol, self.cfg, self.symbol_names.get(signal.symbol, "")
        )
        epsilon = float(self.cfg.get("limit_price_epsilon", 0.001))
        if signal.direction == "buy" and change >= limit_up - epsilon:
            return "buy_blocked"
        if signal.direction == "sell" and change <= -limit_up + epsilon:
            return "sell_blocked"
        return None

    def _apply_portfolio_risk(
        self,
        current_assets: float,
        date_str: str,
        all_dates: list[pd.Timestamp],
        date_to_pos: dict[pd.Timestamp, int],
        pending: list[tuple[Signal, BaseStrategy]],
    ) -> tuple[list[tuple[Signal, BaseStrategy]], bool, bool]:
        """Apply T+1 liquidation and accurately report the durable risk lock."""
        risk_status = self.risk.check_portfolio_risk(
            current_assets,
            date_str,
            trading_dates=all_dates,
            date_to_pos=date_to_pos,
        )
        if risk_status is None and self.risk.check_daily_loss(current_assets):
            risk_status = "daily loss limit"
        risk_blocked = self._has_pending_liquidation(pending)
        if risk_blocked:
            risk_status = risk_status or "circuit breaker liquidation pending"
        if not risk_status:
            return pending, risk_blocked, False

        liquidate = False
        if risk_status == "portfolio drawdown circuit breaker":
            liquidate = bool(self.cfg.get("liquidate_on_circuit_breaker", True))
            if liquidate:
                print(
                    f"  WARNING [{date_str}] {risk_status}: generate T+1 "
                    "liquidation signals and enter a persistent risk lock"
                )
                liquidation_signals = self._generate_liquidation_signals(date_str)
                pending = self._dedupe_pending_signals(
                    [item for item in pending if item[0].direction == "sell"]
                    + liquidation_signals
                )
            else:
                print(
                    f"  WARNING [{date_str}] {risk_status}: block new entries "
                    "under a persistent risk lock"
                )
        if (
            isinstance(self.risk, PersistentRiskManager)
            and self.risk.persistent_lock
            and not self._risk_lock_logged
        ):
            self.risk_events.append(
                {
                    "date": self.risk.lock_date or date_str,
                    "event": "persistent_portfolio_risk_lock",
                    "drawdown": self.risk.lock_drawdown,
                }
            )
            self._risk_lock_logged = True
        return pending, True, liquidate

    def _prepare_run(
        self,
        symbols_dict: dict[str, str],
        start_date: str,
        end_date: str,
        start_ts: pd.Timestamp,
        end_ts: pd.Timestamp,
        per_symbol_config: dict[str, dict] | None,
        profile: str | None,
        config_route: str,
        data_dir: str | None,
        cache_dir: str | None,
    ) -> tuple[
        dict[str, pd.DataFrame],
        dict[str, dict[str, pd.Series]],
        list[pd.Timestamp],
        dict[pd.Timestamp, int],
    ]:
        """Optionally compute indicators on pre-start history while trading flat."""
        if self._indicator_state == "cold":
            prepared = super()._prepare_run(
                symbols_dict,
                start_date,
                end_date,
                start_ts,
                end_ts,
                per_symbol_config,
                profile,
                config_route,
                data_dir,
                cache_dir,
            )
        else:
            warm_start = start_ts - pd.Timedelta(days=self._warmup_calendar_days)
            data_map, indicator_map, _, _ = super()._prepare_run(
                symbols_dict,
                warm_start.strftime("%Y-%m-%d"),
                end_date,
                warm_start,
                end_ts,
                per_symbol_config,
                profile,
                config_route,
                data_dir,
                cache_dir,
            )
            trading_dates = sorted(
                {
                    date
                    for frame in data_map.values()
                    for date in frame.index
                    if start_ts <= date <= end_ts
                }
            )
            date_to_pos = {
                pd.Timestamp(date): index for index, date in enumerate(trading_dates)
            }
            prepared = data_map, indicator_map, trading_dates, date_to_pos
        self._allocation_raw_series = self._build_allocation_raw_series(prepared[0])
        self._allocation_score_cache = {}
        return prepared

    def run(
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
    ) -> dict:
        """Run with an explicit cold or warm indicator-state contract."""
        indicator_state = str(indicator_state).lower()
        if indicator_state not in {"cold", "warm"}:
            raise ValueError("indicator_state must be either 'cold' or 'warm'")
        warmup_calendar_days = _require_int(
            "warmup_calendar_days", warmup_calendar_days, min_value=120
        )
        self._indicator_state = indicator_state
        self._warmup_calendar_days = warmup_calendar_days
        self._requested_start_date = start_date
        self._requested_end_date = end_date
        return super().run(
            symbols_dict,
            start_date,
            end_date,
            per_symbol_config=per_symbol_config,
            profile=profile,
            config_route=config_route,
            data_dir=data_dir,
            cache_dir=cache_dir,
        )

    def _build_result(self, final_assets: float, all_dates: list[pd.Timestamp]) -> dict:
        """Extend the inherited report with allocation and resolved-config audits."""
        result = super()._build_result(final_assets, all_dates)
        result.update(
            {
                "indicator_state": self._indicator_state,
                "allocation_lookbacks": list(self.ALLOCATION_LOOKBACKS),
                "order_events": list(self.order_events),
                "resolved_symbol_configs": {
                    code: dict(config) for code, config in self.symbol_configs.items()
                },
                "persistent_risk_lock": bool(
                    isinstance(self.risk, PersistentRiskManager)
                    and self.risk.persistent_lock
                ),
            }
        )
        return result


CausalBacktestEngine = _CausalBacktestEngine

__all__ = ["CausalBacktestEngine"]
