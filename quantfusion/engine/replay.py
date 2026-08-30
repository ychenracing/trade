"""Production regime replay and deployment decision engines."""

from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Callable, Sequence

import pandas as pd

from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.config.regime import (
    WEAK_CONFIRMED_DRAWDOWN,
    WEAK_DRAWDOWN_ALERT,
    WEAK_EMERGENCY_DRAWDOWN,
    WEAK_TERMINAL_DRAWDOWN,
)
from quantfusion.config.weak import weak_regime_config, weak_regime_policy
from quantfusion.domain.models import AccountState, BarContext
from quantfusion.domain.rules import require_finite
from quantfusion.engine.universe import BacktestEngine, SleeveBacktestEngine
from quantfusion.regime.evidence import (
    detect_regime,
    local_frame,
    normalized_timestamp,
    select_positive_momentum_leaders,
)
from quantfusion.regime.models import (
    DailyRouteStep,
    DeploymentDecision,
    LeaderSelection,
    RegimeRoute,
)
from quantfusion.regime.state_machine import boundary_route, simulate_route_sequence
from quantfusion.strategy.weak import (
    CashPreservationStrategy,
    PositiveMomentumHoldStrategy,
)

_local_frame = local_frame
_normalized_timestamp = normalized_timestamp
_weak_regime_config = weak_regime_config
_weak_regime_policy = weak_regime_policy

