"""Read-only diagnostics use causal dates, native fills, and explicit gaps."""

from copy import deepcopy
import hashlib

import pandas as pd
import pytest

from scripts import decision_diagnostics as diagnostic
from quantfusion.data.sessions import load_calendar


def _prices():
    dates = pd.to_datetime([d for d in load_calendar().sessions if "2025-01-01" <= d <= "2025-08-01"])
    return pd.DataFrame({"open": 100., "close": 100., "high": 101.,
                         "low": 99., "volume": 1_000_000.}, index=dates)


def test_label_uses_next_session_open_and_native_fees():
    frame = _prices()
    before = frame.copy(deep=True)
    label = diagnostic.execution_label("300308", frame, "2025-04-01", 5,
                                       load_calendar().sessions, "2025-07-20")
    assert label["status"] == "MATURE"
    assert label["entry_date"] == "2025-04-02"
    assert label["exit_date"] == "2025-04-10"  # April 4 exchange holiday.
    assert label["net_return"] < 0.
    assert len(label["fills"]) == 2
    assert label["fills"][0]["price"] > 100.
    assert label["fills"][1]["price"] < 100.
    assert label["fills"][0]["commission"] > 0.
    pd.testing.assert_frame_equal(frame, before)


@pytest.mark.parametrize("case,expected", [("immature", "IMMATURE"), ("missing", "MISSING_DATA"), ("limit", "ENTRY_BLOCKED"), ("suspension", "UNVERIFIED_SESSION")])
def test_ineligible_labels_are_never_zero_returns(case, expected):
    frame = _prices()
    cutoff = "2025-07-20"
    if case == "immature":
        cutoff = "2025-04-03"
    elif case == "missing":
        frame = frame.drop(pd.Timestamp("2025-04-02"))
    elif case == "limit":
        frame.loc[pd.Timestamp("2025-04-02"), ["open", "high", "close"]] = 125.
    else:
        frame.loc[pd.Timestamp("2025-04-02"), "volume"] = 0.
    result = diagnostic.execution_label("300308", frame, "2025-04-01", 5,
                                        load_calendar().sessions, cutoff)
    assert result["status"] == expected
    assert result["net_return"] is None


def test_score_summary_counts_dates_and_events_without_double_counting():
    rows = [dict(date="2025-04-01", symbol=code, score=.5, group=group,
                 confirmation_count=1, score_components={"momentum": .25},
                 labels={"5": dict(status="MATURE", net_return=value,
                     entry_date="2025-04-02", exit_date="2025-04-10")})
            for code, group, value in [("300308", "high", .1), ("300502", "low", -.1)]]
    before = deepcopy(rows)
    result = diagnostic.score_summary(rows)
    assert result["event_count"] == 2
    assert result["date_count"] == 1
    assert result["momentum_capped_fraction"] == 1.
    assert result["tied_event_fraction"] == 1.
    assert result["windows"]["5"]["high"]["date_weighted_mean"] == .1
    assert rows == before


def test_missing_advice_identity_is_not_certified():
    result = diagnostic.advice_identity({"mode": "account_decision_support"}, {}, load_calendar())
    assert result["status"] == "IDENTITY_UNVERIFIED"
    assert result["missing_or_mismatched"]


def test_risk_attribution_counts_plans_separately_from_fills():
    events = [dict(date="2025-04-01", event="account_budget_envelope",
                   buy_scale=0., gross_before=100., gross_cap=50.,
                   buy_shares_removed=100, new_reduction_orders=2)]
    result = diagnostic.risk_attribution(dict(risk_events=events, trades=[], order_events=[]))
    assert result["planned_reduction_orders"] == 2
    assert result["filled_budget_reductions"] == 0
    assert result["blocked_buy_shares"] == 100
    assert result["episodes"][0]["right_censored"]


