#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression, causality, liquidity, risk, and upgrade tests for version 16."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

import pandas as pd
import pytest

import codex_quant_fusion_v14 as v14
import codex_quant_fusion_v15 as v15
import codex_quant_fusion_v16 as quant
from validate_codex_quant_fusion_v16 import evaluate_upgrade


DATA_DIR = Path(__file__).resolve().parent / "market_data_qfq"
SYMBOLS = dict(quant.DEFAULT_SYMBOLS)


def run_quiet(  # noqa: PLR0913 - Test scenarios use keyword-only controls.
    *,
    engine: quant.BacktestEngine | None = None,
    symbols: dict[str, str] | None = None,
    start: str = "2025-04-01",
    end: str = "2026-07-20",
    indicator_state: str = "cold",
    allocation_mode: str | None = None,
    data_dir: Path = DATA_DIR,
):
    """Run a deterministic v16 backtest without emitting its console report."""
    engine = engine or quant.BacktestEngine(2_000_000)
    with contextlib.redirect_stdout(io.StringIO()):
        result = engine.run(
            symbols or SYMBOLS,
            start,
            end,
            data_dir=str(data_dir),
            indicator_state=indicator_state,
            allocation_mode=allocation_mode,
        )
    return engine, result


@pytest.fixture(scope="module")
def cold_result():
    """Cache the primary cold ensemble result for regression assertions."""
    return run_quiet()[1]


@pytest.fixture(scope="module")
def warm_result():
    """Cache the primary warm ensemble result for regression assertions."""
    return run_quiet(indicator_state="warm")[1]


@pytest.fixture(scope="module")
def weak_result():
    """Cache the pre-target weak-regime ensemble result."""
    return run_quiet(start="2024-01-02", end="2025-03-31")[1]


@pytest.fixture(scope="module")
def high_cost_result():
    """Cache the 50-basis-point one-way slippage stress result."""
    engine = quant.BacktestEngine(2_000_000, cfg={"slippage": 0.005})
    return run_quiet(engine=engine)[1]


@pytest.mark.parametrize(
    "policy",
    [
        quant.V16Policy(allocation_mode="single"),
        quant.V16Policy(allocation_mode="ensemble"),
    ],
)
def test_policy_accepts_both_allocation_modes(policy):
    assert policy.allocation_mode in {"single", "ensemble"}
    assert policy.drawdown_alert < policy.confirmed_drawdown
    assert policy.confirmed_drawdown < policy.emergency_drawdown


@pytest.mark.parametrize(
    "kwargs",
    [
        {"allocation_mode": "invalid"},
        {"drawdown_alert": 0.0},
        {"confirmed_drawdown": True},
        {"drawdown_alert": 0.15, "confirmed_drawdown": 0.15},
        {"confirmed_drawdown": 0.18, "emergency_drawdown": 0.175},
        {"drawdown_confirmations": True},
        {"allocation_horizons": ((5, 5, 20),)},
        {"allocation_horizons": ((3, 5, 10), (3, 5, 10))},
        {"max_order_adv_ratio": 0.0},
        {"max_order_adv_ratio": True},
    ],
)
def test_policy_rejects_invalid_contracts(kwargs):
    with pytest.raises(ValueError):
        quant.V16Policy(**kwargs)


def test_confirmed_risk_lock_requires_two_closes_and_records_shadow_alert():
    policy = quant.V16Policy(allocation_mode="single")
    manager = quant.ConfirmedPersistentRiskManager(
        {"max_drawdown": policy.confirmed_drawdown}, policy
    )
    assert manager.check_portfolio_risk(100.0, "2026-01-01") is None
    assert manager.check_portfolio_risk(85.0, "2026-01-02") is None
    assert manager.breach_streak == 1
    assert manager.check_portfolio_risk(84.0, "2026-01-05") == (
        "portfolio drawdown circuit breaker"
    )
    events = manager.drain_audit_events()
    assert any(event["event"] == "portfolio_drawdown_alert_on" for event in events)
    assert any(
        event["event"] == "confirmed_portfolio_drawdown_lock" for event in events
    )
    assert manager.persistent_lock is True


