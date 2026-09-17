"""Reproduce the bounded AB5 candidate screen from a reconstructed source tree."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import pickle
import subprocess
import sys
import time
from pathlib import Path


def run(root: str, mode: str, scenario_id: str, out: str) -> None:
    os.chdir(root)
    sys.path.insert(0, root)
    from quantfusion.application.stress_scenarios import _multi_seed_scenarios
    from quantfusion.config.universe import SYMBOL_NAMES
    from quantfusion.engine.replay import ProductionReplayEngine

    scenarios = _multi_seed_scenarios(
        random_samples=50, permutation_samples=50,
        seeds=(20260807, 20260817, 20260827),
    )
    scenario = next(row for row in scenarios if row["scenario_id"] == scenario_id)
    cfg = {"account_risk_budget_enabled": False} if mode == "absent" else {}
    engine = ProductionReplayEngine(2_000_000, cfg=cfg)
    started = time.monotonic()
    with contextlib.redirect_stdout(io.StringIO()):
        result = engine.run(
            {code: SYMBOL_NAMES[code] for code in scenario["symbols"]},
            "2025-04-01", "2026-07-20",
            data_dir=f"{root}/data/market", regime_data_dir=f"{root}/data/regime",
            indicator_state="warm",
        )
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"{mode}-{scenario_id}"
    raw = out_dir / f"{name}.pickle"
    with raw.open("wb") as handle:
        pickle.dump(result, handle, protocol=5)
    curve = result["equity_curve"]
    trades = result["trades"]
    source_tree = subprocess.check_output(["git", "write-tree"], text=True).strip()
    summary = {k: result[k] for k in (
        "total_return", "annual_return", "max_drawdown", "sharpe", "calmar",
        "total_trades", "sleeve_fill_count", "date_symbol_side_count",
    )}
    summary.update(
        scenario=scenario, mode=mode, engine="ProductionReplayEngine",
        source_tree=source_tree,
        frozen_data_sha256=hashlib.sha256(Path("data/market/manifest.json").read_bytes()).hexdigest(),
        elapsed_s=time.monotonic() - started,
        terminal_wealth=1 + result["total_return"], final_cash=result.get("final_cash"),
        operation_days=len({trade.date for trade in trades}),
        commission=sum(trade.commission for trade in trades),
        stamp_duty=sum(trade.stamp_duty_cost for trade in trades),
        mean_cash_ratio=float((curve["cash"] / curve["assets"]).mean()),
        trade_notional=sum(trade.shares * trade.price for trade in trades),
        average_equity=float(curve["assets"].mean()),
        observation_count=len(result.get("account_budget_observations", [])),
        budget_status=result.get("account_risk_budget", {}).get("status"),
    )
    summary["turnover_notional_over_average_equity"] = summary["trade_notional"] / summary["average_equity"]
    summary["raw_sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
    (out_dir / f"{name}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    print(json.dumps({"name": name, "terminal_wealth": summary["terminal_wealth"],
                      "max_drawdown": summary["max_drawdown"],
                      "buckets": summary["date_symbol_side_count"]}))


if __name__ == "__main__":
    run(*sys.argv[1:])
