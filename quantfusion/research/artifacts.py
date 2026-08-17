"""Strict optimizer reports, resume validation, and cache signatures."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from quantfusion.research.candidates import WalkForwardFold, canonical_json
from quantfusion.research.catalog import LocalDataCatalog
from quantfusion.research.evaluation import CandidateEvaluation
from quantfusion.research.fingerprints import (
    engine_source_sha,
    optimizer_source_sha,
    replay_source_sha,
)

_canonical_json = canonical_json

def _format_pct(value: float) -> str:
    return f"{value:.2%}"


def _atomic_text(path: Path, content: str) -> None:
    """Replace an artifact only after its complete bytes reach disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.stem}-", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary_name).replace(path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


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
                f"| production baseline | {_format_pct(baseline['total_return'])} | "
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
                else "Promotion gate: failed; the production baseline is retained."
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
    regime_data_fingerprint: str,
) -> dict[str, CandidateEvaluation]:
    """Load a prior report only when its data and selection protocol match."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("engine") != "Quant Fusion":
        raise ValueError("Resume report was produced by a different engine")
    metadata = payload.get("run_metadata", {})
    if metadata.get("symbols") != symbols:
        raise ValueError("Resume report uses a different symbol universe")
    if metadata.get("data_coverage") != catalog.coverage():
        raise ValueError("Resume report uses a different data snapshot")
    if metadata.get("data_fingerprint") != catalog.fingerprint:
        raise ValueError("Resume report data bytes do not match the current snapshot")
    if metadata.get("regime_data_fingerprint") != regime_data_fingerprint:
        raise ValueError("Resume report regime index bytes do not match the snapshot")
    current_engine_sha = engine_source_sha()
    if metadata.get("engine_sha256") != current_engine_sha:
        raise ValueError("Resume report uses different execution code")
    current_replay_sha = replay_source_sha()
    if metadata.get("production_replay_sha256") != current_replay_sha:
        raise ValueError("Resume report uses different production replay code")
    current_optimizer_sha = optimizer_source_sha()
    if metadata.get("optimizer_sha256") != current_optimizer_sha:
        raise ValueError("Resume report uses different optimizer code")
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
    regime_data_fingerprint: str,
    indicator_state: str = "warm",
    warmup_calendar_days: int = 365,
) -> str:
    """Bind automatic cache reuse to code, data, folds, capital, and limits."""
    payload = {
        "engine_sha256": engine_source_sha(),
        "optimizer_sha256": optimizer_source_sha(),
        "production_replay_sha256": replay_source_sha(),
        "data_fingerprint": catalog.fingerprint,
        "regime_data_fingerprint": regime_data_fingerprint,
        "symbols": symbols,
        "folds": [
            {"train": asdict(fold.train), "validation": asdict(fold.validation)}
            for fold in folds
        ],
        "drawdown_limit": drawdown_limit,
        "initial_capital": initial_capital,
        "indicator_state": indicator_state,
        "warmup_calendar_days": warmup_calendar_days,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()[:20]


atomic_text = _atomic_text
load_resume_evaluations = _load_resume_evaluations
cache_signature = _cache_signature
