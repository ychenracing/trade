#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Leakage-resistant parameter search for :mod:`quant_fusion`.

The optimizer is deliberately a separate research layer.  It never reimplements
signals, fills, T+1 handling, transaction costs, or portfolio accounting.  Every
candidate is executed by ``quant_fusion.BacktestEngine``.

Selection uses expanding walk-forward training/validation folds. A final holdout
is executed only after parameter selection and acts as a one-time promotion gate;
it cannot be used to choose another parameter candidate. Historical optimization
is not a guarantee of future performance.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import itertools
import json
import math
import random
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol, cast

import pandas as pd

import quant_fusion as qf


MAX_SYMBOL_WEIGHT = 0.60
MAX_TOTAL_WEIGHT = 1.00
MAX_POSITIONS = 6
DEFAULT_DRAWDOWN_LIMIT = 0.20
INTEGER_SYMBOL_PARAMETERS = {
    "entry_period",
    "exit_period",
    "adx_period",
    "atr_period",
    "rsi_period",
    "ma_short",
    "ma_long",
    "max_units",
    "reversal_exit_period",
}
POLICY_RISK_KEYS = (
    "drawdown_alert",
    "confirmed_drawdown",
    "emergency_drawdown",
    "terminal_drawdown",
    "concentration_drawdown_adjustment",
)
POLICY_RISK_PROFILES: dict[str, dict[str, float]] = {
    "baseline": {},
    "moderate": {
        "drawdown_alert": 0.14,
        "confirmed_drawdown": 0.18,
        "emergency_drawdown": 0.22,
        "terminal_drawdown": 0.24,
        "concentration_drawdown_adjustment": 0.015,
    },
    "defensive": {
        "drawdown_alert": 0.10,
        "confirmed_drawdown": 0.14,
        "emergency_drawdown": 0.18,
        "terminal_drawdown": 0.20,
        "concentration_drawdown_adjustment": 0.010,
    },
}


def _canonical_json(value: Any) -> str:
    """Return deterministic compact JSON for IDs and audit comparisons."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _timestamp(value: Any) -> pd.Timestamp:
    """Parse one scalar date and narrow pandas' Timestamp-or-NaT typing."""
    parsed = pd.Timestamp(value)
    if parsed is pd.NaT:
        raise ValueError(f"Invalid date: {value!r}")
    return cast(pd.Timestamp, parsed)


def _date_string(value: Any) -> str:
    """Return a normalized ISO date for artifacts and engine calls."""
    return _timestamp(value).strftime("%Y-%m-%d")


def _candidate_id(payload: dict[str, Any]) -> str:
    """Return a stable short ID, with a human-readable name for the baseline."""
    if not any(payload.values()):
        return "baseline"
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return f"candidate-{digest[:12]}"


@dataclass(frozen=True)
class DateWindow:
    """One inclusive, immutable backtest window."""

    name: str
    start: str
    end: str
    role: str

    def __post_init__(self) -> None:
        start = _timestamp(self.start)
        end = _timestamp(self.end)
        if start > end:
            raise ValueError(
                f"Invalid window {self.name}: {self.start} through {self.end}"
            )
        if self.role not in {"train", "validation", "test"}:
            raise ValueError("Window role must be train, validation, or test")


@dataclass(frozen=True)
class WalkForwardFold:
    """Pair one expanding training window with its later validation window."""

    name: str
    train: DateWindow
    validation: DateWindow

    def __post_init__(self) -> None:
        if self.train.role != "train" or self.validation.role != "validation":
            raise ValueError("A fold must contain train and validation roles")
        if _timestamp(self.train.end) >= _timestamp(self.validation.start):
            raise ValueError("Training must end before validation starts")


