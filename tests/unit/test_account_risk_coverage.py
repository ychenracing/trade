from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest

from quantfusion.config.engine import default_engine_config
from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.domain.models import Signal
from quantfusion.risk.account_budget import (
    account_budget_capacity,
    plan_account_risk_budget,
)
from quantfusion.risk.account_risk_epoch import consume_account_risk_epoch
from quantfusion.risk.managers import RecoverableDrawdownRiskManager


def _cfg(**overrides: float) -> dict:
    cfg = default_engine_config()
    cfg.update(overrides)
    return cfg


def test_dual_peak_uses_cycle_floor_without_erasing_lifetime_terminal_floor() -> None:
    capacity = account_budget_capacity(
        80_000.0,
        100_000.0,
        _cfg(max_total_weight=1.0),
        0,
        cycle_peak_assets=80_000.0,
        lifetime_peak_assets=100_000.0,
        terminal_drawdown=0.28,
    )
    assert capacity["cycle_floor"] == pytest.approx(65_600.0)
    assert capacity["lifetime_terminal_floor"] == pytest.approx(72_000.0)
    assert capacity["effective_policy_floor"] == pytest.approx(72_000.0)
    assert capacity["gross_cap"] > 0.0


def test_terminal_lock_never_rearms_deployable_capacity() -> None:
    capacity = account_budget_capacity(
        80_000.0,
        100_000.0,
        _cfg(max_total_weight=1.0),
        0,
        cycle_peak_assets=80_000.0,
        lifetime_peak_assets=100_000.0,
        terminal_drawdown=0.28,
        terminal_lock_active=True,
    )
    assert capacity["terminal_lock_active"] is True
    assert capacity["gross_cap"] == 0.0


def test_no_qualified_buy_means_no_ordinary_budget_trim() -> None:
    cfg = _cfg(max_total_weight=0.50)
    books = [(0, "300308", "turtle_breakout", 9_000, 10.0)]
    receipt, actions = plan_account_risk_budget(
        100_000.0,
        100_000.0,
        cfg,
        books,
        [],
        lambda _: 1.0,
        date_str="2026-01-05",
        preserve_strategy_valid_holdings=True,
        cycle_peak_assets=100_000.0,
        lifetime_peak_assets=100_000.0,
        terminal_drawdown=0.28,
    )
    assert receipt["gross_before"] > receipt["gross_cap"]
    assert receipt["normal_budget_trim_suppressed"] is True
    assert actions == []


def test_capacity_reallocation_is_minimal_and_gives_no_early_buy_credit() -> None:
    cfg = _cfg(
        max_total_weight=1.0,
        slippage=0.0,
        commission_rate=0.0,
        stamp_duty=0.0,
        min_commission=0.0,
    )
    books = [
        (0, "603986", "turtle_breakout", 2_000, 10.0),
        (0, "300308", "dual_ma", 7_000, 10.0),
    ]
    buy = Signal(
        "688072",
        "atr_channel",
        "buy",
        target_shares=2_000,
        price=10.0,
        signal_date="2026-01-05",
    )
    scores = {"603986": 1.0, "300308": 9.0, "688072": 10.0}
    receipt, actions = plan_account_risk_budget(
        100_000.0,
        100_000.0,
        cfg,
        books,
        [(0, buy, 20_000.0)],
        scores.__getitem__,
        date_str="2026-01-05",
        preserve_strategy_valid_holdings=True,
        cycle_peak_assets=100_000.0,
        lifetime_peak_assets=100_000.0,
        terminal_drawdown=0.28,
        sellable_shares_by_book={
            (0, "603986", "turtle_breakout"): 2_000,
            (0, "300308", "dual_ma"): 7_000,
        },
    )
    assert receipt["buy_scale"] == pytest.approx(0.5)
    assert receipt["capacity_reallocation_triggered"] is True
    assert len(actions) == 1
    action = actions[0]
    assert action.reason == "capacity_reallocation"
    assert (action.symbol, action.strategy_name) == (
        "603986",
        "turtle_breakout",
    )
    assert action.shares == 1_000
    # The planned sell is not credited to the current buy batch.
    assert receipt["buy_scale"] < 1.0


def test_reallocation_does_not_sell_an_equal_or_better_holding() -> None:
    cfg = _cfg(
        max_total_weight=1.0,
        slippage=0.0,
        commission_rate=0.0,
        stamp_duty=0.0,
        min_commission=0.0,
    )
    books = [(0, "300308", "dual_ma", 9_000, 10.0)]
    buy = Signal(
        "688072",
        "atr_channel",
        "buy",
        target_shares=2_000,
        price=10.0,
        signal_date="2026-01-05",
    )
    scores = {"300308": 10.0, "688072": 10.0}
    receipt, actions = plan_account_risk_budget(
        100_000.0,
        100_000.0,
        cfg,
        books,
        [(0, buy, 20_000.0)],
        scores.__getitem__,
        date_str="2026-01-05",
        preserve_strategy_valid_holdings=True,
        cycle_peak_assets=100_000.0,
        lifetime_peak_assets=100_000.0,
        terminal_drawdown=0.28,
        sellable_shares_by_book={(0, "300308", "dual_ma"): 9_000},
    )
    assert receipt["buy_scale"] < 1.0
    assert actions == []


def test_manager_publishes_distinct_peaks_after_cycle_rearm() -> None:
    policy = replace(PortfolioPolicy(), rearm_trading_days=1)
    manager = RecoverableDrawdownRiskManager(
        {"max_drawdown": policy.confirmed_drawdown}, policy
    )
    manager.lifetime_peak_assets = 100_000.0
    manager.peak_assets = 100_000.0
    manager.persistent_lock = True
    manager.terminal_lock = False
    manager.lock_start_position = 0
    manager.cycle_lock_count = 1
    dates = [pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-06")]
    status = manager.check_portfolio_risk(
        80_000.0,
        "2026-01-06",
        trading_dates=dates,
        date_to_pos={date: index for index, date in enumerate(dates)},
    )
    assert status is None
    assert manager.peak_assets == 80_000.0
    assert manager.lifetime_peak_assets == 100_000.0
    events = manager.drain_audit_events()
    assert any(event["event"] == "portfolio_drawdown_rearmed" for event in events)
    assert all(event["event"] != "account_risk_epoch_snapshot" for event in events)
    snapshot = consume_account_risk_epoch(
        date="2026-01-06",
        equity=80_000.0,
        lifetime_peak_assets=100_000.0,
    )
    assert snapshot is not None
    assert snapshot.cycle_peak_assets == 80_000.0
    assert snapshot.lifetime_peak_assets == 100_000.0
    assert snapshot.terminal_lock_active is False
