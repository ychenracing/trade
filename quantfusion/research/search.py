"""Walk-forward selection and one-time holdout promotion gate."""

from __future__ import annotations

import json
import math
from dataclasses import asdict
from pathlib import Path
from typing import Any

from quantfusion.research.candidates import (
    Candidate,
    DateWindow,
    WalkForwardFold,
    DEFAULT_DRAWDOWN_LIMIT,
    MAX_POSITIONS,
    MAX_SYMBOL_WEIGHT,
    MAX_TOTAL_WEIGHT,
    timestamp,
)
from quantfusion.research.evaluation import (
    CandidateEvaluation,
    CandidateRunnerProtocol,
    apply_parameter_support,
    pareto_frontier,
)

_apply_parameter_support = apply_parameter_support
_timestamp = timestamp

class WalkForwardOptimizer:
    """Select a candidate from pre-test folds, then reveal the final holdout."""

    def __init__(
        self,
        runner: CandidateRunnerProtocol,
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
                try:
                    evaluation = self.evaluate(candidate)
                except (ValueError, KeyError, TypeError, RuntimeError) as error:
                    # An invalid parameter combination (e.g. a scaled
                    # exit_period >= entry_period for a routed symbol) or an
                    # engine invariant failure makes evaluation raise. Skip the
                    # candidate instead of crashing the whole optimization, and
                    # leave a trace in the progress stream so the failure is
                    # not silent.
                    if self.progress:
                        print(
                            f"  skipping {candidate.candidate_id}: {error}",
                            flush=True,
                        )
                    continue
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
                    "hierarchically maximize robust validation return, then reduce "
                    "drawdown and trades; penalize fold instability and "
                    "train-validation degradation; reject isolated parameter spikes"
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
            (
                item
                for item in evaluations
                if item.candidate.candidate_id == "baseline"
            ),
            None,
        )
        if baseline is None:
            # ``evaluate`` skips candidates that raise (see optimize); if the
            # baseline itself failed it is absent here. Surface the missing
            # baseline explicitly instead of an opaque StopIteration, since the
            # holdout comparison below depends on it.
            raise RuntimeError(
                "baseline candidate evaluation is required for the holdout "
                "comparison but was skipped after failing to evaluate"
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
        ordinary_return_ratio = (
            (1.0 + selected_test.total_return)
            / max(1.0 + baseline_test.total_return, 1e-12)
        )
        stress_return_ratio = (
            (1.0 + selected_stress_test.total_return)
            / max(1.0 + baseline_stress_test.total_return, 1e-12)
        )
        ordinary_trade_limit = math.ceil(baseline_test.total_trades * 1.03)
        stress_trade_limit = math.ceil(baseline_stress_test.total_trades * 1.03)
        promotion_checks = {
            "ordinary_drawdown_within_limit": (
                abs(selected_test.max_drawdown) <= self.drawdown_limit + 1e-12
            ),
            "stress_drawdown_within_limit": (
                abs(selected_stress_test.max_drawdown) <= self.drawdown_limit + 1e-12
            ),
            "ordinary_return_within_one_percent_of_baseline_wealth": (
                ordinary_return_ratio >= 0.99 - 1e-12
            ),
            "stress_return_within_one_percent_of_baseline_wealth": (
                stress_return_ratio >= 0.99 - 1e-12
            ),
            "ordinary_drawdown_not_meaningfully_worse": (
                abs(selected_test.max_drawdown)
                <= abs(baseline_test.max_drawdown) + 0.005 + 1e-12
            ),
            "stress_drawdown_not_meaningfully_worse": (
                abs(selected_stress_test.max_drawdown)
                <= abs(baseline_stress_test.max_drawdown) + 0.005 + 1e-12
            ),
            "ordinary_trades_not_meaningfully_higher": (
                selected_test.total_trades <= ordinary_trade_limit
                or ordinary_return_ratio >= 1.05
            ),
            "stress_trades_not_meaningfully_higher": (
                selected_stress_test.total_trades <= stress_trade_limit
                or stress_return_ratio >= 1.05
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
                        "holdout and stressed holdout must respect the hard drawdown "
                        "limit; wealth may trail baseline by at most 1%, drawdown by "
                        "at most 0.5 percentage point, and trades by at most 3% unless "
                        "wealth improves by at least 5%"
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
                    "delta_total_trades": (
                        selected_test.total_trades - baseline_test.total_trades
                    ),
                    "stress_delta_total_trades": (
                        selected_stress_test.total_trades
                        - baseline_stress_test.total_trades
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
