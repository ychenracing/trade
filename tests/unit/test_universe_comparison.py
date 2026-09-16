"""Pure metric contracts for research-universe comparison reports."""

from __future__ import annotations

import pandas as pd
import pytest

from quantfusion.application.universe_comparison import summarize_universe_result
from quantfusion.domain.models import TradeRecord


def _market_frame(closes: list[float]) -> pd.DataFrame:
    index = pd.to_datetime(["2023-01-03", "2023-01-04", "2023-01-05"])
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1_000_000.0] * 3,
        },
        index=index,
    )


def test_summarize_universe_result_uses_audited_engine_outputs() -> None:
    equity = pd.DataFrame(
        {
            "assets": [100.0, 110.0, 105.0],
            "cash": [100.0, 55.0, 42.0],
            "position_value": [0.0, 55.0, 63.0],
        },
        index=pd.to_datetime(["2023-01-03", "2023-01-04", "2023-01-05"]),
    )
    trades = [
        TradeRecord("300308", "trend", "buy", 1, 50.0, "2023-01-04", gross_value=50.0),
        TradeRecord("300502", "trend", "buy", 1, 60.0, "2023-01-05", gross_value=60.0),
    ]
    result = {
        "total_return": 0.05,
        "annual_return": 0.20,
        "max_drawdown": -0.10,
        "total_trades": 2,
        "equity_curve": equity,
        "trades": trades,
        "risk_events": [{"event": "sector_guard_on"}, {"event": "sector_guard_on"}, {"event": "risk_trim"}],
        "max_concurrent_symbols": 2,
    }
    market_frames = {
        "300308": _market_frame([50.0, 50.0, 50.0]),
        "300502": _market_frame([50.0, 50.0, 50.0]),
        "300394": _market_frame([50.0, 50.0, 50.0]),
    }

    summary = summarize_universe_result(
        "pool_b",
        {"300308": "中际旭创", "300502": "新易盛", "300394": "天孚通信"},
        "2023-01-01",
        "2026-09-16",
        result,
        market_frames=market_frames,
    )

    assert summary["pool"] == "pool_b"
    assert summary["symbol_count"] == 3
    assert summary["start_date"] == "2023-01-01"
    assert summary["end_date"] == "2026-09-16"
    assert summary["total_return"] == 0.05
    assert summary["annual_return"] == 0.20
    assert summary["max_drawdown"] == -0.10
    assert summary["total_trades"] == 2
    assert summary["turnover_ratio"] == pytest.approx(110.0 / 105.0)
    assert summary["all_cash_day_ratio"] == pytest.approx(1.0 / 3.0)
    assert summary["average_cash_ratio"] == pytest.approx((1.0 + 0.5 + 0.4) / 3.0)
    assert summary["holding_concentration_hhi_mean"] == pytest.approx(0.75)
    assert summary["holding_concentration_hhi_max"] == pytest.approx(1.0)
    assert summary["risk_event_count"] == 3
    assert summary["risk_event_types"] == {"risk_trim": 1, "sector_guard_on": 2}


def test_hhi_uses_latest_close_known_by_date_for_suspended_holdings() -> None:
    dates = pd.to_datetime(["2023-01-03", "2023-01-04", "2023-01-05"])
    equity = pd.DataFrame(
        {
            "assets": [100.0, 100.0, 100.0],
            "cash": [100.0, 50.0, 50.0],
            "position_value": [0.0, 50.0, 50.0],
        },
        index=dates,
    )
    frame = _market_frame([50.0, 50.0, 50.0]).iloc[:2].copy()
    summary = summarize_universe_result(
        "pool_a",
        {"300308": "中际旭创"},
        "2023-01-01",
        "2023-01-05",
        {
            "total_return": 0.0,
            "annual_return": 0.0,
            "max_drawdown": 0.0,
            "total_trades": 1,
            "equity_curve": equity,
            "trades": [
                TradeRecord(
                    "300308",
                    "trend",
                    "buy",
                    1,
                    50.0,
                    "2023-01-04",
                    gross_value=50.0,
                )
            ],
            "risk_events": [],
            "max_concurrent_symbols": 1,
        },
        market_frames={"300308": frame},
    )
    assert summary["holding_concentration_hhi_mean"] == 1.0
    assert summary["holding_concentration_hhi_max"] == 1.0


def test_comparison_summary_rejects_missing_equity_audit_fields() -> None:
    with pytest.raises(ValueError, match="equity_curve"):
        summarize_universe_result(
            "pool_a",
            {"300308": "中际旭创"},
            "2023-01-01",
            "2026-09-16",
            {
                "total_return": 0.0,
                "annual_return": 0.0,
                "max_drawdown": 0.0,
                "total_trades": 0,
                "equity_curve": pd.DataFrame({"assets": [100.0]}),
                "trades": [],
                "risk_events": [],
                "max_concurrent_symbols": 0,
            },
        )
