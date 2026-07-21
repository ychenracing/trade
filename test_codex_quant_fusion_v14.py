#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic regression and causality tests for Codex Quant Fusion v14."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

import pandas as pd

import codex_quant_fusion_v14 as quant

DATA_DIR = Path(__file__).resolve().parent / "market_data_qfq"
SYMBOLS = {
    "300308": "中际旭创",
    "300502": "新易盛",
    "300394": "天孚通信",
    "688008": "澜起科技",
    "603986": "兆易创新",
}


def run(symbols=SYMBOLS, end="2026-07-20", engine=None):
    engine = engine or quant.BacktestEngine(2000000)
    with contextlib.redirect_stdout(io.StringIO()):
        result = engine.run(symbols, "2025-04-01", end, data_dir=str(DATA_DIR))
    return (engine, result)


def test_config_and_static_quality_contract():
    cfg = quant.BacktestEngine._validate_config(quant.BacktestEngine._default_config())
    assert cfg["sector_shock_confirmations"] == 2
    assert cfg["sector_shock_return"] == -0.05
    assert cfg["sector_guard_enabled"] is True
    assert cfg["sector_guard_min_symbols"] == 5


def test_akshare_localized_columns_remain_supported_without_non_english_source():
    frame = pd.DataFrame(
        {
            "\u65e5\u671f": ["2025-01-02"],
            "\u5f00\u76d8": [10.0],
            "\u6536\u76d8": [11.0],
            "\u6700\u9ad8": [12.0],
            "\u6700\u4f4e": [9.0],
            "\u6210\u4ea4\u91cf": [1000],
        }
    )
    normalized = quant.DataFetcher._normalize_columns(frame)
    assert list(normalized.columns) == ["open", "close", "high", "low", "volume"]


def test_repeated_run_resets_all_mutable_state():
    engine = quant.BacktestEngine(2000000)
    _, first = run(end="2026-06-30", engine=engine)
    _, second = run(end="2026-06-30", engine=engine)
    assert first["final_assets"] == second["final_assets"]
    assert first["total_trades"] == second["total_trades"]
    assert first["risk_events"] == second["risk_events"]


def test_symbol_input_order_does_not_change_result():
    _, normal = run(end="2026-06-30")
    reversed_symbols = dict(reversed(list(SYMBOLS.items())))
    _, reversed_result = run(symbols=reversed_symbols, end="2026-06-30")
    assert normal["final_assets"] == reversed_result["final_assets"]
    assert normal["max_drawdown"] == reversed_result["max_drawdown"]


def test_sector_guard_is_confirmed_before_july_crash_and_executes_t_plus_one():
    _, result = run()
    guard_on = [x for x in result["risk_events"] if x["event"] == "sector_guard_on"]
    assert [x["date"] for x in guard_on] == ["2026-06-26"]
    exits = [
        t for t in result["trades"] if t.reason == "sector breadth risk liquidation"
    ]
    assert exits
    assert {t.signal_date for t in exits} == {"2026-06-26"}
    assert {t.date for t in exits} == {"2026-06-29"}


def test_sector_guard_does_not_degenerate_into_a_single_symbol_stop():
    _, result = run(symbols={"300308": "中际旭创"})
    assert result["risk_events"] == []


def test_future_rows_cannot_change_past_result(tmp_path):
    # Extreme mutations after June must not change any earlier equity or trade.
    for code in SYMBOLS:
        df = pd.read_csv(DATA_DIR / f"{code}.csv")
        mutated = df.copy()
        mask = pd.to_datetime(mutated["date"]) > pd.Timestamp("2026-06-30")
        mutated.loc[mask, ["open", "high", "low", "close"]] *= 9.0
        mutated.to_csv(tmp_path / f"{code}.csv", index=False)
    _, baseline = run()
    engine = quant.BacktestEngine(2000000)
    with contextlib.redirect_stdout(io.StringIO()):
        changed = engine.run(
            SYMBOLS, "2025-04-01", "2026-07-20", data_dir=str(tmp_path)
        )
    left = baseline["equity_curve"].loc[:"2026-06-30", "assets"]
    right = changed["equity_curve"].loc[:"2026-06-30", "assets"]
    pd.testing.assert_series_equal(left, right)
    baseline_trades = [t for t in baseline["trades"] if t.date <= "2026-06-30"]
    changed_trades = [t for t in changed["trades"] if t.date <= "2026-06-30"]
    assert baseline_trades == changed_trades


def test_requested_period_snapshot():
    _, result = run()
    assert result["total_return"] == 10.744712798193506
    assert result["max_drawdown"] == -0.15177698573034473
    assert result["final_assets"] == 23489425.59638701
