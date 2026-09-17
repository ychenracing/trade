from __future__ import annotations

from pathlib import Path

import pytest

from quantfusion.application import backtest_cli, daily_scan
from quantfusion.config.research_universes import DEFAULT_RESEARCH_START_DATE
from scripts import download_market_data as download


def test_backtest_cli_keeps_only_current_date_flags() -> None:
    parser = backtest_cli.build_argument_parser()
    args = parser.parse_args(
        ["--start-date", "2024-01-01", "--end-date", "2025-12-31", "--no-plot"]
    )
    assert args.start_date == "2024-01-01"
    assert args.end_date == "2025-12-31"
    with pytest.raises(SystemExit):
        parser.parse_args(["--start", "2024-01-01", "--no-plot"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--end", "2025-12-31", "--no-plot"])


def test_backtest_window_has_one_current_default() -> None:
    assert backtest_cli.resolve_backtest_window(
        "", "", today="2026-09-17"
    ) == (DEFAULT_RESEARCH_START_DATE, "2026-09-17")
    assert not hasattr(backtest_cli, "LEGACY_BACKTEST_START_DATE")
    assert not hasattr(backtest_cli, "LEGACY_BACKTEST_END_DATE")


def test_market_data_entrypoint_is_unique() -> None:
    root = Path(__file__).resolve().parents[2]
    downloaders = {path.name for path in (root / "scripts").glob("download_*.py")}
    assert downloaders == {"download_market_data.py"}


def test_download_cli_keeps_only_current_date_flags() -> None:
    parser = download.build_argument_parser()
    args = parser.parse_args(
        ["--start-date", "2024-01-01", "--end-date", "2025-12-31"]
    )
    assert args.start_date == "2024-01-01"
    assert args.end_date == "2025-12-31"
    with pytest.raises(SystemExit):
        parser.parse_args(["--start", "2024-01-01"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--end", "2025-12-31"])


def test_download_path_has_one_current_window_and_provider_route() -> None:
    assert download.resolve_download_window(
        "", "", today="2026-09-17"
    ) == (DEFAULT_RESEARCH_START_DATE, "2026-09-17")
    assert not hasattr(download, "LEGACY_START_DATE")
    assert not hasattr(download, "LEGACY_END_DATE")
    assert not hasattr(download, "_download")


def test_daily_scan_rejects_removed_allow_stale_flag() -> None:
    parser = daily_scan.build_argument_parser()
    parser.parse_args(["--start-date", "2026-01-01", "--end-date", "2026-09-17"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--allow-stale"])
