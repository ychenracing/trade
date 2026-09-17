"""Read-only causal trace for the bounded account-risk root-cause replay."""
from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import inspect
import io
import json
import math
import os
import sys
from collections import defaultdict
from dataclasses import asdict, is_dataclass
from functools import wraps
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


def plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite value: {value!r}")
        return value
    try:
        import numpy as np
        if isinstance(value, np.generic):
            return plain(value.item())
    except ImportError:
        pass
    try:
        import pandas as pd
        if isinstance(value, pd.Timestamp):
            return value.strftime("%Y-%m-%d")
    except ImportError:
        pass
    if is_dataclass(value):
        return plain(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [plain(item) for item in value]
    if hasattr(value, "__dict__"):
        return plain(vars(value))
    return str(value)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def queue(items: Iterable[tuple[Any, Any]]) -> list[dict[str, Any]]:
    rows = []
    for signal, strategy in items:
        row = plain(signal)
        if not isinstance(row, dict):
            row = {"value": row}
        row["strategy_object"] = getattr(strategy, "name", None)
        rows.append(row)
    return rows


def snapshot(states: Sequence[Any], date: Any) -> dict[str, Any]:
    sleeves, cash, gross = [], 0.0, 0.0
    for state_index, state in enumerate(states):
        sleeve = state.sleeve
        cash += float(sleeve.cash)
        books = []
        for symbol, positions in sorted(sleeve.positions.items()):
            frame = state.data_map.get(symbol)
            mark = None if frame is None else float(
                sleeve._latest_close_on_or_before(frame, date)
            )
            for strategy, position in sorted(positions.items()):
                value = None if mark is None else float(position.shares * mark)
                gross += value or 0.0
                books.append({
                    "state_index": state_index,
                    "sleeve": str(sleeve.sleeve_name),
                    "symbol": str(symbol),
                    "strategy": str(strategy),
                    "shares": int(position.shares),
                    "mark": mark,
                    "market_value": value,
                    "entry_price": float(position.entry_price),
                    "entry_date": str(position.entry_date),
                })
        sleeves.append({
            "state_index": state_index,
            "sleeve": str(sleeve.sleeve_name),
            "cash": float(sleeve.cash),
            "regime": str(getattr(sleeve, "_regime_state", "UNKNOWN")),
            "positions": books,
            "pending": queue(state.pending),
            "risk_peak": float(getattr(sleeve.risk, "peak_assets", 0.0)),
            "risk_lifetime_peak": float(
                getattr(sleeve.risk, "lifetime_peak_assets", 0.0)
            ),
            "risk_lock": bool(getattr(sleeve.risk, "persistent_lock", False)),
        })
    return {"cash": cash, "gross_exposure": gross, "sleeves": sleeves}


def pending(snapshot_: Mapping[str, Any]) -> dict[tuple[int, int], dict[str, Any]]:
    return {
        (int(sleeve["state_index"]), index): row
        for sleeve in snapshot_.get("sleeves", [])
        for index, row in enumerate(sleeve.get("pending", []))
    }


def books(snapshot_: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        book
        for sleeve in snapshot_.get("sleeves", [])
        for book in sleeve.get("positions", [])
    ]


class Observer:
    """Wrap real score and budget methods, copying evidence only."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.scores: list[dict[str, Any]] = []
        self.latest: dict[str, dict[str, float]] = {}
        self.originals: list[tuple[Any, str, Any]] = []

    def patch(self, cls: Any, name: str, factory: Any) -> None:
        original = getattr(cls, name)
        self.originals.append((cls, name, original))
        setattr(cls, name, factory(original))

    def __enter__(self) -> "Observer":
        from quantfusion.engine.ensemble_allocation import EnsembleAllocationMixin
        self.patch(
            EnsembleAllocationMixin,
            "_overlay_allocation_score",
            self.score_wrapper,
        )
        self.patch(
            EnsembleAllocationMixin,
            "_apply_account_risk_budget",
            self.budget_wrapper,
        )
        return self

    def __exit__(self, *_: Any) -> bool:
        for cls, name, original in reversed(self.originals):
            setattr(cls, name, original)
        self.originals.clear()
        return False

    def score_wrapper(self, original: Any) -> Any:
        @wraps(original)
        def call(owner: Any, states: Sequence[Any], date: Any) -> Any:
            view = original(states, date)
            symbols = sorted(
                {s for state in states for s in state.sleeve.positions}
                | {signal.symbol for state in states for signal, _ in state.pending}
            )
            date_str = date.strftime("%Y-%m-%d")
            scores = {symbol: float(view(symbol)) for symbol in symbols}
            self.latest[date_str] = scores
            self.scores.append({
                "date": date_str,
                "scores": scores,
                "status": str(getattr(view, "status", "UNKNOWN")),
                "failures": plain(getattr(view, "failures", ())),
            })
            return view
        return call

    def budget_wrapper(self, original: Any) -> Any:
        signature = inspect.signature(original)

        @wraps(original)
        def call(owner: Any, *args: Any, **kwargs: Any) -> Any:
            bound = signature.bind(owner, *args, **kwargs)
            bound.apply_defaults()
            values = bound.arguments
            states, date, events = values["states"], values["date"], values["events"]
            before = snapshot(states, date)
            order_starts = [
                len(getattr(state.sleeve, "order_events", [])) for state in states
            ]
            event_start = len(events)
            result = original(owner, *args, **kwargs)
            after = snapshot(states, date)
            new_events = plain(events[event_start:])
            envelope = next((
                row for row in reversed(new_events)
                if row.get("event") == "account_budget_envelope"
            ), None)
            before_pending, after_pending = pending(before), pending(after)

            def identity(row: Mapping[str, Any]) -> tuple[Any, ...]:
                return (
                    row.get("direction"), row.get("symbol"),
                    row.get("strategy_name"), row.get("signal_date"),
                    row.get("reason"), row.get("strategy_object"),
                )

            remaining: dict[tuple[int, tuple[Any, ...]], list[dict[str, Any]]]
            remaining = defaultdict(list)
            for slot, row in after_pending.items():
                remaining[(slot[0], identity(row))].append(row)
            authorization = []
            for slot, pre in before_pending.items():
                if pre.get("direction") != "buy":
                    continue
                matches = remaining.get((slot[0], identity(pre)), [])
                post = matches.pop(0) if matches else None
                requested = int(pre.get("target_shares", 0) or 0)
                authorized = int(post.get("target_shares", 0) or 0) if post else 0
                authorization.append({
                    "slot": list(slot),
                    "symbol": pre.get("symbol"),
                    "strategy": pre.get("strategy_name"),
                    "signal_date": pre.get("signal_date"),
                    "reason": pre.get("reason"),
                    "requested_shares": requested,
                    "authorized_shares": authorized,
                    "authorization_ratio": authorized / requested if requested else 1.0,
                })
            existing_sells = {
                (slot[0], row.get("symbol"), row.get("strategy_name"), row.get("reason"))
                for slot, row in before_pending.items()
                if row.get("direction") == "sell"
            }
            new_sells = []
            for slot, row in after_pending.items():
                key = (slot[0], row.get("symbol"), row.get("strategy_name"), row.get("reason"))
                if row.get("direction") == "sell" and key not in existing_sells:
                    new_sells.append({**row, "state_index": slot[0]})
            order_events = []
            for state_index, (state, start) in enumerate(
                zip(states, order_starts, strict=True)
            ):
                for event in getattr(state.sleeve, "order_events", [])[start:]:
                    row = plain(event)
                    if not isinstance(row, dict):
                        row = {"value": row}
                    row.update(
                        state_index=state_index,
                        sleeve=str(state.sleeve.sleeve_name),
                    )
                    order_events.append(row)
            score = self.latest.get(date.strftime("%Y-%m-%d"), {})
            held = books(before)
            for book in held:
                book["allocation_score"] = score.get(str(book["symbol"]))
            self.rows.append({
                "date": date.strftime("%Y-%m-%d"),
                "equity": float(values["assets"]),
                "lifetime_peak": float(values["peak"]),
                "shock_floor": float(values["shock_floor"]),
                "preserve_strategy_valid_holdings": bool(
                    values["preserve_strategy_valid_holdings"]
                ),
                "risk_alert_active_argument": values["risk_alert_active"],
                "portfolio_evidence_buy_symbols": (
                    None if values["portfolio_evidence_buy_symbols"] is None
                    else sorted(values["portfolio_evidence_buy_symbols"])
                ),
                "before": before,
                "after": after,
                "books": held,
                "effective_buy_intents": [
                    row for row in before_pending.values()
                    if row.get("direction") == "buy"
                ],
                "buy_authorization": authorization,
                "new_budget_sell_plans": new_sells,
                "new_order_events": order_events,
                "envelope": envelope,
                "new_account_events": new_events,
            })
            return result
        return call


def symbols(pool: str, data_dir: Path, end: str) -> dict[str, str]:
    if pool == "core17":
        from quantfusion.config.universe import SYMBOL_NAMES
        return dict(SYMBOL_NAMES)
    from scripts.compare_universes import active_symbols_for_pool
    active = active_symbols_for_pool(pool, data_dir, end_date=end)
    if not active:
        raise ValueError(f"{pool} has no active symbols")
    return dict(active)


def metrics(result: Mapping[str, Any]) -> dict[str, Any]:
    import numpy as np
    trades, curve = list(result["trades"]), result["equity_curve"]
    gross = sum(abs(float(t.gross_value or t.shares * t.price)) for t in trades)
    commission = sum(float(t.commission) for t in trades)
    stamp = sum(float(t.stamp_duty_cost) for t in trades)
    mean_nav = float(curve["assets"].mean())
    conservation = float(
        (curve["assets"] - curve["cash"] - curve["position_value"]).abs().max()
    )
    return {
        "wealth_multiple": 1.0 + float(result["total_return"]),
        "final_assets": float(curve["assets"].iloc[-1]),
        "max_drawdown": abs(float(result["max_drawdown"])),
        "date_symbol_side_buckets": int(result["date_symbol_side_count"]),
        "active_fill_days": len({str(t.date) for t in trades}),
        "sleeve_fills": int(result["sleeve_fill_count"]),
        "trade_records": int(result["total_trades"]),
        "gross_traded_value": gross,
        "mean_nav": mean_nav,
        "turnover_over_average_equity": gross / mean_nav,
        "commission": commission,
        "stamp_duty": stamp,
        "commission_plus_stamp": commission + stamp,
        "minimum_cash": float(curve["cash"].min()),
        "maximum_conservation_error": conservation,
        "equity_rows": int(len(curve)),
        "finite": bool(
            np.isfinite(
                curve[["assets", "cash", "position_value"]].to_numpy(dtype=float)
            ).all()
            and all(math.isfinite(v) for v in (gross, mean_nav, commission, stamp))
        ),
        "account_risk_budget_enabled": bool(
            result["account_risk_budget"]["enabled"]
        ),
        "account_risk_budget_status": str(
            result["account_risk_budget"]["status"]
        ),
    }


def equity_rows(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    curve = result["equity_curve"].reset_index()
    date_column = curve.columns[0]
    rows = []
    for row in curve.to_dict("records"):
        row["date"] = plain(row.pop(date_column))
        rows.append(plain(row))
    return rows


def run(args: argparse.Namespace) -> None:
    source_root = Path(args.source_root).resolve()
    market_dir, regime_dir = Path(args.market_dir).resolve(), Path(args.regime_dir).resolve()
    output = Path(args.output).resolve()
    sys.path.insert(0, str(source_root))
    os.chdir(source_root)
    from quantfusion.engine.replay import ProductionReplayEngine

    universe = symbols(args.pool, market_dir, args.end)
    with Observer() as observer, contextlib.redirect_stdout(io.StringIO()):
        result = ProductionReplayEngine(2_000_000.0).run(
            universe,
            args.start,
            args.end,
            data_dir=str(market_dir),
            regime_data_dir=str(regime_dir),
            indicator_state="warm",
            warmup_calendar_days=365,
        )
    payload = {
        "schema_version": 1,
        "kind": "account_risk_capacity_root_cause_replay",
        "identity": {
            "mode": args.mode,
            "pool": args.pool,
            "window": args.window,
            "start": args.start,
            "end": args.end,
            "source_root_tree": os.popen("git write-tree").read().strip(),
            "source_head": os.popen("git rev-parse HEAD").read().strip(),
            "market_manifest_sha256": sha(market_dir / "manifest.json"),
            "input_identity_sha256": args.input_identity_sha256,
            "observer_sha256": sha(Path(__file__)),
            "symbols": sorted(universe),
        },
        "metrics": metrics(result),
        "equity_curve": equity_rows(result),
        "trades": plain(result.get("trades", [])),
        "order_events": plain(result.get("order_events", [])),
        "risk_events": plain(result.get("risk_events", [])),
        "route_sequence": plain(result.get("route_sequence", [])),
        "budget_ledger": observer.rows,
        "allocation_score_ledger": observer.scores,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, allow_nan=False
    ).encode("utf-8")
    with output.open("wb") as raw:
        with gzip.GzipFile(
            filename="", fileobj=raw, mode="wb", compresslevel=6, mtime=0
        ) as handle:
            handle.write(encoded)
    print(json.dumps({
        "mode": args.mode,
        "pool": args.pool,
        "window": args.window,
        "wealth": payload["metrics"]["wealth_multiple"],
        "budget_rows": len(observer.rows),
        "output_sha256": sha(output),
    }, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--market-dir", required=True)
    parser.add_argument("--regime-dir", required=True)
    parser.add_argument("--mode", choices=("incumbent", "candidate"), required=True)
    parser.add_argument(
        "--pool",
        choices=("core17", "pool_b", "pool_d", "pool_g", "pool_f"),
        required=True,
    )
    parser.add_argument("--window", choices=("long", "common"), required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--input-identity-sha256", required=True)
    parser.add_argument("--output", required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
