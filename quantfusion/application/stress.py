#!/usr/bin/env python3
"""Production-replay stress tests for universe composition and ordering."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import os
import random
import tempfile
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any

from quantfusion.application import engine_api as qf
from quantfusion.application import regime_api as ra
from quantfusion.config.paths import (
    MARKET_DATA_DIR,
    PROJECT_ROOT,
    REGIME_DATA_DIR,
    VALIDATION_ARTIFACT_DIR,
    resolve_repository_data_dir,
)
from quantfusion.config.universe import SYMBOL_NAMES as NAMES


DATA_DIR = MARKET_DATA_DIR
START_DATE = "2025-04-01"
END_DATE = "2026-07-20"
INITIAL_CAPITAL = 2_000_000.0
ORDERED_CODES = tuple(NAMES)
DEFAULT_SEEDS = (20260807, 20260817, 20260827)
ATTRIBUTION_CATEGORIES = (
    "initial_entry",
    "add",
    "re_entry",
    "risk_reduction",
    "strategy_exit",
    "sector_liquidation",
    "route_migration",
    "sticky_replacement",
)


def _artifact_path(path: Path) -> str:
    """Prefer a portable repository-relative path in persisted artifacts."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _reason_category(trade: qf.TradeRecord) -> str:
    """Map free-text execution reasons into stable audit categories."""
    reason = str(trade.reason).lower().replace("-", "_")
    if "sticky" in reason or "rotation" in reason or "replacement" in reason:
        return "sticky_replacement"
    if "route" in reason or "migration" in reason:
        return "route_migration"
    if trade.direction == "buy":
        if "re_entry" in reason or "reentry" in reason or "probe" in reason:
            return "re_entry"
        if "add" in reason or "pyramid" in reason or "confirm" in reason:
            return "add"
        return "initial_entry"
    if "sector" in reason:
        return "sector_liquidation"
    if any(
        token in reason
        for token in (
            "risk",
            "drawdown",
            "stop",
            "reduction",
            "choppy",
            "transition",
            "catastrophe",
            "trim",
        )
    ):
        return "risk_reduction"
    return "strategy_exit"


def _metrics(
    codes: tuple[str, ...],
    *,
    data_dir: str | Path = DATA_DIR,
    regime_data_dir: str | Path = REGIME_DATA_DIR,
) -> dict[str, Any]:
    with contextlib.redirect_stdout(io.StringIO()):
        result = ra.ProductionReplayEngine(INITIAL_CAPITAL).run(
            {code: NAMES[code] for code in codes},
            START_DATE,
            END_DATE,
            data_dir=str(data_dir),
            regime_data_dir=str(regime_data_dir),
            indicator_state="warm",
        )
    attribution = {category: 0 for category in ATTRIBUTION_CATEGORIES}
    for trade in result["trades"]:
        attribution[_reason_category(trade)] += 1
    return {
        "symbol_count": len(codes),
        "symbols": list(codes),
        "total_return": float(result["total_return"]),
        "max_drawdown": float(result["max_drawdown"]),
        "sharpe": float(result["sharpe"]),
        "calmar": float(result["calmar"]),
        "total_trades": int(result["total_trades"]),
        "sleeve_fill_count": int(result["sleeve_fill_count"]),
        "reason_attribution": attribution,
        "max_concurrent_symbols": int(result["max_concurrent_symbols"]),
        "terminal_risk_lock": bool(result["terminal_risk_lock"]),
        "deployment_policy": str(result["deployment_policy"]),
    }