class ProductionRouteController:
    """Apply the daily outer route inside one persistent production ledger.

    The controller never injects an account snapshot and never replaces the
    ensemble's execution engine. It filters or adds close-generated T+1 orders
    while the existing sleeve cash, positions, pending orders, sticky state,
    risk peaks, cooldowns, and strategy instances continue across every route
    transition.
    """

    def __init__(
        self,
        route_sequence: Sequence[DailyRouteStep],
        *,
        leader_data_dir: str | Path,
    ) -> None:
        self.route_by_date = {step.date: step.route for step in route_sequence}
        self.starts_defensive = bool(route_sequence) and route_sequence[0].route in {
            RegimeRoute.WEAK.value,
            RegimeRoute.CASH.value,
        }
        self.leader_data_dir = str(leader_data_dir)
        self.previous_route: str | None = None
        self.events: list[dict[str, Any]] = []
        self.journal: list[dict[str, Any]] = []
        self._leader_cache: dict[str, tuple[str, ...]] = {}
        self._weak_strategies: dict[
            tuple[str, str], PositiveMomentumHoldStrategy
        ] = {}
        self._weak_episode_leaders: tuple[str, ...] = ()
        self._carry_trend_book = False
        self._restoring_trend_cash = False

    @staticmethod
    def _drop_buys(states: list[Any]) -> None:
        for state in states:
            state.pending = [
                item for item in state.pending if item[0].direction != "buy"
            ]

    @staticmethod
    def _queue_liquidations(
        states: list[Any], date_str: str, *, weak_only: bool
    ) -> None:
        for state in states:
            liquidations = state.sleeve._generate_liquidation_signals(
                date_str,
                reason="production outer-route migration",
            )
            selected = [
                item
                for item in liquidations
                if (
                    item[0].strategy_name == PositiveMomentumHoldStrategy.name
                ) == weak_only
            ]
            if not selected:
                continue
            state.pending = state.sleeve._dedupe_pending_signals(
                [item for item in state.pending if item[0].direction == "sell"]
                + selected
            )

    def _leaders(self, symbols: Sequence[str], date_str: str) -> tuple[str, ...]:
        cached = self._leader_cache.get(date_str)
        if cached is not None:
            return cached
        try:
            selected = select_positive_momentum_leaders(
                tuple(symbols),
                data_dir=self.leader_data_dir,
                as_of=date_str,
            ).selected_symbols
        except (OSError, RuntimeError, ValueError):
            selected = ()
        self._leader_cache[date_str] = tuple(selected)
        return tuple(selected)

    def _append_weak_signals(
        self,
        states: list[Any],
        date: pd.Timestamp,
        symbols_dict: dict[str, str],
    ) -> tuple[str, ...]:
        date_str = date.strftime("%Y-%m-%d")
        leaders = self._weak_episode_leaders
        if not leaders:
            return ()
        # Weak routing uses one account sleeve, not three duplicate weak books.
        # All idle cash is migrated to this sleeve on the route transition.
        for state in states[:1]:
            current_assets = state.sleeve._total_assets(state.data_map, date)
            for symbol in symbols_dict:
                key = (str(state.sleeve.sleeve_name), symbol)
                strategy = self._weak_strategies.get(key)
                if strategy is None:
                    cfg = dict(state.sleeve.cfg)
                    cfg.update(_weak_regime_config(max(len(leaders), 1)))
                    strategy = PositiveMomentumHoldStrategy(cfg)
                    self._weak_strategies[key] = strategy
                # Dynamic weak-route strategies own real positions and therefore
                # must participate in every liquidation path. Keep them in the
                # external registry so the sleeve risk/sector/route controls can
                # find them without the core signal loop evaluating them a second
                # time on the same close.
                registered = state.sleeve.external_strategy_instances.setdefault(
                    symbol, []
                )
                if strategy not in registered:
                    registered.append(strategy)
                if symbol not in leaders and strategy.position is None:
                    continue
                frame = state.data_map.get(symbol)
                indicators = state.indicator_map.get(symbol)
                if frame is None or indicators is None or date not in frame.index:
                    continue
                ctx = BarContext(
                    i=frame.index.get_loc(date),
                    df=frame,
                    current_assets=current_assets,
                    indicators=indicators,
                    symbol=symbol,
                    date=date_str,
                )
                signal = strategy.on_bar(ctx)
                if signal is None:
                    continue
                if signal.direction == "buy":
                    if symbol not in leaders or state.sleeve._pending_has_buy(
                        state.pending, symbol, strategy.name
                    ):
                        continue
                elif signal.direction == "sell":
                    if state.sleeve._pending_has_sell(
                        state.pending, symbol, strategy.name
                    ):
                        continue
                state.pending.append((signal, strategy))
            state.pending = state.sleeve._dedupe_pending_signals(state.pending)
        return leaders

    @staticmethod
    def _shift_free_cash(states: list[Any], weights: Sequence[float]) -> None:
        """Move idle cash causally and neutralize the external flow in risk peaks."""
        total_cash = sum(float(state.sleeve.cash) for state in states)
        if total_cash <= 0:
            return
        targets = [total_cash * float(weight) for weight in weights]
        targets[-1] = total_cash - sum(targets[:-1])
        for state, target in zip(states, targets, strict=True):
            old = float(state.sleeve.cash)
            state.sleeve.cash = target
            flow = target - old
            risk = state.sleeve.risk
            for attribute in (
                "peak_assets",
                "lifetime_peak_assets",
                "daily_start_assets",
            ):
                if hasattr(risk, attribute):
                    setattr(
                        risk,
                        attribute,
                        max(0.0, float(getattr(risk, attribute, 0.0)) + flow),
                    )

    def after_close(
        self,
        states: list[Any],
        date: pd.Timestamp,
        symbols_dict: dict[str, str],
    ) -> None:
        """Route close-generated orders while preserving all execution state."""
        date_str = date.strftime("%Y-%m-%d")
        route = self.route_by_date.get(
            date_str, self.previous_route or RegimeRoute.CASH.value
        )
        changed = route != self.previous_route
        if changed:
            self.events.append(
                {
                    "date": date_str,
                    "event": "production_route_transition",
                    "from": self.previous_route,
                    "to": route,
                }
            )

        leaders: tuple[str, ...] = ()
        if route == RegimeRoute.TRANSITION_TO_WEAK.value:
            # The cross-market overlay is the execution owner for an existing
            # trend book. It applies the dual-confirmed transition throttle;
            # stacking another route-level scale here changes the downstream
            # strategy path and double-counts the same risk evidence.
            pass
        elif route == RegimeRoute.CASH.value:
            self._drop_buys(states)
            if changed:
                self._queue_liquidations(states, date_str, weak_only=False)
                self._queue_liquidations(states, date_str, weak_only=True)
            self._carry_trend_book = False
            self._weak_episode_leaders = ()
        elif route in {
            RegimeRoute.WEAK.value,
            RegimeRoute.TRANSITION_TO_TREND.value,
        }:
            if changed and route == RegimeRoute.WEAK.value:
                self._carry_trend_book = any(
                    strat_name != PositiveMomentumHoldStrategy.name
                    for state in states
                    for positions in state.sleeve.positions.values()
                    for strat_name in positions
                )
            if self._carry_trend_book:
                # Preserve an established trend book through an index-led risk
                # episode. Existing strategies retain their own sell logic;
                # the cross-market overlay remains the sole buy/trim authority
                # so route and overlay cannot multiply the same reduction.
                pass
            else:
                self._drop_buys(states)
                if changed and not self._weak_episode_leaders:
                    # Freeze the leaders for this weak episode. Re-ranking every
                    # close turned the defensive book into a hidden rotation
                    # strategy, increasing trades precisely when conditions are
                    # least forgiving. A new weak episode receives a new,
                    # causal selection from its transition close.
                    self._weak_episode_leaders = self._leaders(
                        tuple(symbols_dict), date_str
                    )
                self._shift_free_cash(states, (1.0, 0.0, 0.0))
                leaders = self._append_weak_signals(states, date, symbols_dict)
        else:
            carried_trend_book = self._carry_trend_book
            self._carry_trend_book = False
            self._weak_episode_leaders = ()
            if changed and self.previous_route in {
                RegimeRoute.WEAK.value,
                RegimeRoute.CASH.value,
                RegimeRoute.TRANSITION_TO_TREND.value,
            }:
                self._queue_liquidations(states, date_str, weak_only=True)
                self._restoring_trend_cash = not carried_trend_book
            if self._restoring_trend_cash:
                self._shift_free_cash(states, (1 / 3, 1 / 3, 1 / 3))
                weak_positions = any(
                    PositiveMomentumHoldStrategy.name in positions
                    for state in states
                    for positions in state.sleeve.positions.values()
                )
                if not weak_positions:
                    self._restoring_trend_cash = False

        sleeve_rows = []
        for state in states:
            risk = state.sleeve.risk
            sleeve_rows.append(
                {
                    "name": state.sleeve.sleeve_name,
                    "cash": float(state.sleeve.cash),
                    "positions": sorted(state.sleeve.positions),
                    "pending": len(state.pending),
                    "risk_lock": bool(getattr(risk, "persistent_lock", False)),
                    "risk_peak": float(getattr(risk, "peak_assets", 0.0)),
                    "sticky_leader": getattr(
                        state.sleeve, "_sticky_leader", None
                    ),
                }
            )
        self.journal.append(
            {
                "date": date_str,
                "route": route,
                "leaders": list(leaders),
                "sleeves": sleeve_rows,
            }
        )
        self.previous_route = route

    def result_snapshot(self) -> dict[str, Any]:
        """Return the complete serializable route and persistence audit."""
        cooldowns = {
            f"{sleeve}:{symbol}": {
                "cooldown_end": strategy._cooldown_end,
                "exit_reason": strategy._exit_reason,
                "failures": strategy._failures,
            }
            for (sleeve, symbol), strategy in sorted(self._weak_strategies.items())
        }
        return {
            "engine": "ProductionReplayEngine",
            "route_sequence": [
                {"date": date, "route": route}
                for date, route in sorted(self.route_by_date.items())
            ],
            "transition_events": list(self.events),
            "daily_journal": list(self.journal),
            "weak_cooldowns": cooldowns,
        }

    @property
    def current_route(self) -> str | None:
        """Return the route only when it owns overlay execution next session."""
        # An established trend account retains the normal cross-market overlay
        # as its single risk owner. Only a dedicated weak/cash book suppresses
        # duplicate overlay execution.
        if self._carry_trend_book and self.previous_route in {
            RegimeRoute.WEAK.value,
            RegimeRoute.TRANSITION_TO_TREND.value,
        }:
            return None
        return self.previous_route


