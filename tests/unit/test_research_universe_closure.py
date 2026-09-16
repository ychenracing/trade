"""Regression contracts for research-universe closure and reproducibility."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

from scripts import compare_universes as compare
from scripts import download_eastmoney_qfq as download
from quantfusion.application.universe_comparison import summarize_universe_result


def _write_market_csv(path: Path, dates: pd.DatetimeIndex) -> str:
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": 10.0,
            "high": 10.0,
            "low": 10.0,
            "close": 10.0,
            "volume": 100_000.0,
        }
    )
    frame.to_csv(path, index=False)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_comparison_requires_complete_manifest_even_when_csv_files_exist(tmp_path) -> None:
    for code in compare.required_market_symbols(("pool_b",)):
        _write_market_csv(tmp_path / f"{code}.csv", pd.bdate_range("2023-01-03", periods=3))

    with pytest.raises(ValueError, match="manifest"):
        compare.validate_market_data_directory(tmp_path, ("pool_b",))


def test_compare_cli_explicitly_enables_research_index_provider_fallback(
    tmp_path, monkeypatch
) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        compare,
        "validate_market_data_directory",
        lambda *args, **kwargs: None,
    )

    def fake_refresh(*args, **kwargs):
        captured.update(kwargs)
        return {"indices": {}}

    monkeypatch.setattr(compare.market_data_contracts, "refresh_regime_indices", fake_refresh)
    monkeypatch.setattr(
        compare,
        "validate_regime_data_directory",
        lambda *args, **kwargs: {"sha256": {}},
    )
    monkeypatch.setattr(
        compare,
        "_run_pool",
        lambda pool_name, **kwargs: {
            "pool": pool_name,
            "symbol_count": 1,
            "observed_start_date": "2023-01-03",
            "observed_end_date": "2023-01-05",
            "total_return": 0.0,
            "max_drawdown": 0.0,
            "total_trades": 0,
            "holding_concentration_hhi_mean": 0.0,
        },
    )
    monkeypatch.setattr(compare, "write_universe_comparison", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compare_universes",
            "--pool",
            "pool_b",
            "--end-date",
            "2026-09-15",
            "--data-dir",
            str(tmp_path / "market"),
            "--regime-data-dir",
            str(tmp_path / "regime"),
            "--output-dir",
            str(tmp_path / "out"),
        ],
    )

    assert compare.main() == 0
    assert captured["strict"] is True
    assert captured["allow_provider_fallback"] is True


def test_known_future_listing_is_not_applicable_without_fabricated_history() -> None:
    assert download.prelisting_not_applicable("688825", "2026-07-26") is True
    assert download.prelisting_not_applicable("688825", "2026-07-27") is False
    assert download.prelisting_not_applicable("300308", "2023-01-01") is False


def test_comparison_accepts_only_manifest_attested_prelisting_na(tmp_path) -> None:
    required = compare.required_market_symbols(("pool_g",))
    entries: dict[str, object] = {}
    not_applicable = {"688825"}
    dates = pd.bdate_range(end="2025-12-31", periods=3)
    for code in required:
        if code in not_applicable:
            entries[code] = {
                "status": "not_applicable_pre_listing",
                "first_trading_date": "2026-07-27",
            }
            continue
        path = tmp_path / f"{code}.csv"
        entries[code] = {
            "status": "observed",
            "sha256": _write_market_csv(path, dates),
        }
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "complete": True,
                "requested_end": "2025-12-31",
                "symbols": entries,
                "not_applicable_symbols": ["688825"],
            }
        ),
        encoding="utf-8",
    )

    compare.validate_market_data_directory(
        tmp_path,
        ("pool_g",),
        end_date="2025-12-31",
    )
    active = compare.active_symbols_for_pool(
        "pool_g",
        tmp_path,
        end_date="2025-12-31",
    )
    assert "688825" not in active
    assert len(active) == 26


def test_summary_discloses_per_member_observation_and_late_listing() -> None:
    equity = pd.DataFrame(
        {
            "assets": [100.0, 100.0],
            "cash": [100.0, 100.0],
            "position_value": [0.0, 0.0],
        },
        index=pd.to_datetime(["2023-01-03", "2026-09-15"]),
    )
    old = pd.DataFrame(
        {"close": [10.0, 11.0]},
        index=pd.to_datetime(["2023-01-03", "2026-09-15"]),
    )
    late = pd.DataFrame(
        {"close": [20.0, 21.0]},
        index=pd.to_datetime(["2026-07-27", "2026-09-15"]),
    )
    summary = summarize_universe_result(
        "pool_test",
        {"300308": "中际旭创", "688825": "长鑫科技"},
        "2023-01-01",
        "2026-09-15",
        {
            "total_return": 0.0,
            "annual_return": 0.0,
            "max_drawdown": 0.0,
            "total_trades": 0,
            "equity_curve": equity,
            "trades": [],
            "risk_events": [],
            "max_concurrent_symbols": 0,
        },
        market_frames={"300308": old, "688825": late},
    )

    assert summary["active_symbol_count"] == 2
    assert summary["late_listing_members"] == ["688825"]
    assert summary["member_observations"]["300308"]["first_observation"] == "2023-01-03"
    assert summary["member_observations"]["688825"] == {
        "name": "长鑫科技",
        "status": "late_listing",
        "first_trading_date": "2026-07-27",
        "first_observation": "2026-07-27",
        "last_observation": "2026-09-15",
    }
