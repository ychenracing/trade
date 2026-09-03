"""Stress result summaries, hard gates, and promotion evaluation."""

# Keep the established adjacent-pair formulas literal during decomposition.
# ruff: noqa: RUF007

from __future__ import annotations

from typing import Any

START_DATE = "2025-04-01"
END_DATE = "2026-07-20"
INITIAL_CAPITAL = 2_000_000.0
TRADE_COUNT_SEMANTICS = "trade_records"
ENGINE = "ProductionReplayEngine"
DEPLOYMENT_POLICY = "production_daily_replay"
STRESS_CONTRACT_VERSION = 2
MAX_ACCOUNT_DRAWDOWN = 0.18
# Exact 18% is valid; the tolerance only absorbs machine representation noise.
DRAWDOWN_COMPARISON_TOLERANCE = 1e-15
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
    side_buckets = [float(item["date_symbol_side_count"]) for item in results]
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
        "date_symbol_side_buckets_median": _quantile(side_buckets, 0.50),
        "date_symbol_side_buckets_p90": _quantile(side_buckets, 0.90),
        "date_symbol_side_buckets_worst": (max(side_buckets) if side_buckets else 0.0),
        "sleeve_fills_p90": _quantile(fills, 0.90),
        "sleeve_fills_worst": max(fills) if fills else 0.0,
        "risk_action_orders_median": _quantile(risk_actions, 0.50),
    }


def _wealth_change(result: dict[str, Any], base: dict[str, Any]) -> float:
    return (1.0 + float(result["total_return"])) / (
        1.0 + float(base["total_return"])
    ) - 1.0


def _absolute_hard_gates(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate absolute safety and retained accounting-correctness gates."""
    if not results:
        raise ValueError("Absolute hard gates require non-empty results")
    random_results = [
        item for item in results if item["scenario_type"] == "random_subset"
    ]
    random_summary = _summary(random_results)
    all_summary = _summary(results)
    worst = min(results, key=lambda item: float(item["max_drawdown"]), default=None)
    checks = {
        "all_scenarios_max_drawdown_at_most_18pct": (
            abs(float(worst["max_drawdown"]))
            <= MAX_ACCOUNT_DRAWDOWN + DRAWDOWN_COMPARISON_TOLERANCE
            if worst is not None
            else True
        ),
        "random_p90_date_symbol_side_buckets_at_most_160": (
            random_summary["date_symbol_side_buckets_p90"] <= 160
        ),
        "all_date_symbol_side_buckets_at_most_200": (
            all_summary["date_symbol_side_buckets_worst"] <= 200
        ),
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "observed": {
            "worst_scenario_id": (
                str(worst["scenario_id"]) if worst is not None else None
            ),
            "all_worst_drawdown": (
                float(worst["max_drawdown"]) if worst is not None else None
            ),
            "random_p90_date_symbol_side_buckets": random_summary[
                "date_symbol_side_buckets_p90"
            ],
            "all_worst_date_symbol_side_buckets": all_summary[
                "date_symbol_side_buckets_worst"
            ],
        },
    }


def _retained_robustness_hard_gates(
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Evaluate the pre-existing prefix discontinuity protections."""
    by_id = {str(item["scenario_id"]): item for item in results}
    prefixes = sorted(
        (item for item in results if item["scenario_type"] == "prefix"),
        key=lambda item: int(item["symbol_count"]),
    )
    if "prefix-09" not in by_id or "prefix-10" not in by_id or len(prefixes) < 2:
        raise ValueError("Retained robustness hard gates require prefix results")
    nine_to_ten = _wealth_change(by_id["prefix-10"], by_id["prefix-09"])
    adjacent_changes = [
        _wealth_change(right, left)
        for left, right in zip(prefixes, prefixes[1:], strict=False)
    ]
    worst_adjacent = min(adjacent_changes)
    checks = {
        "prefix_9_to_10_wealth_above_minus_10pct": nine_to_ten > -0.10,
        "worst_adjacent_wealth_at_least_minus_30pct": (
            worst_adjacent >= -0.30 - 1e-12
        ),
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "observed": {
            "prefix_9_to_10_wealth_change": nine_to_ten,
            "worst_adjacent_wealth_change": worst_adjacent,
        },
    }


def _terminal_lock_transition(base: object, result: object) -> str:
    return f"{str(bool(base)).lower()}_to_{str(bool(result)).lower()}"


def _robustness_diagnostics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe add-one discontinuities without turning them into hard gates."""
    by_id = {str(item["scenario_id"]): item for item in results}
    cases: list[dict[str, Any]] = []
    for result in results:
        if result.get("scenario_type") != "add_one":
            continue
        base_id = f"prefix-{int(result['base_size']):02d}"
        base = by_id.get(base_id)
        if base is None:
            continue
        cases.append(
            {
                "scenario_id": str(result["scenario_id"]),
                "base_scenario_id": base_id,
                "wealth_change": _wealth_change(result, base),
                "drawdown_severity_change": abs(float(result["max_drawdown"]))
                - abs(float(base["max_drawdown"])),
                "trade_record_delta": int(result["total_trades"])
                - int(base["total_trades"]),
                "date_symbol_side_bucket_delta": int(
                    result["date_symbol_side_count"]
                )
                - int(base["date_symbol_side_count"]),
                "terminal_risk_lock_transition": _terminal_lock_transition(
                    base.get("terminal_risk_lock", False),
                    result.get("terminal_risk_lock", False),
                ),
                "reason_attribution": result.get("reason_attribution", {}),
            }
        )
    wealth_changes = [float(item["wealth_change"]) for item in cases]
    worst = min(cases, key=lambda item: float(item["wealth_change"]), default=None)
    return {
        "scenario_count": len(cases),
        "wealth_change": {
            "minimum": min(wealth_changes) if wealth_changes else None,
            "p05": _quantile(wealth_changes, 0.05) if wealth_changes else None,
            "p10": _quantile(wealth_changes, 0.10) if wealth_changes else None,
            "median": _quantile(wealth_changes, 0.50) if wealth_changes else None,
        },
        "worst_case": worst,
        "cases": cases,
    }


def _transition_reference_by_id(
    reference: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Validate a retained current-semantic artifact used only for transition."""
    if reference.get("trade_count_semantics") != TRADE_COUNT_SEMANTICS:
        raise ValueError("Initial baseline reference must use trade_records semantics")
    raw_results = reference.get("results")
    if not isinstance(raw_results, list) or not raw_results:
        raise ValueError("Initial baseline reference must contain non-empty results")
    by_id: dict[str, dict[str, Any]] = {}
    for item in raw_results:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("scenario_id"), str)
            or not isinstance(item.get("scenario_type"), str)
        ):
            raise TypeError("Initial baseline reference has an invalid result")
        scenario_id = item["scenario_id"]
        if scenario_id in by_id:
            raise ValueError("Initial baseline reference has duplicate scenario IDs")
        by_id[scenario_id] = item
    return by_id