class ProductionReplayEngine:
    """Run daily causal routing through one persistent Quant Fusion account."""

    def __init__(
        self,
        initial_capital: float = 2_000_000,
        cfg: dict | None = None,
        policy: PortfolioPolicy | None = None,
    ) -> None:
        self.initial_capital = require_finite(
            "initial_capital", initial_capital, min_value=0.01
        )
        self.cfg = dict(cfg or {})
        self.policy = policy
        self.delegate: BacktestEngine | None = None

    def run(
        self,
        symbols_dict: dict[str, str],
        start_date: str,
        end_date: str,
        *,
        data_dir: str,
        regime_data_dir: str,
        leader_data_dir: str | None = None,
        indicator_state: str = "warm",
        warmup_calendar_days: int = 365,
        per_symbol_config: dict[str, dict] | None = None,
        profile: str | None = None,
        config_route: str = "auto",
        risk_state: dict | None = None,
        account_state: AccountState | None = None,
    ) -> dict[str, Any]:
        route_sequence = simulate_route_sequence(
            regime_data_dir,
            start_date=start_date,
            end_date=end_date,
        )
        controller = ProductionRouteController(
            route_sequence,
            leader_data_dir=leader_data_dir or data_dir,
        )
        starts_defensive = controller.starts_defensive
        replay_policy = self.policy or (
            replace(
                PortfolioPolicy(),
                drawdown_alert=WEAK_DRAWDOWN_ALERT,
                confirmed_drawdown=WEAK_CONFIRMED_DRAWDOWN,
                emergency_drawdown=WEAK_EMERGENCY_DRAWDOWN,
                terminal_drawdown=WEAK_TERMINAL_DRAWDOWN,
                concentration_drawdown_adjustment=0.01,
            )
            if starts_defensive
            else PortfolioPolicy()
        )
        self.delegate = BacktestEngine(
            self.initial_capital,
            cfg=self.cfg,
            policy=replay_policy,
        )
        result = self.delegate.run(
            symbols_dict,
            start_date,
            end_date,
            per_symbol_config=per_symbol_config,
            profile=profile,
            config_route=config_route,
            data_dir=data_dir,
            indicator_state=indicator_state,
            warmup_calendar_days=warmup_calendar_days,
            allocation_mode="ensemble",
            risk_state=risk_state,
            account_state=account_state,
            route_controller=controller,
        )
        result["route_sequence"] = result["production_replay"]["route_sequence"]
        result["deployment_policy"] = "production_daily_replay"
        result["requested_symbols"] = sorted(symbols_dict)
        result["selected_symbols"] = sorted(symbols_dict)
        result["unavailable_symbols"] = []
        return result


