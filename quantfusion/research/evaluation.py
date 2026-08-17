"""Production-replay candidate evaluation and Pareto ranking."""

from __future__ import annotations

import contextlib
import io
import math
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol

from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.research import replay_api as ra
from quantfusion.research.candidates import (
    Candidate,
    DateWindow,
    MAX_POSITIONS,
    POLICY_RISK_KEYS,
)
from quantfusion.research.catalog import LocalDataCatalog

ProductionReplayEngine = ra.ProductionReplayEngine
REGIME_INDEX_FILES = ra.REGIME_INDEX_FILES

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


class CandidateRunnerProtocol(Protocol):
    """Minimal runner contract used to unit-test selection without market data."""

    def run(
        self, candidate: Candidate, window: DateWindow, *, stress: bool = False
    ) -> WindowMetrics: ...


class CandidateRunner:
    """Execute candidates through the production daily-route replay."""

    def __init__(
        self,
        symbols: dict[str, str],
        catalog: LocalDataCatalog,
        *,
        regime_data_dir: str | Path,
        initial_capital: float = 2_000_000,
        indicator_state: str = "warm",
        warmup_calendar_days: int = 365,
        quiet: bool = True,
    ) -> None:
        self.symbols = dict(symbols)
        self.catalog = catalog
        self.regime_data_dir = Path(regime_data_dir)
        if not self.regime_data_dir.is_dir():
            raise ValueError(
                f"Regime data directory does not exist: {self.regime_data_dir}"
            )
        for code in REGIME_INDEX_FILES.values():
            path = self.regime_data_dir / f"{code}.csv"
            if not path.is_file():
                raise ValueError(f"Missing regime index data for {code}: {path}")
        self.initial_capital = float(initial_capital)
        self.indicator_state = indicator_state
        self.warmup_calendar_days = int(warmup_calendar_days)
        self.quiet = bool(quiet)

    def run(
        self, candidate: Candidate, window: DateWindow, *, stress: bool = False
    ) -> WindowMetrics:
        """Run one candidate/window and verify hard portfolio invariants."""
        symbols = self.catalog.available_symbols(window)
        engine = ra.ProductionReplayEngine(
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
                regime_data_dir=str(self.regime_data_dir),
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
    median_validation_trades: float = field(init=False)
    worst_validation_trades: int = field(init=False)
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
        validation_trades = [item.total_trades for item in self.validation]
        self.median_validation_trades = float(statistics.median(validation_trades))
        self.worst_validation_trades = max(validation_trades)
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
            - 0.02 * (self.median_validation_trades / 100.0)
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
                "median_validation_trades": self.median_validation_trades,
                "worst_validation_trades": self.worst_validation_trades,
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
            defaults = PortfolioPolicy().as_dict()
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
    """Return feasible return/drawdown/trade points not dominated by another."""
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
            trades_no_worse = (
                other.median_validation_trades
                <= candidate.median_validation_trades + 1e-12
            )
            strictly_better = (
                other.median_validation_annual_return
                > candidate.median_validation_annual_return + 1e-12
                or abs(other.worst_validation_drawdown)
                < abs(candidate.worst_validation_drawdown) - 1e-12
                or other.median_validation_trades
                < candidate.median_validation_trades - 1e-12
            )
            if (
                return_no_worse
                and drawdown_no_worse
                and trades_no_worse
                and strictly_better
            ):
                dominated = True
                break
        if not dominated:
            frontier.append(candidate)
    return sorted(
        frontier,
        key=lambda item: (
            -round(item.median_validation_annual_return / 0.02),
            abs(item.worst_validation_drawdown),
            item.median_validation_trades,
            -item.robust_selection_score,
            item.candidate.candidate_id,
        ),
    )


candidate_distance = _candidate_distance
apply_parameter_support = _apply_parameter_support