def _initial_baseline_gates(
    results: list[dict[str, Any]], reference: dict[str, Any] | None
) -> dict[str, Any]:
    """Protect retained economics while establishing contract v2 once."""
    if reference is None:
        return {
            "status": "no_transition_reference",
            "applicable": False,
            "passed": False,
            "checks": {},
            "observed": {},
        }
    reference_by_id = _transition_reference_by_id(reference)
    current_by_id = {str(item["scenario_id"]): item for item in results}
    if set(current_by_id) != set(reference_by_id) or any(
        current_by_id[scenario_id].get("scenario_type")
        != reference_by_id[scenario_id].get("scenario_type")
        for scenario_id in current_by_id
    ):
        raise ValueError(
            "Initial baseline reference must match the exact scenario IDs and families"
        )
    prefix_ratios = {
        scenario_id: (1.0 + float(item["total_return"]))
        / (1.0 + float(reference_by_id[scenario_id]["total_return"]))
        for scenario_id, item in current_by_id.items()
        if item.get("scenario_type") == "prefix"
    }
    if "prefix-05" not in prefix_ratios:
        raise ValueError("Initial baseline reference is missing prefix-05")
    other_prefix_ratios = [
        ratio for scenario_id, ratio in prefix_ratios.items() if scenario_id != "prefix-05"
    ]
    if not other_prefix_ratios:
        raise ValueError("Initial baseline reference is missing other prefix scenarios")
    current_worst_return = min(float(item["total_return"]) for item in results)
    reference_worst_return = min(
        float(item["total_return"]) for item in reference_by_id.values()
    )
    current_add_one = _robustness_diagnostics(results)["wealth_change"]["minimum"]
    reference_add_one = _robustness_diagnostics(list(reference_by_id.values()))[
        "wealth_change"
    ]["minimum"]
    if current_add_one is None or reference_add_one is None:
        raise ValueError("Initial baseline comparison requires add-one scenarios")
    checks = {
        "prefix_05_wealth_at_least_99pct": prefix_ratios["prefix-05"]
        >= 0.99 - 1e-12,
        "other_prefix_wealth_at_least_95pct": min(other_prefix_ratios)
        >= 0.95 - 1e-12,
        "worst_total_return_not_worse_by_0_02": current_worst_return
        >= reference_worst_return - 0.02 - 1e-12,
        "worst_add_one_diagnostic_not_worse_by_0_03": float(current_add_one)
        >= float(reference_add_one) - 0.03 - 1e-12,
    }
    return {
        "status": "compared",
        "applicable": True,
        "passed": all(checks.values()),
        "checks": checks,
        "observed": {
            "prefix_05_wealth_ratio": prefix_ratios["prefix-05"],
            "other_prefix_wealth_ratio_min": min(other_prefix_ratios),
            "worst_total_return": current_worst_return,
            "reference_worst_total_return": reference_worst_return,
            "worst_add_one_wealth_change": current_add_one,
            "reference_worst_add_one_wealth_change": reference_add_one,
        },
    }


