"""AB5 ordinary-path executable-loss debit and bounded cohort allocation."""
from __future__ import annotations

import math


from quantfusion.config.engine import default_engine_config
from quantfusion.config.overlay import CONCENTRATION_CAP
from quantfusion.domain.models import Signal
from quantfusion.risk import account_budget as budget


def _complete(
    books: list[tuple[int, str, str, int, float]],
    *,
    stop_ratio: float,
) -> dict[tuple[int, str, str], budget.ProtectionEvidence]:
    return {
        (state, symbol, strategy): budget.ProtectionEvidence(
            stop_price=price * stop_ratio,
            source="test_close_known_stop",
            complete=True,
        )
        for state, symbol, strategy, _, price in books
    }


def test_complete_protection_avoids_false_ordinary_trim_and_missing_falls_back():
    cfg = default_engine_config()
    books = [(0, "300394", "turtle_breakout", 4_000, 200.0)]

    fallback, fallback_actions = budget.plan_account_risk_budget(
        930_000.0,
        1_000_000.0,
        cfg,
        books,
        [],
        lambda _: 1.0,
        date_str="2026-04-24",
    )
    protected, protected_actions = budget.plan_account_risk_budget(
        930_000.0,
        1_000_000.0,
        cfg,
        books,
        [],
        lambda _: 1.0,
        date_str="2026-04-24",
        protection_by_book=_complete(books, stop_ratio=0.98),
    )

    assert fallback["protection_fallback_book_count"] == 1
    assert fallback["ordinary_held_loss_debit_before"] > fallback["remaining_loss_budget"]
    assert fallback_actions
    assert protected["protection_complete_book_count"] == 1
    assert protected["ordinary_held_loss_debit_before"] <= protected["remaining_loss_budget"]
    assert protected_actions == []
    assert protected["ordinary_held_loss_debit_before"] < fallback[
        "ordinary_held_loss_debit_before"
    ]


def test_missing_buy_stop_is_fail_closed_but_complete_stop_uses_available_budget():
    cfg = default_engine_config()
    missing = Signal(
        "300308",
        "dual_ma",
        "buy",
        1_000,
        100.0,
        signal_date="2026-04-01",
    )
    protected = Signal(
        "300308",
        "dual_ma",
        "buy",
        1_000,
        100.0,
        stop_loss=90.0,
        signal_date="2026-04-01",
    )

    missing_receipt, _ = budget.plan_account_risk_budget(
        90_000.0,
        100_000.0,
        cfg,
        [],
        [(0, missing, 100_000.0)],
        lambda _: 1.0,
        date_str="2026-04-01",
    )
    protected_receipt, _ = budget.plan_account_risk_budget(
        90_000.0,
        100_000.0,
        cfg,
        [],
        [(0, protected, 100_000.0)],
        lambda _: 1.0,
        date_str="2026-04-01",
    )

    assert missing_receipt["protection_fallback_buy_count"] == 1
    assert protected_receipt["protection_complete_buy_count"] == 1
    assert 0 < missing_receipt["approved_buy_shares"][0]
    assert (
        missing_receipt["approved_buy_shares"][0]
        < protected_receipt["approved_buy_shares"][0]
        < protected.target_shares
    )
    assert protected_receipt["ordinary_total_loss_debit"] <= protected_receipt[
        "remaining_loss_budget"
    ]


def test_equivalent_sleeves_share_one_scale_independent_of_queue_order():
    cfg = default_engine_config()
    first = Signal(
        "603986",
        "atr_channel",
        "buy",
        1_000,
        100.0,
        stop_loss=90.0,
        reason="confirmed breakout",
        signal_date="2026-01-05",
        fusion_votes=2,
        fusion_label="two_strategy_confirmation",
    )
    second = Signal(
        "603986",
        "atr_channel",
        "buy",
        1_600,
        100.0,
        stop_loss=90.0,
        reason="confirmed breakout",
        signal_date="2026-01-05",
        fusion_votes=2,
        fusion_label="two_strategy_confirmation",
    )

    forward_buys = [(0, first, 100_000.0), (1, second, 160_000.0)]
    reverse_buys = list(reversed(forward_buys))
    forward, _ = budget.plan_account_risk_budget(
        180_000.0,
        200_000.0,
        cfg,
        [],
        forward_buys,
        lambda _: 1.0,
        date_str="2026-01-05",
        preserve_strategy_valid_holdings=True,
    )
    reverse, _ = budget.plan_account_risk_budget(
        180_000.0,
        200_000.0,
        cfg,
        [],
        reverse_buys,
        lambda _: 1.0,
        date_str="2026-01-05",
        preserve_strategy_valid_holdings=True,
    )

    assert forward["ordinary_buy_cohort_count"] == 1
    assert forward["buy_scales"][0] == forward["buy_scales"][1]
    assert 0.0 < forward["buy_scales"][0] < 1.0
    forward_by_state = {
        state: quantity
        for (state, _, _), quantity in zip(
            forward_buys, forward["approved_buy_shares"], strict=True
        )
    }
    reverse_by_state = {
        state: quantity
        for (state, _, _), quantity in zip(
            reverse_buys, reverse["approved_buy_shares"], strict=True
        )
    }
    assert reverse_by_state == forward_by_state


