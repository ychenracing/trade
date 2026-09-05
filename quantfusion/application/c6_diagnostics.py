"""Strict, R-bound non-canonical C6 diagnostic execution and payload helpers."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import subprocess
import sys
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import Any

from quantfusion.application.c6_contract import canonical_json_bytes as _canonical_bytes
from quantfusion.io.c6_stream import load_object, select_records, write_json
from quantfusion.application.c6_predicates import (
    _l1_predicate_rows, _predicate_rows,
    _attach_interventions as _attach_interventions,
    _attribution as _attribution,
    _first_path_divergence as _first_path_divergence,
    _path_hashes as _path_hashes,
    _post_lock_effect as _post_lock_effect,
    _prefix_hash as _prefix_hash,
    _score_comparison as _score_comparison,
    _symbol_pnl as _symbol_pnl,
    base_counterpart_id as base_counterpart_id,
    compare_s_paths as compare_s_paths,
    risk_execution_telemetry as risk_execution_telemetry,
)


def _as_mapping(value: object, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{where} must be an object with string keys")
    return value


def validate_manifest_identity(
    actual_ids: Sequence[str], frozen_manifest: Mapping[str, Any]
) -> list[str]:
    """Validate exact sorted LF-delimited ID identity against a frozen manifest."""
    ids = list(actual_ids)
    if any(
        not isinstance(item, str) or not item or "\n" in item or "\r" in item
        for item in ids
    ):
        raise ValueError("manifest IDs must be non-empty single-line strings")
    if len(set(ids)) != len(ids):
        raise ValueError("manifest contains duplicate IDs")
    if ids != sorted(ids):
        raise ValueError("manifest ID order is not lexicographic")
    count = frozen_manifest.get("count")
    unique_count = frozen_manifest.get("unique_count", count)
    if count != len(ids) or unique_count != len(ids):
        raise ValueError("manifest count does not match frozen identity")
    embedded = frozen_manifest.get("ids")
    if embedded is not None and embedded != ids:
        raise ValueError("manifest IDs do not match frozen order")
    digest = hashlib.sha256("".join(f"{item}\n" for item in ids).encode()).hexdigest()
    if frozen_manifest.get("sha256") != digest:
        raise ValueError("manifest SHA-256 does not match frozen identity")
    return ids


def first_official_mdd_breach(
    equity_samples: Sequence[Mapping[str, Any]],
    *,
    threshold: float = 0.18,
    numeric_tolerance: float = 1e-15,
) -> dict[str, Any] | None:
    """Return the first official sampled running-peak breach, or ``None``."""
    if not math.isfinite(threshold) or not math.isfinite(numeric_tolerance):
        raise ValueError("breach threshold and tolerance must be finite")
    if threshold < 0 or numeric_tolerance < 0:
        raise ValueError("breach threshold and tolerance must be non-negative")
    peak: float | None = None
    peak_timestamp: str | None = None
    prior_timestamp: str | None = None
    for ordinal, raw in enumerate(equity_samples):
        sample = _as_mapping(raw, f"equity_samples[{ordinal}]")
        timestamp = sample.get("timestamp")
        equity = sample.get("equity")
        if not isinstance(timestamp, str) or not timestamp:
            raise ValueError("official equity sample timestamp must be a string")
        if prior_timestamp is not None and timestamp < prior_timestamp:
            raise ValueError("official equity samples are not in sample order")
        if isinstance(equity, bool) or not isinstance(equity, (int, float)) or not math.isfinite(float(equity)):
            raise ValueError("official equity samples must contain finite equity")
        current = float(equity)
        if peak is None or current > peak:
            peak, peak_timestamp = current, timestamp
        if peak <= 0:
            raise ValueError("official running peak must be positive")
        drawdown = current / peak - 1.0
        if abs(drawdown) > threshold + numeric_tolerance:
            return {
                "event_type": "first_official_mdd_breach",
                "timestamp": timestamp,
                "sample_ordinal": ordinal,
                "peak_value": peak,
                "peak_timestamp": peak_timestamp,
                "current_assets": current,
                "drawdown": drawdown,
                "threshold": threshold,
                "numeric_tolerance": numeric_tolerance,
                "peak_owner": "official_running_peak",
                "state_source": "official_equity_samples",
            }
        prior_timestamp = timestamp
    return None


def _manager_event(events: Sequence[Mapping[str, Any]], name: str) -> dict[str, Any]:
    event = next((item for item in events if item.get("event") == name and item.get("sleeve") == "portfolio"), None)
    return {
        "timestamp": None if event is None else event.get("date"),
        "peak_owner": None if event is None else ("manager_lifetime_peak" if name == "terminal_portfolio_drawdown_lock" else "manager_cycle_peak"),
        "peak_timestamp": None if event is None else event.get("peak_timestamp"),
        "peak_value": None if event is None else event.get("peak_assets"),
        "current_assets": None if event is None else event.get("current_assets"),
        "drawdown": None if event is None else event.get("drawdown"),
        "threshold": None if event is None else event.get("threshold"),
        "status_source": None if event is None else str(event.get("event")),
    }


def maximum_cluster_weight(positions: Sequence[Mapping[str, Any]], assets: Mapping[str, float], groups: Mapping[str, str]) -> float:
    """Aggregate marked holdings by cluster at each official account sample."""
    totals: dict[tuple[str, str], float] = {}
    for position in positions:
        date = position["timestamp"]
        key = (date, groups.get(position["symbol"], "unmapped"))
        totals[key] = totals.get(key, 0.0) + position["market_value"]
    return max((value / assets[date] for (date, _), value in totals.items()), default=0.0)


def _l2_evaluate(scenario: Mapping[str, Any]) -> dict[str, Any]:
    """Run one selected-candidate scenario and retain the frozen L2 fields."""
    from quantfusion.application import stress, stress_metrics
    from quantfusion.config.overlay import SYMBOL_SUB_INDUSTRY
    from quantfusion.config.paths import MARKET_DATA_DIR, REGIME_DATA_DIR
    from quantfusion.engine.replay import ProductionReplayEngine

    from quantfusion.risk.overlay.policy import CrossMarketOverlay
    intervention = "C6_BASE_PLUS_S" if getattr(CrossMarketOverlay, "C6_S_PRODUCTION", False) else "C6_BASE"
    codes = [str(item) for item in scenario["symbols"]]
    with contextlib.redirect_stdout(io.StringIO()):
        result = ProductionReplayEngine(stress_metrics.INITIAL_CAPITAL).run_c6_diagnostic(
            {code: stress.NAMES[code] for code in codes},
            stress_metrics.START_DATE, stress_metrics.END_DATE,
            data_dir=str(MARKET_DATA_DIR), regime_data_dir=str(REGIME_DATA_DIR),
            indicator_state="warm",
            diagnostic_request={"schema_version": 1, "intervention_id": intervention,
                                "recording_mode": "DEFAULT", "scenario_id": scenario["scenario_id"],
                                "diagnostic_noncanonical": True, "allow_publication": False},
        )
    attribution = {name: 0 for name in stress_metrics.ATTRIBUTION_CATEGORIES}
    for trade in result["trades"]:
        attribution[stress._reason_category(trade)] += 1
    equity = result["equity_curve"]
    drawdown = (equity["assets"] / equity["assets"].cummax()) - 1.0
    breach_indices = list(drawdown.index[drawdown.abs() > 0.18 + 1e-15])
    breach_index = breach_indices[0] if breach_indices else None
    breach = {
        "timestamp": None if breach_index is None else str(breach_index.date()),
        "sample_ordinal": None if breach_index is None else int(equity.index.get_loc(breach_index)),
        "peak_timestamp": None if breach_index is None else str(equity.loc[:breach_index, "assets"].idxmax().date()),
        "peak_value": None if breach_index is None else float(equity.loc[:breach_index, "assets"].max()),
        "equity": None if breach_index is None else float(equity.loc[breach_index, "assets"]),
        "drawdown": None if breach_index is None else float(drawdown.loc[breach_index]),
        "threshold": 0.18, "tolerance": 1e-15,
    }
    events = list(result.get("risk_events", []))
    _, positions = _sleeve_paths(result)
    max_cluster = maximum_cluster_weight(
        positions, {str(date.date()): float(row.assets) for date, row in equity.iterrows()}, SYMBOL_SUB_INDUSTRY
    )
    telemetry = {
        "cash_days": int((equity["position_value"] == 0).sum()),
        "max_gross_ratio": float((equity["position_value"] / equity["assets"]).max()),
        "max_cluster_weight": max_cluster,
        **risk_execution_telemetry(result, SYMBOL_SUB_INDUSTRY),
        "first_evidence_timestamp": min((row["timestamp"] for row in _risk_records(result)
                                           if row["event_type"] in {"ACCOUNT_ALERT", "CONFIRMED_LOCK", "EMERGENCY_LOCK", "TERMINAL_LOCK", "CROSS_MARKET", "SUBINDUSTRY", "CONCENTRATION", "LAYERED_STOP"}), default=None),
        "first_official_mdd_breach": breach,
        "first_account_alert_event": _manager_event(events, "portfolio_drawdown_alert_on"),
        "first_confirmed_cycle_lock": _manager_event(events, "confirmed_cycle_drawdown_lock"),
        "first_emergency_cycle_lock": _manager_event(events, "emergency_cycle_drawdown_lock"),
        "first_terminal_lock": _manager_event(events, "terminal_portfolio_drawdown_lock"),
        "cycle_lock_count": int(result.get("cycle_lock_count", 0)),
        "terminal_lock_count": int(bool(result.get("terminal_risk_lock", False))),
        "mdd_slack": 0.18 - abs(float(result["max_drawdown"])),
        "near_18pct": abs(0.18 - abs(float(result["max_drawdown"]))) <= 1e-12,
    }
    keys = ("scenario_id", "scenario_type", "symbols", "symbol_count", "base_size", "added_symbol", "seed", "sample_size")
    return {
        **{key: scenario.get(key) for key in keys},
        "total_return": float(result["total_return"]),
        "max_drawdown": float(result["max_drawdown"]), "sharpe": float(result["sharpe"]),
        "calmar": float(result["calmar"]), "total_trades": int(result["total_trades"]),
        "sleeve_fill_count": int(result["sleeve_fill_count"]),
        "date_symbol_side_count": int(result["date_symbol_side_count"]),
        "reason_attribution": attribution,
        "max_concurrent_symbols": int(result["max_concurrent_symbols"]),
        "terminal_risk_lock": bool(result["terminal_risk_lock"]),
        "deployment_policy": "production_daily_replay", "diagnostic_telemetry": telemetry,
        "execution_receipts": {"orders": result["_c6_orders"], "fills": result["_c6_fills"],
                               "action_lifecycle": _action_records(result), "exposure_series": _exposure_records(result)},
    }


_VARIANTS = {
    "baseline": "BASELINE", "F0-only": "F0_ONLY", "F0+F1": "F0_F1",
    "U-only": "U_ONLY", "C6-Base": "C6_BASE", "C6-Base+S": "C6_BASE_PLUS_S",
    "W0-no-601869": "W0_NO_601869", "W1-data-map-only": "W1_DATA_MAP_ONLY",
    "W2-pool-denominator-only": "W2_POOL_DENOMINATOR_ONLY",
    "W3-real-intents-fixed-reference-U": "W3_REAL_INTENTS_FIXED_REFERENCE_U",
    "W4-full-base-production-pool-relative": "W4_FULL_BASE_PRODUCTION_POOL_RELATIVE",
    "W5-full-base-production-pool-relative-no-lock": "W5_FULL_BASE_PRODUCTION_POOL_RELATIVE_NO_LOCK",
}


def _empty_s_evidence() -> dict[str, Any]:
    coverage = {"observed_count": 0, "minimum_observed": 4, "observed_industries": 0, "minimum_observed_industries": 3, "decision_timestamp": None, "latest_source_timestamp": None, "freshness_max_sessions": 0, "freshness_passed": False, "coverage_passed": False, "unmapped_weight": 0.0, "unmapped_limit": 0.05, "unmapped_passed": True}
    leave = {"mode": "recomputed", "target_cluster": "none", "removed_components": [], "remaining_components": [], "observed_count": 0, "observed_industries": 0, "minimum_observed": 4, "minimum_observed_industries": 3, "recomputed_fast_return": 0.0, "fast_return_threshold": -0.06, "recomputed_declining_ratio": 0.0, "breadth_threshold": 0.60, "recomputed_stressed_cluster_set": [], "freshness_passed": False, "coverage_passed": False, "same_evidence_preserved": False, "passed": False}
    fill = {"t_plus_one_passed": False, "open_available": False, "not_suspended": False, "not_limit_blocked": False, "adv_capacity_shares": 0, "lot_size": 100, "nonzero_executable_lot": False}
    return {"first_causal_stressed_cluster_close": None, "worst_cluster": None, "worst_cluster_weight": None, "stressed_cluster_set": [], "coverage": coverage, "leave_held_components_out": leave, "first_early_sell_required_close": None, "risk_level": 0, "portfolio_fast_return": 0.0, "existing_concentration_eligible": False, "cluster_symbol_count": 0, "minimum_cluster_size": 2, "legacy_gate_open": False, "early_sell_required": False, "scheduled_execution_batch": None, "lead_batch_count": 0, "pre_trade_open_drawdown": None, "official_sample_relation": "NO_SCHEDULED_BATCH", "identical_valuation_instant_proven": False, "planned_shares": 0, "executable_lot_shares": 0, "fillability": fill, "book_fillability": [], "queue_fillability": None, "shortfall": {"shares": 0, "reason": "T_PLUS_ONE"}, "pre_sell_crossing_buy_witness": False}


def finalize_s_evidence(
    evidence: Mapping[str, Any],
    equity_samples: Sequence[Mapping[str, Any]],
    calendar: Sequence[str],
) -> dict[str, Any]:
    """Relate a decision-only S observation to the official breach sample."""
    result = dict(evidence)
    schedule = result.get("scheduled_execution_batch")
    breach = first_official_mdd_breach(equity_samples)
    if not isinstance(schedule, Mapping) or breach is None:
        result["lead_batch_count"] = 0
        result["official_sample_relation"] = "NO_SCHEDULED_BATCH"
        result["identical_valuation_instant_proven"] = False
        return result
    decision = str(schedule["decision_close"]).split("T", 1)[0]
    execution = str(schedule["execution_open"]).split("T", 1)[0]
    official = str(breach["timestamp"]).split("T", 1)[0]
    days = [str(item).split("T", 1)[0] for item in calendar]
    result["lead_batch_count"] = sum(
        decision < item <= official for item in days
    )
    if execution < official:
        relation = "PRECEDES_OFFICIAL_SAMPLE"
    elif execution == official:
        relation = "OPEN_MARK_GAP_NOT_OFFICIAL_SAMPLE"
    else:
        relation = "UNAVOIDABLE_AT_OFFICIAL_SAMPLE"
    result["official_sample_relation"] = relation
    result["identical_valuation_instant_proven"] = False
    return result


def _sleeve_paths(result: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Reconstruct factual sleeve cash and marked positions from run ledgers."""
    cash, positions = [], []
    for state_index, (report, state) in enumerate(zip(result["_c6_sleeve_results"], result["_c6_states"], strict=True)):
        held: dict[tuple[str, str], int] = {}
        trades_by_date: dict[str, list[Any]] = {}
        for trade in report["trades"]:
            trades_by_date.setdefault(str(trade.date), []).append(trade)
        for sample_ordinal, (date, row) in enumerate(report["equity_curve"].iterrows()):
            timestamp = str(date.date())
            for trade in trades_by_date.get(timestamp, []):
                key = (str(trade.symbol), str(trade.strategy_name))
                held[key] = held.get(key, 0) + int(trade.shares) * (1 if trade.direction == "buy" else -1)
            cash.append({"sample_ordinal": sample_ordinal, "timestamp": timestamp, "state_index": state_index, "sleeve_name": report["sleeve_name"], "cash": float(row["cash"])})
            for symbol, strategy in sorted(held):
                shares = held[symbol, strategy]
                if shares <= 0:
                    continue
                frame = state.data_map[symbol]
                mark = float(frame.loc[frame.index <= date, "close"].iloc[-1])
                positions.append({"sample_ordinal": sample_ordinal, "timestamp": timestamp, "state_index": state_index, "sleeve_name": report["sleeve_name"], "strategy_name": strategy, "symbol": symbol, "shares": shares, "mark": mark, "market_value": shares * mark})
    cash.sort(key=lambda item: (item["sample_ordinal"], item["state_index"]))
    positions.sort(key=lambda item: (item["sample_ordinal"], item["state_index"], item["symbol"], item["strategy_name"]))
    return cash, positions