def test_emergency_risk_lock_does_not_wait_for_confirmation():
    policy = quant.V16Policy(allocation_mode="single")
    manager = quant.ConfirmedPersistentRiskManager(
        {"max_drawdown": policy.confirmed_drawdown}, policy
    )
    assert manager.check_portfolio_risk(100.0, "2026-01-01") is None
    assert manager.check_portfolio_risk(82.4, "2026-01-02") == (
        "portfolio drawdown circuit breaker"
    )
    assert any(
        event["event"] == "emergency_portfolio_drawdown_lock"
        for event in manager.drain_audit_events()
    )


def test_cold_ensemble_snapshot_and_performance_protection(cold_result):
    assert cold_result["engine_version"] == "16.0"
    assert cold_result["allocation_mode"] == "ensemble"
    assert cold_result["total_return"] == pytest.approx(10.383426579337005)
    assert cold_result["final_assets"] == pytest.approx(22_766_853.15867401)
    assert cold_result["max_drawdown"] == pytest.approx(-0.14620862008604604)
    assert cold_result["total_return"] >= 9.5
    assert cold_result["max_drawdown"] >= -0.16


def test_warm_ensemble_snapshot_and_performance_protection(warm_result):
    assert warm_result["indicator_state"] == "warm"
    assert warm_result["total_return"] == pytest.approx(11.381205637715)
    assert warm_result["max_drawdown"] == pytest.approx(-0.14795172934)
    assert warm_result["total_return"] >= 10.4
    assert warm_result["max_drawdown"] >= -0.16


def test_weak_regime_improves_return_and_drawdown(weak_result):
    assert weak_result["total_return"] == pytest.approx(0.2624830009697503)
    assert weak_result["max_drawdown"] == pytest.approx(-0.16058575656488822)
    assert weak_result["total_return"] >= 0.0
    assert weak_result["max_drawdown"] >= -0.18
    locks = [
        event
        for event in weak_result["risk_events"]
        if event["event"] == "persistent_portfolio_risk_lock"
    ]
    assert len(locks) == 3


def test_high_cost_stress_stays_above_the_hard_return_floor(high_cost_result):
    assert high_cost_result["total_return"] == pytest.approx(9.333969058758)
    assert high_cost_result["max_drawdown"] == pytest.approx(-0.148116701202)
    assert high_cost_result["total_return"] >= 9.0


def test_ensemble_assets_and_audits_equal_the_sum_of_sleeves(cold_result):
    summaries = cold_result["sleeve_summaries"]
    assert [summary["name"] for summary in summaries] == ["fast", "base", "slow"]
    assert sum(summary["initial_capital"] for summary in summaries) == pytest.approx(
        cold_result["initial_capital"]
    )
    assert sum(summary["final_assets"] for summary in summaries) == pytest.approx(
        cold_result["final_assets"]
    )
    assert all(":" in trade.strategy_name for trade in cold_result["trades"])
    assert all("sleeve" in event for event in cold_result["order_events"])
    per_sleeve_ratio = cold_result["per_sleeve_max_order_adv_ratio"]
    assert per_sleeve_ratio * len(summaries) == pytest.approx(
        cold_result["portfolio_max_order_adv_ratio"]
    )
    assert all(
        summary["max_order_adv_ratio"] == pytest.approx(per_sleeve_ratio)
        for summary in summaries
    )


def test_single_mode_matches_v15_when_no_hard_lock_is_triggered():
    _, candidate = run_quiet(allocation_mode="single")
    baseline_engine = v15.BacktestEngine(2_000_000)
    with contextlib.redirect_stdout(io.StringIO()):
        baseline = baseline_engine.run(
            SYMBOLS,
            "2025-04-01",
            "2026-07-20",
            data_dir=str(DATA_DIR),
            indicator_state="cold",
        )
    assert candidate["final_assets"] == baseline["final_assets"]
    assert candidate["max_drawdown"] == baseline["max_drawdown"]
    assert candidate["trades"] == baseline["trades"]


def test_default_market_data_does_not_hit_the_adv_cap(cold_result):
    clipped = [
        event
        for event in cold_result["order_events"]
        if event["event"] == "clipped_to_adv_capacity"
    ]
    assert clipped == []


