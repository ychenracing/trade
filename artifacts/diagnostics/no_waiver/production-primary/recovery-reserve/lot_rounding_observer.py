import sys
import json
import contextlib
import io
import math
from pathlib import Path

sys.path.insert(0, "/workspace/scratch/bf273963828a/trade-reserve")
from quantfusion.risk import account_budget as b
from quantfusion.engine.replay import ProductionReplayEngine
from quantfusion.application import stress_scenarios as s, stress_metrics as m
from quantfusion.config.paths import MARKET_DATA_DIR, REGIME_DATA_DIR
from quantfusion.config.universe import SYMBOL_NAMES

original = b.plan_account_risk_budget
records = []


def observed(*args, **kwargs):
    receipt, actions = original(*args, **kwargs)
    if (
        kwargs.get("preserve_strategy_valid_holdings")
        and not kwargs.get("risk_alert_active")
        and not receipt["observed_shock_confirmed"]
        and not receipt["reduction_suspended_during_episode"]
        and receipt["gross_before"] > receipt["gross_cap"]
    ):
        books = args[3]
        target = receipt["gross_before"] - receipt["gross_cap"]
        fraction = target / receipt["gross_before"]
        desired = [shares * fraction for _, _, _, shares, _ in books]
        reduced = [math.floor(q / 100) * 100 for q in desired]
        planned = sum(q * book[4] for q, book in zip(reduced, books))
        while planned + 1e-8 < target:
            eligible = [i for i, book in enumerate(books) if reduced[i] < book[3]]
            i = max(
                eligible, key=lambda i: ((desired[i] - reduced[i]) * books[i][4], -i)
            )
            extra = min(100, books[i][3] - reduced[i])
            reduced[i] += extra
            planned += extra * books[i][4]
        current = {a.symbol for a in actions}
        proposed = {book[1] for q, book in zip(reduced, books) if q}
        records.append(
            {
                "date": kwargs["date_str"],
                "current_symbols": sorted(current),
                "proposed_symbols": sorted(proposed),
                "required_relief": target,
                "current_relief": sum(a.shares * a.price for a in actions),
                "proposed_relief": planned,
                "current_orders": len(actions),
                "proposed_orders": sum(q > 0 for q in reduced),
            }
        )
    return receipt, actions


b.plan_account_risk_budget = observed
scenario = next(
    x
    for x in s._multi_seed_scenarios(
        random_samples=50, permutation_samples=50, seeds=s.DEFAULT_SEEDS
    )
    if x["scenario_id"] == "leave-one-out-603986"
)
with contextlib.redirect_stdout(io.StringIO()):
    r = ProductionReplayEngine(m.INITIAL_CAPITAL).run(
        {c: SYMBOL_NAMES[c] for c in scenario["symbols"]},
        m.START_DATE,
        m.END_DATE,
        data_dir=str(MARKET_DATA_DIR),
        regime_data_dir=str(REGIME_DATA_DIR),
        indicator_state="warm",
    )
result = {
    "non_mutating_observer": True,
    "wealth": 1 + r["total_return"],
    "max_drawdown": r["max_drawdown"],
    "buckets": r["date_symbol_side_count"],
    "records": records,
    "planned_symbol_dates_removed": sum(
        len(set(x["current_symbols"]) - set(x["proposed_symbols"])) for x in records
    ),
    "planned_orders_removed": sum(
        x["current_orders"] - x["proposed_orders"] for x in records
    ),
}
Path("/workspace/scratch/bf273963828a/lot-rounding-diagnosis.json").write_text(
    json.dumps(result, indent=2) + "\n"
)
print(json.dumps({k: v for k, v in result.items() if k != "records"}))