# ── 2026-08-16 报告 P0-4: promotion gates vs the incumbent formal stress artifact ──
# 任何 cross-market / risk 改动晋级前，除绝对硬门外，还必须相对既有正式
# universe stress 基线满足以下"不大幅恶化"契约（2026-08-16 报告 P0-4 建议门槛
# 方向：固定牛市财富 ≥99%、random DD P90 不恶化、worst DD 不显著恶化、false
# risk action 数不增加、date/symbol/side 桶不明显增加）。注意：此处 P0-4 指
# 2026-08-16 报告，与 2026-08-07 旧报告中的 P0-4（灾变冷却阻断再入场）不同。
PROMOTION_PREFIX_WEALTH_RATIO = 0.99
PROMOTION_DD_P90_TOLERANCE = 0.005
PROMOTION_DD_P95_TOLERANCE = 0.005
PROMOTION_WORST_DD_TOLERANCE = 0.010
PROMOTION_WORST_RETURN_TOLERANCE = 0.020
PROMOTION_ADD_ONE_TOLERANCE = 0.030
PROMOTION_SIDE_BUCKET_P90_TOLERANCE = 5.0
PROMOTION_SIDE_BUCKET_WORST_TOLERANCE = 10.0
PROMOTION_RISK_ACTION_TOLERANCE = 2.0


def _permutation_invariance(results: list[dict[str, Any]]) -> dict[str, Any]:
    """同一 seed 的全排列场景必须产生完全一致的指标（Gate C）。"""
    groups: dict[int, list[dict[str, Any]]] = {}
    for item in results:
        if item.get("scenario_type") != "permutation":
            continue
        groups.setdefault(int(item.get("seed", 0)), []).append(item)
    deviations: list[float] = []
    numeric_fields = (
        "total_return",
        "max_drawdown",
        "sharpe",
        "calmar",
        "total_trades",
        "sleeve_fill_count",
        "date_symbol_side_count",
        "max_concurrent_symbols",
    )
    for members in groups.values():
        for left, right in zip(members, members[1:], strict=False):
            deviations.extend(
                abs(float(left[field]) - float(right[field]))
                for field in numeric_fields
            )
            if left["reason_attribution"] != right["reason_attribution"]:
                deviations.append(1.0)
            if left["terminal_risk_lock"] != right["terminal_risk_lock"]:
                deviations.append(1.0)
    worst = max(deviations) if deviations else 0.0
    return {
        "checked_groups": len(groups),
        "worst_deviation": worst,
        "invariant": worst <= 1e-12,
    }


