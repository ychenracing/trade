"""Candidate schemas, IDs, and bounded parameter spaces."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, cast

import pandas as pd

from quantfusion.config.engine import (
    PER_SYMBOL_OVERRIDE_KEYS,
    default_engine_config,
    validate_engine_config,
)
from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.config.profiles import config_for_symbol
from quantfusion.engine.universe import BacktestEngine

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
SLEEVE_WEIGHT_PROFILES: dict[str, dict[str, float]] = {
    "baseline": {
        "transition_fast_weight": 0.20,
        "transition_base_weight": 0.35,
        "transition_slow_weight": 0.45,
    },
    "slow_defensive": {
        "transition_fast_weight": 0.15,
        "transition_base_weight": 0.35,
        "transition_slow_weight": 0.50,
    },
    "fast_recovery": {
        "transition_fast_weight": 0.25,
        "transition_base_weight": 0.30,
        "transition_slow_weight": 0.45,
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
        sleeve_profile = engine.pop("dynamic_sleeve_profile", None)
        if sleeve_profile is not None:
            try:
                engine.update(SLEEVE_WEIGHT_PROFILES[str(sleeve_profile)])
            except KeyError as error:
                raise ValueError(
                    f"Unknown dynamic_sleeve_profile: {sleeve_profile!r}"
                ) from error
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
        """Return the untouched production defaults."""
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
        scalable = PER_SYMBOL_OVERRIDE_KEYS - {
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
        policy = PortfolioPolicy(**self.policy_overrides)
        BacktestEngine(cfg=self.engine_config(), policy=policy)

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
            defaults = default_engine_config()
            cfg["slippage"] = max(
                float(cfg.get("slippage", defaults["slippage"])), 0.002
            )
            cfg["commission_rate"] = max(
                float(cfg.get("commission_rate", defaults["commission_rate"])) * 1.5,
                defaults["commission_rate"],
            )
        return cfg

    def policy(self) -> PortfolioPolicy:
        """Build the validated portfolio policy used by this candidate."""
        return PortfolioPolicy(**self.policy_overrides)

    def per_symbol_config(self, symbols: dict[str, str]) -> dict[str, dict[str, Any]]:
        """Scale each symbol's routed profile without erasing industry differences."""
        result: dict[str, dict[str, Any]] = {}
        for code, name in symbols.items():
            base = config_for_symbol(code, name=name)
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
            merged = validate_engine_config({**base, **overrides})
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
    def for_stage(cls, stage: str) -> ParameterSpace:
        """Return one risk -> turnover -> return search stage.

        Stages deliberately isolate parameter families so a candidate cannot
        hide a risk regression behind an unrelated entry/exit change.
        """
        normalized = str(stage).lower()
        if normalized == "all":
            return cls.default()
        if normalized == "risk":
            return cls(
                engine={
                    "cm_risk_continuous_confirm_days": (2, 3, 4),
                    "cm_risk_level2_drawdown": (0.06, 0.08, 0.10),
                    "cm_risk_level3_drawdown": (0.10, 0.12, 0.14),
                    "regime_transition_scale": (0.70, 0.85, 1.0),
                    "regime_transition_pyramid_scale": (0.0, 0.25, 0.50),
                },
                symbol_multipliers={},
                policy={
                    "risk_profile": ("baseline", "moderate", "defensive")
                },
            )
        if normalized == "turnover":
            return cls(
                engine={
                    "sticky_min_score_gap": (0.15, 0.20, 0.25),
                    "sticky_confirm_days": (4, 6, 8),
                    "sticky_cycle_days": (5, 8, 10),
                    "sticky_rotated_cooldown_days": (15, 20, 30),
                    "transition_max_positions": (3, 4, 5),
                },
                symbol_multipliers={},
                policy={"candidate_reference_percentile": (0.45, 0.50, 0.55)},
            )
        if normalized == "return":
            return cls(
                engine={
                    "momentum_lookback": (5, 10, 20),
                    "dynamic_sleeve_profile": (
                        "baseline", "slow_defensive", "fast_recovery"
                    ),
                },
                symbol_multipliers={
                    "entry_period": (0.85, 1.0, 1.15),
                    "exit_period": (0.85, 1.0, 1.15),
                    "trail_atr_mult": (0.90, 1.0, 1.10),
                    "risk_pct": (0.90, 1.0, 1.10),
                },
                policy={},
            )
        raise ValueError("stage must be risk, turnover, return, or all")

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
        defaults = default_engine_config()
        default_policy = PortfolioPolicy().as_dict()

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


canonical_json = _canonical_json
timestamp = _timestamp
date_string = _date_string
candidate_id = _candidate_id
