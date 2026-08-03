"""Causal regime routing layered over the frozen Quant Fusion engine.

The production trend engine remains untouched.  This module selects between
that engine and a low-turnover weak-regime policy using information available
strictly before the requested deployment period.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Sequence, cast

import pandas as pd

import quant_fusion as qf


REGIME_INDEX_FILES = {"broad": "000300", "technology": "000682"}
LEADER_LOOKBACK = 240
MAX_LEADERS = 3
MAX_SYMBOL_WEIGHT = 0.59
PROFIT_ACTIVATION = 0.30
TRAILING_ATR_MULTIPLIER = 3.0
WEAK_ENTRY_ATR_MULTIPLIER = 5.0
WEAK_HARD_STOP = 0.22
WEAK_TIME_STOP_DAYS = 80
WEAK_TIME_STOP_RETURN = -0.10
MAX_EVIDENCE_STALENESS_DAYS = 10


@dataclass(frozen=True, slots=True)
class IndexTrend:
    """One index observation available at the deployment boundary."""

    code: str
    observed_date: str
    close: float
    ma20: float
    ma60: float
    trending: bool


@dataclass(frozen=True, slots=True)
class RegimeEvidence:
    """Fixed-index regime evidence with explicit failure-closed coverage."""

    as_of: str
    regime: str
    observations: tuple[IndexTrend, ...]


@dataclass(frozen=True, slots=True)
class LeaderSelection:
    """Positive 240-session leaders observable before deployment."""

    as_of: str
    requested_symbols: tuple[str, ...]
    observed_symbols: int
    selected_symbols: tuple[str, ...]
    selected_returns: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class DeploymentDecision:
    """Auditable choice between the frozen trend and weak-regime policies."""

    name: str
    boundary: str
    reason: str
    regime: RegimeEvidence
    leaders: LeaderSelection | None


def _timestamp(value: str | pd.Timestamp) -> pd.Timestamp:
    """Parse one finite timestamp and narrow pandas' optional NaT type."""
    parsed = pd.Timestamp(value)
    if parsed is pd.NaT:
        raise ValueError("date must not be NaT")
    return cast(pd.Timestamp, parsed)


def _normalized_timestamp(value: str | pd.Timestamp) -> pd.Timestamp:
    return cast(pd.Timestamp, _timestamp(value).normalize())


def _local_frame(data_dir: str | Path, code: str, end_date: str) -> pd.DataFrame:
    """Load a local validated frame without reading beyond ``end_date``."""
    boundary = _normalized_timestamp(end_date)
    start = cast(pd.Timestamp, boundary - pd.Timedelta(days=900)).strftime(
        "%Y-%m-%d"
    )
    frame = qf.DataFetcher.load_stock_data(
        code,
        start,
        boundary.strftime("%Y-%m-%d"),
        data_dir=str(data_dir),
    )
    return frame.loc[frame.index <= boundary].copy()


def detect_regime(data_dir: str | Path, *, as_of: str) -> RegimeEvidence:
    """Require both fixed indices to have MA20 above MA60 for a trend route."""
    boundary = _normalized_timestamp(as_of)
    observations: list[IndexTrend] = []
    for code in REGIME_INDEX_FILES.values():
        try:
            frame = _local_frame(data_dir, code, str(boundary.date()))
        except (OSError, RuntimeError, ValueError):
            continue
        closes = pd.Series(
            pd.to_numeric(frame["close"], errors="coerce"), index=frame.index
        ).dropna()
        if len(closes) < 60:
            continue
        close = float(closes.iloc[-1])
        ma20 = float(closes.tail(20).mean())
        ma60 = float(closes.tail(60).mean())
        if not all(math.isfinite(value) and value > 0 for value in (close, ma20, ma60)):
            continue
        observed_date = _normalized_timestamp(str(closes.index[-1]))
        if (boundary - observed_date).days > MAX_EVIDENCE_STALENESS_DAYS:
            continue
        observations.append(
            IndexTrend(
                code=code,
                observed_date=str(observed_date.date()),
                close=close,
                ma20=ma20,
                ma60=ma60,
                trending=ma20 > ma60,
            )
        )
    if len(observations) != len(REGIME_INDEX_FILES):
        regime = "unknown"
    else:
        regime = "trending" if all(item.trending for item in observations) else "choppy"
    return RegimeEvidence(
        as_of=str(boundary.date()),
        regime=regime,
        observations=tuple(observations),
    )