def test_small_adv_limit_clips_orders_to_causal_capacity():
    policy = quant.V16Policy(allocation_mode="single", max_order_adv_ratio=0.00005)
    engine = quant.BacktestEngine(2_000_000, policy=policy)
    _, result = run_quiet(engine=engine, allocation_mode="single")
    clipped = [
        event
        for event in result["order_events"]
        if event["event"] == "clipped_to_adv_capacity"
    ]
    assert clipped
    for event in clipped:
        capacity = int(event["prior_adv"] * policy.max_order_adv_ratio // 100) * 100
        assert event["adjusted_shares"] <= capacity
        assert event["adjusted_shares"] < event["requested_shares"]
    assert min(trade.cash_after for trade in result["trades"]) >= 0


def test_partial_adv_sell_preserves_only_the_unfilled_target():
    policy = quant.V16Policy(allocation_mode="single", max_order_adv_ratio=0.00001)
    engine = quant.SleeveBacktestEngine(
        100_000,
        cfg=None,
        policy=policy,
        allocation_lookbacks=(5, 10, 20),
        sleeve_name="test",
    )
    strategy = v14.TurtleBreakoutStrategy(engine.cfg)
    position = v14.Position(
        symbol="300308",
        strategy_name=strategy.name,
        shares=1_000,
        entry_price=10.0,
        entry_date="2026-01-01",
        highest_close_since_entry=10.0,
    )
    strategy.position = position
    engine.positions = {"300308": {strategy.name: position}}
    dates = pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-05"])
    engine._execution_data_map = {
        "300308": pd.DataFrame(
            {"volume": [10_000_000.0] * 3},
            index=dates,
        )
    }
    engine._execution_date = dates[-1]
    signal = v14.Signal(
        symbol="300308",
        strategy_name=strategy.name,
        direction="sell",
        target_shares=500,
        price=10.0,
        signal_date="2026-01-02",
    )
    assert engine._execute_sell(signal, strategy, "2026-01-05") is False
    assert position.shares == 900
    assert signal.target_shares == 400
    assert engine.trades[-1].shares == 100


def test_future_volume_cannot_change_past_single_sleeve_trades(tmp_path):
    cutoff = pd.Timestamp("2026-01-05")
    for code in SYMBOLS:
        frame = pd.read_csv(DATA_DIR / f"{code}.csv")
        future = pd.to_datetime(frame["date"]) > cutoff
        frame.loc[future, "volume"] *= 100.0
        frame.to_csv(tmp_path / f"{code}.csv", index=False)
    _, baseline = run_quiet(allocation_mode="single")
    _, changed = run_quiet(allocation_mode="single", data_dir=tmp_path)
    baseline_trades = [
        trade for trade in baseline["trades"] if trade.date <= str(cutoff.date())
    ]
    changed_trades = [
        trade for trade in changed["trades"] if trade.date <= str(cutoff.date())
    ]
    assert baseline_trades == changed_trades


def test_cfg_drawdown_override_must_match_an_explicit_policy():
    policy = quant.V16Policy(confirmed_drawdown=0.15)
    with pytest.raises(ValueError, match="conflicts"):
        quant.BacktestEngine(2_000_000, cfg={"max_drawdown": 0.16}, policy=policy)


def test_report_identifies_version_16(cold_result, capsys):
    quant.PerformanceReport.print_report(cold_result, SYMBOLS)
    assert "Codex Quant v16 performance report" in capsys.readouterr().out


def test_runtime_mode_is_reflected_in_policy_metadata():
    _, result = run_quiet(allocation_mode="single")
    assert result["allocation_mode"] == "single"
    assert result["v16_policy"]["allocation_mode"] == "single"


def test_upgrade_validator_rejects_large_wealth_loss():
    scenarios = {
        "v15_cold": {"final_assets": 100.0, "max_drawdown": -0.15},
        "v15_warm": {"final_assets": 100.0, "max_drawdown": -0.15},
        "v16_cold": {"final_assets": 94.0, "max_drawdown": -0.15},
        "v16_warm": {"final_assets": 100.0, "max_drawdown": -0.15},
        "v16_high_cost": {"total_return": 9.1},
        "v16_weak": {"total_return": 0.1, "max_drawdown": -0.17},
    }
    evaluation = evaluate_upgrade(scenarios)
    assert evaluation["approved"] is False
    assert any(
        failure["name"] == "cold_wealth_rejection_gate"
        for failure in evaluation["hard_failures"]
    )