@dataclass(frozen=True)
class Candidate:
    """A compact set of portfolio, policy, and route-preserving modifiers."""

    engine_overrides: dict[str, Any] = field(default_factory=dict)
    symbol_multipliers: dict[str, float] = field(default_factory=dict)
    policy_overrides: dict[str, Any] = field(default_factory=dict)
    candidate_id: str = field(init=False)

    def __post_init__(self) -> None:
        engine = dict(self.engine_overrides)
        multipliers = {
            key: float(value) for key, value in self.symbol_multipliers.items()
        }
        policy = dict(self.policy_overrides)
        object.__setattr__(self, "engine_overrides", engine)
        object.__setattr__(self, "symbol_multipliers", multipliers)
        object.__setattr__(self, "policy_overrides", policy)
        payload = {
            "engine_overrides": engine,
            "symbol_multipliers": multipliers,
            "policy_overrides": policy,
        }
        object.__setattr__(self, "candidate_id", _candidate_id(payload))
        self._validate_boundaries()

    @classmethod
    def baseline(cls) -> Candidate:
        """Return the untouched qf.1 production defaults."""
        return cls()

    def _validate_boundaries(self) -> None:
        """Reject candidates that could violate the user's portfolio contract."""
        raw_maximum = self.engine_overrides.get("max_positions", MAX_POSITIONS)
        if isinstance(raw_maximum, bool) or not isinstance(raw_maximum, int):
            raise ValueError("max_positions must be an integer")
        maximum = raw_maximum
        if not 1 <= maximum <= MAX_POSITIONS:
            raise ValueError(f"max_positions must be between 1 and {MAX_POSITIONS}")
        symbol_weight = float(
            self.engine_overrides.get("max_symbol_weight", MAX_SYMBOL_WEIGHT)
        )
        total_weight = float(
            self.engine_overrides.get("max_total_weight", MAX_TOTAL_WEIGHT)
        )
        if not 0 < symbol_weight <= MAX_SYMBOL_WEIGHT:
            raise ValueError("max_symbol_weight must be in (0, 0.60]")
        if not 0 < total_weight <= MAX_TOTAL_WEIGHT:
            raise ValueError("max_total_weight must be in (0, 1.00]")
        if symbol_weight > total_weight:
            raise ValueError("max_symbol_weight must not exceed max_total_weight")
        scalable = qf.BacktestEngine._PER_SYMBOL_OVERRIDE_KEYS - {
            "atr_method",
            "reversal_turtle_enabled",
            "reversal_dual_ma_enabled",
            "reversal_atr_channel_enabled",
        }
        for name, multiplier in self.symbol_multipliers.items():
            if name not in scalable:
                raise ValueError(f"Unsupported per-symbol multiplier: {name}")
            if not math.isfinite(multiplier) or multiplier <= 0:
                raise ValueError(f"Multiplier {name} must be finite and positive")
        # Constructors invoke the execution engine's own config and policy checks.
        policy = qf.PortfolioPolicy(**self.policy_overrides)
        qf.BacktestEngine(cfg=self.engine_config(), policy=policy)

    def engine_config(self, *, stress: bool = False) -> dict[str, Any]:
        """Return hard-capped engine settings, optionally with stressed costs."""
        cfg = {
            **self.engine_overrides,
            "max_positions": min(
                int(self.engine_overrides.get("max_positions", MAX_POSITIONS)),
                MAX_POSITIONS,
            ),
            "max_symbol_weight": min(
                float(
                    self.engine_overrides.get("max_symbol_weight", MAX_SYMBOL_WEIGHT)
                ),
                MAX_SYMBOL_WEIGHT,
            ),
            "max_total_weight": min(
                float(self.engine_overrides.get("max_total_weight", MAX_TOTAL_WEIGHT)),
                MAX_TOTAL_WEIGHT,
            ),
        }
        if stress:
            defaults = qf.BacktestEngine._default_config()
            cfg["slippage"] = max(
                float(cfg.get("slippage", defaults["slippage"])), 0.002
            )
            cfg["commission_rate"] = max(
                float(cfg.get("commission_rate", defaults["commission_rate"])) * 1.5,
                defaults["commission_rate"],
            )
        return cfg

    def policy(self) -> qf.PortfolioPolicy:
        """Build the validated portfolio policy used by this candidate."""
        return qf.PortfolioPolicy(**self.policy_overrides)

    def per_symbol_config(self, symbols: dict[str, str]) -> dict[str, dict[str, Any]]:
        """Scale each symbol's routed profile without erasing industry differences."""
        result: dict[str, dict[str, Any]] = {}
        for code, name in symbols.items():
            base = qf.BacktestEngine.config_for_symbol(code, name=name)
            overrides: dict[str, Any] = {
                "max_symbol_weight": min(
                    float(base["max_symbol_weight"]), MAX_SYMBOL_WEIGHT
                )
            }
            for parameter, multiplier in self.symbol_multipliers.items():
                raw = float(base[parameter]) * multiplier
                value: Any = (
                    max(1, int(round(raw)))
                    if parameter in INTEGER_SYMBOL_PARAMETERS
                    else raw
                )
                overrides[parameter] = value
            merged = qf.BacktestEngine._validate_config({**base, **overrides})
            if int(merged["exit_period"]) >= int(merged["entry_period"]):
                raise ValueError(
                    f"{self.candidate_id} makes exit_period >= entry_period for {code}"
                )
            if int(merged["ma_short"]) >= int(merged["ma_long"]):
                raise ValueError(
                    f"{self.candidate_id} makes ma_short >= ma_long for {code}"
                )
            result[code] = overrides
        return result

    def as_dict(self) -> dict[str, Any]:
        """Return the serializable optimizer configuration."""
        return {
            "candidate_id": self.candidate_id,
            "engine_overrides": dict(self.engine_overrides),
            "symbol_multipliers": dict(self.symbol_multipliers),
            "policy_overrides": dict(self.policy_overrides),
        }