def test_first_divergence_includes_prior_continuous_cash():
    trade = dict(date="2025-04-02", symbol="300308", direction="buy", shares=100,
                 net_cash_flow=-10010., strategy_name="turtle_breakout")
    extra = {**trade, "date": "2025-04-03", "symbol": "300502"}
    base = dict(trades=[trade], order_events=[], risk_events=[])
    larger = dict(trades=[trade, extra], order_events=[], risk_events=[])
    result = diagnostic.pool_divergence(base, larger)
    assert result["first_trade_divergence"] == "2025-04-03"
    assert result["before"]["13"]["cash"] == 2_000_000. - 10010.
    assert result["before"]["13"]["holdings"] == {"300308": 100}


def test_sell_advice_uses_calendar_and_does_not_assert_human_fill():
    action = {"symbol": "300308", "action": "SELL", "recommended_shares": 100}
    result = diagnostic._sell_availability(action, _prices(), "2025-04-03", load_calendar(), "2025-07-20")
    assert result["next_session"] == "2025-04-07"
    assert result["status"] == "OPEN_PRICE_REVIEW_ONLY"
    assert result["actual_human_fill"] == "UNKNOWN"


def test_recovery_receipt_cannot_authorize_earlier_same_day_buy():
    events = [
        dict(date="2025-04-01", event="account_budget_envelope", buy_scale=0., gross_before=100., gross_cap=50.),
        dict(date="2025-04-02", event="account_budget_envelope", buy_scale=1., gross_before=0., gross_cap=100.),
    ]
    trades = [dict(date=day, direction="buy") for day in ("2025-04-02", "2025-04-03")]
    episode = diagnostic.risk_attribution(dict(risk_events=events, trades=trades))["episodes"][0]
    assert episode["release_date"] == "2025-04-02"
    assert episode["first_buy_after_release"] == "2025-04-03"


def _saved_advice():
    frame, calendar, day = _prices(), load_calendar(), "2025-04-01"
    observed = frame.loc[frame.index <= pd.Timestamp(day)]
    advice = {
        "mode": "account_decision_support", "as_of": day, "snapshot_date": day, "requested_as_of": day,
        "account_code_sha256": diagnostic.account_source_sha(),
        "engine_config_sha256": diagnostic.canonical_sequence_sha(diagnostic.default_engine_config()),
        "account_snapshot_sha256": "0" * 64,
        "scan_dates": {"calendar_sha256": calendar.sha256, "requested_as_of": day,
                       "required_evidence_date": day, "next_trading_date": "2025-04-02"},
        "market_evidence": {"300308": {"frame_sha256": hashlib.sha256(observed.to_csv(index=True).encode()).hexdigest(),
            "config_sha256": diagnostic.canonical_sequence_sha(diagnostic.symbol_config("300308")), "evidence_date": day}},
    }
    return advice, {"300308": frame}, calendar


def test_saved_market_identity_positive_does_not_certify_private_snapshot():
    advice, frames, calendar = _saved_advice()
    result = diagnostic.advice_identity(advice, frames, calendar)
    assert result["status"] == "MARKET_CODE_CONFIG_VERIFIED"
    assert result["actual_human_fill"] == "UNKNOWN"
    assert "not supplied" in result["snapshot_bytes"]


@pytest.mark.parametrize("field", ["snapshot_date", "requested_as_of", "required_evidence_date", "next_trading_date", "market_evidence_date"])
def test_saved_advice_inconsistent_dates_are_rejected(field):
    advice, frames, calendar = _saved_advice()
    if field == "market_evidence_date":
        advice["market_evidence"]["300308"]["evidence_date"] = "2025-03-31"
    elif field in {"required_evidence_date", "next_trading_date"}:
        advice["scan_dates"][field] = "2025-03-31"
    else:
        advice[field] = "2025-03-31"
    result = diagnostic.advice_identity(advice, frames, calendar)
    assert result["status"] == "IDENTITY_UNVERIFIED"
    assert result["missing_or_mismatched"]
