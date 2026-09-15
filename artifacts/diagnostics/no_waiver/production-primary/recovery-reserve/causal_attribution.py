import pickle
import sys
import json
import collections
import hashlib
from pathlib import Path

root = Path("/workspace/scratch/bf273963828a")
sys.path.insert(0, str(root / "trade-reserve"))
out = (
    root
    / "trade-final/artifacts/diagnostics/no_waiver/production-primary/recovery-reserve"
)
causal = []
for sid in ["prefix-17", "leave-one-out-603986", "add-one-05-002384"]:
    p = root / "recovery-causal-baseline" / f"{sid}.pkl"
    r = pickle.loads(p.read_bytes())
    c = r["equity_curve"]
    peak = c.assets.cummax()
    last = r["trades"][-1].date
    budget = r["account_risk_budget"]["latest"]
    causal.append(
        {
            "scenario_id": sid,
            "raw_sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            "wealth": 1 + r["total_return"],
            "drawdown": abs(r["max_drawdown"]),
            "last_trade_date": last,
            "cash_only_sessions_after_last_trade": int(
                ((c.index > last) & (c.position_value == 0)).sum()
            ),
            "final_equity": float(c.assets.iloc[-1]),
            "lifetime_peak": float(peak.iloc[-1]),
            "lifetime_floor": float(0.82 * peak.iloc[-1]),
            "final_remaining_budget": budget["remaining_loss_budget"],
            "final_gross_cap": budget["gross_cap"],
        }
    )
a = pickle.load(
    open(root / "proportional-reserve-screen/leave-one-out-603986.pkl", "rb")
)
b = pickle.load(open(root / "reserve-lots-screen/leave-one-out-603986.pkl", "rb"))


def ledger(r):
    d = collections.defaultdict(float)
    for t in r["trades"]:
        d[t.symbol] += t.net_cash_flow
    assert abs(sum(d.values()) + 2e6 - r["equity_curve"].assets.iloc[-1]) < 1e-6
    return d


la, lb = ledger(a), ledger(b)
delta = [
    {
        "symbol": s,
        "proportional_net_cash_flow": la[s],
        "lots_net_cash_flow": lb[s],
        "difference_lots_minus_proportional": lb[s] - la[s],
    }
    for s in sorted(set(la) | set(lb))
]
delta.sort(key=lambda x: x["difference_lots_minus_proportional"])
curves = a["equity_curve"].join(b["equity_curve"], lsuffix="_a", rsuffix="_b")
different = curves[(curves.assets_a - curves.assets_b).abs() > 1e-6]
result = {
    "baseline_floor_diagnosis": causal,
    "lot_effect_attribution": {
        "classification": "Exact full-period cash-ledger accounting; not a causal counterfactual or authorization for symbol-specific rules. Both final portfolios have zero positions.",
        "wealth_difference": b["total_return"] - a["total_return"],
        "first_equity_divergence": str(different.index[0].date()),
        "symbol_contributions": delta,
    },
}
(out / "causal-attribution.json").write_text(json.dumps(result, indent=2) + "\n")
print(
    json.dumps(
        {
            "baseline": causal,
            "first_divergence": str(different.index[0].date()),
            "top_negative_deltas": delta[:3],
        },
        indent=2,
    )
)
