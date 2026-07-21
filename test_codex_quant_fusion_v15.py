#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression, causality, configuration, and risk tests for version 15."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

import pandas as pd
import pytest

import codex_quant_fusion_v15 as quant


DATA_DIR = Path(__file__).resolve().parent / "market_data_qfq"
SYMBOLS = dict(quant.DEFAULT_SYMBOLS)


def run(
    *,
    symbols: dict[str, str] | None = None,
    start: str = "2025-04-01",
    end: str = "2026-07-20",
    indicator_state: str = "cold",
    engine: quant.BacktestEngine | None = None,
    data_dir: Path = DATA_DIR,
):
    """Run a quiet deterministic test backtest and return engine plus result."""
    engine = engine or quant.BacktestEngine(2_000_000)
    with contextlib.redirect_stdout(io.StringIO()):
        result = engine.run(
            symbols or SYMBOLS,
            start,
            end,
            data_dir=str(data_dir),
            indicator_state=indicator_state,
        )
    return engine, result


def test_cold_target_snapshot_and_performance_floor():
    _, result = run()
    assert result["engine_version"] == "15.0"
    assert result["total_return"] == pytest.approx(10.093524375734129)
    assert result["final_assets"] == pytest.approx(22_187_048.751468256)
    assert result["max_drawdown"] == pytest.approx(-0.1473076902280132)
    assert result["total_return"] >= 9.0
    assert result["max_drawdown"] >= -0.18
    assert result["persistent_risk_lock"] is False


def test_warm_state_is_explicit_and_retains_target_performance():
    _, result = run(indicator_state="warm")
    assert result["indicator_state"] == "warm"
    assert result["total_return"] == pytest.approx(11.039359202179128)
    assert result["max_drawdown"] == pytest.approx(-0.14980703338668825)


def test_dynamic_allocation_is_independent_of_caller_symbol_order():
    _, normal = run()
    reversed_symbols = dict(reversed(list(SYMBOLS.items())))
    _, reversed_result = run(symbols=reversed_symbols)
    assert normal["final_assets"] == reversed_result["final_assets"]
    assert normal["trades"] == reversed_result["trades"]


def test_repeated_run_resets_v15_state():
    engine = quant.BacktestEngine(2_000_000)
    _, first = run(engine=engine)
    _, second = run(engine=engine)
    assert first["final_assets"] == second["final_assets"]
    assert first["trades"] == second["trades"]
    assert first["order_events"] == second["order_events"]


def test_auto_route_honors_explicit_global_and_symbol_precedence():
    engine = quant.BacktestEngine(
        2_000_000,
        cfg={"entry_period": 40, "risk_pct": 0.025, "max_symbol_weight": 0.55},
    )
    with contextlib.redirect_stdout(io.StringIO()):
        engine.run(
            SYMBOLS,
            "2025-04-01",
            "2025-08-01",
            data_dir=str(DATA_DIR),
            per_symbol_config={"300308": {"entry_period": 14}},
        )
    assert engine.symbol_configs["300308"]["entry_period"] == 14
    assert engine.symbol_configs["300502"]["entry_period"] == 40
    for config in engine.symbol_configs.values():
        assert config["risk_pct"] == 0.025
        assert config["max_symbol_weight"] == 0.55


def test_orders_are_clipped_and_cash_never_becomes_negative():
    _, result = run()
    clipped = [
        event
        for event in result["order_events"]
        if event["event"] == "clipped_to_exposure_capacity"
    ]
    assert clipped
    assert all(
        event["adjusted_shares"] < event["requested_shares"] for event in clipped
    )
    assert min(trade.cash_after for trade in result["trades"]) >= 0


def test_persistent_lock_improves_the_pre_target_weak_regime():
    _, result = run(start="2024-01-02", end="2025-03-31")
    locks = [
        event
        for event in result["risk_events"]
        if event["event"] == "persistent_portfolio_risk_lock"
    ]
    assert len(locks) == 1
    assert locks[0]["date"] == "2024-06-07"
    assert not any(
        trade.direction == "buy" and trade.date > locks[0]["date"]
        for trade in result["trades"]
    )
    assert result["total_return"] == pytest.approx(0.22664758, abs=1e-7)
    assert result["max_drawdown"] >= -0.19


def test_future_rows_cannot_change_past_equity_or_trades(tmp_path):
    for code in SYMBOLS:
        frame = pd.read_csv(DATA_DIR / f"{code}.csv")
        future = pd.to_datetime(frame["date"]) > pd.Timestamp("2026-06-30")
        frame.loc[future, ["open", "high", "low", "close"]] *= 9.0
        frame.to_csv(tmp_path / f"{code}.csv", index=False)
    _, baseline = run()
    _, changed = run(data_dir=tmp_path)
    left = baseline["equity_curve"].loc[:"2026-06-30", "assets"]
    right = changed["equity_curve"].loc[:"2026-06-30", "assets"]
    pd.testing.assert_series_equal(left, right)
    baseline_trades = [
        trade for trade in baseline["trades"] if trade.date <= "2026-06-30"
    ]
    changed_trades = [
        trade for trade in changed["trades"] if trade.date <= "2026-06-30"
    ]
    assert baseline_trades == changed_trades


def test_data_end_settlement_is_disabled_and_cannot_infer_future_rows():
    engine = quant.BacktestEngine(2_000_000)
    assert engine.cfg["close_position_on_data_end"] is False
    assert engine._close_positions_on_data_end({}, pd.Timestamp("2026-01-01")) == set()
    legacy_request = quant.BacktestEngine(
        2_000_000, cfg={"close_position_on_data_end": True}
    )
    assert legacy_request.cfg["close_position_on_data_end"] is False


def test_v15_headers_report_the_engine_and_requested_trading_period(capsys):
    engine = quant.BacktestEngine(2_000_000)
    result = engine.run(
        SYMBOLS,
        "2025-04-01",
        "2025-05-30",
        data_dir=str(DATA_DIR),
        indicator_state="warm",
    )
    quant.PerformanceReport.print_report(result, SYMBOLS)
    output = capsys.readouterr().out
    assert "Codex Quant v15 backtest" in output
    assert "Period: 2025-04-01 ~ 2025-05-30" in output
    assert "Codex Quant v15 performance report" in output


@pytest.mark.parametrize("value", [True, 365.5, "365", 119])
def test_warmup_calendar_days_rejects_non_integer_or_short_values(value):
    engine = quant.BacktestEngine(2_000_000)
    with pytest.raises(ValueError, match="warmup_calendar_days"):
        engine.run(
            SYMBOLS,
            "2025-04-01",
            "2025-05-30",
            data_dir=str(DATA_DIR),
            indicator_state="warm",
            warmup_calendar_days=value,
        )
