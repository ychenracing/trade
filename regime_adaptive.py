"""Causal regime routing layered over the frozen Quant Fusion engine.

The production trend engine remains untouched. This module selects between
that engine and a low-turnover weak-regime policy using information available
strictly before the requested deployment period.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Sequence, cast

import numpy as np
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

# The weak route retains its low-turnover stock-level protection, but no longer
# disables portfolio risk almost completely. These thresholds leave validated
# sub-20% paths unchanged while forcing correlated weak-market books to de-risk
# after a confirmed portfolio drawdown.
WEAK_DRAWDOWN_ALERT = 0.15
WEAK_CONFIRMED_DRAWDOWN = 0.20
WEAK_EMERGENCY_DRAWDOWN = 0.23
WEAK_TERMINAL_DRAWDOWN = 0.26
WEAK_DAILY_LOSS_LIMIT = 0.12


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
    unavailable_symbols: tuple[str, ...] = ()


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
    """Return a normalized finite timestamp."""
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
    """Require both fixed indices to have fresh, complete trend evidence."""
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
        if not all(
            math.isfinite(value) and value > 0 for value in (close, ma20, ma60)
        ):
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
    """Select positive long-horizon leaders with deterministic tie-breaking.

    Uses multi-factor weak-market scoring (section 12.2):
    - 240-day momentum (25%)
    - 120-day relative strength vs reference basket (25%)
    - 60-day momentum (20%)
    - Drawdown resilience (15%)
    - Trend repair: 5-day vs 20-day momentum (15%)
    """
    normalized = tuple(sorted(str(symbol) for symbol in symbols))
    if not normalized or len(normalized) != len(set(normalized)):
        raise ValueError("symbols must be a non-empty set without duplicates")
    if maximum < 1:
        raise ValueError("maximum must be positive")
    boundary = _normalized_timestamp(as_of)
    observations: list[tuple[float, str]] = []
    observed_codes: set[str] = set()

    # Load reference basket for relative strength calculation
    reference_symbols = ("300308", "300502", "300394", "688008", "603986")
    ref_returns: list[float] = []
    for ref_code in reference_symbols:
        try:
            ref_frame = _local_frame(data_dir, ref_code, str(boundary.date()))
            ref_closes = pd.Series(
                pd.to_numeric(ref_frame["close"], errors="coerce"),
                index=ref_frame.index,
            ).dropna()
            if len(ref_closes) >= 121:
                ref_ret = float(ref_closes.iloc[-1] / ref_closes.iloc[-121] - 1.0)
                ref_returns.append(ref_ret)
        except (OSError, RuntimeError, ValueError):
            continue
    ref_avg_return = float(np.mean(ref_returns)) if ref_returns else 0.0

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
        observed_codes.add(code)
        momentum_240 = float(closes.iloc[-1] / closes.iloc[-LEADER_LOOKBACK - 1] - 1.0)
        if not (math.isfinite(momentum_240) and momentum_240 > 0):
            continue

        # Multi-factor weak-market scoring (section 12.2)
        # 60-day momentum
        if len(closes) >= 61:
            momentum_60 = float(closes.iloc[-1] / closes.iloc[-61] - 1.0)
        else:
            momentum_60 = 0.0

        # Relative strength vs reference basket (120-day): compare the
        # symbol's own 120-day return against the basket's 120-day return.
        if len(closes) >= 121:
            symbol_120 = float(closes.iloc[-1] / closes.iloc[-121] - 1.0)
            rs_120 = symbol_120 - ref_avg_return if ref_avg_return != 0 else 0.0
        else:
            rs_120 = 0.0

        # Drawdown resilience: how far from 60-day peak (lower is better)
        if len(closes) >= 60:
            peak_60 = float(closes.iloc[-60:].max())
            drawdown_from_peak = 1.0 - float(closes.iloc[-1] / peak_60) if peak_60 > 0 else 0.0
            resilience = 1.0 - min(1.0, drawdown_from_peak)
        else:
            resilience = 0.0

        # Trend repair: 5-day vs 20-day momentum
        if len(closes) >= 21:
            mom_5 = float(closes.iloc[-1] / closes.iloc[-6] - 1.0)
            mom_20 = float(closes.iloc[-1] / closes.iloc[-21] - 1.0)
            trend_repair = mom_5 - mom_20
        else:
            trend_repair = 0.0

        # Composite weak-market score
        weak_score = (
            0.25 * momentum_240
            + 0.25 * max(0.0, rs_120)
            + 0.20 * momentum_60
            + 0.15 * resilience
            + 0.15 * max(0.0, trend_repair)
        )
        if math.isfinite(weak_score):
            observations.append((weak_score, code))
    leaders = sorted(observations, key=lambda item: (-item[0], item[1]))[:maximum]
    return LeaderSelection(
        as_of=str(_timestamp(as_of).date()),
        requested_symbols=normalized,
        observed_symbols=len(observed_codes),
        selected_symbols=tuple(code for _, code in leaders),
        selected_returns=tuple(score for score, _ in leaders),
        unavailable_symbols=tuple(sorted(set(normalized) - observed_codes)),
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
            entry_index = int(cast(Any, ctx.df.index).searchsorted(entry_timestamp))
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
    """Apply independent weak-market portfolio drawdown protection."""
    return qf.PortfolioPolicy(
        allocation_mode="single",
        drawdown_alert=WEAK_DRAWDOWN_ALERT,
        confirmed_drawdown=WEAK_CONFIRMED_DRAWDOWN,
        emergency_drawdown=WEAK_EMERGENCY_DRAWDOWN,
        terminal_drawdown=WEAK_TERMINAL_DRAWDOWN,
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
        "daily_loss_limit": WEAK_DAILY_LOSS_LIMIT,
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
    ) -> DeploymentDecision:
        """Make a point-in-time route decision from data through ``as_of``."""
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
        account_state: qf.AccountState | None = None,
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
        if mode not in {"auto", "trend", "weak"}:
            raise ValueError("deployment_mode must be auto, trend, or weak")
        if not isinstance(allow_unavailable_symbols, bool):
            raise ValueError("allow_unavailable_symbols must be bool")
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
                # Trend route keeps the default three-sleeve ensemble
                # (3x full-capital deployment) to maximize bull-market returns.
                effective_allocation = allocation_mode or "ensemble"
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
                else {code: code for code in qf.PortfolioPolicy().regime_symbols}
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
        result["unavailable_symbols"] = list(unavailable_symbols)
        return result