def _current_incumbent_by_id(
    incumbent: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Validate and index an accepted current-semantic incumbent."""
    if incumbent.get("stress_contract_version") != STRESS_CONTRACT_VERSION:
        raise ValueError(
            f"Incumbent stress artifact must use contract version {STRESS_CONTRACT_VERSION}"
        )
    if incumbent.get("trade_count_semantics") != TRADE_COUNT_SEMANTICS:
        raise ValueError("Incumbent stress artifact must use trade_records semantics")
    if (
        incumbent.get("acceptance_status") != "accepted"
        or incumbent.get("canonical") is not True
    ):
        raise ValueError("Incumbent stress artifact must be accepted and canonical")
    raw_results = incumbent.get("results")
    if not isinstance(raw_results, list) or not raw_results:
        raise ValueError("Incumbent stress artifact must contain non-empty results")
    by_id: dict[str, dict[str, Any]] = {}
    for item in raw_results:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("scenario_id"), str)
            or not item["scenario_id"]
            or not isinstance(item.get("scenario_type"), str)
            or not item["scenario_type"]
        ):
            raise ValueError(
                "Incumbent stress artifact has an invalid structured result"
            )
        scenario_id = item["scenario_id"]
        if scenario_id in by_id:
            raise ValueError(
                "Incumbent stress artifact has duplicate scenario_id values"
            )
        by_id[scenario_id] = item
    return by_id


def _promotion_gates(
    results: list[dict[str, Any]], incumbent: dict[str, Any] | None
) -> dict[str, Any]:
    """相对既有当前基线的强制晋级门；无基线时失败关闭。"""
    permutation = _permutation_invariance(results)
    payload: dict[str, Any] = {
        "baseline": "incumbent_universe_stress",
        "permutation_invariance": permutation,
    }
    if incumbent is None:
        payload.update(
            {
                "status": "no_incumbent_baseline",
                "applicable": False,
                "passed": False,
                "note": (
                    "未找到已接受且使用 trade_records 语义的正式基线；晋级失败关闭。"
                ),
            }
        )
        return payload
    by_id = _current_incumbent_by_id(incumbent)
    current_by_id = {str(item["scenario_id"]): item for item in results}
    shared_ids = sorted(sid for sid in current_by_id if sid in by_id)
    if not shared_ids:
        raise ValueError("Incumbent stress artifact has no shared scenario IDs")
    fixed_families = {"prefix", "leave_one_out", "add_one"}
    mandatory_fixed_ids = {
        sid
        for sid, item in current_by_id.items()
        if item.get("scenario_type") in fixed_families
    }
    if any(
        sid not in by_id
        or by_id[sid].get("scenario_type") != current_by_id[sid].get("scenario_type")
        for sid in mandatory_fixed_ids
    ):
        raise ValueError(
            "Incumbent stress artifact is missing a mandatory fixed scenario ID or family"
        )
    if any(
        item.get("scenario_type") == "random_subset" for item in current_by_id.values()
    ) and not any(
        item.get("scenario_type") == "random_subset" for item in by_id.values()
    ):
        raise ValueError(
            "Incumbent stress artifact is missing the random_subset comparison family"
        )

    def _family(family: str, source: dict[str, dict[str, Any]]) -> list[dict]:
        return [item for item in source.values() if item.get("scenario_type") == family]

    def _summary_or_empty(family: str, source: dict[str, dict[str, Any]]) -> dict:
        family_results = _family(family, source)
        return _summary(family_results) if family_results else {}

    prefix_ratios = [
        (1.0 + float(item["total_return"])) / (1.0 + float(by_id[sid]["total_return"]))
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
        "prefix_wealth_ratio_min": (min(prefix_ratios) if prefix_ratios else None),
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
        checks["random_date_symbol_side_buckets_p90_not_increased"] = (
            cur_random["date_symbol_side_buckets_p90"]
            <= inc_random["date_symbol_side_buckets_p90"]
            + PROMOTION_SIDE_BUCKET_P90_TOLERANCE
        )
        observed["random_date_symbol_side_buckets_p90"] = cur_random[
            "date_symbol_side_buckets_p90"
        ]
        checks["random_risk_actions_not_increased"] = (
            cur_random["risk_action_orders_median"]
            <= inc_random["risk_action_orders_median"] + PROMOTION_RISK_ACTION_TOLERANCE
        )
        observed["random_risk_action_orders_median"] = cur_random[
            "risk_action_orders_median"
        ]
    checks["all_worst_dd_not_significantly_worse"] = (
        cur_all["drawdown_worst"]
        >= inc_all["drawdown_worst"] - PROMOTION_WORST_DD_TOLERANCE
    )
    observed["all_worst_dd"] = cur_all["drawdown_worst"]
    checks["all_worst_date_symbol_side_buckets_not_increased"] = (
        cur_all["date_symbol_side_buckets_worst"]
        <= inc_all["date_symbol_side_buckets_worst"]
        + PROMOTION_SIDE_BUCKET_WORST_TOLERANCE
    )
    observed["all_worst_date_symbol_side_buckets"] = cur_all[
        "date_symbol_side_buckets_worst"
    ]
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
            "shared_scenario_count": sum(1 for sid in current_by_id if sid in by_id),
            "tolerances": {
                "prefix_wealth_ratio": PROMOTION_PREFIX_WEALTH_RATIO,
                "dd_p90": PROMOTION_DD_P90_TOLERANCE,
                "dd_p95": PROMOTION_DD_P95_TOLERANCE,
                "worst_dd": PROMOTION_WORST_DD_TOLERANCE,
                "worst_return": PROMOTION_WORST_RETURN_TOLERANCE,
                "add_one_wealth": PROMOTION_ADD_ONE_TOLERANCE,
                "date_symbol_side_buckets_p90": (PROMOTION_SIDE_BUCKET_P90_TOLERANCE),
                "date_symbol_side_buckets_worst": (
                    PROMOTION_SIDE_BUCKET_WORST_TOLERANCE
                ),
                "risk_action_median": PROMOTION_RISK_ACTION_TOLERANCE,
            },
            "passed": all(checks.values()),
            "checks": checks,
            "observed": observed,
        }
    )
    return payload


def _promotion_accepted(promotion: dict[str, Any]) -> bool:
    permutation = promotion.get("permutation_invariance", {})
    if not isinstance(permutation, dict) or not permutation.get("invariant"):
        return False
    return promotion.get("passed") is True