def _run_scenario(
    scenario: dict[str, Any],
    *,
    data_dir: str | Path = DATA_DIR,
    regime_data_dir: str | Path = REGIME_DATA_DIR,
) -> dict[str, Any]:
    codes = tuple(str(code) for code in scenario["symbols"])
    return {
        **scenario,
        **_metrics(codes, data_dir=data_dir, regime_data_dir=regime_data_dir),
    }


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    location = (len(ordered) - 1) * probability
    lower = int(location)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = location - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [float(item["total_return"]) for item in results]
    drawdowns = [float(item["max_drawdown"]) for item in results]
    trades = [float(item["total_trades"]) for item in results]
    fills = [float(item.get("sleeve_fill_count", 0)) for item in results]
    severities = [abs(value) for value in drawdowns]
    risk_actions = [
        float(
            item.get("reason_attribution", {}).get("risk_reduction", 0)
            + item.get("reason_attribution", {}).get("sector_liquidation", 0)
        )
        for item in results
    ]
    return {
        "scenario_count": len(results),
        "return_median": _quantile(returns, 0.50),
        "return_p10": _quantile(returns, 0.10),
        "return_p05": _quantile(returns, 0.05),
        "return_worst": min(returns) if returns else 0.0,
        "drawdown_median": _quantile(drawdowns, 0.50),
        "drawdown_p10": _quantile(drawdowns, 0.10),
        "drawdown_p50_severity": _quantile(severities, 0.50),
        "drawdown_p90_severity": _quantile(severities, 0.90),
        "drawdown_p95_severity": _quantile(severities, 0.95),
        "drawdown_worst": min(drawdowns) if drawdowns else 0.0,
        "trades_median": _quantile(trades, 0.50),
        "trades_p90": _quantile(trades, 0.90),
        "trades_worst": max(trades) if trades else 0.0,
        "sleeve_fills_p90": _quantile(fills, 0.90),
        "sleeve_fills_worst": max(fills) if fills else 0.0,
        "risk_action_orders_median": _quantile(risk_actions, 0.50),
    }


def _scenarios(
    *,
    random_samples: int,
    permutation_samples: int,
    seed: int,
    include_fixed: bool = True,
) -> list[dict[str, Any]]:
    """Build one deterministic seed block; fixed scenarios are optional."""
    if random_samples < 1 or permutation_samples < 1:
        raise ValueError("sample counts must be positive")
    smallest_capacity = min(
        math.comb(len(ORDERED_CODES), size) for size in (3, 5, 8, 12, 16)
    )
    if random_samples > smallest_capacity:
        raise ValueError(
            f"random_samples exceeds unique subset capacity {smallest_capacity}"
        )
    if permutation_samples > math.factorial(len(ORDERED_CODES)):
        raise ValueError("permutation_samples exceeds unique ordering capacity")
    rng = random.Random(seed)
    scenarios: list[dict[str, Any]] = []
    if include_fixed:
        for count in range(1, len(ORDERED_CODES) + 1):
            scenarios.append(
                {
                    "scenario_id": f"prefix-{count:02d}",
                    "scenario_type": "prefix",
                    "symbols": list(ORDERED_CODES[:count]),
                }
            )
        for omitted in ORDERED_CODES:
            scenarios.append(
                {
                    "scenario_id": f"leave-one-out-{omitted}",
                    "scenario_type": "leave_one_out",
                    "omitted_symbol": omitted,
                    "symbols": [code for code in ORDERED_CODES if code != omitted],
                }
            )
        for base_size in (5, 9, 13):
            base = ORDERED_CODES[:base_size]
            for added in ORDERED_CODES[base_size:]:
                scenarios.append(
                    {
                        "scenario_id": f"add-one-{base_size:02d}-{added}",
                        "scenario_type": "add_one",
                        "base_size": base_size,
                        "added_symbol": added,
                        "symbols": [*base, added],
                    }
                )
    for size in (3, 5, 8, 12, 16):
        seen: set[tuple[str, ...]] = set()
        while len(seen) < random_samples:
            seen.add(tuple(sorted(rng.sample(ORDERED_CODES, size))))
        for index, subset in enumerate(sorted(seen), start=1):
            scenarios.append(
                {
                    "scenario_id": f"random-{seed}-{size:02d}-{index:03d}",
                    "scenario_type": "random_subset",
                    "seed": seed,
                    "sample_size": size,
                    "symbols": list(subset),
                }
            )
    permutations: set[tuple[str, ...]] = set()
    while len(permutations) < permutation_samples:
        sample = list(ORDERED_CODES)
        rng.shuffle(sample)
        permutations.add(tuple(sample))
    for index, ordering in enumerate(sorted(permutations), start=1):
        scenarios.append(
            {
                "scenario_id": f"permutation-{seed}-{index:03d}",
                "scenario_type": "permutation",
                "seed": seed,
                "symbols": list(ordering),
            }
        )
    return scenarios