@dataclass(frozen=True)
class ParameterSpace:
    """Finite search domains sampled without using any backtest result."""

    engine: dict[str, tuple[Any, ...]]
    symbol_multipliers: dict[str, tuple[float, ...]]
    policy: dict[str, tuple[Any, ...]]

    @classmethod
    def default(cls) -> ParameterSpace:
        """Return a conservative route-preserving search space for technology stocks."""
        return cls(
            engine={
                "max_positions": (3, 4, 5, 6),
                "momentum_lookback": (5, 10, 20),
            },
            symbol_multipliers={
                "entry_period": (0.85, 1.0, 1.15),
                "exit_period": (0.85, 1.0, 1.15),
                "trail_atr_mult": (0.85, 1.0, 1.15),
                "risk_pct": (0.80, 1.0, 1.20),
                "hard_stop": (0.85, 1.0, 1.15),
            },
            policy={
                "candidate_reference_percentile": (0.40, 0.50, 0.60),
                "rearm_trading_days": (7, 10, 15),
                "risk_profile": ("baseline", "moderate", "defensive"),
            },
        )

    @classmethod
    def from_json(cls, path: str | Path) -> ParameterSpace:
        """Load explicit finite domains from a JSON file."""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))

        def domains(section: str) -> dict[str, tuple[Any, ...]]:
            raw = payload.get(section, {})
            if not isinstance(raw, dict):
                raise ValueError(f"Search-space section {section} must be an object")
            normalized: dict[str, tuple[Any, ...]] = {}
            for key, values in raw.items():
                if not isinstance(values, list) or not values:
                    raise ValueError(f"Search domain {section}.{key} must be non-empty")
                normalized[str(key)] = tuple(values)
            return normalized

        return cls(
            engine=domains("engine"),
            symbol_multipliers=domains("symbol_multipliers"),
            policy=domains("policy"),
        )

    def candidates(self, maximum: int, seed: int) -> list[Candidate]:
        """Build supported one-factor variants, then deterministic pair interactions."""
        if maximum < 1:
            raise ValueError("maximum candidates must be positive")
        factors: list[tuple[str, str, tuple[Any, ...]]] = []
        for section, values in (
            ("engine", self.engine),
            ("symbol", self.symbol_multipliers),
            ("policy", self.policy),
        ):
            for key in sorted(values):
                domain = tuple(values[key])
                if not domain:
                    raise ValueError(f"Search domain {section}.{key} is empty")
                factors.append((section, key, domain))
        seen = {"baseline"}
        candidates = [Candidate.baseline()]
        defaults = qf.BacktestEngine._default_config()
        default_policy = qf.PortfolioPolicy().as_dict()

        def default_value(section: str, key: str) -> Any:
            if section == "engine":
                return defaults.get(key)
            if section == "symbol":
                return 1.0
            if key == "risk_profile":
                return "baseline"
            return default_policy.get(key)

        variants = [
            (section, key, value)
            for section, key, domain in factors
            for value in domain
            if value != default_value(section, key)
        ]

        def build(selected: Iterable[tuple[str, str, Any]]) -> Candidate | None:
            engine: dict[str, Any] = {}
            symbol: dict[str, float] = {}
            policy: dict[str, Any] = {}
            for section, key, value in selected:
                if section == "engine":
                    engine[key] = value
                elif section == "symbol":
                    symbol[key] = float(value)
                elif key == "risk_profile":
                    try:
                        policy.update(POLICY_RISK_PROFILES[str(value)])
                    except KeyError:
                        return None
                else:
                    policy[key] = value
            try:
                return Candidate(engine, symbol, policy)
            except (TypeError, ValueError):
                return None

        # Every non-baseline parameter first gets an interpretable isolated test.
        for variant in variants:
            candidate = build((variant,))
            if candidate is None or candidate.candidate_id in seen:
                continue
            seen.add(candidate.candidate_id)
            candidates.append(candidate)
            if len(candidates) >= maximum:
                return candidates

        all_pairs = [
            pair
            for pair in itertools.combinations(variants, 2)
            if pair[0][:2] != pair[1][:2]
        ]
        risk_variants = [
            variant for variant in variants if variant[:2] == ("policy", "risk_profile")
        ]
        conservative_variants = [
            variant
            for variant in variants
            if (
                variant[0] == "engine"
                and variant[1] == "max_positions"
                and int(variant[2]) < MAX_POSITIONS
            )
            or (
                variant[0] == "symbol"
                and (
                    (variant[1] == "entry_period" and float(variant[2]) > 1.0)
                    or (
                        variant[1]
                        in {"exit_period", "hard_stop", "risk_pct", "trail_atr_mult"}
                        and float(variant[2]) < 1.0
                    )
                )
            )
            or (
                variant[:2] == ("policy", "rearm_trading_days")
                and int(variant[2]) > int(default_policy["rearm_trading_days"])
            )
        ]
        primary_risk = risk_variants[:1]
        secondary_risk = risk_variants[1:]
        primary_pairs = [
            (risk_variant, conservative)
            for risk_variant in primary_risk
            for conservative in conservative_variants
        ]
        primary_triples = [
            (risk_variant, left, right)
            for risk_variant in primary_risk
            for left, right in itertools.combinations(conservative_variants, 2)
            if left[:2] != right[:2]
        ]
        secondary_pairs = [
            (risk_variant, conservative)
            for risk_variant in secondary_risk
            for conservative in conservative_variants
        ]
        priority_pairs = [*primary_pairs, *secondary_pairs]

        def pair_key(
            pair: tuple[tuple[str, str, Any], tuple[str, str, Any]],
        ) -> frozenset[str]:
            return frozenset(_canonical_json(list(variant)) for variant in pair)

        priority_keys = {pair_key(pair) for pair in priority_pairs}
        pairs = [pair for pair in all_pairs if pair_key(pair) not in priority_keys]
        # This PRNG makes research sampling reproducible; it protects no secret.
        random.Random(seed).shuffle(pairs)  # nosec B311
        for combination in [
            *primary_pairs,
            *primary_triples,
            *secondary_pairs,
            *pairs,
        ]:
            candidate = build(combination)
            if candidate is None:
                continue
            if candidate.candidate_id in seen:
                continue
            seen.add(candidate.candidate_id)
            candidates.append(candidate)
            if len(candidates) >= maximum:
                break
        return candidates


class LocalDataCatalog:
    """Index deterministic CSV coverage and choose only symbols available per window."""

    def __init__(
        self,
        data_dir: str | Path,
        symbols: dict[str, str],
        regime_symbols: Iterable[str],
        *,
        min_window_rows: int = 5,
    ) -> None:
        self.data_dir = Path(data_dir)
        if not self.data_dir.is_dir():
            raise ValueError(f"Data directory does not exist: {self.data_dir}")
        if min_window_rows < 1:
            raise ValueError("min_window_rows must be positive")
        self.symbols = dict(symbols)
        self.min_window_rows = int(min_window_rows)
        self._dates: dict[str, pd.DatetimeIndex] = {}
        fingerprint = hashlib.sha256()
        required = sorted(set(symbols) | set(regime_symbols))
        for code in required:
            path = self.data_dir / f"{code}.csv"
            if not path.is_file():
                raise ValueError(f"Missing local market data for {code}: {path}")
            fingerprint.update(code.encode("ascii"))
            fingerprint.update(hashlib.sha256(path.read_bytes()).digest())
            frame = pd.read_csv(path)
            if "date" not in frame.columns:
                raise ValueError(f"Market data for {code} is missing the date column")
            dates = pd.DatetimeIndex(pd.to_datetime(frame["date"], errors="coerce"))
            dates = dates[~dates.isna()].sort_values().unique()
            if dates.empty:
                raise ValueError(f"Market data for {code} contains no valid dates")
            self._dates[code] = pd.DatetimeIndex(dates)
        combined = sorted({date for dates in self._dates.values() for date in dates})
        self.calendar = pd.DatetimeIndex(combined)
        self.fingerprint = fingerprint.hexdigest()

    def available_symbols(self, window: DateWindow) -> dict[str, str]:
        """Return tradable inputs with enough rows in this historical window."""
        start = _timestamp(window.start)
        end = _timestamp(window.end)
        available = {
            code: name
            for code, name in self.symbols.items()
            if int(((self._dates[code] >= start) & (self._dates[code] <= end)).sum())
            >= self.min_window_rows
        }
        if not available:
            raise ValueError(f"No supplied symbol has enough data in {window.name}")
        return available

    def coverage(self) -> dict[str, dict[str, Any]]:
        """Return date ranges recorded in the optimization artifact."""
        return {
            code: {
                "first_date": _date_string(dates[0]),
                "last_date": _date_string(dates[-1]),
                "rows": len(dates),
            }
            for code, dates in sorted(self._dates.items())
            if code in self.symbols
        }


