"""Bounded offline decision diagnostics, never a strategy promotion gate.

Synthetic point-in-time accounts are not human account history. Fixed-horizon
labels use the existing matcher and fees. Production parameters are untouched.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import contextlib
from dataclasses import asdict
import hashlib
import io
import json
import os
from pathlib import Path
import statistics
import tempfile
from typing import Any
from unittest.mock import patch

import pandas as pd

from quantfusion.account.models import AccountSnapshot
from quantfusion.application import account_scan
from quantfusion.config.engine import default_engine_config
from quantfusion.config.paths import MARKET_DATA_DIR, REGIME_DATA_DIR, PROJECT_ROOT
from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.config.profiles import config_for_symbol
from quantfusion.config.regime import REGIME_INDEX_FILES
from quantfusion.config.universe import SYMBOL_NAMES, VALIDATION_UNIVERSES
from quantfusion.data.providers import DataFetcher
from quantfusion.data.sessions import DEFAULT_CALENDAR_FILE, TradingCalendar, load_calendar
from quantfusion.domain.models import Signal
from quantfusion.engine.replay import ProductionReplayEngine
from quantfusion.engine.universe import BacktestEngine, SleeveBacktestEngine
from quantfusion.io.artifacts import atomic_json
from quantfusion.research.fingerprints import (
    _json_default, account_source_sha, canonical_sequence_sha,
    economic_sequence_fingerprints, engine_source_sha,
)
from quantfusion.strategy.trend import TurtleBreakoutStrategy

START, END, CAPITAL = "2025-04-01", "2026-07-20", 2_000_000.
WINDOWS = (5, 20)
REPLAYS = ("replay13", "replay17", "replay17-no-budget", "weak5", "weak5-no-budget")
SCOPE = {
    "start": START, "observation_cutoff": END, "sample_stride_sessions": 5,
    "label_windows_sessions": list(WINDOWS), "capital": CAPITAL,
    "sampling": "every fifth exchange session, anchored at the first session on/after start",
    "groups": "same-date score rank halves; odd median separate; ties disclosed",
    "labels": "one lot; next-session opening buy and opening sell h sessions later; native costs/limits/ADV",
    "account_history": "independent synthetic all-cash snapshots, not continuous historical account returns",
    "history_status": "previously reused history, not untouched out-of-sample",
    "pool_case": "existing 13-stock prefix versus current 17; selected before results",
    "risk_case": "continuous native 17-stock replay AB5 on/off; no peak reset or curve stitching",
    "weak_case": "existing five-stock pool, 2024-01-01..2024-12-31, continuous production replay AB5 on/off",
    "promotion": "diagnostic only; no economic candidate deployed",
}


def plain(value: Any) -> Any:
    return json.loads(json.dumps(value, default=_json_default, allow_nan=False))


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def symbol_config(code: str) -> dict:
    return config_for_symbol(code, name=SYMBOL_NAMES.get(code, code))


def identity(market_dir: Path, regime_dir: Path, calendar_file: Path) -> dict:
    return {
        "source_head": os.environ.get("GITHUB_SHA"),
        "engine_source_sha256": engine_source_sha(), "account_source_sha256": account_source_sha(),
        "diagnostic_source_sha256": file_sha(Path(__file__)),
        "engine_config_sha256": canonical_sequence_sha(default_engine_config()),
        "calendar_sha256": load_calendar(calendar_file).sha256,
        "lock_sha256": file_sha(PROJECT_ROOT / "requirements-lock.txt"),
        "market_files": {p.name: file_sha(p) for p in sorted(market_dir.glob("*.csv"))},
        "index_files": {p.name: file_sha(p) for p in sorted(regime_dir.glob("*.csv"))},
        "scope_sha256": canonical_sequence_sha(SCOPE),
    }


def _probe(symbol: str) -> tuple[SleeveBacktestEngine, TurtleBreakoutStrategy]:
    engine = SleeveBacktestEngine(CAPITAL, cfg=None, policy=PortfolioPolicy(),
                                  allocation_lookbacks=(20, 60, 120), sleeve_name="signal_label")
    names = {symbol: SYMBOL_NAMES.get(symbol, symbol)}
    engine._reset_run_state(names)
    engine.symbol_configs = engine._resolve_symbol_configs(names, None, "auto")
    return engine, TurtleBreakoutStrategy(symbol_config(symbol))


def execution_label(symbol: str, frame: pd.DataFrame, signal_date: str, horizon: int,
                    sessions: tuple[str, ...], cutoff: str) -> dict:
    """Independently funded native one-lot probe, not account performance.

    Weekend recommendations use the latest complete session; the next open is
    still strictly after the recommendation date. Missing/zero-volume sessions
    are unresolved rather than assumed suspension fills.
    """
    if horizon not in WINDOWS:
        raise ValueError("only preregistered 5/20-session labels are supported")
    label = {"status": "IMMATURE", "net_return": None, "horizon": horizon,
             "fills": [], "actual_human_fill": "UNKNOWN"}
    entry = bisect_right(sessions, signal_date)
    exit_index = entry + horizon
    if entry == 0 or exit_index >= len(sessions):
        label["status"] = "CALENDAR_OUT_OF_RANGE"
        return label
    label.update(entry_date=sessions[entry], exit_date=sessions[exit_index])
    if sessions[exit_index] > cutoff:
        return label
    expected = pd.to_datetime(sessions[entry-1:exit_index+1])
    if frame.empty or frame.index.has_duplicates or not expected.isin(frame.index).all():
        label["status"] = "MISSING_DATA"
        return label
    selected = frame.loc[expected, ["open", "close", "high", "low", "volume"]]
    if selected.isna().any().any() or not all(0 < float(v) < float("inf") for v in selected.to_numpy().ravel()):
        label["status"] = "UNVERIFIED_SESSION"
        return label
    engine, strategy = _probe(symbol)
    dates = {pd.Timestamp(day): i for i, day in enumerate(sessions)}
    entry_day, exit_day = pd.Timestamp(sessions[entry]), pd.Timestamp(sessions[exit_index])
    buy = Signal(symbol, strategy.name, "buy", 100,
                 float(frame.loc[pd.Timestamp(sessions[entry-1]), "close"]),
                 reason="fixed_horizon_signal_label", signal_date=sessions[entry-1])
    engine._execute_pending_signals([(buy, strategy)],
                                   {symbol: frame.loc[frame.index <= entry_day]}, entry_day, dates)
    if len(engine.trades) != 1 or engine.trades[0].shares != 100:
        label.update(status="ENTRY_BLOCKED", order_events=plain(engine.order_events))
        return label
    sell = Signal(symbol, strategy.name, "sell", 100,
                  float(frame.loc[pd.Timestamp(sessions[exit_index-1]), "close"]),
                  reason="fixed_horizon_signal_label", signal_date=sessions[exit_index-1])
    engine._execute_pending_signals([(sell, strategy)],
                                   {symbol: frame.loc[frame.index <= exit_day]}, exit_day, dates)
    label["fills"] = plain(engine.trades)
    if len(engine.trades) != 2 or engine.trades[-1].shares != 100:
        label.update(status="EXIT_BLOCKED", order_events=plain(engine.order_events))
        return label
    entry_cost = -engine.trades[0].net_cash_flow
    label.update(status="MATURE", net_return=engine.trades[-1].net_cash_flow/entry_cost-1.,
                 modeled_commission_and_duty=sum(t.commission+t.stamp_duty_cost for t in engine.trades),
                 execution_model="native matcher, standardized one-lot signal probe; not account return")
    return label


def _distribution(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "min": None, "median": None, "max": None, "mean": None}
    return {"count": len(values), "min": min(values), "median": statistics.median(values),
            "max": max(values), "mean": statistics.mean(values)}


def _group_summary(rows: list[dict], horizon: int) -> dict:
    labels = [(r["date"], r.get("labels", {}).get(str(horizon), {"status": "MISSING_LABEL"})) for r in rows]
    daily, intervals = defaultdict(list), {}
    for day, label in labels:
        if label["status"] == "MATURE":
            daily[day].append(label["net_return"])
            intervals[day] = (label["entry_date"], label["exit_date"])
    nonoverlap, last_exit = [], ""
    for day in sorted(daily):
        if intervals[day][0] > last_exit:
            nonoverlap.append(statistics.mean(daily[day]))
            last_exit = intervals[day][1]
    return {
        "events": len(rows), "dates": len({r["date"] for r in rows}),
        "statuses": dict(Counter(label["status"] for _, label in labels)), "mature_dates": len(daily),
        "date_weighted_mean": statistics.mean(statistics.mean(v) for v in daily.values()) if daily else None,
        "nonoverlap_dates": len(nonoverlap), "nonoverlap_mean": statistics.mean(nonoverlap) if nonoverlap else None,
    }


def score_summary(rows: list[dict]) -> dict:
    """Descriptive date-weighted contrasts, not independent-event significance."""
    by_date = defaultdict(list)
    for row in rows:
        by_date[row["date"]].append(row)
    tied = sum(sum(n for n in Counter(r["score"] for r in day).values() if n > 1) for day in by_date.values())
    names = sorted({name for row in rows for name in row.get("score_components", {})})
    result = {
        "date_count": len(by_date), "event_count": len(rows),
        "score_distribution": _distribution([r["score"] for r in rows]),
        "components": {name: _distribution([r["score_components"][name] for r in rows
                       if name in r.get("score_components", {})]) for name in names},
        "momentum_capped_fraction": sum(r.get("score_components", {}).get("momentum") == .25 for r in rows)/len(rows) if rows else None,
        "tied_event_fraction": tied/len(rows) if rows else None,
        "confirmation_counts": dict(Counter(str(r["confirmation_count"]) for r in rows)),
        "candidates_per_date": {day: len(items) for day, items in sorted(by_date.items())}, "windows": {},
        "inference": "descriptive only; same-date dependence and reused history prevent independent-event significance",
    }
    for horizon in WINDOWS:
        window = {group: _group_summary([r for r in rows if r.get("group") == group], horizon)
                  for group in ("high", "low", "median", "single")}
        window["by_confirmation"] = {str(count): _group_summary(
            [r for r in rows if r["confirmation_count"] == count], horizon) for count in (1, 2, 3)}
        paired = []
        for records in by_date.values():
            groups = defaultdict(list)
            for row in records:
                label = row.get("labels", {}).get(str(horizon), {})
                if label.get("status") == "MATURE":
                    groups[row.get("group", "single")].append(label["net_return"])
            if groups["high"] and groups["low"]:
                paired.append(statistics.mean(groups["high"])-statistics.mean(groups["low"]))
        window["paired_high_minus_low"] = _distribution(paired)
        result["windows"][str(horizon)] = window
    return result


def advice_identity(advice: dict, frames: dict[str, pd.DataFrame], calendar: TradingCalendar) -> dict:
    """Verify current code/config/calendar and observed market prefixes only."""
    bad = []
    for key, value in {"account_code_sha256": account_source_sha(),
                       "engine_config_sha256": canonical_sequence_sha(default_engine_config())}.items():
        if advice.get(key) != value:
            bad.append(key)
    if advice.get("scan_dates", {}).get("calendar_sha256") != calendar.sha256:
        bad.append("calendar_sha256")
    evidence, day = advice.get("market_evidence", {}), advice.get("as_of")
    if not evidence:
        bad.append("market_evidence")
    if not day:
        bad.append("as_of")
    if not advice.get("account_snapshot_sha256"):
        bad.append("account_snapshot_sha256")
    if day:
        start = pd.Timestamp(day)-pd.Timedelta(days=700)
        for code, entry in evidence.items():
            frame = frames.get(code)
            if frame is None:
                bad.append(f"market:{code}")
                continue
            observed = frame.loc[(frame.index >= start) & (frame.index <= pd.Timestamp(day))]
            if hashlib.sha256(observed.to_csv(index=True).encode()).hexdigest() != entry.get("frame_sha256"):
                bad.append(f"market:{code}")
            if canonical_sequence_sha(symbol_config(code)) != entry.get("config_sha256"):
                bad.append(f"config:{code}")
    return {"status": "IDENTITY_UNVERIFIED" if bad else "MARKET_CODE_CONFIG_VERIFIED",
            "missing_or_mismatched": sorted(bad),
            "snapshot_bytes": "hash recorded; original private snapshot not supplied to label evaluator",
            "index_bytes": "saved hashes retained; original dated index files not re-certified",
            "actual_human_fill": "UNKNOWN"}


def _sell_availability(action: dict, frame: pd.DataFrame | None, day: str,
                       calendar: TradingCalendar, cutoff: str) -> dict:
    next_day = calendar.next_session(day)
    result = {"symbol": action["symbol"], "next_session": next_day,
              "recommended_shares": action.get("recommended_shares"),
              "actual_human_fill": "UNKNOWN", "status": "IMMATURE"}
    if next_day > cutoff:
        return result
    if frame is None or pd.Timestamp(next_day) not in frame.index:
        result["status"] = "MISSING_DATA"
        return result
    if float(frame.loc[pd.Timestamp(next_day), "volume"]) <= 0:
        result["status"] = "UNVERIFIED_SESSION"
        return result
    quantity = action.get("recommended_shares")
    if type(quantity) is not int or quantity <= 0:
        result["status"] = "SELLABLE_QUANTITY_UNAVAILABLE"
        return result
    engine, strategy = _probe(action["symbol"])
    opened = float(frame.loc[pd.Timestamp(next_day), "open"])
    signal = Signal(action["symbol"], strategy.name, "sell", quantity, signal_date=day)
    blocked = engine._opening_limit_state(signal, frame, pd.Timestamp(next_day), opened)
    result.update(status="OPEN_LIMIT_DOWN" if blocked == "sell_blocked" else "OPEN_PRICE_REVIEW_ONLY",
                  observed_open=opened,
                  caveat="No position replay or fill asserted; original continuous position state and execution record required")
    return result


def evaluate_advice(advice: dict, frames: dict[str, pd.DataFrame], calendar: TradingCalendar, cutoff: str) -> dict:
    verification = advice_identity(advice, frames, calendar)
    output = {"advice_sha256": canonical_sequence_sha(advice), "identity": verification,
              "observed_until": cutoff, "rows": []}
    if verification["status"] != "MARKET_CODE_CONFIG_VERIFIED":
        return output
    candidates = advice.get("candidate_diagnostics", [])
    if not candidates:
        candidates = [dict(symbol=a["symbol"], score=a.get("confidence", 0.),
                           confirmation_count=len(a.get("strategies", [])), score_components={},
                           indicative_target_shares=a.get("indicative_target_shares", 0))
                      for a in advice.get("actions", []) if a["action"] == "BUY_CANDIDATE"]
    ordered = sorted(candidates, key=lambda r: (-r["score"], r["symbol"]))
    n = len(ordered)
    for i, candidate in enumerate(ordered):
        group = "single" if n == 1 else "high" if i < n//2 else "low" if i >= (n+1)//2 else "median"
        row = {**candidate, "date": advice["as_of"], "group": group,
               "actual_human_fill": "UNKNOWN", "labels": {}}
        for horizon in WINDOWS:
            row["labels"][str(horizon)] = execution_label(candidate["symbol"], frames[candidate["symbol"]],
                advice["as_of"], horizon, calendar.sessions, cutoff)
        output["rows"].append(row)
    output["sell_recommendations"] = [_sell_availability(a, frames.get(a["symbol"]), advice["as_of"], calendar, cutoff)
        for a in advice.get("actions", []) if a["action"] in {"SELL", "REDUCE_REVIEW"}]
    return output


def _load_frames(market_dir: Path, cutoff: str) -> dict[str, pd.DataFrame]:
    codes = set(SYMBOL_NAMES) | set(PortfolioPolicy().regime_symbols)
    return {code: DataFetcher.load_stock_data(code, "2019-01-01", cutoff, data_dir=str(market_dir)) for code in sorted(codes)}


def collect_scores(output: Path, market_dir: Path, regime_dir: Path, calendar_file: Path) -> dict:
    calendar, frames = load_calendar(calendar_file), _load_frames(market_dir, END)
    days = [d for d in calendar.sessions if START <= d <= END][::SCOPE["sample_stride_sessions"]]
    original_loader = DataFetcher.load_stock_data
    rows, scans, selection = [], [], []
    for day in days:
        with tempfile.TemporaryDirectory(prefix="trade-advice-") as temporary:
            indices = Path(temporary)
            for code in REGIME_INDEX_FILES.values():
                original_loader(code, "2019-01-01", day, data_dir=str(regime_dir)).to_csv(indices / f"{code}.csv", index_label="date")
            snapshot = AccountSnapshot(3, "synthetic-research", day, CAPITAL, CAPITAL, ())
            raw_snapshot = {**asdict(snapshot), "positions": {}}

            def local_loader(code, start_date, end_date, data_dir=None, cache_dir=None):
                if data_dir is not None:
                    return original_loader(code, start_date, end_date, data_dir=data_dir)
                frame = frames[code]
                return frame.loc[(frame.index >= pd.Timestamp(start_date)) & (frame.index <= pd.Timestamp(end_date))].copy()

            # Adapt acquisition only; eligibility, score, size, risk and coverage remain production calls.
            with patch.object(DataFetcher, "load_stock_data", side_effect=local_loader), patch.object(
                account_scan.data_contracts, "refresh_regime_indices", return_value={}
            ):
                try:
                    advice = account_scan.AccountSignalEngine(cache_dir=str(indices / "unused-cache"),
                        regime_data_dir=str(indices), calendar_file=calendar_file).run(
                            snapshot, dict(SYMBOL_NAMES), as_of=day, expected_account_id="synthetic-research")
                except (OSError, ValueError) as exc:
                    scans.append({"date": day, "status": "INPUT_UNAVAILABLE", "reason": str(exc)})
                    continue
            advice["account_snapshot_sha256"] = hashlib.sha256(json.dumps(raw_snapshot, sort_keys=True, allow_nan=False).encode()).hexdigest()
            advice["research_context"] = "synthetic independent same-day snapshot; not human history"
            saved = output / "advice" / f"account_signals_{day}.json"
            atomic_json(advice, saved)
            evaluation = evaluate_advice(json.loads(saved.read_text()), frames, calendar, END)
            atomic_json(evaluation, output / "evaluations" / f"{day}.json")
            scans.append({"date": day, "status": evaluation["identity"]["status"],
                "route": advice["deployment_decision"]["name"], "data_complete": advice["data_complete"],
                "identity_gaps": evaluation["identity"]["missing_or_mismatched"]})
            if advice["deployment_decision"]["name"] == "frozen_trend_engine":
                rows.extend(evaluation["rows"])
            candidates, slots = advice.get("candidate_diagnostics", []), advice["candidate_slots"]
            actual = {c["symbol"] for c in candidates if c["selected_for_slots"]}
            lexical = {c["symbol"] for c in sorted(candidates, key=lambda x: x["symbol"])[:slots]}
            selection.append({"date": day, "candidates": len(candidates), "slots": slots,
                "actual_selected": sorted(actual), "lexical_selected": sorted(lexical), "selection_changed": actual != lexical,
                "positive_advice_quantities": sum(c["indicative_target_shares"] > 0 for c in candidates),
                "comparison": "slot selection only, not a counterfactual portfolio return"})
    report = {"scope": SCOPE, "identity": identity(market_dir, regime_dir, calendar_file),
        "scans": scans, "rows": rows, "selection": selection, "summary": score_summary(rows),
        "production_change": "NONE", "canonical": False,
        "validity_conclusion": "Descriptive evidence only; synthetic states and reused history do not establish deployed score gains."}
    atomic_json(report, output / "score.json")
    return {"summary": report["summary"], "scan_statuses": dict(Counter(r["status"] for r in scans)),
            "selection_change_dates": sum(r["selection_changed"] for r in selection), "production_change": "NONE"}


def risk_attribution(result: dict) -> dict:
    receipts = sorted([e for e in result.get("risk_events", []) if e.get("event") == "account_budget_envelope"], key=lambda e: e["date"])
    trades = result.get("trades", [])
    episodes, current = [], []
    for receipt in receipts:
        binding = receipt.get("buy_scale", 1.) < 1. or receipt.get("gross_before", 0.) > receipt.get("gross_cap", float("inf"))+1e-8
        if binding:
            current.append(receipt)
        elif current:
            episodes.append({"records": current, "release_date": receipt["date"], "right_censored": False})
            current = []
    if current:
        episodes.append({"records": current, "release_date": None, "right_censored": True})
    for episode in episodes:
        records = episode.pop("records")
        start, end, release = records[0]["date"], records[-1]["date"], episode["release_date"]
        first_buy = next((t["date"] for t in trades if release and t["date"] >= release and t["direction"] == "buy"), None)
        episode.update(start=start, last_binding_date=end, binding_sessions=len(records), first_buy_after_release=first_buy,
            first_receipt=records[0], last_receipt=records[-1], blocked_buy_shares=sum(r.get("buy_shares_removed", 0) for r in records),
            other_risk_events=dict(Counter(e["event"] for e in result.get("risk_events", []) if start <= e["date"] <= end and e["event"] != "account_budget_envelope")))
    reductions = [t for t in trades if t["direction"] == "sell" and "account_budget_trim" in t.get("reason", "")]
    return {"receipt_count": len(receipts), "episodes": episodes,
        "blocked_buy_shares": sum(r.get("buy_shares_removed", 0) for r in receipts),
        "planned_reduction_orders": sum(r.get("new_reduction_orders", 0) for r in receipts), "filled_budget_reductions": len(reductions),
        "budget_reduction_fees": sum(t.get("commission", 0.)+t.get("stamp_duty_cost", 0.) for t in reductions),
        "order_reasons": dict(Counter(e.get("event", "unknown") for e in result.get("order_events", []))),
        "interpretation": "plans are not fills; removed shares are repeated intents, not unique forgone holdings or profit"}


def _first_difference(left: list[dict], right: list[dict]) -> str | None:
    a, b = defaultdict(list), defaultdict(list)
    for records, target in ((left, a), (right, b)):
        for row in records:
            target[row["date"]].append(row)
    return next((d for d in sorted(set(a) | set(b)) if canonical_sequence_sha(a[d]) != canonical_sequence_sha(b[d])), None)


def pool_divergence(smaller: dict, larger: dict) -> dict:
    first_trade = _first_difference(smaller.get("trades", []), larger.get("trades", []))
    first_order = _first_difference(smaller.get("order_events", []), larger.get("order_events", []))
    first_risk = _first_difference(smaller.get("risk_events", []), larger.get("risk_events", []))
    boundary = min(d for d in (first_trade, first_order) if d) if first_trade or first_order else None
    before, at_boundary = {}, {}
    for name, result in (("13", smaller), ("17", larger)):
        cash, positions = CAPITAL, Counter()
        for trade in result.get("trades", []):
            if boundary and trade["date"] < boundary:
                cash += trade["net_cash_flow"]
                positions[trade["symbol"]] += trade["shares"] * (1 if trade["direction"] == "buy" else -1)
        before[name] = {"cash": cash, "holdings": {k: v for k, v in sorted(positions.items()) if v}}
        at_boundary[name] = {key: [e for e in result.get(key, []) if e["date"] == boundary] for key in ("trades", "order_events", "risk_events")}
    return {"first_trade_divergence": first_trade, "first_order_divergence": first_order, "first_risk_divergence": first_risk,
        "boundary": boundary, "before": before, "at_boundary": at_boundary,
        "effective_policies": {"13": smaller.get("effective_portfolio_policy"), "17": larger.get("effective_portfolio_policy")},
        "interpretation": "observed first divergence and continuous ledgers; pool size also changes existing effective policy; not a single-contributor causal proof"}


def run_replay(task: str, output: Path, market_dir: Path, regime_dir: Path, calendar_file: Path) -> dict:
    weak = task.startswith("weak5")
    count = 5 if weak else 13 if task == "replay13" else 17
    codes = next(c for c in VALIDATION_UNIVERSES.values() if len(c) == count)
    config = {"account_risk_budget_enabled": not task.endswith("no-budget")}
    start, end = ("2024-01-01", "2024-12-31") if weak else (START, END)
    names = {c: SYMBOL_NAMES[c] for c in codes}
    with contextlib.redirect_stdout(io.StringIO()):
        result = (ProductionReplayEngine(CAPITAL, cfg=config).run(names, start, end,
                    data_dir=str(market_dir), regime_data_dir=str(regime_dir), indicator_state="warm") if weak else
                  BacktestEngine(CAPITAL, cfg=config).run(names, start, end, data_dir=str(market_dir), indicator_state="warm"))
    records = plain({key: result.get(key, []) for key in ("trades", "order_events", "risk_events", "fusion_events", "regime_state_series", "pending_signals")})
    records["equity_curve"] = plain(result["equity_curve"].reset_index().to_dict("records"))
    records["effective_portfolio_policy"] = result.get("effective_portfolio_policy")
    records["production_replay"] = plain(result.get("production_replay"))
    metrics = {key: result[key] for key in ("final_assets", "total_return", "max_drawdown", "total_trades", "date_symbol_side_count")}
    metrics["fees"] = sum(t["commission"]+t["stamp_duty_cost"] for t in records["trades"])
    metrics["gross_turnover_over_initial_capital"] = sum(t["gross_value"] for t in records["trades"])/CAPITAL
    report = {"scope": SCOPE, "identity": identity(market_dir, regime_dir, calendar_file),
        "run": {"symbols": list(codes), "config_override": config, "start": start, "end": end,
                "engine": "ProductionReplayEngine" if weak else "BacktestEngine", "initial_state": "empty continuous canonical replay"},
        "metrics": plain(metrics), "fingerprints": economic_sequence_fingerprints(result), "records": records,
        "risk_attribution": risk_attribution(records), "canonical": False, "production_change": "NONE"}
    atomic_json(report, output / f"{task}.json")
    return {"task": task, "metrics": report["metrics"], "binding_episodes": len(report["risk_attribution"]["episodes"])}


def summarize_runs(output: Path) -> dict:
    reports = {task: json.loads((output / f"{task}.json").read_text()) for task in REPLAYS}
    if len({canonical_sequence_sha(r["identity"]) for r in reports.values()}) != 1:
        raise ValueError("comparisons require identical source/data/config/scope identities")
    comparisons = {}
    for name in ("replay17", "weak5"):
        on, off = reports[name], reports[f"{name}-no-budget"]
        if any(on["run"][k] != off["run"][k] for k in ("symbols", "start", "end", "engine", "initial_state")):
            raise ValueError("counterfactual has a different universe, window, route or initial state")
        comparisons[name] = {"on": on["metrics"], "off": off["metrics"],
            "on_minus_off": {k: on["metrics"][k]-off["metrics"][k] for k in on["metrics"]}, "attribution": on["risk_attribution"]}
    report = {"identity": reports["replay17"]["identity"], "scope": SCOPE, "risk": comparisons,
        "pool": pool_divergence(reports["replay13"]["records"], reports["replay17"]["records"]),
        "interpretation": "same-condition continuous counterfactual; net wealth, drawdown, fees and turnover; no removed-intent upside sum",
        "production_change": "NONE", "canonical": False, "candidate_conclusion": "Attribution is not evidence of a deployable isolated defect; no relaxation or promotion."}
    atomic_json(report, output / "risk-pool.json")
    return {"risk": {name: {k: v[k] for k in ("on", "off", "on_minus_off")} for name, v in comparisons.items()},
        "first_trade_divergence": report["pool"]["first_trade_divergence"], "first_order_divergence": report["pool"]["first_order_divergence"], "production_change": "NONE"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", choices=("score", *REPLAYS, "summarize", "evaluate"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--market-dir", type=Path, default=MARKET_DATA_DIR)
    parser.add_argument("--regime-dir", type=Path, default=REGIME_DATA_DIR)
    parser.add_argument("--calendar-file", type=Path, default=DEFAULT_CALENDAR_FILE)
    parser.add_argument("--advice", type=Path, help="private account_signals JSON; this command never uploads it")
    parser.add_argument("--cutoff", default=END, help="label observation cutoff, not a strategy parameter")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with contextlib.redirect_stdout(io.StringIO()):
        if args.task == "score":
            result = collect_scores(args.output, args.market_dir, args.regime_dir, args.calendar_file)
        elif args.task == "summarize":
            result = summarize_runs(args.output)
        elif args.task == "evaluate":
            if args.advice is None:
                parser.error("evaluate requires --advice")
            result = evaluate_advice(json.loads(args.advice.read_text()), _load_frames(args.market_dir, args.cutoff), load_calendar(args.calendar_file), args.cutoff)
            atomic_json(result, args.output / "evaluation.json")
            result = {"identity": result["identity"], "evaluated_rows": len(result["rows"])}
        else:
            result = run_replay(args.task, args.output, args.market_dir, args.regime_dir, args.calendar_file)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
