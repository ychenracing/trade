#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate v16 against explicit v15 wealth and drawdown protection gates."""

from __future__ import annotations

import contextlib
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import codex_quant_fusion_v15 as v15
import codex_quant_fusion_v16 as v16


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "market_data_qfq"
TARGET_START = "2025-04-01"
TARGET_END = "2026-07-20"
WEAK_START = "2024-01-02"
WEAK_END = "2025-03-31"


@dataclass(frozen=True)
class UpgradeThresholds:
    """Define warnings and hard rejection gates for a strategy upgrade."""

    wealth_warning_ratio: float = 0.98
    wealth_rejection_ratio: float = 0.95
    target_drawdown_tolerance: float = 0.01
    high_cost_return_floor: float = 9.0
    weak_return_floor: float = 0.0
    weak_drawdown_floor: float = -0.18


def _run_v15(indicator_state: str) -> dict:
    """Run one quiet version 15 baseline for an apples-to-apples comparison."""
    engine = v15.BacktestEngine(2_000_000)
    with contextlib.redirect_stdout(io.StringIO()):
        return engine.run(
            v15.DEFAULT_SYMBOLS,
            TARGET_START,
            TARGET_END,
            data_dir=str(DATA_DIR),
            indicator_state=indicator_state,
        )


def _run_v16(
    start: str,
    end: str,
    *,
    indicator_state: str = "cold",
    slippage: float = 0.001,
) -> dict:
    """Run one quiet version 16 validation scenario."""
    engine = v16.BacktestEngine(2_000_000, cfg={"slippage": slippage})
    with contextlib.redirect_stdout(io.StringIO()):
        return engine.run(
            v16.DEFAULT_SYMBOLS,
            start,
            end,
            data_dir=str(DATA_DIR),
            indicator_state=indicator_state,
        )


def _metric_snapshot(result: dict) -> dict[str, float]:
    """Keep the validation artifact compact and JSON serializable."""
    return {
        "final_assets": float(result["final_assets"]),
        "total_return": float(result["total_return"]),
        "max_drawdown": float(result["max_drawdown"]),
        "sharpe": float(result["sharpe"]),
    }


def evaluate_upgrade(
    scenarios: dict[str, dict],
    thresholds: UpgradeThresholds | None = None,
) -> dict[str, Any]:
    """Evaluate hard gates and warnings without running any backtests."""
    limits = thresholds or UpgradeThresholds()
    checks: list[dict[str, Any]] = []

    def add_check(name: str, actual: float, minimum: float) -> None:
        checks.append(
            {
                "name": name,
                "actual": float(actual),
                "minimum": float(minimum),
                "passed": bool(actual >= minimum),
            }
        )

    for state in ("cold", "warm"):
        baseline = scenarios[f"v15_{state}"]
        candidate = scenarios[f"v16_{state}"]
        ratio = float(candidate["final_assets"] / baseline["final_assets"])
        add_check(
            f"{state}_wealth_rejection_gate",
            ratio,
            limits.wealth_rejection_ratio,
        )
        add_check(
            f"{state}_target_drawdown_gate",
            float(candidate["max_drawdown"]),
            float(baseline["max_drawdown"] - limits.target_drawdown_tolerance),
        )
        checks.append(
            {
                "name": f"{state}_wealth_warning_gate",
                "actual": ratio,
                "minimum": limits.wealth_warning_ratio,
                "passed": bool(ratio >= limits.wealth_warning_ratio),
                "warning_only": True,
            }
        )
    add_check(
        "high_cost_return_gate",
        float(scenarios["v16_high_cost"]["total_return"]),
        limits.high_cost_return_floor,
    )
    add_check(
        "weak_return_gate",
        float(scenarios["v16_weak"]["total_return"]),
        limits.weak_return_floor,
    )
    add_check(
        "weak_drawdown_gate",
        float(scenarios["v16_weak"]["max_drawdown"]),
        limits.weak_drawdown_floor,
    )
    hard_failures = [
        check
        for check in checks
        if not check["passed"] and not check.get("warning_only", False)
    ]
    warnings = [
        check
        for check in checks
        if not check["passed"] and check.get("warning_only", False)
    ]
    return {
        "approved": not hard_failures,
        "checks": checks,
        "hard_failures": hard_failures,
        "warnings": warnings,
    }


def run_validation() -> dict[str, Any]:
    """Run all required scenarios and return a complete validation artifact."""
    scenarios = {
        "v15_cold": _run_v15("cold"),
        "v15_warm": _run_v15("warm"),
        "v16_cold": _run_v16(TARGET_START, TARGET_END, indicator_state="cold"),
        "v16_warm": _run_v16(TARGET_START, TARGET_END, indicator_state="warm"),
        "v16_high_cost": _run_v16(
            TARGET_START, TARGET_END, indicator_state="cold", slippage=0.005
        ),
        "v16_weak": _run_v16(WEAK_START, WEAK_END, indicator_state="cold"),
    }
    evaluation = evaluate_upgrade(scenarios)
    evaluation["scenarios"] = {
        name: _metric_snapshot(result) for name, result in scenarios.items()
    }
    return evaluation


def main() -> int:
    """Print the validation artifact and return a shell-friendly status code."""
    validation = run_validation()
    print(json.dumps(validation, indent=2, sort_keys=True))
    return 0 if validation["approved"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