def select_positive_momentum_leaders(
    symbols: Sequence[str],
    *,
    data_dir: str | Path,
    as_of: str,
    maximum: int = MAX_LEADERS,
) -> LeaderSelection:
    """Select positive long-horizon leaders with deterministic tie-breaking."""
    normalized = tuple(sorted(str(symbol) for symbol in symbols))
    if not normalized or len(normalized) != len(set(normalized)):
        raise ValueError("symbols must be a non-empty set without duplicates")
    if maximum < 1:
        raise ValueError("maximum must be positive")
    boundary = _normalized_timestamp(as_of)
    observations: list[tuple[float, str]] = []
    observed = 0
    for code in normalized:
        try:
            frame = _local_frame(data_dir, code, str(boundary.date()))
            closes = pd.Series(
                pd.to_numeric(frame["close"], errors="coerce"), index=frame.index
            ).dropna()
        except (OSError, RuntimeError, ValueError):
            continue
        if len(closes) < LEADER_LOOKBACK + 1:
            continue
        observed_date = _normalized_timestamp(str(closes.index[-1]))
        if (boundary - observed_date).days > MAX_EVIDENCE_STALENESS_DAYS:
            continue
        observed += 1
        momentum = float(closes.iloc[-1] / closes.iloc[-LEADER_LOOKBACK - 1] - 1.0)
        if math.isfinite(momentum) and momentum > 0:
            observations.append((momentum, code))
    leaders = sorted(observations, key=lambda item: (-item[0], item[1]))[:maximum]
    return LeaderSelection(
        as_of=str(_timestamp(as_of).date()),
        requested_symbols=normalized,
        observed_symbols=observed,
        selected_symbols=tuple(code for _, code in leaders),
        selected_returns=tuple(momentum for momentum, _ in leaders),
    )


class PositiveMomentumHoldStrategy(qf.BaseStrategy):
    """Enter once, then protect gains with a profit-activated ATR chandelier."""

    name = "positive_momentum_hold"

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self._has_entered = False

    def on_bar(self, ctx: qf.BarContext) -> qf.Signal | None:
        close = float(ctx.df["close"].iloc[ctx.i])
        if not math.isfinite(close) or close <= 0:
            return None
        if self.position is None:
            if self._has_entered:
                return None
            shares = qf._floor_to_lot(
                ctx.current_assets * float(self.cfg["strategy_weight"]) / close
            )
            if shares <= 0:
                return None
            atr = ctx.indicators.get("atr")
            atr_value = float(atr.iloc[ctx.i]) if atr is not None else float("nan")
            hard_stop = close * (1.0 - WEAK_HARD_STOP)
            atr_stop = (
                close - WEAK_ENTRY_ATR_MULTIPLIER * atr_value
                if math.isfinite(atr_value) and atr_value > 0
                else hard_stop
            )
            return self._make_buy_signal(
                ctx,
                shares,
                stop_loss=max(hard_stop, atr_stop),
                reason="causal positive-240-session leader entry with disaster stop",
            )

        self._has_entered = True
        position = self.position
        position.highest_close_since_entry = max(
            position.highest_close_since_entry, close
        )
        if position.stop_loss > 0 and close <= position.stop_loss:
            return self._make_sell_signal(ctx, "weak-regime disaster stop")
        try:
            entry_timestamp = pd.Timestamp(position.entry_date)
            if pd.isna(entry_timestamp):
                raise ValueError("entry_date must resolve to a valid timestamp")
            entry_index = int(
                cast(Any, ctx.df.index).searchsorted(entry_timestamp)
            )
            held_days = max(ctx.i - entry_index, 0)
        except (TypeError, ValueError):
            held_days = 0
        return_since_entry = close / position.entry_price - 1.0
        if (
            held_days >= WEAK_TIME_STOP_DAYS
            and return_since_entry <= WEAK_TIME_STOP_RETURN
        ):
            return self._make_sell_signal(ctx, "weak-regime time stop")
        peak_gain = position.highest_close_since_entry / position.entry_price - 1.0
        if peak_gain < PROFIT_ACTIVATION:
            return None
        atr = ctx.indicators.get("atr")
        atr_value = float(atr.iloc[ctx.i]) if atr is not None else float("nan")
        if not math.isfinite(atr_value) or atr_value <= 0:
            return None
        stop = position.highest_close_since_entry - TRAILING_ATR_MULTIPLIER * atr_value
        position.stop_loss = max(position.stop_loss, stop)
        if close > position.stop_loss:
            return None
        return self._make_sell_signal(
            ctx,
            f"profit-activated {TRAILING_ATR_MULTIPLIER:g}-ATR chandelier",
        )


class CashPreservationStrategy(qf.BaseStrategy):
    """Deliberately emit no orders when causal evidence is insufficient."""

    name = "cash_preservation"

    def on_bar(self, ctx: qf.BarContext) -> None:
        del ctx
        return None


def _weak_regime_policy() -> qf.PortfolioPolicy:
    """Disable unrelated liquidations while retaining the execution contract."""
    return qf.PortfolioPolicy(
        allocation_mode="single",
        drawdown_alert=0.95,
        confirmed_drawdown=0.97,
        emergency_drawdown=0.98,
        terminal_drawdown=0.99,
        concentration_drawdown_adjustment=0.01,
        candidate_reference_percentile=0.0,
        market_regime_enabled=False,
    )