def test_integer_binary_search_matches_slow_lot_oracle():
    cfg = default_engine_config()
    signals = [
        Signal(
            "603986",
            "atr_channel",
            "buy",
            shares,
            100.0,
            stop_loss=90.0,
            reason="confirmed breakout",
            signal_date="2026-01-05",
            fusion_votes=2,
            fusion_label="two_strategy_confirmation",
        )
        for shares in (1_100, 2_300)
    ]
    buys = [
        (state, signal, signal.target_shares * signal.price)
        for state, signal in enumerate(signals)
    ]
    receipt, actions = budget.plan_account_risk_budget(
        210_000.0,
        240_000.0,
        cfg,
        [],
        buys,
        lambda _: 1.0,
        date_str="2026-01-05",
        preserve_strategy_valid_holdings=True,
    )

    risks = [
        budget._ordinary_risk_debit(
            signal.symbol,
            signal.price,
            cfg,
            receipt,
            budget.ProtectionEvidence(
                signal.stop_loss, "test_signal_stop", complete=True
            ),
        )
        for signal in signals
    ]
    requested = [signal.target_shares for signal in signals]
    max_lots = max(quantity // 100 for quantity in requested)
    best_lots = 0
    best_shares = [0 for _ in requested]
    for lots in range(max_lots + 1):
        approved = budget._cohort_approved_shares(requested, lots, max_lots)
        gross = sum(
            quantity * signal.price
            for quantity, signal in zip(approved, signals, strict=True)
        )
        debit = budget._grouped_debit(
            risks,
            approved,
            concentration_threshold=CONCENTRATION_CAP * receipt["equity"],
        )
        if (
            gross <= receipt["ordinary_gross_cap"] + 1e-8
            and debit <= receipt["remaining_loss_budget"] + 1e-8
        ):
            best_lots = lots
            best_shares = approved

    row = receipt["ordinary_buy_cohorts"][0]
    assert row["approved_lots"] == best_lots
    assert receipt["approved_buy_shares"] == best_shares
    assert receipt["ordinary_total_loss_debit"] <= receipt["remaining_loss_budget"]
    assert actions == []


def test_pathological_request_uses_logarithmic_feasibility_evaluations():
    cfg = default_engine_config()
    signal = Signal(
        "603986",
        "atr_channel",
        "buy",
        10_000_000,
        100.0,
        stop_loss=90.0,
        reason="confirmed breakout",
        signal_date="2026-01-05",
        fusion_votes=2,
        fusion_label="two_strategy_confirmation",
    )
    # At an all-time equity peak the incumbent gross/gap envelope is non-binding,
    # so plan_account_risk_budget must pass buys through at full size without
    # inventing a tighter ordinary cut (healthy production == AB5-off path).
    receipt, _ = budget.plan_account_risk_budget(
        1_000_000_000.0,
        1_000_000_000.0,
        cfg,
        [],
        [(0, signal, signal.target_shares * signal.price)],
        lambda _: 1.0,
        date_str="2026-01-05",
        preserve_strategy_valid_holdings=True,
    )
    assert receipt["ordinary_allocator_active"] is False
    assert receipt["approved_buy_shares"] == [signal.target_shares]
    assert receipt["buy_scales"] == [1.0]

    # Complexity of the scarce-budget cohort search is checked directly: a
    # 100,000-lot request must stay O(log lots) even when the envelope is open.
    capacity = budget.account_budget_capacity(
        1_000_000_000.0, 1_000_000_000.0, cfg, 0,
    )
    risk = budget._ordinary_risk_debit(
        signal.symbol,
        signal.price,
        cfg,
        capacity,
        budget.ProtectionEvidence(
            stop_price=90.0, source="signal_stop", complete=True,
        ),
    )
    _, diagnostics = budget._allocate_ordinary_buy_cohorts(
        buys=[(0, signal, signal.target_shares * signal.price)],
        buy_risks=[risk],
        blocked=[False],
        quality_classes=[4],
        score=lambda _: 1.0,
        held_groups=set(),
        held_risks=[],
        held_shares=[],
        gross_before=0.0,
        ordinary_gross_cap=capacity["ordinary_gross_cap"],
        remaining_loss_budget=capacity["remaining_loss_budget"],
        concentration_threshold=0.8 * 1_000_000_000.0,
    )
    row = diagnostics["ordinary_buy_cohorts"][0]
    requested_lots = row["requested_lots"]
    evaluations = row["feasibility_evaluations"]
    assert requested_lots == 100_000
    assert diagnostics["ordinary_buy_precomputed_held_book_count"] == 0
    assert evaluations <= math.ceil(math.log2(requested_lots + 1)) + 6
    assert requested_lots / evaluations >= 10.0
    assert row["approved_shares"] == [signal.target_shares]


def test_residual_shortfall_trims_highest_marginal_risk_book_first():
    cfg = default_engine_config()
    books = [
        (0, "300394", "turtle_breakout", 5_000, 100.0),
        (0, "603986", "atr_channel", 3_000, 100.0),
    ]
    protection = {
        (0, "603986", "atr_channel"): budget.ProtectionEvidence(
            99.0, "tight_trailing_stop", complete=True
        )
    }
    receipt, actions = budget.plan_account_risk_budget(
        800_000.0,
        900_000.0,
        cfg,
        books,
        [],
        lambda symbol: {"300394": 0.1, "603986": 0.9}[symbol],
        date_str="2026-01-05",
        protection_by_book=protection,
    )

    assert receipt["ordinary_held_loss_debit_before"] > receipt[
        "remaining_loss_budget"
    ]
    assert receipt["ordinary_held_loss_debit_after"] <= receipt[
        "remaining_loss_budget"
    ]
    assert actions
    assert actions[0].symbol == "300394"
    assert all(action.symbol == "300394" for action in actions)
