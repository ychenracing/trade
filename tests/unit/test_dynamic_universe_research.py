from __future__ import annotations

import pandas as pd

from quantfusion.research import dynamic_universe as du


def test_watch_universe_is_curated_pool_g_outside_production_core() -> None:
    watch = du.watch_symbols()
    assert watch
    assert not (set(watch) & set(du.ORDERED_SYMBOLS))
    assert set(watch).issubset(set(du.get_universe_pool("pool_g").symbols))


def test_promotion_requires_two_monthly_selections_and_demotion_two_misses() -> None:
    state = du.CandidateState()

    state, events = du.advance_candidate_state(
        state,
        selected_symbol="688041",
        decision_date="2025-01-31",
        effective_date="2025-02-05",
    )
    assert events == ()
    assert state.active_symbol is None

    state, events = du.advance_candidate_state(
        state,
        selected_symbol="688041",
        decision_date="2025-02-28",
        effective_date="2025-03-03",
    )
    assert [event["event"] for event in events] == ["promote"]
    assert state.active_symbol == "688041"

    state, events = du.advance_candidate_state(
        state,
        selected_symbol=None,
        decision_date="2025-03-31",
        effective_date="2025-04-01",
    )
    assert events == ()
    assert state.active_symbol == "688041"

    state, events = du.advance_candidate_state(
        state,
        selected_symbol=None,
        decision_date="2025-04-30",
        effective_date="2025-05-06",
    )
    assert [event["event"] for event in events] == ["demote"]
    assert state.active_symbol is None


def test_monthly_decision_dates_use_only_observed_dates_inside_window() -> None:
    calendar = pd.bdate_range("2025-01-01", "2025-03-31")
    dates = du.monthly_decision_dates(
        calendar,
        start_date="2025-01-15",
        end_date="2025-03-10",
    )
    assert dates == (
        pd.Timestamp("2025-01-31"),
        pd.Timestamp("2025-02-28"),
        pd.Timestamp("2025-03-10"),
    )


def test_forward_return_begins_after_decision_at_next_session_open() -> None:
    frame = pd.DataFrame(
        {
            "open": [10.0, 20.0, 40.0],
            "close": [11.0, 30.0, 44.0],
        },
        index=pd.to_datetime(["2025-01-31", "2025-02-03", "2025-02-04"]),
    )
    assert du._forward_return(
        frame,
        decision_date=pd.Timestamp("2025-01-31"),
        horizon=2,
    ) == 44.0 / 20.0 - 1.0


def test_research_verdict_rejects_when_time_split_evidence_is_missing() -> None:
    empty_window = {
        "complete_60d_count": 0,
        "median_excess_20d": None,
        "median_excess_60d": None,
        "positive_excess_hit_rate_20d": None,
        "positive_excess_hit_rate_60d": None,
    }
    full_window = {
        "complete_60d_count": 4,
        "median_excess_20d": 0.01,
        "median_excess_60d": 0.02,
        "positive_excess_hit_rate_20d": 0.75,
        "positive_excess_hit_rate_60d": 0.75,
    }
    verdict = du._research_verdict(
        promotions=[{"event": "promote"}] * 4,
        windows={
            "full": full_window,
            "early": {**full_window, "complete_60d_count": 4},
            "late": empty_window,
        },
        start_date="2023-01-01",
        end_date="2026-09-15",
    )
    assert verdict["status"] == "reject_production_promotion"
    assert not verdict["checks"]["both_time_splits_have_two_complete_60d_promotions"]