def _multi_seed_scenarios(
    *, random_samples: int, permutation_samples: int, seeds: tuple[int, ...]
) -> list[dict[str, Any]]:
    if not seeds or len(set(seeds)) != len(seeds):
        raise ValueError("stress seeds must be non-empty and unique")
    scenarios: list[dict[str, Any]] = []
    for index, seed in enumerate(seeds):
        scenarios.extend(
            _scenarios(
                random_samples=random_samples,
                permutation_samples=permutation_samples,
                seed=seed,
                include_fixed=index == 0,
            )
        )
    return scenarios


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
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


def _tree_fingerprint(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: str(item)):
        try:
            label = path.relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            label = path.resolve().as_posix()
        digest.update(label.encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _run_signature(
    scenarios: list[dict[str, Any]], data_dir: Path, regime_data_dir: Path
) -> str:
    source_files = list(PROJECT_ROOT.glob("*.py")) + list(
        (PROJECT_ROOT / "quantfusion").rglob("*.py")
    )
    data_files = list(data_dir.glob("*.csv")) + list(regime_data_dir.glob("*.csv"))
    payload = {
        "source_fingerprint": _tree_fingerprint(source_files),
        "data_fingerprint": _tree_fingerprint(data_files),
        "scenarios": scenarios,
        "start_date": START_DATE,
        "end_date": END_DATE,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _validated_checkpoint_results(
    payload: dict[str, Any], scenarios: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Reject stale, duplicated, malformed, or scenario-mismatched progress."""
    raw_results = payload.get("results", [])
    if not isinstance(raw_results, list):
        raise ValueError("Stress checkpoint results must be a list")
    expected = {str(item["scenario_id"]): item for item in scenarios}
    completed: dict[str, dict[str, Any]] = {}
    for item in raw_results:
        if not isinstance(item, dict):
            raise ValueError("Stress checkpoint result must be an object")
        scenario_id = str(item.get("scenario_id", ""))
        if scenario_id not in expected:
            raise ValueError(f"Stress checkpoint contains unknown scenario {scenario_id}")
        if scenario_id in completed:
            raise ValueError(f"Stress checkpoint duplicates scenario {scenario_id}")
        for key, value in expected[scenario_id].items():
            if item.get(key) != value:
                raise ValueError(
                    f"Stress checkpoint scenario definition changed: {scenario_id}"
                )
        for key in ("total_return", "max_drawdown", "sharpe", "calmar"):
            value = item.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"Stress checkpoint {scenario_id} has invalid {key}")
            if not math.isfinite(float(value)):
                raise ValueError(f"Stress checkpoint {scenario_id} has non-finite {key}")
        for key in ("total_trades", "sleeve_fill_count"):
            value = item.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"Stress checkpoint {scenario_id} has invalid {key}")
        if item.get("deployment_policy") != "production_daily_replay":
            raise ValueError(
                f"Stress checkpoint {scenario_id} was not a production replay"
            )
        completed[scenario_id] = item
    return completed


def _wealth_change(result: dict[str, Any], base: dict[str, Any]) -> float:
    return (1.0 + float(result["total_return"])) / (
        1.0 + float(base["total_return"])
    ) - 1.0


def _hard_gates(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {str(item["scenario_id"]): item for item in results}
    random_results = [
        item for item in results if item["scenario_type"] == "random_subset"
    ]
    add_one = [item for item in results if item["scenario_type"] == "add_one"]
    prefixes = sorted(
        (item for item in results if item["scenario_type"] == "prefix"),
        key=lambda item: int(item["symbol_count"]),
    )
    random_summary = _summary(random_results)
    all_summary = _summary(results)
    add_one_changes = [
        _wealth_change(item, by_id[f"prefix-{int(item['base_size']):02d}"])
        for item in add_one
    ]
    nine_to_ten = _wealth_change(by_id["prefix-10"], by_id["prefix-09"])
    adjacent_changes = [
        _wealth_change(right, left)
        for left, right in zip(prefixes, prefixes[1:], strict=False)
    ]
    checks = {
        "random_p90_drawdown_at_most_20pct": (
            random_summary["drawdown_p90_severity"] <= 0.20 + 1e-12
        ),
        "random_worst_drawdown_at_most_22pct": (
            abs(random_summary["drawdown_worst"]) <= 0.22 + 1e-12
        ),
        "all_worst_drawdown_at_most_22_5pct": (
            abs(all_summary["drawdown_worst"]) <= 0.225 + 1e-12
        ),
        "prefix_9_to_10_wealth_above_minus_10pct": nine_to_ten > -0.10,
        "worst_adjacent_wealth_at_least_minus_30pct": (
            min(adjacent_changes) >= -0.30 - 1e-12
        ),
        "worst_add_one_wealth_at_least_minus_18pct": (
            min(add_one_changes) >= -0.18 - 1e-12
        ),
        "random_p90_account_orders_at_most_160": (
            random_summary["trades_p90"] <= 160
        ),
        "all_account_orders_at_most_200": all_summary["trades_worst"] <= 200,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "observed": {
            "random_p90_drawdown": random_summary["drawdown_p90_severity"],
            "random_worst_drawdown": random_summary["drawdown_worst"],
            "all_worst_drawdown": all_summary["drawdown_worst"],
            "prefix_9_to_10_wealth_change": nine_to_ten,
            "worst_adjacent_wealth_change": min(adjacent_changes),
            "worst_add_one_wealth_change": min(add_one_changes),
            "random_p90_account_orders": random_summary["trades_p90"],
            "all_worst_account_orders": all_summary["trades_worst"],
        },
    }


# ── 2026-08-16 报告 P0-4: promotion gates vs the incumbent formal stress artifact ──
# 任何 cross-market / risk 改动晋级前，除绝对硬门外，还必须相对既有正式
# universe stress 基线满足以下"不大幅恶化"契约（2026-08-16 报告 P0-4 建议门槛
# 方向：固定牛市财富 ≥99%、random DD P90 不恶化、worst DD 不显著恶化、false
# risk action 数不增加、account orders 不明显增加）。注意：此处 P0-4 指
# 2026-08-16 报告，与 2026-08-07 旧报告中的 P0-4（灾变冷却阻断再入场）不同。
PROMOTION_PREFIX_WEALTH_RATIO = 0.99
PROMOTION_DD_P90_TOLERANCE = 0.005
PROMOTION_DD_P95_TOLERANCE = 0.005
PROMOTION_WORST_DD_TOLERANCE = 0.010
PROMOTION_WORST_RETURN_TOLERANCE = 0.020
PROMOTION_ADD_ONE_TOLERANCE = 0.030
PROMOTION_TRADES_P90_TOLERANCE = 5.0
PROMOTION_TRADES_WORST_TOLERANCE = 10.0
PROMOTION_RISK_ACTION_TOLERANCE = 2.0


def _permutation_invariance(results: list[dict[str, Any]]) -> dict[str, Any]:
    """同一 seed 的全排列场景必须产生完全一致的指标（Gate C）。"""
    groups: dict[int, list[dict[str, Any]]] = {}
    for item in results:
        if item.get("scenario_type") != "permutation":
            continue
        groups.setdefault(int(item.get("seed", 0)), []).append(item)
    deviations: list[float] = []
    for members in groups.values():
        for left, right in zip(members, members[1:], strict=False):
            deviations.extend(
                (
                    abs(float(left["total_return"]) - float(right["total_return"])),
                    abs(
                        float(left["max_drawdown"])
                        - float(right["max_drawdown"])
                    ),
                    abs(float(left["total_trades"]) - float(right["total_trades"])),
                )
            )
    worst = max(deviations) if deviations else 0.0
    return {
        "checked_groups": len(groups),
        "worst_deviation": worst,
        "invariant": worst <= 1e-12,
    }


def _promotion_gates(
    results: list[dict[str, Any]], incumbent: dict[str, Any] | None
) -> dict[str, Any]:
    """相对既有基线的强制晋级门（2026-08-16 报告 P0-4），无基线时仅建立基准。"""
    permutation = _permutation_invariance(results)
    payload: dict[str, Any] = {
        "baseline": "incumbent_universe_stress",
        "permutation_invariance": permutation,
    }
    if not incumbent or not isinstance(incumbent.get("results"), list):
        payload.update(
            {
                "status": "no_incumbent_baseline",
                "passed": permutation["invariant"],
                "note": (
                    "未找到既有正式 universe stress 基线 — 本次运行仅建立"
                    "基线并检查排列不变性。"
                ),
            }
        )
        return payload
    by_id = {
        str(item.get("scenario_id")): item for item in incumbent["results"]
    }
    current_by_id = {str(item["scenario_id"]): item for item in results}

    def _family(family: str, source: dict[str, dict[str, Any]]) -> list[dict]:
        return [
            item
            for item in source.values()
            if item.get("scenario_type") == family
        ]

    def _summary_or_empty(family: str, source: dict[str, dict[str, Any]]) -> dict:
        family_results = _family(family, source)
        return _summary(family_results) if family_results else {}

    prefix_ratios = [
        (1.0 + float(item["total_return"]))
        / (1.0 + float(by_id[sid]["total_return"]))
        for sid, item in current_by_id.items()
        if item.get("scenario_type") == "prefix" and sid in by_id
    ]
    cur_random = _summary_or_empty("random_subset", current_by_id)
    inc_random = _summary_or_empty("random_subset", by_id)
    cur_loo = _summary_or_empty("leave_one_out", current_by_id)
    inc_loo = _summary_or_empty("leave_one_out", by_id)
    # ``all_*`` gates must compare the same scenario set on both sides, so
    # aggregate only scenarios present in both the current and incumbent runs
    # (random subsets may legitimately differ across runs by seed).
    shared_ids = sorted(sid for sid in current_by_id if sid in by_id)
    cur_all = _summary([current_by_id[sid] for sid in shared_ids])
    inc_all = _summary([by_id[sid] for sid in shared_ids])

    def _add_one_min(source: dict[str, dict[str, Any]]) -> float | None:
        pairs = [
            _wealth_change(item, source[f"prefix-{int(item['base_size']):02d}"])
            for item in _family("add_one", source)
            if f"prefix-{int(item['base_size']):02d}" in source
        ]
        return min(pairs) if pairs else None

    cur_add_one = _add_one_min(current_by_id)
    inc_add_one = _add_one_min(by_id)

    checks: dict[str, bool] = {
        "permutation_invariant": permutation["invariant"],
    }
    observed: dict[str, Any] = {
        "prefix_wealth_ratio_min": (
            min(prefix_ratios) if prefix_ratios else None
        ),
    }
    if prefix_ratios:
        checks["fixed_prefix_wealth_at_least_99pct"] = (
            min(prefix_ratios) >= PROMOTION_PREFIX_WEALTH_RATIO - 1e-12
        )
    if cur_random and inc_random:
        checks["random_dd_p90_not_worse"] = (
            cur_random["drawdown_p90_severity"]
            <= inc_random["drawdown_p90_severity"] + PROMOTION_DD_P90_TOLERANCE
        )
        observed["random_dd_p90"] = cur_random["drawdown_p90_severity"]
        checks["random_dd_p95_not_worse"] = (
            cur_random["drawdown_p95_severity"]
            <= inc_random["drawdown_p95_severity"] + PROMOTION_DD_P95_TOLERANCE
        )
        observed["random_dd_p95"] = cur_random["drawdown_p95_severity"]
        checks["random_worst_return_not_worse"] = (
            cur_random["return_worst"]
            >= inc_random["return_worst"] - PROMOTION_WORST_RETURN_TOLERANCE
        )
        observed["random_worst_return"] = cur_random["return_worst"]
        checks["random_trades_p90_not_increased"] = (
            cur_random["trades_p90"]
            <= inc_random["trades_p90"] + PROMOTION_TRADES_P90_TOLERANCE
        )
        observed["random_trades_p90"] = cur_random["trades_p90"]
        checks["random_risk_actions_not_increased"] = (
            cur_random["risk_action_orders_median"]
            <= inc_random["risk_action_orders_median"]
            + PROMOTION_RISK_ACTION_TOLERANCE
        )
        observed["random_risk_action_orders_median"] = cur_random[
            "risk_action_orders_median"
        ]
    checks["all_worst_dd_not_significantly_worse"] = (
        cur_all["drawdown_worst"]
        >= inc_all["drawdown_worst"] - PROMOTION_WORST_DD_TOLERANCE
    )
    observed["all_worst_dd"] = cur_all["drawdown_worst"]
    checks["all_worst_trades_not_increased"] = (
        cur_all["trades_worst"]
        <= inc_all["trades_worst"] + PROMOTION_TRADES_WORST_TOLERANCE
    )
    observed["all_worst_trades"] = cur_all["trades_worst"]
    if cur_loo and inc_loo:
        checks["leave_one_out_worst_return_not_worse"] = (
            cur_loo["return_worst"]
            >= inc_loo["return_worst"] - PROMOTION_WORST_RETURN_TOLERANCE
        )
        observed["leave_one_out_worst_return"] = cur_loo["return_worst"]
    if cur_add_one is not None and inc_add_one is not None:
        checks["add_one_discontinuity_not_worse"] = (
            cur_add_one >= inc_add_one - PROMOTION_ADD_ONE_TOLERANCE
        )
        observed["worst_add_one_wealth_change"] = cur_add_one
    payload.update(
        {
            "status": "compared",
            "incumbent_scenario_count": len(by_id),
            "shared_scenario_count": sum(
                1 for sid in current_by_id if sid in by_id
            ),
            "tolerances": {
                "prefix_wealth_ratio": PROMOTION_PREFIX_WEALTH_RATIO,
                "dd_p90": PROMOTION_DD_P90_TOLERANCE,
                "dd_p95": PROMOTION_DD_P95_TOLERANCE,
                "worst_dd": PROMOTION_WORST_DD_TOLERANCE,
                "worst_return": PROMOTION_WORST_RETURN_TOLERANCE,
                "add_one_wealth": PROMOTION_ADD_ONE_TOLERANCE,
                "trades_p90": PROMOTION_TRADES_P90_TOLERANCE,
                "trades_worst": PROMOTION_TRADES_WORST_TOLERANCE,
                "risk_action_median": PROMOTION_RISK_ACTION_TOLERANCE,
            },
            "passed": all(checks.values()),
            "checks": checks,
            "observed": observed,
        }
    )
    return payload


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--random-samples", type=int, default=50)
    parser.add_argument("--permutation-samples", type=int, default=50)
    parser.add_argument(
        "--seeds",
        default=",".join(str(seed) for seed in DEFAULT_SEEDS),
        help="Comma-separated deterministic seeds",
    )
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    parser.add_argument("--regime-data-dir", default=str(REGIME_DATA_DIR))
    parser.add_argument(
        "--checkpoint",
        default=str(PROJECT_ROOT / "artifacts" / "checkpoints" / "stress.json"),
    )
    parser.add_argument("--checkpoint-every", type=int, default=10)
    return parser


def main() -> int:
    """Run, checkpoint, gate, and atomically publish the formal audit."""
    args = build_argument_parser().parse_args()
    seeds = tuple(int(value.strip()) for value in args.seeds.split(",") if value.strip())
    if (
        args.workers < 1
        or args.random_samples < 1
        or args.permutation_samples < 1
        or args.checkpoint_every < 1
        or not seeds
    ):
        raise ValueError("workers, sample counts, checkpoint interval and seeds must be positive")
    data_dir = resolve_repository_data_dir(args.data_dir).resolve()
    regime_data_dir = resolve_repository_data_dir(args.regime_data_dir).resolve()
    missing_stock = [
        code
        for code in ORDERED_CODES
        if not (data_dir / f"{code}.csv").is_file()
    ]
    missing_regime = [
        code
        for code in ra.REGIME_INDEX_FILES.values()
        if not (regime_data_dir / f"{code}.csv").is_file()
    ]
    if missing_stock or missing_regime:
        raise ValueError(
            "Stress data snapshot is incomplete "
            f"(stocks={missing_stock}, regime_indices={missing_regime})"
        )
    scenarios = _multi_seed_scenarios(
        random_samples=args.random_samples,
        permutation_samples=args.permutation_samples,
        seeds=seeds,
    )
    signature = _run_signature(scenarios, data_dir, regime_data_dir)
    checkpoint = Path(args.checkpoint)
    completed: dict[str, dict[str, Any]] = {}
    if checkpoint.is_file():
        payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        if payload.get("signature") != signature:
            raise ValueError("Stress checkpoint code, data, or scenario signature changed")
        completed = _validated_checkpoint_results(payload, scenarios)
    pending = [
        scenario
        for scenario in scenarios
        if str(scenario["scenario_id"]) not in completed
    ]
    worker = partial(
        _run_scenario, data_dir=data_dir, regime_data_dir=regime_data_dir
    )
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        for start in range(0, len(pending), args.checkpoint_every):
            chunk = pending[start : start + args.checkpoint_every]
            for result in executor.map(worker, chunk):
                completed[str(result["scenario_id"])] = result
            _atomic_json(
                checkpoint,
                {
                    "signature": signature,
                    "completed": len(completed),
                    "scenario_count": len(scenarios),
                    "results": sorted(
                        completed.values(), key=lambda item: item["scenario_id"]
                    ),
                },
            )
            print(f"checkpoint {len(completed)}/{len(scenarios)}", flush=True)
    results = sorted(completed.values(), key=lambda item: item["scenario_id"])
    prefixes = sorted(
        (item for item in results if item["scenario_type"] == "prefix"),
        key=lambda item: item["symbol_count"],
    )
    wealth = [1.0 + item["total_return"] for item in prefixes]
    adjacent = [
        {
            "from_count": left["symbol_count"],
            "to_count": right["symbol_count"],
            "wealth_change": _wealth_change(right, left),
        }
        for left, right in zip(prefixes, prefixes[1:], strict=False)
    ]
    common = {
        "engine": "ProductionReplayEngine",
        "deployment_policy": "production_daily_replay",
        "portfolio_policy": qf.PortfolioPolicy().as_dict(),
        "data_directory": _artifact_path(data_dir),
        "regime_data_directory": _artifact_path(regime_data_dir),
        "start_date": START_DATE,
        "end_date": END_DATE,
        "indicator_state": "warm",
        "seeds": list(seeds),
        "run_signature": signature,
    }
    prefix_artifact = {
        **common,
        "ordering": list(ORDERED_CODES),
        "minimum_to_maximum_wealth_ratio": min(wealth) / max(wealth),
        "worst_adjacent_transition": min(
            adjacent, key=lambda item: item["wealth_change"]
        ),
        "summary": _summary(prefixes),
        "results": prefixes,
    }
    by_type = {
        scenario_type: _summary(
            [item for item in results if item["scenario_type"] == scenario_type]
        )
        for scenario_type in sorted({item["scenario_type"] for item in results})
    }
    gates = _hard_gates(results)
    # 2026-08-16 报告 P0-4: 在覆盖正式工件之前加载既有基线，评估强制晋级门。
    incumbent_path = VALIDATION_ARTIFACT_DIR / "universe_stress.json"
    incumbent: dict[str, Any] | None = None
    if incumbent_path.is_file():
        try:
            incumbent = json.loads(incumbent_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"⚠ 无法读取既有基线 universe_stress.json: {exc}")
    promotion = _promotion_gates(results, incumbent)
    universe_artifact = {
        **common,
        "scenario_count": len(results),
        "random_samples_per_size_per_seed": args.random_samples,
        "permutation_samples_per_seed": args.permutation_samples,
        "hard_gates": gates,
        "promotion_gates": promotion,
        "summary": {"all": _summary(results), "by_type": by_type},
        "results": results,
    }
    _atomic_json(VALIDATION_ARTIFACT_DIR / "prefix_stress.json", prefix_artifact)
    _atomic_json(VALIDATION_ARTIFACT_DIR / "universe_stress.json", universe_artifact)
    print(
        json.dumps(
            {
                "hard_gates": gates,
                "promotion_gates": promotion,
                "summary": universe_artifact["summary"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if gates["passed"] and promotion["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
