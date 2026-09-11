import unittest

from c6_v42_gap_projection import classify_record, select_target_records


def record(scenario, *, breach="2025-01-04", events=(), actions=(), orders=(), fills=(), mdd=0.19):
    return {
        "variant_id": "C6-Base+AB5",
        "scenario_id": scenario,
        "official_metrics": {"max_drawdown": mdd},
        "drawdown_series": [
            {"timestamp": "2025-01-03", "drawdown": -0.17},
            {"timestamp": breach, "drawdown": -mdd},
        ],
        "risk_events": [
            {"timestamp": item["date"], "raw_event": {"event": "account_budget_envelope", **item}}
            for item in events
        ],
        "action_lifecycle": list(actions),
        "orders": list(orders),
        "fills": list(fills),
        "equity_series": [],
        "exposure_series": [],
    }


def action(*, filled=0, execution="2025-01-03", planned=100):
    return {
        "action_id": "a1",
        "reason": "account_budget_trim",
        "observation_timestamp": "2025-01-02",
        "execution_timestamp": execution,
        "planned_shares": planned,
        "retained_shares": planned,
        "filled_shares": filled,
    }


def order(*, status, filled=0, blocked=None, execution="2025-01-03"):
    return {
        "order_ordinal": 7,
        "action_id": "a1",
        "reason": "account_budget_trim",
        "decision_timestamp": "2025-01-02",
        "execution_timestamp": execution,
        "authorized_shares": 100,
        "filled_shares": filled,
        "status": status,
        "blocked_reason": blocked,
    }


class GapProjectionTests(unittest.TestCase):
 def test_classifies_signal_that_only_binds_at_the_breach_as_late(self):
    row = record("late", events=[
        {"date": "2025-01-03", "buy_envelope_binding": False,
         "new_reduction_orders": 0, "buy_shares_removed": 0},
        {"date": "2025-01-04", "buy_envelope_binding": True,
         "new_reduction_orders": 1, "buy_shares_removed": 0},
    ])
    self.assertEqual(classify_record(row)["primary_classification"], "SIGNAL_TOO_LATE")


 def test_classifies_prebreach_unfilled_budget_order_as_execution_blocked(self):
    row = record(
        "blocked",
        events=[{"date": "2025-01-02", "buy_envelope_binding": True,
                 "new_reduction_orders": 1, "buy_shares_removed": 0}],
        actions=[action()],
        orders=[order(status="blocked", blocked="limit_down")],
    )
    self.assertEqual(classify_record(row)["primary_classification"], "EXECUTION_BLOCKED")


 def test_classifies_filled_prebreach_relief_followed_by_breach_as_insufficient(self):
    row = record(
        "small",
        events=[{"date": "2025-01-02", "buy_envelope_binding": True,
                 "new_reduction_orders": 1, "buy_shares_removed": 0}],
        actions=[action(filled=100)],
        orders=[order(status="filled", filled=100)],
        fills=[{"order_ordinal": 7, "timestamp": "2025-01-03", "shares": 100}],
    )
    result = classify_record(row)
    self.assertEqual(result["primary_classification"], "ACTION_INSUFFICIENT")
    self.assertEqual(result["prebreach_budget_filled_shares"], 100)


 def test_selects_five_fixed_representatives_and_nearest_other_residual(self):
    fixed = [
        "random-20260817-03-027",
        "random-20260807-12-040",
        "random-20260817-08-023",
        "random-20260807-05-040",
        "leave-one-out-300308",
    ]
    rows = [record(name, mdd=0.20 + index / 1000) for index, name in enumerate(fixed)]
    rows += [record("nearest", mdd=0.18001), record("farther", mdd=0.181)]
    selected = select_target_records(rows)
    self.assertEqual([item["scenario_id"] for item in selected[:5]], fixed)
    self.assertEqual(selected[5]["scenario_id"], "nearest")


 def test_rejects_a_fixed_representative_that_is_not_a_residual(self):
    rows = [record(name) for name in [
        "random-20260817-03-027", "random-20260807-12-040",
        "random-20260817-08-023", "random-20260807-05-040",
        "leave-one-out-300308", "nearest",
    ]]
    rows[0]["official_metrics"]["max_drawdown"] = 0.17
    with self.assertRaisesRegex(ValueError, "fixed representative is not an MDD residual"):
        select_target_records(rows)


if __name__ == "__main__":
    unittest.main()