def build_walk_forward_folds(
    calendar: pd.DatetimeIndex,
    *,
    start: str,
    test_start: str,
    end: str,
    train_months: int = 12,
    validation_months: int = 6,
    step_months: int = 6,
    minimum_folds: int = 2,
) -> tuple[list[WalkForwardFold], DateWindow]:
    """Build expanding training folds without touching the final holdout."""
    for name, value in (
        ("train_months", train_months),
        ("validation_months", validation_months),
        ("step_months", step_months),
        ("minimum_folds", minimum_folds),
    ):
        if value < 1:
            raise ValueError(f"{name} must be positive")
    if step_months < validation_months:
        raise ValueError(
            "step_months must be at least validation_months so validation folds "
            "do not overlap"
        )
    bounded = calendar[(calendar >= _timestamp(start)) & (calendar <= _timestamp(end))]
    if bounded.empty:
        raise ValueError("No trading dates exist inside the requested range")
    holdout_dates = bounded[bounded >= _timestamp(test_start)]
    if holdout_dates.empty:
        raise ValueError("test_start leaves no final holdout dates")
    actual_test_start = holdout_dates[0]
    pretest = bounded[bounded < actual_test_start]
    if pretest.empty:
        raise ValueError("No pre-test data is available for parameter selection")
    first_validation_target = bounded[0] + pd.DateOffset(months=train_months)
    validation_start_candidates = pretest[pretest >= first_validation_target]
    if validation_start_candidates.empty:
        raise ValueError("Training span leaves no validation window")
    cursor = validation_start_candidates[0]
    folds: list[WalkForwardFold] = []
    while cursor < actual_test_start:
        validation_end_target = (
            cursor + pd.DateOffset(months=validation_months) - pd.Timedelta(days=1)
        )
        validation_dates = pretest[
            (pretest >= cursor) & (pretest <= validation_end_target)
        ]
        training_dates = pretest[pretest < cursor]
        if validation_dates.empty or training_dates.empty:
            break
        fold_number = len(folds) + 1
        folds.append(
            WalkForwardFold(
                name=f"fold_{fold_number}",
                train=DateWindow(
                    f"fold_{fold_number}_train",
                    _date_string(training_dates[0]),
                    _date_string(training_dates[-1]),
                    "train",
                ),
                validation=DateWindow(
                    f"fold_{fold_number}_validation",
                    _date_string(validation_dates[0]),
                    _date_string(validation_dates[-1]),
                    "validation",
                ),
            )
        )
        next_target = cursor + pd.DateOffset(months=step_months)
        next_dates = pretest[pretest >= next_target]
        if next_dates.empty or next_dates[0] <= cursor:
            break
        cursor = next_dates[0]
    if len(folds) < minimum_folds:
        raise ValueError(
            f"Only {len(folds)} walk-forward folds are available; "
            f"at least {minimum_folds} are required"
        )
    test_window = DateWindow(
        "final_holdout",
        _date_string(actual_test_start),
        _date_string(bounded[-1]),
        "test",
    )
    return folds, test_window


@dataclass(frozen=True)
class WindowMetrics:
    """Small JSON-friendly metric set retained from one engine run."""

    window: str
    role: str
    symbols: tuple[str, ...]
    stressed: bool
    total_return: float
    annual_return: float
    max_drawdown: float
    sharpe: float
    calmar: float
    total_trades: int
    final_assets: float

    @classmethod
    def from_result(
        cls,
        window: DateWindow,
        symbols: Iterable[str],
        stressed: bool,
        result: dict[str, Any],
    ) -> WindowMetrics:
        """Normalize engine output and reject non-finite selection metrics."""
        numeric = {
            key: float(result[key])
            for key in (
                "total_return",
                "annual_return",
                "max_drawdown",
                "sharpe",
                "calmar",
                "final_assets",
            )
        }
        if not all(math.isfinite(value) for value in numeric.values()):
            raise RuntimeError(f"Non-finite metric returned for {window.name}")
        return cls(
            window=window.name,
            role=window.role,
            symbols=tuple(sorted(symbols)),
            stressed=stressed,
            total_return=numeric["total_return"],
            annual_return=numeric["annual_return"],
            max_drawdown=numeric["max_drawdown"],
            sharpe=numeric["sharpe"],
            calmar=numeric["calmar"],
            total_trades=int(result["total_trades"]),
            final_assets=numeric["final_assets"],
        )


class CandidateRunner(Protocol):
    """Minimal runner contract used to unit-test selection without market data."""

    def run(
        self, candidate: Candidate, window: DateWindow, *, stress: bool = False
    ) -> WindowMetrics: ...


class CandidateRunner:
    """Execute candidates through the unmodified qf.1 engine."""

    def __init__(
        self,
        symbols: dict[str, str],
        catalog: LocalDataCatalog,
        *,
        initial_capital: float = 2_000_000,
        indicator_state: str = "warm",
        warmup_calendar_days: int = 365,
        quiet: bool = True,
    ) -> None:
        self.symbols = dict(symbols)
        self.catalog = catalog
        self.initial_capital = float(initial_capital)
        self.indicator_state = indicator_state
        self.warmup_calendar_days = int(warmup_calendar_days)
        self.quiet = bool(quiet)

    def run(
        self, candidate: Candidate, window: DateWindow, *, stress: bool = False
    ) -> WindowMetrics:
        """Run one candidate/window and verify hard portfolio invariants."""
        symbols = self.catalog.available_symbols(window)
        engine = qf.BacktestEngine(
            self.initial_capital,
            cfg=candidate.engine_config(stress=stress),
            policy=candidate.policy(),
        )
        output = io.StringIO()
        manager = (
            contextlib.redirect_stdout(output)
            if self.quiet
            else contextlib.nullcontext()
        )
        with manager:
            result = engine.run(
                symbols,
                window.start,
                window.end,
                per_symbol_config=candidate.per_symbol_config(symbols),
                config_route="auto",
                data_dir=str(self.catalog.data_dir),
                indicator_state=self.indicator_state,
                warmup_calendar_days=self.warmup_calendar_days,
            )
        if int(result["portfolio_max_positions"]) > MAX_POSITIONS:
            raise RuntimeError("Engine reported a portfolio position limit above six")
        if int(result["max_concurrent_symbols"]) > MAX_POSITIONS:
            raise RuntimeError("Engine held more than six symbols")
        return WindowMetrics.from_result(window, symbols, stress, result)