class RegimeAdaptiveBacktestEngine:
    """Preserve the trend engine and route weak deployments causally."""

    def __init__(
        self,
        initial_capital: float = 2_000_000,
        cfg: dict | None = None,
        policy: PortfolioPolicy | None = None,
    ) -> None:
        self.initial_capital = require_finite(
            "initial_capital", initial_capital, min_value=0.01
        )
        self.cfg = dict(cfg or {})
        self.policy = policy
        self.last_decision: DeploymentDecision | None = None
        self.delegate: Any = None

    @staticmethod
    def _available_local_symbols(
        symbols_dict: dict[str, str],
        *,
        data_dir: str | None,
        start_date: str,
        end_date: str,
        warmup_calendar_days: int,
    ) -> dict[str, str]:
        """Return symbols with at least one causal observation in the run window."""
        if data_dir is None:
            return dict(symbols_dict)
        earliest = _normalized_timestamp(start_date) - pd.Timedelta(
            days=warmup_calendar_days
        )
        latest = _normalized_timestamp(end_date)
        available: dict[str, str] = {}
        for code, name in symbols_dict.items():
            try:
                frame = _local_frame(data_dir, code, str(latest.date()))
            except (OSError, RuntimeError, ValueError):
                continue
            if not frame.loc[(frame.index >= earliest) & (frame.index <= latest)].empty:
                available[code] = name
        return available

    @staticmethod
    def _boundary(start_date: str, selection_boundary: str | None) -> str:
        if selection_boundary is not None:
            boundary = _normalized_timestamp(selection_boundary)
            if boundary >= _normalized_timestamp(start_date):
                raise ValueError("selection_boundary must be before start_date")
            return str(boundary.date())
        return str((_normalized_timestamp(start_date) - pd.Timedelta(days=1)).date())

    def decide_current(
        self,
        symbols_dict: dict[str, str],
        *,
        as_of: str,
        data_dir: str | Path,
        leader_data_dir: str | Path | None = None,
        leader_frame_loader: Callable[[str, str], pd.DataFrame] | None = None,
    ) -> DeploymentDecision:
        """Make a point-in-time route decision from data through ``as_of``.

        This is the CURRENT-day route used by ``daily_signal_scan`` and the
        account engine (report 3.3/3.4 "历史和账户使用同一状态机"). It is driven
        by the same low-frequency daily state machine as the audited
        ``route_sequence``, so the label the user sees each day matches the
        route that drives the decision. It fails closed to CASH on stale or
        incomplete evidence.
        """
        boundary = _normalized_timestamp(as_of)
        route = boundary_route(data_dir, as_of=str(boundary.date()))
        regime = detect_regime(data_dir, as_of=str(boundary.date()))
        when = str(boundary.date())
        if route == RegimeRoute.CASH:
            return DeploymentDecision(
                name="cash_preservation",
                boundary=when,
                reason="dynamic route failed closed to CASH (stale/incomplete evidence)",
                regime=regime,
                leaders=None,
            )
        if route in (RegimeRoute.TREND, RegimeRoute.TRANSITION_TO_TREND):
            return DeploymentDecision(
                name="frozen_trend_engine",
                boundary=when,
                reason="dynamic route is in a confirmed medium-term uptrend",
                regime=regime,
                leaders=None,
            )
        leaders = select_positive_momentum_leaders(
            tuple(symbols_dict),
            data_dir=leader_data_dir or data_dir,
            as_of=when,
            frame_loader=leader_frame_loader,
        )
        name = (
            "positive_momentum_hold" if leaders.selected_symbols else "cash_preservation"
        )
        return DeploymentDecision(
            name,
            when,
            "dynamic route drifted to weak; selected only positive leaders",
            regime,
            leaders,
        )

    def decide(
        self,
        symbols_dict: dict[str, str],
        *,
        start_date: str,
        data_dir: str | Path,
        leader_data_dir: str | Path | None = None,
        selection_boundary: str | None = None,
    ) -> DeploymentDecision:
        """Choose a route using only complete evidence available at the boundary."""
        boundary = self._boundary(start_date, selection_boundary)
        regime = detect_regime(data_dir, as_of=boundary)
        if regime.regime == "unknown":
            return DeploymentDecision(
                name="cash_preservation",
                boundary=boundary,
                reason="fixed-index evidence is incomplete, invalid, or stale",
                regime=regime,
                leaders=None,
            )
        if regime.regime == "trending":
            return DeploymentDecision(
                name="frozen_trend_engine",
                boundary=boundary,
                reason="both fixed indices had MA20 above MA60 before deployment",
                regime=regime,
                leaders=None,
            )
        leaders = select_positive_momentum_leaders(
            tuple(symbols_dict),
            data_dir=leader_data_dir or data_dir,
            as_of=boundary,
        )
        name = "positive_momentum_hold" if leaders.selected_symbols else "cash_preservation"
        reason = (
            "fixed-index trend was not confirmed; selected only positive "
            "240-session leaders"
            if leaders.selected_symbols
            else "fixed-index trend was not confirmed and no positive "
            "240-session leader was observable"
        )
        return DeploymentDecision(name, boundary, reason, regime, leaders)

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
        indicator_state: str = "cold",
        warmup_calendar_days: int = 365,
        allocation_mode: str | None = None,
        risk_state: dict | None = None,
        account_state: AccountState | None = None,
        selection_boundary: str | None = None,
        deployment_mode: str = "auto",
        regime_data_dir: str | None = None,
        leader_data_dir: str | None = None,
        allow_unavailable_symbols: bool = False,
    ) -> dict:
        """Run a trend-preserving or weak-regime deployment with one schema.

        The trend route fails closed when a requested symbol has no observable
        local data. Research callers that intentionally evaluate pre-listing
        universes may opt in to filtering with ``allow_unavailable_symbols``.
        """
        mode = str(deployment_mode).lower()
        if mode not in {"auto", "replay", "trend", "weak"}:
            raise ValueError("deployment_mode must be auto, replay, trend, or weak")
        if not isinstance(allow_unavailable_symbols, bool):
            raise ValueError("allow_unavailable_symbols must be bool")
        evidence_dir = regime_data_dir or data_dir
        if evidence_dir is None:
            raise ValueError(
                "regime-adaptive mode requires a local data_dir or regime_data_dir"
            )
        if mode in {"auto", "replay"}:
            if data_dir is None:
                raise ValueError("production replay requires a local stock data_dir")
            tradable_symbols = self._available_local_symbols(
                symbols_dict,
                data_dir=data_dir,
                start_date=start_date,
                end_date=end_date,
                warmup_calendar_days=warmup_calendar_days,
            )
            unavailable = tuple(sorted(set(symbols_dict) - set(tradable_symbols)))
            if unavailable and not allow_unavailable_symbols:
                raise RuntimeError(
                    "requested replay symbols have no observable data: "
                    + ", ".join(unavailable)
                )
            if not tradable_symbols:
                raise RuntimeError("production replay has no observable trade symbols")
            replay = ProductionReplayEngine(
                self.initial_capital,
                cfg=self.cfg,
                policy=self.policy,
            )
            result = replay.run(
                tradable_symbols,
                start_date,
                end_date,
                data_dir=data_dir,
                regime_data_dir=str(evidence_dir),
                leader_data_dir=leader_data_dir,
                indicator_state=indicator_state,
                warmup_calendar_days=warmup_calendar_days,
                per_symbol_config=per_symbol_config,
                profile=profile,
                config_route=config_route,
                risk_state=risk_state,
                account_state=account_state,
            )
            self.delegate = replay.delegate
            decision = self.decide_current(
                tradable_symbols,
                as_of=end_date,
                data_dir=evidence_dir,
                leader_data_dir=leader_data_dir,
            )
            self.last_decision = decision
            result["deployment_decision"] = asdict(decision)
            result["unavailable_symbols"] = list(unavailable)
            return result
        decision = self.decide(
            symbols_dict,
            start_date=start_date,
            data_dir=evidence_dir,
            leader_data_dir=leader_data_dir,
            selection_boundary=selection_boundary,
        )
        if mode == "trend":
            decision = replace(
                decision,
                name="frozen_trend_engine",
                reason="trend mode forced by caller",
            )
        elif mode == "weak":
            leaders = select_positive_momentum_leaders(
                tuple(symbols_dict),
                data_dir=leader_data_dir or evidence_dir,
                as_of=decision.boundary,
            )
            decision = replace(
                decision,
                name=(
                    "positive_momentum_hold"
                    if leaders.selected_symbols
                    else "cash_preservation"
                ),
                reason="weak mode forced by caller",
                leaders=leaders,
            )
        self.last_decision = decision
        executed_symbols: tuple[str, ...] = ()
        unavailable_symbols: tuple[str, ...] = ()
        result: dict[str, Any] | None = None

        if decision.name == "frozen_trend_engine":
            tradable_symbols = self._available_local_symbols(
                symbols_dict,
                data_dir=data_dir,
                start_date=start_date,
                end_date=end_date,
                warmup_calendar_days=warmup_calendar_days,
            )
            unavailable_symbols = tuple(
                sorted(set(symbols_dict) - set(tradable_symbols))
            )
            if unavailable_symbols and not allow_unavailable_symbols:
                raise RuntimeError(
                    "requested trend symbols have no observable data: "
                    + ", ".join(unavailable_symbols)
                )
            if not tradable_symbols:
                leaders = LeaderSelection(
                    as_of=decision.boundary,
                    requested_symbols=tuple(sorted(symbols_dict)),
                    observed_symbols=0,
                    selected_symbols=(),
                    selected_returns=(),
                    unavailable_symbols=tuple(sorted(symbols_dict)),
                )
                decision = replace(
                    decision,
                    name="cash_preservation",
                    reason="trend was confirmed but no requested symbol had observable data",
                    leaders=leaders,
                )
                self.last_decision = decision
            else:
                executed_symbols= tuple(sorted(tradable_symbols))
                # Trend route keeps the default three-sleeve ensemble. The
                # total capital (e.g. 2,000,000) is split across the fast,
                # base and slow virtual sub-accounts; the sum never exceeds
                # the total capital and no leverage is used.
                effective_allocation = allocation_mode or "ensemble"
                self.delegate = BacktestEngine(
                    self.initial_capital, cfg=self.cfg, policy=self.policy
                )
                result = self.delegate.run(
                    tradable_symbols,
                    start_date,
                    end_date,
                    per_symbol_config=per_symbol_config,
                    profile=profile,
                    config_route=config_route,
                    data_dir=data_dir,
                    indicator_state=indicator_state,
                    warmup_calendar_days=warmup_calendar_days,
                    allocation_mode=effective_allocation,
                    risk_state=risk_state,
                    account_state=account_state,
                )
        if decision.name != "frozen_trend_engine":
            if self.cfg or self.policy is not None:
                raise ValueError(
                    "weak-regime policy does not accept constructor cfg or policy overrides"
                )
            if per_symbol_config or profile is not None or config_route != "auto":
                raise ValueError("weak-regime policy does not accept trend profile overrides")
            if risk_state is not None or account_state is not None:
                raise NotImplementedError("weak-regime policy does not inject external state")
            leaders = decision.leaders
            selected = leaders.selected_symbols if leaders is not None else ()
            unavailable_symbols = (
                leaders.unavailable_symbols if leaders is not None else ()
            )
            executed_symbols = tuple(selected)
            run_symbols = (
                {code: symbols_dict[code] for code in selected}
                if selected
                else {code: code for code in PortfolioPolicy().regime_symbols}
            )
            self.delegate = SleeveBacktestEngine(
                self.initial_capital,
                cfg=_weak_regime_config(len(selected)),
                policy=_weak_regime_policy(),
                allocation_lookbacks=(5, 10, 20),
                sleeve_name="weak_regime",
            )
            self.delegate.strategy_templates = [
                PositiveMomentumHoldStrategy if selected else CashPreservationStrategy
            ]
            weak_result = self.delegate.run(
                run_symbols,
                start_date,
                end_date,
                data_dir=data_dir,
                indicator_state=indicator_state,
                warmup_calendar_days=warmup_calendar_days,
            )
            weak_result.update(
                {
                    "effective_portfolio_policy": self.delegate.policy.as_dict(),
                    "portfolio_max_positions": max(1, len(selected)),
                    "portfolio_cash_model": "single_account",
                    "allocation_mode": "single",
                }
            )
            result = weak_result
        if result is None:
            raise RuntimeError("deployment route completed without a backtest result")
        result["deployment_decision"] = asdict(decision)
        result["deployment_policy"] = decision.name
        result["requested_symbols"] = sorted(symbols_dict)
        result["selected_symbols"] = list(executed_symbols)
        result["unavailable_symbols"] = list(unavailable_symbols)
        # Report 3.3/3.4: emit the auditable daily route sequence so the
        # historical replay, the current-day account route and the report all
        # share the same state machine (P0-4 "历史和账户使用同一状态机").
        try:
            route_seq = simulate_route_sequence(
                evidence_dir, start_date=start_date, end_date=end_date
            )
            result["route_sequence"] = [
                {"date": step.date, "route": step.route} for step in route_seq
            ]
        except (OSError, RuntimeError, ValueError, TypeError):
            result["route_sequence"] = []
        return result
