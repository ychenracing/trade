"""Research-only low-churn candidate-universe promotion study."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from typing import Any

import pandas as pd

from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.config.regime import MAX_EVIDENCE_STALENESS_DAYS
from quantfusion.config.research_universes import get_universe_pool
from quantfusion.config.universe import ESTABLISHED_BASE_CORE, ORDERED_SYMBOLS
from quantfusion.data.providers import DataFetcher
from quantfusion.regime.evidence import select_positive_momentum_leaders

PROMOTION_STREAK = 2
DEMOTION_MISS_STREAK = 2
EVALUATION_HORIZONS = (20, 60)
MAX_PROMOTIONS_PER_YEAR = 4.0


@dataclass(frozen=True, slots=True)
class CandidateState:
    active_symbol: str | None = None
    last_selected_symbol: str | None = None
    selection_streak: int = 0
    miss_streak: int = 0


def watch_symbols() -> tuple[str, ...]:
    """Return curated research symbols outside the immutable production core."""
    core = set(ORDERED_SYMBOLS)
    return tuple(code for code in get_universe_pool("pool_g").symbols if code not in core)


def advance_candidate_state(
    state: CandidateState,
    *,
    selected_symbol: str | None,
    decision_date: str,
    effective_date: str,
) -> tuple[CandidateState, tuple[dict[str, Any], ...]]:
    """Advance one monthly decision without changing account state."""
    if selected_symbol is not None and selected_symbol == state.last_selected_symbol:
        selection_streak = state.selection_streak + 1
    else:
        selection_streak = 1 if selected_symbol is not None else 0

    active = state.active_symbol
    miss_streak = state.miss_streak
    events: list[dict[str, Any]] = []

    if active is None:
        miss_streak = 0
        if selected_symbol is not None and selection_streak >= PROMOTION_STREAK:
            active = selected_symbol
            events.append(
                {
                    "event": "promote",
                    "symbol": selected_symbol,
                    "decision_date": decision_date,
                    "effective_date": effective_date,
                    "selection_streak": selection_streak,
                }
            )
    elif selected_symbol == active:
        miss_streak = 0
    else:
        miss_streak += 1
        if miss_streak >= DEMOTION_MISS_STREAK:
            events.append(
                {
                    "event": "demote",
                    "symbol": active,
                    "decision_date": decision_date,
                    "effective_date": effective_date,
                    "miss_streak": miss_streak,
                }
            )
            active = None
            miss_streak = 0
            if selected_symbol is not None and selection_streak >= PROMOTION_STREAK:
                active = selected_symbol
                events.append(
                    {
                        "event": "promote",
                        "symbol": selected_symbol,
                        "decision_date": decision_date,
                        "effective_date": effective_date,
                        "selection_streak": selection_streak,
                    }
                )

    return (
        CandidateState(
            active_symbol=active,
            last_selected_symbol=selected_symbol,
            selection_streak=selection_streak,
            miss_streak=miss_streak,
        ),
        tuple(events),
    )


def _load_frames(
    data_dir: Path,
    *,
    start_date: str,
    end_date: str,
) -> dict[str, pd.DataFrame]:
    pool_g = get_universe_pool("pool_g").symbols
    required = tuple(dict.fromkeys((*pool_g, *ESTABLISHED_BASE_CORE)))
    warm_start = str((pd.Timestamp(start_date) - pd.Timedelta(days=365)).date())
    frames: dict[str, pd.DataFrame] = {}
    for code in required:
        frames[code] = DataFetcher.load_stock_data(
            code,
            warm_start,
            end_date,
            data_dir=str(data_dir),
        )
    return frames


def monthly_decision_dates(
    calendar: pd.DatetimeIndex,
    *,
    start_date: str,
    end_date: str,
) -> tuple[pd.Timestamp, ...]:
    """Return the final observed trading session of each calendar month."""
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    dates = calendar[(calendar >= start) & (calendar <= end)]
    if dates.empty:
        return ()
    grouped = pd.Series(dates, index=dates).groupby(dates.to_period("M")).max()
    return tuple(pd.Timestamp(value) for value in grouped.tolist())


def _next_session(calendar: pd.DatetimeIndex, decision_date: pd.Timestamp) -> pd.Timestamp | None:
    later = calendar[calendar > decision_date]
    return pd.Timestamp(later[0]) if len(later) else None


def _eligible_watch_symbols(
    frames: dict[str, pd.DataFrame],
    *,
    decision_date: pd.Timestamp,
) -> tuple[str, ...]:
    """Use only causal data with at least one board lot of prior-ADV capacity."""
    ratio = float(PortfolioPolicy().max_order_adv_ratio)
    eligible: list[str] = []
    for code in watch_symbols():
        frame = frames[code].loc[frames[code].index <= decision_date]
        if len(frame) < 61 or "volume" not in frame.columns:
            continue
        last_date = pd.Timestamp(frame.index[-1])
        if (decision_date - last_date).days > MAX_EVIDENCE_STALENESS_DAYS:
            continue
        volumes = pd.to_numeric(frame["volume"], errors="coerce").dropna().tail(20)
        if len(volumes) < 20 or (volumes <= 0).any():
            continue
        prior_adv = float(volumes.mean())
        if prior_adv * ratio < 100.0:
            continue
        eligible.append(code)
    return tuple(eligible)


def _select_candidate(
    frames: dict[str, pd.DataFrame],
    *,
    decision_date: pd.Timestamp,
) -> str | None:
    eligible = _eligible_watch_symbols(frames, decision_date=decision_date)
    if not eligible:
        return None

    def load(code: str, boundary: str) -> pd.DataFrame:
        del boundary
        return frames[code]

    selection = select_positive_momentum_leaders(
        eligible,
        data_dir="unused",
        as_of=str(decision_date.date()),
        maximum=1,
        frame_loader=load,
    )
    selection.require_ready("dynamic candidate-universe research")
    return selection.selected_symbols[0] if selection.selected_symbols else None


def _forward_return(
    frame: pd.DataFrame,
    *,
    decision_date: pd.Timestamp,
    horizon: int,
) -> float | None:
    future = frame.loc[frame.index > decision_date]
    if len(future) < horizon:
        return None
    entry = float(future.iloc[0]["open"])
    exit_price = float(future.iloc[horizon - 1]["close"])
    if entry <= 0 or exit_price <= 0:
        return None
    return exit_price / entry - 1.0


def _benchmark_return(
    frames: dict[str, pd.DataFrame],
    *,
    decision_date: pd.Timestamp,
    horizon: int,
) -> float | None:
    values = [
        value
        for code in ESTABLISHED_BASE_CORE
        if (
            value := _forward_return(
                frames[code],
                decision_date=decision_date,
                horizon=horizon,
            )
        )
        is not None
    ]
    return float(median(values)) if values else None


def _evaluate_promotions(
    events: list[dict[str, Any]],
    frames: dict[str, pd.DataFrame],
) -> list[dict[str, Any]]:
    evaluated: list[dict[str, Any]] = []
    for event in events:
        if event["event"] != "promote":
            continue
        decision = pd.Timestamp(event["decision_date"])
        row = dict(event)
        for horizon in EVALUATION_HORIZONS:
            candidate = _forward_return(
                frames[str(event["symbol"])],
                decision_date=decision,
                horizon=horizon,
            )
            benchmark = _benchmark_return(
                frames,
                decision_date=decision,
                horizon=horizon,
            )
            row[f"forward_return_{horizon}d"] = candidate
            row[f"core_median_return_{horizon}d"] = benchmark
            row[f"excess_return_{horizon}d"] = (
                candidate - benchmark
                if candidate is not None and benchmark is not None
                else None
            )
        evaluated.append(row)
    return evaluated


def _window_metrics(
    promotions: list[dict[str, Any]],
    *,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    rows = [
        row
        for row in promotions
        if start <= pd.Timestamp(row["decision_date"]) <= end
    ]
    metrics: dict[str, Any] = {
        "start_date": start_date,
        "end_date": end_date,
        "promotion_count": len(rows),
    }
    for horizon in EVALUATION_HORIZONS:
        excess = [
            float(row[f"excess_return_{horizon}d"])
            for row in rows
            if row[f"excess_return_{horizon}d"] is not None
        ]
        metrics[f"complete_{horizon}d_count"] = len(excess)
        metrics[f"median_excess_{horizon}d"] = (
            float(median(excess)) if excess else None
        )
        metrics[f"positive_excess_hit_rate_{horizon}d"] = (
            sum(value > 0 for value in excess) / len(excess) if excess else None
        )
    return metrics


def _research_verdict(
    *,
    promotions: list[dict[str, Any]],
    windows: dict[str, dict[str, Any]],
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    years = max((pd.Timestamp(end_date) - pd.Timestamp(start_date)).days / 365.25, 1.0)
    promotions_per_year = len(promotions) / years
    full = windows["full"]
    early = windows["early"]
    late = windows["late"]
    checks = {
        "at_least_four_complete_60d_promotions": full["complete_60d_count"] >= 4,
        "both_time_splits_have_two_complete_60d_promotions": (
            early["complete_60d_count"] >= 2 and late["complete_60d_count"] >= 2
        ),
        "full_median_excess_positive_20d": (
            full["median_excess_20d"] is not None and full["median_excess_20d"] > 0
        ),
        "full_median_excess_positive_60d": (
            full["median_excess_60d"] is not None and full["median_excess_60d"] > 0
        ),
        "early_median_excess_positive_20d": (
            early["median_excess_20d"] is not None and early["median_excess_20d"] > 0
        ),
        "late_median_excess_positive_20d": (
            late["median_excess_20d"] is not None and late["median_excess_20d"] > 0
        ),
        "full_hit_rate_at_least_half_20d": (
            full["positive_excess_hit_rate_20d"] is not None
            and full["positive_excess_hit_rate_20d"] >= 0.5
        ),
        "full_hit_rate_at_least_half_60d": (
            full["positive_excess_hit_rate_60d"] is not None
            and full["positive_excess_hit_rate_60d"] >= 0.5
        ),
        "promotion_frequency_is_low": promotions_per_year <= MAX_PROMOTIONS_PER_YEAR,
    }
    return {
        "status": (
            "evidence_supports_production_replay_research"
            if all(checks.values())
            else "reject_production_promotion"
        ),
        "checks": checks,
        "promotions_per_year": promotions_per_year,
    }


def run_dynamic_universe_research(
    data_dir: str | Path,
    *,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    """Run causal monthly promotion research; future returns are evaluation-only."""
    root = Path(data_dir)
    frames = _load_frames(root, start_date=start_date, end_date=end_date)
    calendar = pd.DatetimeIndex(frames[ORDERED_SYMBOLS[0]].index)
    decisions = monthly_decision_dates(
        calendar,
        start_date=start_date,
        end_date=end_date,
    )
    state = CandidateState()
    events: list[dict[str, Any]] = []
    decisions_out: list[dict[str, Any]] = []
    for decision in decisions:
        effective = _next_session(calendar, decision)
        if effective is None:
            break
        selected = _select_candidate(frames, decision_date=decision)
        state, emitted = advance_candidate_state(
            state,
            selected_symbol=selected,
            decision_date=str(decision.date()),
            effective_date=str(effective.date()),
        )
        events.extend(emitted)
        decisions_out.append(
            {
                "decision_date": str(decision.date()),
                "effective_date": str(effective.date()),
                "selected_symbol": selected,
                "active_symbol": state.active_symbol,
                "selection_streak": state.selection_streak,
                "miss_streak": state.miss_streak,
            }
        )

    promotions = _evaluate_promotions(events, frames)
    split = pd.Timestamp("2024-12-31")
    windows = {
        "full": _window_metrics(
            promotions,
            start_date=start_date,
            end_date=end_date,
        ),
        "early": _window_metrics(
            promotions,
            start_date=start_date,
            end_date=str(split.date()),
        ),
        "late": _window_metrics(
            promotions,
            start_date="2025-01-01",
            end_date=end_date,
        ),
    }
    verdict = _research_verdict(
        promotions=promotions,
        windows=windows,
        start_date=start_date,
        end_date=end_date,
    )
    return {
        "kind": "dynamic_candidate_universe_research",
        "scope": "research_only",
        "core_universe": list(ORDERED_SYMBOLS),
        "benchmark_core": sorted(ESTABLISHED_BASE_CORE),
        "watch_universe": list(watch_symbols()),
        "rules": {
            "decision_frequency": "monthly_last_observed_session",
            "execution_boundary": "promotion effective no earlier than next observed session",
            "promotion_streak": PROMOTION_STREAK,
            "demotion_miss_streak": DEMOTION_MISS_STREAK,
            "maximum_active_candidates": 1,
            "liquidity_contract": (
                "prior 20-session ADV times existing max_order_adv_ratio "
                "must support at least one 100-share lot"
            ),
            "candidate_selector": (
                "existing causal select_positive_momentum_leaders over Watch only"
            ),
            "future_returns": "post-hoc evaluation only; never used for selection",
        },
        "decision_count": len(decisions_out),
        "events": events,
        "promotions": promotions,
        "windows": windows,
        "verdict": verdict,
        "final_state": asdict(state),
    }


__all__ = [
    "CandidateState",
    "advance_candidate_state",
    "monthly_decision_dates",
    "run_dynamic_universe_research",
    "watch_symbols",
]