@dataclass
class CandidateEvaluation:
    """All pre-test evidence for one predetermined candidate."""

    candidate: Candidate
    training: list[WindowMetrics]
    validation: list[WindowMetrics]
    stress_validation: list[WindowMetrics]
    drawdown_limit: float
    feasible: bool = field(init=False)
    rejection_reasons: list[str] = field(init=False)
    median_validation_annual_return: float = field(init=False)
    compound_validation_return: float = field(init=False)
    worst_validation_return: float = field(init=False)
    worst_validation_drawdown: float = field(init=False)
    worst_stress_drawdown: float = field(init=False)
    validation_stability: float = field(init=False)
    generalization_gap: float = field(init=False)
    selection_score: float = field(init=False)
    parameter_neighbor_count: int = field(init=False, default=0)
    parameter_supported: bool = field(init=False, default=False)
    neighbor_median_score: float | None = field(init=False, default=None)
    robust_selection_score: float = field(init=False)

    def __post_init__(self) -> None:
        if not self.validation or len(self.validation) != len(self.stress_validation):
            raise ValueError("Validation and stress-validation metrics must align")
        annual = [item.annual_return for item in self.validation]
        training_annual = [item.annual_return for item in self.training]
        self.median_validation_annual_return = statistics.median(annual)
        self.compound_validation_return = (
            math.prod(1.0 + item.total_return for item in self.validation) - 1.0
        )
        self.worst_validation_return = min(
            item.total_return for item in self.validation
        )
        self.worst_validation_drawdown = min(
            item.max_drawdown for item in self.validation
        )
        self.worst_stress_drawdown = min(
            item.max_drawdown for item in self.stress_validation
        )
        self.validation_stability = (
            statistics.pstdev(annual) if len(annual) > 1 else 0.0
        )
        train_median = statistics.median(training_annual) if training_annual else 0.0
        self.generalization_gap = max(
            0.0, train_median - self.median_validation_annual_return
        )
        self.selection_score = (
            self.median_validation_annual_return
            - 0.25 * self.validation_stability
            - 0.10 * self.generalization_gap
            + 0.05 * self.worst_validation_return
        )
        self.robust_selection_score = self.selection_score
        self.rejection_reasons = []
        if abs(self.worst_validation_drawdown) > self.drawdown_limit + 1e-12:
            self.rejection_reasons.append("validation drawdown exceeds hard limit")
        if abs(self.worst_stress_drawdown) > self.drawdown_limit + 1e-12:
            self.rejection_reasons.append("stress drawdown exceeds hard limit")
        self.feasible = not self.rejection_reasons

    def as_dict(self) -> dict[str, Any]:
        """Return all selection evidence without pandas or engine objects."""
        return {
            "candidate": self.candidate.as_dict(),
            "drawdown_limit": self.drawdown_limit,
            "feasible": self.feasible,
            "rejection_reasons": list(self.rejection_reasons),
            "summary": {
                "median_validation_annual_return": self.median_validation_annual_return,
                "compound_validation_return": self.compound_validation_return,
                "worst_validation_return": self.worst_validation_return,
                "worst_validation_drawdown": self.worst_validation_drawdown,
                "worst_stress_drawdown": self.worst_stress_drawdown,
                "validation_stability": self.validation_stability,
                "generalization_gap": self.generalization_gap,
                "selection_score": self.selection_score,
                "parameter_neighbor_count": self.parameter_neighbor_count,
                "parameter_supported": self.parameter_supported,
                "neighbor_median_score": self.neighbor_median_score,
                "robust_selection_score": self.robust_selection_score,
            },
            "training": [asdict(item) for item in self.training],
            "validation": [asdict(item) for item in self.validation],
            "stress_validation": [asdict(item) for item in self.stress_validation],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CandidateEvaluation:
        """Rebuild a fully checked evaluation from a prior audit artifact."""
        config = payload["candidate"]
        candidate = Candidate(
            config.get("engine_overrides", {}),
            config.get("symbol_multipliers", {}),
            config.get("policy_overrides", {}),
        )

        def window_metrics(item: dict[str, Any]) -> WindowMetrics:
            return WindowMetrics(
                window=str(item["window"]),
                role=str(item["role"]),
                symbols=tuple(str(code) for code in item["symbols"]),
                stressed=bool(item["stressed"]),
                total_return=float(item["total_return"]),
                annual_return=float(item["annual_return"]),
                max_drawdown=float(item["max_drawdown"]),
                sharpe=float(item["sharpe"]),
                calmar=float(item["calmar"]),
                total_trades=int(item["total_trades"]),
                final_assets=float(item["final_assets"]),
            )

        return cls(
            candidate=candidate,
            training=[window_metrics(item) for item in payload["training"]],
            validation=[window_metrics(item) for item in payload["validation"]],
            stress_validation=[
                window_metrics(item) for item in payload["stress_validation"]
            ],
            drawdown_limit=float(
                payload.get(
                    "drawdown_limit",
                    payload.get("summary", {}).get("drawdown_limit", 0.20),
                )
            ),
        )


def _candidate_distance(left: Candidate, right: Candidate) -> int:
    """Count changed parameter axes between two optimizer candidates."""

    def flatten(candidate: Candidate) -> dict[tuple[str, str], Any]:
        flattened = {
            **{
                ("engine", key): value
                for key, value in candidate.engine_overrides.items()
            },
            **{
                ("symbol", key): value
                for key, value in candidate.symbol_multipliers.items()
            },
            **{
                ("policy", key): value
                for key, value in candidate.policy_overrides.items()
                if key not in POLICY_RISK_KEYS
            },
        }
        if any(key in candidate.policy_overrides for key in POLICY_RISK_KEYS):
            defaults = qf.PortfolioPolicy().as_dict()
            flattened[("policy", "risk_profile")] = tuple(
                candidate.policy_overrides.get(key, defaults[key])
                for key in POLICY_RISK_KEYS
            )
        return flattened

    first = flatten(left)
    second = flatten(right)
    return sum(
        first.get(key, object()) != second.get(key, object())
        for key in set(first) | set(second)
    )


def _apply_parameter_support(evaluations: list[CandidateEvaluation]) -> None:
    """Reject unsupported parameter spikes and blend each score with neighbors."""
    for evaluation in evaluations:
        neighbors = [
            other
            for other in evaluations
            if other is not evaluation
            and _candidate_distance(evaluation.candidate, other.candidate) == 1
        ]
        evaluation.parameter_neighbor_count = len(neighbors)
        evaluation.parameter_supported = bool(neighbors) or (
            evaluation.candidate.candidate_id == "baseline"
        )
        if neighbors:
            evaluation.neighbor_median_score = statistics.median(
                item.selection_score for item in neighbors
            )
            evaluation.robust_selection_score = (
                0.75 * evaluation.selection_score
                + 0.25 * evaluation.neighbor_median_score
            )
        if not evaluation.parameter_supported:
            evaluation.rejection_reasons.append(
                "candidate is an isolated parameter point without a one-axis neighbor"
            )
            evaluation.feasible = False


def pareto_frontier(
    evaluations: Iterable[CandidateEvaluation],
) -> list[CandidateEvaluation]:
    """Return feasible return/drawdown points not dominated by another candidate."""
    feasible = [item for item in evaluations if item.feasible]
    frontier: list[CandidateEvaluation] = []
    for candidate in feasible:
        dominated = False
        for other in feasible:
            if other is candidate:
                continue
            return_no_worse = (
                other.median_validation_annual_return
                >= candidate.median_validation_annual_return - 1e-12
            )
            drawdown_no_worse = (
                abs(other.worst_validation_drawdown)
                <= abs(candidate.worst_validation_drawdown) + 1e-12
            )
            strictly_better = (
                other.median_validation_annual_return
                > candidate.median_validation_annual_return + 1e-12
                or abs(other.worst_validation_drawdown)
                < abs(candidate.worst_validation_drawdown) - 1e-12
            )
            if return_no_worse and drawdown_no_worse and strictly_better:
                dominated = True
                break
        if not dominated:
            frontier.append(candidate)
    return sorted(
        frontier,
        key=lambda item: (
            -item.robust_selection_score,
            -item.median_validation_annual_return,
            abs(item.worst_validation_drawdown),
            item.candidate.candidate_id,
        ),
    )


class WalkForwardOptimizer:
    """Select a candidate from pre-test folds, then reveal the final holdout."""

    def __init__(
        self,
        runner: CandidateRunner,
        folds: list[WalkForwardFold],
        test_window: DateWindow,
        *,
        drawdown_limit: float = DEFAULT_DRAWDOWN_LIMIT,
        progress: bool = False,
    ) -> None:
        if not folds:
            raise ValueError("At least one walk-forward fold is required")
        if test_window.role != "test":
            raise ValueError("test_window must have the test role")
        if not 0 < drawdown_limit < 1:
            raise ValueError("drawdown_limit must be between zero and one")
        if max(_timestamp(fold.validation.end) for fold in folds) >= _timestamp(
            test_window.start
        ):
            raise ValueError("Every validation fold must end before the holdout")
        self.runner = runner
        self.folds = list(folds)
        self.test_window = test_window
        self.drawdown_limit = float(drawdown_limit)
        self.progress = bool(progress)

    def evaluate(self, candidate: Candidate) -> CandidateEvaluation:
        """Collect training, validation, and stressed validation evidence."""
        training = [self.runner.run(candidate, fold.train) for fold in self.folds]
        validation = [
            self.runner.run(candidate, fold.validation) for fold in self.folds
        ]
        stressed = [
            self.runner.run(candidate, fold.validation, stress=True)
            for fold in self.folds
        ]
        return CandidateEvaluation(
            candidate,
            training,
            validation,
            stressed,
            self.drawdown_limit,
        )

    def optimize(
        self,
        candidates: list[Candidate],
        *,
        cached_evaluations: dict[str, CandidateEvaluation] | None = None,
        cache_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        """Select without holdout access, then compare selected and baseline on test."""
        if not candidates:
            raise ValueError("At least one candidate is required")
        unique = {candidate.candidate_id: candidate for candidate in candidates}
        unique.setdefault("baseline", Candidate.baseline())
        ordered = [
            unique[key] for key in sorted(unique, key=lambda key: key != "baseline")
        ]
        cached = dict(cached_evaluations or {})
        resolved_cache_dir = Path(cache_dir) if cache_dir is not None else None
        if resolved_cache_dir is not None:
            resolved_cache_dir.mkdir(parents=True, exist_ok=True)
        evaluations: list[CandidateEvaluation] = []
        for index, candidate in enumerate(ordered, start=1):
            evaluation = cached.get(candidate.candidate_id)
            cache_path = (
                resolved_cache_dir / f"{candidate.candidate_id}.json"
                if resolved_cache_dir is not None
                else None
            )
            if evaluation is None and cache_path is not None and cache_path.is_file():
                evaluation = CandidateEvaluation.from_dict(
                    json.loads(cache_path.read_text(encoding="utf-8"))
                )
            if evaluation is not None and not self._cache_matches(
                evaluation, candidate
            ):
                evaluation = None
            if self.progress:
                print(
                    f"{'Reusing' if evaluation is not None else 'Evaluating'} "
                    f"candidate {index}/{len(ordered)}: "
                    f"{candidate.candidate_id}",
                    flush=True,
                )
            if evaluation is None:
                evaluation = self.evaluate(candidate)
            if cache_path is not None and not cache_path.is_file():
                temporary = cache_path.with_suffix(".tmp")
                temporary.write_text(
                    json.dumps(
                        evaluation.as_dict(),
                        ensure_ascii=False,
                        indent=2,
                        allow_nan=False,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                temporary.replace(cache_path)
            evaluations.append(evaluation)
        _apply_parameter_support(evaluations)
        frontier = pareto_frontier(evaluations)
        report: dict[str, Any] = {
            "engine": "Quant Fusion",
            "selection_protocol": {
                "test_data_used_for_parameter_selection": False,
                "test_data_used_for_one_time_promotion_gate": True,
                "drawdown_limit": self.drawdown_limit,
                "objective": (
                    "maximize robust validation return after the drawdown hard limit; "
                    "penalize fold instability and train-validation degradation; "
                    "reject isolated parameter spikes without one-axis support"
                ),
                "stress_costs": "0.2% slippage and 1.5x commission",
                "hard_constraints": {
                    "max_symbol_weight": MAX_SYMBOL_WEIGHT,
                    "max_total_weight": MAX_TOTAL_WEIGHT,
                    "max_positions": MAX_POSITIONS,
                },
            },
            "folds": [
                {
                    "name": fold.name,
                    "train": asdict(fold.train),
                    "validation": asdict(fold.validation),
                }
                for fold in self.folds
            ],
            "final_holdout": asdict(self.test_window),
            "evaluations": [item.as_dict() for item in evaluations],
            "pareto_candidate_ids": [item.candidate.candidate_id for item in frontier],
        }
        if not frontier:
            report.update(
                {
                    "status": "no_feasible_candidate",
                    "selected_candidate": None,
                    "holdout_comparison": None,
                }
            )
            return report
        selected = frontier[0]
        baseline = next(
            item for item in evaluations if item.candidate.candidate_id == "baseline"
        )
        # This is the first point at which the holdout is visible to the process.
        baseline_test = self.runner.run(baseline.candidate, self.test_window)
        baseline_stress_test = self.runner.run(
            baseline.candidate, self.test_window, stress=True
        )
        selected_test = (
            baseline_test
            if selected.candidate.candidate_id == "baseline"
            else self.runner.run(selected.candidate, self.test_window)
        )
        selected_stress_test = (
            baseline_stress_test
            if selected.candidate.candidate_id == "baseline"
            else self.runner.run(selected.candidate, self.test_window, stress=True)
        )
        promotion_checks = {
            "ordinary_drawdown_within_limit": (
                abs(selected_test.max_drawdown) <= self.drawdown_limit + 1e-12
            ),
            "stress_drawdown_within_limit": (
                abs(selected_stress_test.max_drawdown) <= self.drawdown_limit + 1e-12
            ),
            "ordinary_return_not_below_baseline": (
                selected_test.total_return >= baseline_test.total_return - 1e-12
            ),
            "stress_return_not_below_baseline": (
                selected_stress_test.total_return
                >= baseline_stress_test.total_return - 1e-12
            ),
        }
        promoted = all(promotion_checks.values())
        recommended = selected.candidate if promoted else baseline.candidate
        status = (
            "baseline_retained"
            if selected.candidate.candidate_id == "baseline"
            else "promoted"
            if promoted
            else "candidate_rejected_on_holdout"
        )
        report.update(
            {
                "status": status,
                "selected_candidate": selected.candidate.as_dict(),
                "recommended_candidate": recommended.as_dict(),
                "promotion_gate": {
                    "passed": promoted,
                    "checks": promotion_checks,
                    "rule": (
                        "holdout and stressed-holdout drawdown must stay within the "
                        "hard limit, and return must not fall below the qf.1 baseline"
                    ),
                },
                "holdout_comparison": {
                    "baseline": asdict(baseline_test),
                    "baseline_stress": asdict(baseline_stress_test),
                    "selected": asdict(selected_test),
                    "selected_stress": asdict(selected_stress_test),
                    "delta_total_return": (
                        selected_test.total_return - baseline_test.total_return
                    ),
                    "delta_max_drawdown": (
                        selected_test.max_drawdown - baseline_test.max_drawdown
                    ),
                    "selected_holdout_drawdown_within_limit": (
                        abs(selected_test.max_drawdown) <= self.drawdown_limit + 1e-12
                    ),
                },
            }
        )
        return report

    def _cache_matches(
        self, evaluation: CandidateEvaluation, candidate: Candidate
    ) -> bool:
        """Accept cached evidence only when candidate, folds, and limits match."""
        if evaluation.candidate.as_dict() != candidate.as_dict():
            return False
        if not math.isclose(evaluation.drawdown_limit, self.drawdown_limit):
            return False
        train_names = [fold.train.name for fold in self.folds]
        validation_names = [fold.validation.name for fold in self.folds]
        return (
            [item.window for item in evaluation.training] == train_names
            and [item.window for item in evaluation.validation] == validation_names
            and [item.window for item in evaluation.stress_validation]
            == validation_names
            and all(not item.stressed for item in evaluation.training)
            and all(not item.stressed for item in evaluation.validation)
            and all(item.stressed for item in evaluation.stress_validation)
        )


def _format_pct(value: float) -> str:
    return f"{value:.2%}"


def render_markdown_summary(report: dict[str, Any]) -> str:
    """Render the selection protocol and honest holdout comparison."""
    lines = [
        "# Quant Fusion Automatic Optimization Result",
        "",
        f"Status: `{report['status']}`.",
        "",
        "The final holdout was not used to choose parameters. Candidate selection used "
        "only expanding training windows, later validation windows, and higher-cost "
        "validation stress runs.",
        "",
    ]
    if report["status"] == "no_feasible_candidate":
        lines.append("No candidate satisfied the out-of-sample drawdown constraint.")
        return "\n".join(lines) + "\n"
    comparison = report["holdout_comparison"]
    baseline = comparison["baseline"]
    selected = comparison["selected"]
    selected_id = report["selected_candidate"]["candidate_id"]
    recommended_id = report["recommended_candidate"]["candidate_id"]
    lines.extend(
        [
            f"Selected candidate: `{selected_id}`.",
            f"Recommended for execution: `{recommended_id}`.",
            "",
            "| Final holdout | Total return | Maximum drawdown | Sharpe | Calmar |",
            "|---|---:|---:|---:|---:|",
            (
                f"| qf.1 baseline | {_format_pct(baseline['total_return'])} | "
                f"{_format_pct(baseline['max_drawdown'])} | "
                f"{baseline['sharpe']:.3f} | {baseline['calmar']:.3f} |"
            ),
            (
                f"| selected | {_format_pct(selected['total_return'])} | "
                f"{_format_pct(selected['max_drawdown'])} | "
                f"{selected['sharpe']:.3f} | {selected['calmar']:.3f} |"
            ),
            "",
            f"Return delta: {_format_pct(comparison['delta_total_return'])}; "
            f"drawdown delta: {_format_pct(comparison['delta_max_drawdown'])}.",
            "",
            (
                "Promotion gate: passed."
                if report["promotion_gate"]["passed"]
                else "Promotion gate: failed; the qf.1 baseline is retained."
            ),
            "",
            "A positive holdout result is evidence for this frozen snapshot, not a "
            "guarantee that the same parameters will remain optimal in live trading.",
        ]
    )
    return "\n".join(lines) + "\n"


def _load_resume_evaluations(
    path: str | Path,
    *,
    symbols: dict[str, str],
    catalog: LocalDataCatalog,
    folds: list[WalkForwardFold],
    drawdown_limit: float,
) -> dict[str, CandidateEvaluation]:
    """Load a prior report only when its data and selection protocol match."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("engine") != "Quant Fusion.1":
        raise ValueError("Resume report was produced by a different engine")
    metadata = payload.get("run_metadata", {})
    if metadata.get("symbols") != symbols:
        raise ValueError("Resume report uses a different symbol universe")
    if metadata.get("data_coverage") != catalog.coverage():
        raise ValueError("Resume report uses a different data snapshot")
    if metadata.get("data_fingerprint") not in {None, catalog.fingerprint}:
        raise ValueError("Resume report data bytes do not match the current snapshot")
    current_engine_sha = hashlib.sha256(Path(qf.__file__).read_bytes()).hexdigest()
    if metadata.get("engine_sha256") not in {None, current_engine_sha}:
        raise ValueError("Resume report uses different execution code")
    expected_folds = [
        {
            "name": fold.name,
            "train": asdict(fold.train),
            "validation": asdict(fold.validation),
        }
        for fold in folds
    ]
    if payload.get("folds") != expected_folds:
        raise ValueError("Resume report uses different walk-forward folds")
    prior_limit = float(payload.get("selection_protocol", {}).get("drawdown_limit"))
    if not math.isclose(prior_limit, drawdown_limit):
        raise ValueError("Resume report uses a different drawdown limit")
    evaluations = [
        CandidateEvaluation.from_dict(item) for item in payload.get("evaluations", [])
    ]
    return {item.candidate.candidate_id: item for item in evaluations}


def _cache_signature(
    *,
    symbols: dict[str, str],
    catalog: LocalDataCatalog,
    folds: list[WalkForwardFold],
    drawdown_limit: float,
    initial_capital: float,
) -> str:
    """Bind automatic cache reuse to code, data, folds, capital, and limits."""
    engine_path = Path(qf.__file__).resolve()
    payload = {
        "engine_sha256": hashlib.sha256(engine_path.read_bytes()).hexdigest(),
        "optimizer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "data_fingerprint": catalog.fingerprint,
        "symbols": symbols,
        "folds": [
            {"train": asdict(fold.train), "validation": asdict(fold.validation)}
            for fold in folds
        ],
        "drawdown_limit": drawdown_limit,
        "initial_capital": initial_capital,
        "indicator_state": "warm",
        "warmup_calendar_days": 365,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()[:20]


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the deterministic local-data optimizer command line."""
    parser = argparse.ArgumentParser(
        description="Walk-forward parameter optimizer for Quant Fusion.1"
    )
    parser.add_argument("--symbol", "-s", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--start", default="2024-01-02")
    parser.add_argument("--test-start", default="2026-01-05")
    parser.add_argument("--end", default="2026-07-20")
    parser.add_argument("--train-months", type=int, default=12)
    parser.add_argument("--validation-months", type=int, default=6)
    parser.add_argument("--step-months", type=int, default=6)
    parser.add_argument("--minimum-folds", type=int, default=2)
    parser.add_argument("--candidates", type=int, default=40)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--drawdown-limit", type=float, default=0.20)
    parser.add_argument("--capital", type=float, default=2_000_000)
    parser.add_argument("--search-space", default="")
    parser.add_argument(
        "--resume-report",
        default="",
        help="Reuse compatible pre-test candidate evaluations from a prior report",
    )
    parser.add_argument("--output-dir", default="optimizer_output")
    return parser


def main() -> int:
    """Run automatic selection and save an auditable report plus loadable config."""
    args = build_argument_parser().parse_args()
    symbols = qf.parse_symbols(args.symbol)
    catalog = LocalDataCatalog(
        args.data_dir,
        symbols,
        qf.PortfolioPolicy().regime_symbols,
    )
    folds, holdout = build_walk_forward_folds(
        catalog.calendar,
        start=args.start,
        test_start=args.test_start,
        end=args.end,
        train_months=args.train_months,
        validation_months=args.validation_months,
        step_months=args.step_months,
        minimum_folds=args.minimum_folds,
    )
    space = (
        ParameterSpace.from_json(args.search_space)
        if args.search_space
        else ParameterSpace.default()
    )
    candidates = space.candidates(args.candidates, args.seed)
    runner = CandidateRunner(symbols, catalog, initial_capital=args.capital)
    optimizer = WalkForwardOptimizer(
        runner,
        folds,
        holdout,
        drawdown_limit=args.drawdown_limit,
        progress=True,
    )
    cached = (
        _load_resume_evaluations(
            args.resume_report,
            symbols=symbols,
            catalog=catalog,
            folds=folds,
            drawdown_limit=args.drawdown_limit,
        )
        if args.resume_report
        else {}
    )
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    signature = _cache_signature(
        symbols=symbols,
        catalog=catalog,
        folds=folds,
        drawdown_limit=args.drawdown_limit,
        initial_capital=args.capital,
    )
    report = optimizer.optimize(
        candidates,
        cached_evaluations=cached,
        cache_dir=output / "candidate_cache" / signature,
    )
    report["run_metadata"] = {
        "symbols": symbols,
        "data_directory": str(Path(args.data_dir)),
        "data_coverage": catalog.coverage(),
        "candidate_count": len(candidates),
        "seed": args.seed,
        "engine_sha256": hashlib.sha256(Path(qf.__file__).read_bytes()).hexdigest(),
        "optimizer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "data_fingerprint": catalog.fingerprint,
        "cache_signature": signature,
    }
    (output / "optimization_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    (output / "optimization_summary.md").write_text(
        render_markdown_summary(report), encoding="utf-8"
    )
    if report.get("recommended_candidate") is not None:
        recommended = dict(report["recommended_candidate"])
        recommended["materialized_per_symbol_config"] = Candidate(
            recommended["engine_overrides"],
            recommended["symbol_multipliers"],
            recommended["policy_overrides"],
        ).per_symbol_config(symbols)
        (output / "recommended_config.json").write_text(
            json.dumps(recommended, ensure_ascii=False, indent=2, allow_nan=False)
            + "\n",
            encoding="utf-8",
        )
    print(render_markdown_summary(report))
    return 0 if report["status"] != "no_feasible_candidate" else 2


if __name__ == "__main__":
    raise SystemExit(main())