def _weak_regime_config(symbol_count: int) -> dict[str, Any]:
    slots = max(1, symbol_count)
    target_weight = min(MAX_SYMBOL_WEIGHT, 1.0 / slots)
    return {
        "strategy_weight": target_weight,
        "max_symbol_weight": target_weight,
        "max_total_weight": 1.0,
        "max_positions": slots,
        "max_units": 1,
        "group_min_slots": 0,
        "daily_loss_limit": 0.99,
        "sector_guard_enabled": False,
        "market_regime_enabled": False,
        "fusion_single_scale": 1.0,
        "combined_group_weight_limits": {
            "overseas_compute": 1.0,
            "domestic_semiconductor": 1.0,
        },
    }


class RegimeAdaptiveBacktestEngine:
    """Preserve the trend engine and route weak deployments causally."""

    def __init__(
        self,
        initial_capital: float = 2_000_000,
        cfg: dict | None = None,
        policy: qf.PortfolioPolicy | None = None,
    ) -> None:
        self.initial_capital = qf._require_finite(
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
        """Drop symbols with no causal observation in the requested window."""
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
    ) -> DeploymentDecision:
        """Make a point-in-time route decision from data through ``as_of``.

        This is the live/manual-decision entry point. It deliberately does
        not rewrite a historical backtest path with information learned
        later; callers keep historical performance and current routing as
        separate artifacts.
        """
        boundary = _normalized_timestamp(as_of)
        next_day = boundary + pd.Timedelta(days=1)
        return self.decide(
            symbols_dict,
            start_date=str(next_day.date()),
            data_dir=data_dir,
            leader_data_dir=leader_data_dir,
            selection_boundary=str(boundary.date()),
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
        boundary = self._boundary(start_date, selection_boundary)
        regime = detect_regime(data_dir, as_of=boundary)
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
        account_state: qf.AccountState | None = None,
        selection_boundary: str | None = None,
        deployment_mode: str = "auto",
        regime_data_dir: str | None = None,
        leader_data_dir: str | None = None,
    ) -> dict:
        """Run a trend-preserving or weak-regime deployment with one schema."""
        mode = str(deployment_mode).lower()
        if mode not in {"auto", "trend", "weak"}:
            raise ValueError("deployment_mode must be auto, trend, or weak")
        evidence_dir = regime_data_dir or data_dir
        if evidence_dir is None:
            raise ValueError(
                "regime-adaptive mode requires a local data_dir or regime_data_dir"
            )
        decision = self.decide(
            symbols_dict,
            start_date=start_date,
            data_dir=evidence_dir,
            leader_data_dir=leader_data_dir,
            selection_boundary=selection_boundary,
        )
        if mode == "trend":
            decision = replace(decision, name="frozen_trend_engine", reason="trend mode forced by caller")
        elif mode == "weak" and decision.name == "frozen_trend_engine":
            leaders = select_positive_momentum_leaders(
                tuple(symbols_dict),
                data_dir=leader_data_dir or evidence_dir,
                as_of=decision.boundary,
            )
            decision = replace(
                decision,
                name=("positive_momentum_hold" if leaders.selected_symbols else "cash_preservation"),
                reason="weak mode forced by caller",
                leaders=leaders,
            )
        self.last_decision = decision
        executed_symbols: tuple[str, ...] = ()
        result: dict[str, Any] | None = None

        if decision.name == "frozen_trend_engine":
            tradable_symbols = self._available_local_symbols(
                symbols_dict,
                data_dir=data_dir,
                start_date=start_date,
                end_date=end_date,
                warmup_calendar_days=warmup_calendar_days,
            )
            if not tradable_symbols:
                leaders = LeaderSelection(
                    as_of=decision.boundary,
                    requested_symbols=tuple(sorted(symbols_dict)),
                    observed_symbols=0,
                    selected_symbols=(),
                    selected_returns=(),
                )
                decision = replace(
                    decision,
                    name="cash_preservation",
                    reason="trend was confirmed but no requested symbol had observable data",
                    leaders=leaders,
                )
                self.last_decision = decision
            else:
                executed_symbols = tuple(sorted(tradable_symbols))
                self.delegate = qf.BacktestEngine(
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
                    allocation_mode=allocation_mode,
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
            executed_symbols = tuple(selected)
            run_symbols = (
                {code: symbols_dict[code] for code in selected}
                if selected
                else {
                    code: code for code in qf.PortfolioPolicy().regime_symbols
                }
            )
            self.delegate = qf.SleeveBacktestEngine(
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
        result["unavailable_symbols"] = sorted(set(symbols_dict) - set(executed_symbols))
        return result