def _action_records(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Export receipts recorded at the boundary; never infer them from future fills."""
    records = [dict(item) for state in result["_c6_states"] for item in getattr(state.sleeve, "_c6_action_lifecycle", [])]
    for record in records:
        for kind in ("planned", "retained", "suppressed"):
            record[kind + "_notional"] = record[kind + "_shares"] * record["reference_price"]
    return sorted(records, key=lambda item: item["emission_ordinal"])


def _risk_records(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    names = {"portfolio_drawdown_alert_on": "ACCOUNT_ALERT", "confirmed_cycle_drawdown_lock": "CONFIRMED_LOCK", "emergency_cycle_drawdown_lock": "EMERGENCY_LOCK", "terminal_portfolio_drawdown_lock": "TERMINAL_LOCK",
             "sector_risk_level": "SUBINDUSTRY", "sector_risk_trim": "SUBINDUSTRY",
             "transition_risk_trim": "CROSS_MARKET", "shock_trim": "CROSS_MARKET",
             "concentration_trim": "CONCENTRATION", "concentration_guard_fail_closed": "CONCENTRATION",
             "layered_stop": "LAYERED_STOP"}
    records = []
    for ordinal, event in enumerate(item for item in result.get("risk_events", []) if item.get("date")):
        source = str(event.get("event", "unclassified"))
        kind = names.get(source, "UNCLASSIFIED")
        sleeve = event.get("sleeve")
        state = {"fast": 0, "base": 1, "slow": 2}.get(sleeve)
        records.append({"emission_ordinal": ordinal, "timestamp": str(event["date"]), "phase_order": None, "event_type": kind, "state_index": state, "sleeve_name": sleeve if state is not None else None, "strategy_name": event.get("strategy") or event.get("strategy_name"), "symbol": event.get("symbol"), "peak_owner": event.get("peak_owner"), "peak_value": event.get("peak_assets"), "current_assets": event.get("current_assets"), "drawdown": event.get("drawdown"), "threshold": event.get("threshold"), "status_source": source, "evidence_flags": [], "raw_event": dict(event)})
    return records


def _warm_snapshot(result: Mapping[str, Any], initial: float) -> dict[str, Any]:
    captured = result.get("_c6_warm_state")
    if not captured or captured["phase"] != "before_first_valuation":
        raise ValueError("warm boundary capture is missing")
    first, execution = captured["first"], captured["execution"]
    sleeves = captured["sleeves"]
    expected_cash = [initial / 3, initial / 3, initial - 2 * (initial / 3)]
    if len(sleeves) != 3 or [s["cash"] for s in sleeves] != expected_cash:
        raise ValueError("warm boundary cash allocation differs from initial capital")
    history = []
    for symbol, frame in sorted(captured["data"].items()):
        if frame.empty or symbol not in captured["indicators"]:
            continue
        fields = captured["indicators"][symbol]
        if any(timestamp >= first for timestamp in frame.index) or any(
                timestamp >= first for series in fields.values() for timestamp in series.index):
            raise ValueError("warm boundary contains future information")
        indicators = {name: series.to_json(date_format="iso") for name, series in sorted(fields.items())}
        history.append({"symbol": symbol, "history_start": str(frame.index[0].date()), "history_end": str(frame.index[-1].date()), "causal_cutoff": str(frame.index[-1].date()), "source_row_count": len(frame), "source_sha256": hashlib.sha256(frame.to_json(date_format="iso").encode()).hexdigest(), "indicator_sha256": hashlib.sha256(_canonical_bytes(indicators)).hexdigest()})
    def peaks(risk: Mapping[str, Any]) -> dict[str, Any]:
        return {"cycle_peak_assets": risk["peak_assets"], "lifetime_peak_assets": risk.get("lifetime_peak_assets"), "daily_start_assets": risk["daily_start_assets"]}
    cash, sleeve_peaks, locks = [], [], []
    for index, sleeve in enumerate(sleeves):
        identity = {"state_index": index, "sleeve_name": sleeve["name"]}
        cash.append({**identity, "cash": sleeve["cash"]})
        sleeve_peaks.append({**identity, **peaks(sleeve["risk"])})
    for index, risk in enumerate([s["risk"] for s in sleeves] + [captured["account_risk"]]):
        # These constructor states were checked before replay by the capture hook.
        locks.append({"owner_kind": "sleeve" if index < 3 else "account", "state_index": index if index < 3 else None, "sleeve_name": sleeves[index]["name"] if index < 3 else None, "cycle_lock": risk["persistent_lock"], "emergency_lock": False, "terminal_lock": risk.get("terminal_lock", False), "rearm_remaining_trading_days": 0})
    return {"phase": captured["phase"], "indicator_history": history, "regime_and_transitions": {"current_regime": sleeves[0]["regime"]["_regime_state"].lower(), "asof_timestamp": None, "transitions": sleeves[0]["regime"]["_regime_state_series"]}, "candidate_sticky_confirmation": [], "overlay_state": [], "sleeve_positions": [], "sleeve_cash": cash, "pending_orders": [], "sleeve_peaks": sleeve_peaks, "account_peaks": peaks(captured["account_risk"]), "locks": locks, "first_decision_timestamp": str(first.date()), "first_execution_timestamp": str(execution.date()), "unauthorized_economic_state_empty": True, "future_information_absent": True}


def validate_indicator_provenance(data: Mapping[str, Any], indicators: Mapping[str, Any],
                                  states: Sequence[Any], ordered_codes: Sequence[str]) -> dict[str, Any]:
    """Compare prepared values, including NaNs, against independently computed inputs."""
    if set(data) != set(ordered_codes) or set(indicators) != set(ordered_codes):
        raise ValueError("full-universe indicator provenance coverage mismatch")
    def frame_hash(frame: Any) -> str:
        return hashlib.sha256(frame.to_csv(float_format="%.17g", lineterminator="\n").encode()).hexdigest()
    frames = {code: frame_hash(data[code]) for code in ordered_codes}
    hashes = {code: frame_hash(__import__('pandas').DataFrame(indicators[code]).sort_index(axis=1)) for code in ordered_codes}
    for state in states:
        for code in set(state.data_map) & set(ordered_codes):
            if not state.data_map[code].equals(data[code]):
                raise ValueError(f"prepared raw frame changed for {code}")
            actual, expected = state.indicator_map[code], indicators[code]
            if set(actual) != set(expected) or any(not actual[key].equals(expected[key]) for key in expected):
                raise ValueError(f"prepared indicator values changed for {code}")
    calendars = [[str(date.date()) for date in state.all_dates] for state in states]
    if not calendars or any(item != calendars[0] for item in calendars):
        raise ValueError("sleeve execution calendars differ")
    return {"prepared_frame_hashes": frames, "indicator_hashes": hashes,
            "old_symbol_frames_unchanged": True, "old_symbol_indicators_unchanged": True,
            "calendar_hash": hashlib.sha256("".join(date + "\n" for date in calendars[0]).encode()).hexdigest()}


def _data_identity(result: Mapping[str, Any]) -> dict[str, Any]:
    from quantfusion.application import stress, stress_artifacts, stress_metrics, stress_scenarios
    from quantfusion.config.paths import MARKET_DATA_DIR, REGIME_DATA_DIR
    from quantfusion.data.providers import DataFetcher
    from quantfusion.indicators.technical import Indicators
    import pandas as pd

    codes = list(stress_scenarios.ORDERED_CODES)
    states = result["_c6_states"]
    # Resolve on a separate unfunded object: the resolver installs a risk manager.
    sleeve = states[0].sleeve
    resolver = type(sleeve)(sleeve.initial_capital, cfg=sleeve._user_cfg,
                           policy=sleeve.policy, allocation_lookbacks=sleeve.ALLOCATION_LOOKBACKS,
                           sleeve_name=sleeve.sleeve_name)
    resolver._profile_strategy_overrides = dict(sleeve._profile_strategy_overrides)
    configs = resolver._resolve_symbol_configs({code: stress.NAMES[code] for code in codes}, None, "auto")
    for state in states:
        if any(state.sleeve.symbol_configs[code] != configs[code]
               for code in set(state.sleeve.symbol_configs) & set(codes)):
            raise ValueError("prepared symbol configuration differs from independent full-universe resolver")
    start = pd.Timestamp(stress_metrics.START_DATE) - pd.Timedelta(days=states[0].sleeve._warmup_calendar_days)
    data, indicators = {}, {}
    with contextlib.redirect_stdout(io.StringIO()):
        for code in codes:
            frame = DataFetcher.load_stock_data(code, str(start.date()), stress_metrics.END_DATE,
                                               data_dir=str(MARKET_DATA_DIR), cache_dir=None)
            frame = frame.loc[(frame.index >= start) & (frame.index <= pd.Timestamp(stress_metrics.END_DATE))].copy()
            data[code] = frame
            indicators[code] = Indicators.compute_all(frame, configs[code])
    facts = validate_indicator_provenance(data, indicators, states, codes)
    scenarios = stress_scenarios._multi_seed_scenarios(random_samples=50, permutation_samples=50, seeds=(20260807, 20260817, 20260827))
    return {**facts, "ordered_symbols": codes,
            "raw_frame_hashes": {code: hashlib.sha256((MARKET_DATA_DIR / f"{code}.csv").read_bytes()).hexdigest() for code in codes},
            "data_fingerprint": stress_artifacts._tree_fingerprint(stress_artifacts._data_files(MARKET_DATA_DIR, REGIME_DATA_DIR)),
            "scenario_signature": stress_scenarios._scenario_signature(scenarios)}


def _exposure_records(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for snapshot in result["_c6_exposure_trace"]:
        base = {"timestamp": snapshot["timestamp"], "phase": snapshot["phase"],
                "gross_notional": snapshot["gross_notional"], "gross_ratio": snapshot["gross_ratio"],
                "symbol": None, "symbol_notional": None, "cluster": None,
                "cluster_notional": None, "cluster_weight": None}
        rows.append({"sample_ordinal": len(rows), **base})
        for symbol, value in sorted(snapshot["symbol_notionals"].items()):
            rows.append({"sample_ordinal": len(rows), **base, "symbol": symbol, "symbol_notional": value})
        for cluster, value in sorted(snapshot["cluster_notionals"].items()):
            rows.append({"sample_ordinal": len(rows), **base, "cluster": cluster,
                         "cluster_notional": value, "cluster_weight": value / snapshot["assets"]})
    return rows


def causal_execution_chain(snapshots, orders, fills, actions, events, equity, breach):
    """Join factual receipts and unchanged-inventory mark intervals."""
    from collections import defaultdict
    from quantfusion.config.overlay import CONCENTRATION_CAP, SYMBOL_SUB_INDUSTRY

    end = breach or (equity[-1]["timestamp"] if equity else None)
    prefix = [row for row in equity if end is None or row["timestamp"] <= end]
    peak = max(prefix, key=lambda row: row["equity"]) if prefix else None
    first_actions = {}
    for action in actions:
        if end is not None and action["timestamp"] > end:
            continue
        key = (action["state_index"], action["symbol"], action["strategy_name"])
        if key not in first_actions:
            first_actions[key] = action
    eligible_events = [event for event in events if event["event_type"] in {
        "CROSS_MARKET", "SUBINDUSTRY", "CONCENTRATION", "LAYERED_STOP"}
        and (end is None or event["timestamp"] <= end)]
    fills_by_date = defaultdict(list)
    for fill in fills:
        fills_by_date[fill["timestamp"]].append(fill)
    batches = []
    for date in sorted({row["execution_timestamp"] for row in orders if row.get("execution_timestamp")
                        and (end is None or row["execution_timestamp"] <= end)}):
        sells = [row for row in orders if row.get("execution_timestamp") == date and row["side"] == "SELL"
                 and row.get("defensive") and row.get("status") != "suppressed"]
        if not sells:
            continue
        buys = [row for row in orders if row.get("execution_timestamp") == date and row["side"] == "BUY"]
        batches.append({"execution_timestamp": date,
                        "retained_sell_order_ordinals": [row["order_ordinal"] for row in sells],
                        "sell_fill_ordinals": [row["fill_ordinal"] for row in fills_by_date[date] if row["side"] == "SELL"],
                        "buy_order_ordinals": [row["order_ordinal"] for row in buys],
                        "carried_buy_order_ordinals": [row["order_ordinal"] for row in buys if row.get("carried_from_order_ordinal") is not None],
                        "post_sell_and_buy_exposure": [{key: row[key] for key in ("phase","assets","gross_notional","gross_ratio","cluster_notionals","symbol_notionals")}
                                                        for row in snapshots if row["timestamp"] == date and row["phase"] in {"after_sells","after_buys"}]})
    crossings, losses = [], {}
    for before, after in zip(snapshots, snapshots[1:]):
        if end is not None and after["timestamp"] > end:
            continue
        if before["phase"] == "after_sells" and after["phase"] == "after_buys" and before["timestamp"] == after["timestamp"]:
            for cluster, value in after["cluster_notionals"].items():
                old_weight = before["cluster_notionals"].get(cluster, 0.) / before["assets"]
                new_weight = value / after["assets"]
                buys = [row for row in fills_by_date[after["timestamp"]] if row["side"] == "BUY" and SYMBOL_SUB_INDUSTRY.get(row["symbol"]) == cluster]
                if old_weight <= CONCENTRATION_CAP < new_weight and buys:
                    crossings.append({"timestamp":after["timestamp"],"cluster":cluster,"before_weight":old_weight,
                                      "after_weight":new_weight,"existing_cap":CONCENTRATION_CAP,
                                      "buy_fill_ordinals":[row["fill_ordinal"] for row in buys]})
        mark_interval = ((before["phase"] == "after_buys" and after["phase"] == "official_sample" and before["timestamp"] == after["timestamp"])
                         or (before["phase"] == "official_sample" and after["phase"] == "batch_start" and before["timestamp"] < after["timestamp"]))
        if not mark_interval or (peak is not None and (before["timestamp"] < peak["timestamp"]
                or (before["timestamp"] == peak["timestamp"] and before["phase"] != "official_sample"))):
            continue
        def book(row):
            return row["state_index"], row["symbol"], row["strategy_name"]
        left, right = {book(row):row for row in before["positions"]}, {book(row):row for row in after["positions"]}
        for key in sorted(set(left) & set(right)):
            a, b = left[key], right[key]
            if a["shares"] != b["shares"]:
                raise ValueError("holdings changed inside a non-execution mark interval")
            pnl = a["shares"] * (b["mark_price"] - a["mark_price"])
            if key not in losses:
                losses[key] = {"state_index":key[0],"sleeve_name":a["sleeve_name"],"symbol":key[1],"strategy_name":key[2],
                               "first_timestamp":before["timestamp"],"last_timestamp":after["timestamp"],
                               "mark_loss_notional":0.,"mark_gain_notional":0.,"observed_interval_count":0}
            row = losses[key]
            row["last_timestamp"] = after["timestamp"]
            row["mark_loss_notional"] += max(-pnl,0.)
            row["mark_gain_notional"] += max(pnl,0.)
            row["observed_interval_count"] += 1
    return {"analysis_end_timestamp":end,"peak_close_timestamp":peak["timestamp"] if peak else None,
            "peak_close_equity":peak["equity"] if peak else None,
            "first_causal_evidence_timestamp":min((row["timestamp"] for row in eligible_events),default=None),
            "first_causal_evidence_emission_ordinals":[row["emission_ordinal"] for row in eligible_events
                                                     if row["timestamp"] == min((event["timestamp"] for event in eligible_events),default=None)],
            "first_action_emission_ordinals_by_book":[row["emission_ordinal"] for _,row in sorted(first_actions.items())],
            "execution_batches":batches,"buy_crossing_witnesses":crossings,
            "retained_mark_pnl":[row for _,row in sorted(losses.items())],
            "price_pnl_excludes_fees_and_execution_changes":True}


def build_causal_matrix(result: Mapping[str, Any], timeline: Mapping[str, Any],
                        evidence: Mapping[str, Any], equity: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    orders, fills = result["_c6_orders"], result["_c6_fills"]
    snapshots = result["_c6_exposure_trace"]
    chain = causal_execution_chain(snapshots, orders, fills, _action_records(result), _risk_records(result),
                                   equity, timeline["first_official_mdd_breach"]["timestamp"])
    evidence = dict(evidence)
    evidence["pre_sell_crossing_buy_witness"] = any(
        row["cluster"] == evidence["worst_cluster"]
        and (evidence["first_early_sell_required_close"] is None or row["timestamp"] <= evidence["first_early_sell_required_close"])
        for row in chain["buy_crossing_witnesses"])

    by_order = {order["order_ordinal"]: order for order in orders}
    expected = [(row["timestamp"], phase) for row in equity
                for phase in ("batch_start", "after_sells", "after_buys", "official_sample")]
    complete = ([(row["timestamp"], row["phase"]) for row in snapshots] == expected
                and len(by_order) == len(orders)
                and all(fill["order_ordinal"] in by_order for fill in fills)
                and sum(order["filled_shares"] for order in orders) == sum(fill["shares"] for fill in fills)
                and len(fills) == sum(len(state.sleeve.trades) for state in result["_c6_states"]))
    findings = {}
    def observe(label: str, date: Any, items: Sequence[Mapping[str, Any]] = (), notional: Any = None) -> None:
        findings[label] = {"label": label, "first_observed_timestamp": date,
                           "first_path_divergence_timestamp": None,
                           "state_indices": sorted({row["state_index"] for row in items if row.get("state_index") is not None}),
                           "symbols": sorted({row["symbol"] for row in items if row.get("symbol")}), "notional": notional}
    for order in orders:
        winner = by_order.get(order["suppression_winner_order_ordinal"])
        if winner is not None and winner["state_index"] != order["state_index"]:
            if "BOOK_IDENTITY_LOSS" not in findings:
                observe("BOOK_IDENTITY_LOSS", order["decision_timestamp"], [order, winner])
    defensive = {(order["execution_timestamp"], order["state_index"], order["symbol"], order["strategy_name"])
                 for order in orders if order["side"] == "SELL" and order["action_id"] is not None
                 and order["execution_timestamp"] is not None and order["status"] != "suppressed"}
    offsets = [fill for fill in fills if fill["side"] == "BUY"
               and (fill["timestamp"], fill["state_index"], fill["symbol"], fill["strategy_name"]) in defensive]
    if offsets:
        observe("ACTION_OFFSET", min(row["timestamp"] for row in offsets), offsets,
                math.fsum(row["notional"] for row in offsets))
    early = evidence["first_early_sell_required_close"]
    if evidence["early_sell_required"] and evidence["lead_batch_count"] > 0 and evidence["executable_lot_shares"] > 0:
        from quantfusion.config.overlay import SYMBOL_SUB_INDUSTRY
        timely = [row for row in orders if row["side"] == "SELL" and row["action_id"] is not None
                  and row["decision_timestamp"] <= early
                  and SYMBOL_SUB_INDUSTRY.get(row["symbol"]) == evidence["worst_cluster"]
                  and row["status"] not in {"suppressed", "cancelled"}]
        if sum(row["requested_shares"] for row in timely) < evidence["planned_shares"]:
            observe("POLICY_LATENCY", early)
    for snapshot in snapshots:
        prior = [row["equity"] for row in equity if row["timestamp"] < snapshot["timestamp"]]
        if snapshot["phase"] == "batch_start" and prior and snapshot["assets"] / max(prior) < 1 - 0.18 - 1e-15:
            observe("OPEN_MARK_GAP", snapshot["timestamp"], snapshot["positions"], snapshot["gross_notional"])
            break
    locks = [item["timestamp"] for key, item in timeline.items() if "lock" in key and item.get("timestamp")]
    if locks:
        lock = min(locks)
        blocked = [order for order in orders if order["blocked_reason"] == "merged_account_lock"
                   and any(event["timestamp"] >= lock for event in order["events"])]
        if blocked:
            observe("LOCK_MEDIATOR", lock, blocked)
    # No exhaustive pathwise proof is available from one factual replay.
    # This remains explicit even when some overlapping mechanisms are observed.
    observe("OTHER_UNRESOLVED", timeline["first_official_mdd_breach"]["timestamp"])
    label_order = ("STATE_OR_DATA_INVALID", "BOOK_IDENTITY_LOSS", "ACTION_OFFSET", "POLICY_LATENCY",
                   "EXECUTION_GAP", "OPEN_MARK_GAP", "NO_EARLY_EVIDENCE", "ALPHA_OR_HOLDING_PATH",
                   "LOCK_MEDIATOR", "OTHER_UNRESOLVED")
    labels = [label for label in label_order if label in findings]
    parallel = sorted(({"event_type": name, **row} for name, row in timeline.items() if row.get("timestamp") is not None),
                      key=lambda row: (row["timestamp"], row["event_type"]))
    return {"event_timeline": dict(timeline), "parallel_event_timeline": parallel, "execution_chain": chain, "required_trace_order_complete": complete,
            "executable_lead_batch_count": evidence["lead_batch_count"], "multi_labels": labels,
            "observed_mechanisms": [findings[label] for label in labels], "s_evidence": dict(evidence),
            "earliest_unavoidable_breach_under_frozen_candidate_family": None,
            "unavoidable_field_justification": "A factual path is not an exhaustive proof over the frozen candidate family."}


def _l1_evaluate(task: tuple[str, Mapping[str, Any], str]) -> dict[str, Any]:
    """Capture a deterministic factual replay record for one frozen evaluation."""
    variant, scenario, recording = task
    from quantfusion.application import stress, stress_metrics
    from quantfusion.config.paths import MARKET_DATA_DIR, REGIME_DATA_DIR
    from quantfusion.engine.replay import ProductionReplayEngine

    codes = [str(item) for item in scenario["symbols"]]
    request = {"schema_version": 1, "intervention_id": _VARIANTS[variant], "recording_mode": recording, "scenario_id": scenario["scenario_id"], "diagnostic_noncanonical": True, "allow_publication": False}
    with contextlib.redirect_stdout(io.StringIO()):
        result = ProductionReplayEngine(stress_metrics.INITIAL_CAPITAL).run_c6_diagnostic(
            {code: stress.NAMES[code] for code in codes}, stress_metrics.START_DATE,
            stress_metrics.END_DATE, diagnostic_request=request,
            data_dir=str(MARKET_DATA_DIR), regime_data_dir=str(REGIME_DATA_DIR),
            indicator_state="warm",
        )
    attribution = {name: 0 for name in stress_metrics.ATTRIBUTION_CATEGORIES}
    for trade in result["trades"]:
        attribution[stress._reason_category(trade)] += 1
    cash_series, position_series = _sleeve_paths(result)
    if recording in {"OFF", "ON"}:
        # Compare native engine ledgers, independently of the receipt serializer.
        from dataclasses import asdict
        native = {"orders": {"batches": result["_c6_pending_path"], "events": result["order_events"],
                             "pending": [[asdict(signal) for signal, _ in state.pending] for state in result["_c6_states"]]},
                  "fills": [asdict(trade) for trade in result["trades"]],
                  "cash_series": cash_series, "position_series": position_series,
                  "equity_series": result["equity_curve"].to_json(date_format="iso")}
        return {"native_path_hashes": _path_hashes(native)}
    orders, fills = result["_c6_orders"], result["_c6_fills"]
    equity = result["equity_curve"]
    equity_series = [{"sample_ordinal": i, "timestamp": str(date.date()), "equity": float(row.assets), "official_sample": True} for i, (date, row) in enumerate(equity.iterrows())]
    drawdown = equity["assets"] / equity["assets"].cummax() - 1.0
    drawdown_series = [{"sample_ordinal": i, "timestamp": str(date.date()), "equity": float(equity.loc[date, "assets"]), "running_peak": float(equity.loc[:date, "assets"].max()), "drawdown": float(value), "official_sample": True} for i, (date, value) in enumerate(drawdown.items())]
    exposure = _exposure_records(result)
    metrics = {"total_return": float(result["total_return"]), "terminal_wealth": float(result["final_assets"]), "max_drawdown": float(result["max_drawdown"]), "sharpe": float(result["sharpe"]), "calmar": float(result["calmar"]), "total_trades": int(result["total_trades"]), "sleeve_fill_count": int(result["sleeve_fill_count"]), "date_symbol_side_count": int(result["date_symbol_side_count"]), "cash_days": int((equity["position_value"] == 0).sum()), "reason_attribution": attribution, "max_concurrent_symbols": int(result["max_concurrent_symbols"]), "terminal_risk_lock": bool(result["terminal_risk_lock"]), "deployment_policy": "production_daily_replay"}
    raw_breach = first_official_mdd_breach(equity_series)
    breach = {"timestamp": None, "sample_ordinal": None, "peak_timestamp": None, "peak_value": None, "equity": None, "drawdown": None, "threshold": 0.18, "tolerance": 1e-15} if raw_breach is None else {"timestamp": raw_breach["timestamp"], "sample_ordinal": raw_breach["sample_ordinal"], "peak_timestamp": raw_breach["peak_timestamp"], "peak_value": raw_breach["peak_value"], "equity": raw_breach["current_assets"], "drawdown": raw_breach["drawdown"], "threshold": 0.18, "tolerance": 1e-15}
    timeline = {"first_official_mdd_breach": breach, "first_account_alert_event": _manager_event(result.get("risk_events", []), "portfolio_drawdown_alert_on"), "first_confirmed_cycle_lock": _manager_event(result.get("risk_events", []), "confirmed_cycle_drawdown_lock"), "first_emergency_cycle_lock": _manager_event(result.get("risk_events", []), "emergency_cycle_drawdown_lock"), "first_terminal_lock": _manager_event(result.get("risk_events", []), "terminal_portfolio_drawdown_lock")}
    initial = stress_metrics.INITIAL_CAPITAL
    warm = _warm_snapshot(result, initial)
    definition = {key: scenario.get(key) for key in ("scenario_id", "scenario_type", "symbols", "symbol_count", "omitted_symbol", "added_symbol", "base_size", "seed", "sample_size")}
    s_evidence = finalize_s_evidence(
        result.get("c6_s_evidence") or _empty_s_evidence(),
        equity_series,
        [str(item.date()) for item in result["_c6_states"][0].all_dates],
    )
    causal = build_causal_matrix(result, timeline, s_evidence, equity_series)
    data_identity = _data_identity(result)
    record = {"evaluation_id": f"{variant}::{scenario['scenario_id']}", "variant_id": variant, "scenario_id": scenario["scenario_id"], "scenario_definition": definition, "official_metrics": metrics, "orders": orders, "fills": fills, "cash_series": cash_series, "position_series": position_series, "equity_series": equity_series, "drawdown_series": drawdown_series, "risk_events": _risk_records(result), "action_lifecycle": _action_records(result), "exposure_series": exposure, "causal_matrix": causal, "warm_boundary": warm, "data_identity": data_identity, "intervention_601869": None}
    if variant.startswith("W"):
        record["_c6_score_trace"] = result["_c6_score_trace"]
    return record


def _manifest(name: str, spec: Mapping[str, Any]) -> dict[str, Any]:
    return {"name": name, "count": spec["count"], "unique_count": spec["unique_count"], "sha256": spec.get("sha256", spec.get("ordered_ids_sha256"))}


def _control_nodes() -> dict[str, list[str]]:
    """Only explicitly covered contracts may inherit an executed test result."""
    groups = {
        ("book-identity", "book_identity"): {
            "carried-winner": "carried_higher_priority_sell_remains_the_winner",
            "same-book-priority": "same_book_preserves_priority_target_and_reason_winner_order",
            "sibling-three-state": "same_real_book_in_three_states_keeps_one_sell_per_state",
            "stable-order": "suppression_audit_has_complete_book_identity_and_stable_order",
            "suppression-audit": "suppression_audit_has_complete_book_identity_and_stable_order"},
        ("fixed-reference", "fixed_reference_admission"): {
            "carried-new": "noncore_candidate_keeps_two_day_confirmation_at_size_six",
            "denominator-isolation": "fixed_reference_denominator_ignores_unrelated_tradable_symbols",
            "differing-signal-date": "emitting_sleeve_contributes_one_vote_per_symbol_and_batch",
            "duplicate-strategy": "emitting_sleeve_contributes_one_vote_per_symbol_and_batch",
            "eligible-newcomer": "fully_sold_symbol_without_buy_does_not_reserve_candidate_capacity",
            "emitting-sleeves": "emitting_sleeve_contributes_one_vote_per_symbol_and_batch",
            "exact-14": "exact_fourteen_keeps_expansion_score_and_confirmation_rules",
            "input-permutation": "equal_score_capacity_tie_break_is_input_permutation_invariant",
            "missing-score": "missing_emitting_sample_rejects_new_symbol_with_audit",
            "route-migration": "route_migration_bypasses_missing_fixed_reference_score"},
        ("retained-winner", "retained_winner"): {
            "call-order": "veto_is_exact_book_identity_and_sell_precedes_authorization",
            "next-batch-release": "veto_is_released_for_the_next_execution_batch",
            "ordinary-full-overlay-zero": "ordinary_full_sell_cannot_revive_buy_after_overlay_zero_fill",
            **{case: f"retained_winner_vetoes_same_batch_buy[{case}-{carry}]" for case, carry in (
                ("adv-zero", True), ("limit-blocked", True), ("missing-open", True), ("partial-fill", True),
                ("suspended", True), ("partial-sublot", False), ("odd-lot-full-liquidation", False))}},
    }
    mapping = {f"{prefix}/{control}": [f"tests/c6_non_economic/test_c6_{module}.py::test_{test}"]
            for (prefix, module), controls in groups.items() for control, test in controls.items()}
    module = "tests/c6_non_economic/test_c6_diagnostics.py::test_warm_boundary_"
    mapping["warm-boundary/causal-state-only"] = [
        module + "captures_raw_state_before_replay_without_aliases",
        module + "missing_capture_cannot_reconstruct_end_state",
        *(module + f"rejects_pre_window_economic_contamination[{case}]"
          for case in ("cash", "positions", "pending", "trades", "lock", "peak", "sticky", "safe_mode", "external_risk")),
    ]
    mapping.update({
        "governance/opinion-no-order-effect": ["tests/c6_non_economic/test_c6_diagnostics.py::test_governance_opinion_cannot_change_executable_signals"],
        "readiness/not-ready-fail-closed": ["tests/c6_non_economic/test_c6_diagnostics.py::test_readiness_not_ready_suppresses_buys_but_preserves_sells"],
        "no-drift/healthy-bull": ["tests/c6_non_economic/test_c6_diagnostics.py::test_healthy_bull_replay_and_s_noop_preserve_all_five_paths"],
        "s/no-op-control": ["tests/c6_non_economic/test_c6_diagnostics.py::test_healthy_bull_replay_and_s_noop_preserve_all_five_paths"],
        "s/common-prefix": ["tests/c6_non_economic/test_c6_diagnostics.py::test_s_common_prefix_uses_one_strict_boundary_and_full_noop_paths"],
        "s/coverage-fail-closed": [f"tests/c6_non_economic/test_c6_diagnostics.py::test_s_comparison_coverage_rejects_missing_duplicate_extra[{case}]" for case in ("missing", "duplicate", "extra")],
        "s/dominant-cluster-order": ["tests/c6_non_economic/test_c6_early_concentration.py::test_dominant_cluster_tie_break_is_label_order_and_input_invariant"],
    })
    return mapping


def _controls(prereg: Mapping[str, Any], name: str) -> list[dict[str, Any]]:
    import tempfile
    import xml.etree.ElementTree as ET
    spec = prereg["scenario_manifests"][name]
    mapping = _control_nodes()
    nodes = sorted({node for control in spec["ids"] for node in mapping.get(control, [])})
    observed: dict[str, bool] = {}
    properties: dict[str, dict[str, Any]] = {}
    with tempfile.TemporaryDirectory(prefix="c6-control-receipts-") as directory:
        report = Path(directory) / "tests.xml"
        if nodes:
            suite = subprocess.run([sys.executable, "-m", "pytest", "-q", f"--junitxml={report}", *nodes], check=False, capture_output=True)
            if suite.returncode not in {0, 1} or not report.is_file():
                raise RuntimeError("synthetic control execution produced no valid receipt")
            xml = report.read_text(encoding="utf-8")
            if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
                raise ValueError("control receipt cannot contain DTD or entity declarations")
            # Private pytest output, UTF-8 only; DTD/entity declarations rejected.
            for case in ET.fromstring(xml).iter("testcase"):  # nosec B314
                node = case.attrib["classname"].replace(".", "/") + ".py::" + case.attrib["name"]
                if node in observed:
                    raise RuntimeError("duplicate synthetic control test receipt")
                observed[node] = not any(case.find(tag) is not None for tag in ("failure", "error", "skipped"))
                properties[node] = {}
                for prop in case.findall("properties/property"):
                    key = prop.attrib["name"]
                    if key.startswith("c6.assertion."):
                        if key in properties[node]:
                            raise ValueError("duplicate synthetic assertion receipt")
                        properties[node][key] = json.loads(prop.attrib["value"])
    rows = []
    for control in spec["ids"]:
        required = mapping.get(control, [])
        receipt = {"control_id": control, "tests": [{"nodeid": node, "passed": observed.get(node)} for node in required], "coverage_complete": bool(required) and all(node in observed for node in required)}
        passed = receipt["coverage_complete"] and all(observed[node] for node in required)
        assertions = []
        for item in spec["assertions_by_control"][control]:
            if item["comparator"] != "equal":
                raise ValueError("unsupported synthetic assertion contract")
            key = "c6.assertion." + item["id"]
            values = [properties[node][key] for node in required if key in properties.get(node, {})]
            actual = values[0] if values and all(value == values[0] for value in values) else (passed if item["expected"] is True and control != "s/no-op-control" else None)
            matched = passed and type(actual) is type(item["expected"]) and actual == item["expected"]
            assertion_receipt = {**receipt, "assertion_id": item["id"], "observed_values": values}
            assertions.append({**item, "actual": actual, "passed": matched, "detail_sha256": hashlib.sha256(_canonical_bytes(assertion_receipt)).hexdigest()})
        passed = passed and all(item["passed"] for item in assertions)
        rows.append({"control_id": control, "passed": passed, "assertions": assertions, "economic_fields": None})
    return rows


def _produce_l1(args: argparse.Namespace) -> dict[str, Any]:
    from quantfusion.application import stress_artifacts, stress_scenarios
    from quantfusion.application.c6_bound_run import DiagnosticCheckpoint, execution_item_ids
    from quantfusion.application.c6_contract import load_preregistration, load_run_bindings, select_binding, strict_json_load

    prereg = load_preregistration(args.preregistration, repository=Path.cwd())
    bindings = load_run_bindings(args.bindings_file)
    binding = next(item for item in bindings["binding_records"] if item["record_id"] == args.binding_record_id)
    selected_binding = select_binding(bindings, binding["workflow_binding_id"], candidate_id=binding["candidate_id"])
    if selected_binding is not binding or binding["stage"] != "L1" or binding["source_revision"] != args.source_revision:
        raise ValueError("CLI identity does not select one exact L1 binding")
    manifests = prereg["scenario_manifests"]
    scenario_ids = Path(manifests["L1_ECONOMIC_SCENARIO_IDS"]["path"]).read_text().splitlines()
    validate_manifest_identity(scenario_ids, manifests["L1_ECONOMIC_SCENARIO_IDS"])
    plan = stress_scenarios._multi_seed_scenarios(random_samples=50, permutation_samples=50, seeds=(20260807, 20260817, 20260827))
    by_id = {item["scenario_id"]: item for item in plan}
    base = binding["candidate_id"] == "C6-Base"
    control_name = "L1_BASE_SYNTHETIC_CONTROL_IDS" if base else "L1_S_SYNTHETIC_CONTROL_IDS"
    control_rows = _controls(prereg, control_name)
    unverified = [row["control_id"] for row in control_rows if not row["passed"]]
    if unverified:
        raise ValueError(f"synthetic controls lack passing execution evidence: {unverified}")
    variants = manifests["L1_BASE_EVALUATION_MANIFEST"]["core_variant_order"] if base else ["C6-Base+S"]
    tasks = [(variant, by_id[scenario], "DEFAULT") for variant in variants for scenario in scenario_ids]
    checkpoint = DiagnosticCheckpoint.from_environment(execution_item_ids(binding, prereg), chunk_size=binding["runtime"]["checkpoint_every"])
    evaluations = checkpoint.map(_l1_evaluate, tasks, [f"evaluation/{variant}::{scenario['scenario_id']}" for variant, scenario, _ in tasks])
    if base:
        interventions = [(variant, by_id["add-one-13-601869"], "DEFAULT") for variant in manifests["L1_BASE_EVALUATION_MANIFEST"]["causal_intervention_order"]]
        # Six interdependent intervention rows are finalized and committed together.
        if checkpoint.chunk_size < len(interventions):
            raise ValueError("checkpoint chunk must hold all causal interventions")
        checkpoint.map(_l1_evaluate, interventions, [f"evaluation/{variant}::{scenario['scenario_id']}" for variant, scenario, _ in interventions], finalize=_attach_interventions)
    evaluations = checkpoint.items[:checkpoint.cursor].project("result")
    chosen = "C6-Base" if base else "C6-Base+S"
    controls = list(checkpoint.map(_identity, control_rows, [f"control/{item}" for item in manifests[control_name]["ids"]], workers=1))
    drift_tasks = [(chosen, by_id[item]) for item in manifests["L1_INSTRUMENTATION_NO_DRIFT_SCENARIO_IDS"]["ids"]]
    pairs = list(checkpoint.map(_no_drift_pair, drift_tasks, [f"no-drift/{item}" for item in manifests["L1_INSTRUMENTATION_NO_DRIFT_SCENARIO_IDS"]["ids"]]))
    specs = prereg["diagnostic_predicate_manifests"]["L1_APPLICABLE_DIAGNOSTIC_PREDICATES"]
    selected = select_records(evaluations, lambda item: item["variant_id"] == chosen)
    eval_name = "L1_BASE_EVALUATION_MANIFEST" if base else "L1_S_EVALUATION_MANIFEST"
    kind = "c6_l1_base" if base else "c6_l1_base_plus_s"
    payload = {"schema_version": 2, "kind": kind, "diagnostic_noncanonical": True, "evaluation_manifest": _manifest(eval_name, manifests[eval_name]), "evaluations": evaluations, "synthetic_control_manifest": _manifest(control_name, manifests[control_name]), "synthetic_controls": controls, "no_drift_manifest": _manifest("L1_INSTRUMENTATION_NO_DRIFT_SCENARIO_IDS", manifests["L1_INSTRUMENTATION_NO_DRIFT_SCENARIO_IDS"]), "no_drift_pairs": pairs, "diagnostic_predicates": []}
    common: list[dict[str, Any]] = []
    no_effect: list[dict[str, Any]] = []
    if base:
        payload["attribution_sensitivity"] = _attribution(evaluations)
    if not base:
        qualification = strict_json_load(Path(args.producer_export) / "payload.json")
        manifest = strict_json_load(Path(args.producer_export) / "manifest.json")
        base_payload = load_object(Path(args.base_producer_export) / "payload.json")
        identity = {"artifact_full_byte_sha256": args.producer_artifact_sha256, "attempt_id": manifest["attempt_id"], "binding_id": manifest["binding_id"], "logical_run_id": manifest["logical_run_id"], "workflow_run_id": manifest["workflow_run_id"]}
        common, no_effect = compare_s_paths(base_payload["evaluations"], evaluations, scenario_ids)
        payload.update({"base_producer_identity": qualification["base_producer_identity"], "qualification_producer_identity": identity, "common_prefix_comparisons": common, "no_effect_comparisons": no_effect})
    reference = stress_artifacts._load_initial_baseline_reference(Path(prereg["transition_reference"]["path"]))
    payload["diagnostic_predicates"] = _l1_predicate_rows(specs, selected, evaluations, controls, pairs, reference, manifests, base, common, no_effect)
    return payload


def build_parser() -> argparse.ArgumentParser:
    """Return the command shape frozen for the R-bound diagnostic runner."""
    parser = argparse.ArgumentParser(description="Run an R-bound C6 diagnostic batch")
    parser.add_argument("--preregistration", required=True)
    parser.add_argument("--bindings-file", required=True)
    parser.add_argument("--binding-record-id", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--producer-export")
    parser.add_argument("--producer-artifact-sha256")
    parser.add_argument("--base-producer-export")
    parser.add_argument("--base-producer-artifact-sha256")
    parser.add_argument("--output", required=True)
    return parser


def _identity(value: dict[str, Any]) -> dict[str, Any]:
    return value


def _no_drift_pair(task: tuple[str, Mapping[str, Any]]) -> dict[str, Any]:
    variant, scenario = task
    off = _l1_evaluate((variant, scenario, "OFF"))
    on = _l1_evaluate((variant, scenario, "ON"))
    a, b = off["native_path_hashes"], on["native_path_hashes"]
    return {"scenario_id": scenario["scenario_id"], "recording_off_path_hashes": a, "recording_on_path_hashes": b, "equal": a == b}


def _produce_l2(args: argparse.Namespace) -> dict[str, Any]:
    from quantfusion.application import stress_artifacts, stress_metrics, stress_scenarios
    from quantfusion.application.c6_bound_run import DiagnosticCheckpoint, execution_item_ids
    from quantfusion.application.c6_contract import load_preregistration, load_run_bindings, select_binding

    prereg = load_preregistration(args.preregistration, repository=Path.cwd())
    bindings = load_run_bindings(args.bindings_file)
    binding = next(
        item for item in bindings["binding_records"]
        if item["record_id"] == args.binding_record_id
    )
    selected = select_binding(
        bindings, binding["workflow_binding_id"], candidate_id=binding["candidate_id"]
    )
    if selected is not binding or binding["stage"] != "L2" or binding["source_revision"] != args.source_revision:
        raise ValueError("CLI identity does not select one exact L2 binding")
    manifest = prereg["scenario_manifests"]["L2_EXACT_SCENARIO_IDS"]
    ids = validate_manifest_identity(manifest["ids"], manifest)
    plan = stress_scenarios._multi_seed_scenarios(
        random_samples=50, permutation_samples=50,
        seeds=(20260807, 20260817, 20260827),
    )
    by_id = {item["scenario_id"]: item for item in plan}
    item_ids = execution_item_ids(binding, prereg)
    checkpoint = DiagnosticCheckpoint.from_environment(item_ids, chunk_size=binding["runtime"]["checkpoint_every"])
    results = list(checkpoint.map(_l2_evaluate, [by_id[item] for item in ids], item_ids))
    summary = stress_metrics._summary(results)
    for key in ("trades_worst", "date_symbol_side_buckets_worst", "sleeve_fills_worst"):
        summary[key] = int(summary[key])
    reference_path = Path(prereg["transition_reference"]["path"])
    reference = stress_artifacts._load_initial_baseline_reference(reference_path)
    payload = {
        "schema_version": 2, "kind": "c6_l2", "diagnostic_noncanonical": True,
        "scenario_manifest": {"name": "L2_EXACT_SCENARIO_IDS", "count": len(ids), "unique_count": len(set(ids)), "sha256": manifest["sha256"]},
        "summary": summary, "results": results,
        "diagnostic_predicates": _predicate_rows(
            prereg["diagnostic_predicate_manifests"]["L2_APPLICABLE_DIAGNOSTIC_PREDICATES"],
            results, reference, ids,
        ),
    }
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    """Produce the exact selected R-bound diagnostic payload."""
    args = build_parser().parse_args(argv)
    if len(args.source_revision) != 40 or any(
        character not in "0123456789abcdef" for character in args.source_revision
    ):
        raise ValueError("source_revision must be a lowercase 40-character Git SHA")
    if args.binding_record_id.endswith(".l2"):
        payload = _produce_l2(args)
    else:
        payload = _produce_l1(args)
    output = Path(args.output)
    if output.exists():
        raise ValueError("diagnostic output path already exists")

    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, payload)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the bound runner
    raise SystemExit(main())
