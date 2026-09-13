"""Offline exchange-notice coverage, not a stock-tail or weekday heuristic."""
from datetime import datetime
import json
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quantfusion.data import sessions

NOW = datetime(2026, 12, 30, 18, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.mark.parametrize("requested,required,next_day", [
    ("2026-09-11", "2026-09-11", "2026-09-14"),
    ("2026-09-12", "2026-09-11", "2026-09-14"),
    ("2026-09-13", "2026-09-11", "2026-09-14"),
    ("2026-02-23", "2026-02-13", "2026-02-24"),
    ("2026-05-04", "2026-04-30", "2026-05-06"),
    ("2026-05-09", "2026-05-08", "2026-05-11"),
    ("2026-09-20", "2026-09-18", "2026-09-21"),
    ("2026-10-10", "2026-10-09", "2026-10-12"),
    ("2025-02-04", "2025-01-27", "2025-02-05"),
    ("2025-10-08", "2025-09-30", "2025-10-09"),
    ("2024-02-09", "2024-02-08", "2024-02-19"),
])
def test_reviewed_notice_sessions(requested, required, next_day):
    dates = sessions.resolve_scan_dates(requested, now=NOW)
    assert dates["requested_as_of"] == requested
    assert dates["required_evidence_date"] == required
    assert dates["next_trading_date"] == next_day
    assert dates["market_timezone"] == "Asia/Shanghai"
    assert len(dates["calendar_sha256"]) == 64


@pytest.mark.parametrize("clock", [
    "2026-09-11T14:59:59+08:00",
    "2026-09-11T15:29:59+08:00",
    "2026-09-11T16:00:00+09:00",
])
def test_incomplete_current_day_is_not_silently_backdated(clock):
    with pytest.raises(ValueError, match="CLOSE_NOT_READY.*当日收盘数据未就绪"):
        sessions.resolve_scan_dates("2026-09-11", now=datetime.fromisoformat(clock))


def test_complete_current_day_and_historical_request():
    dates = sessions.resolve_scan_dates("2026-09-11", now=datetime.fromisoformat("2026-09-11T16:30:00+09:00"))
    assert dates["required_evidence_date"] == "2026-09-11"
    historical = sessions.resolve_scan_dates("2025-02-05", now=datetime.fromisoformat("2026-09-11T10:00:00+08:00"))
    assert historical["required_evidence_date"] == "2025-02-05"


@pytest.mark.parametrize("requested", ["2023-12-29", "2024-01-01", "2026-12-31", "2027-01-04"])
def test_calendar_never_extrapolates_unknown_previous_or_next_session(requested):
    with pytest.raises(ValueError, match="CALENDAR_OUT_OF_RANGE"):
        sessions.resolve_scan_dates(requested, now=datetime(2028, 1, 1, tzinfo=ZoneInfo("UTC")))


def test_missing_calendar_and_missing_annual_source_fail_closed(tmp_path):
    with pytest.raises(ValueError, match="CALENDAR_UNAVAILABLE"):
        sessions.resolve_scan_dates("2026-09-11", calendar_file=tmp_path / "missing", now=NOW)
    raw = json.loads(sessions.DEFAULT_CALENDAR_FILE.read_text())
    raw["years"].pop("2025")
    target = tmp_path / "calendar.json"
    target.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="coverage has gaps"):
        sessions.resolve_scan_dates("2026-09-11", calendar_file=target, now=NOW)


@pytest.mark.parametrize("index,reason", [
    ([], "MARKET_DATA_MISSING"),
    (["2026-09-10"], "TRADING_DAY_COVERAGE"),
    (["2026-09-14"], "FUTURE_EVIDENCE"),
    (["2026-09-11", "2026-09-10"], "INVALID_EVIDENCE_DATE"),
    (["2026-09-11", "2026-09-11"], "INVALID_EVIDENCE_DATE"),
    (["2026-09-11 10:00"], "INVALID_EVIDENCE_DATE"),
])
def test_stock_tail_must_be_the_required_session(index, reason):
    frame = pd.DataFrame({"close": [1.] * len(index)}, index=pd.to_datetime(index))
    with pytest.raises(ValueError, match=reason):
        sessions.require_frame_coverage(frame, sessions.resolve_scan_dates("2026-09-12", now=NOW), "688008")


def test_future_request_and_naive_clock_are_rejected():
    with pytest.raises(ValueError, match="FUTURE_REQUEST"):
        sessions.resolve_scan_dates("2027-01-05", now=NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        sessions.resolve_scan_dates("2026-09-11", now=datetime(2026, 9, 11, 18))


def test_index_evidence_cannot_certify_itself_when_all_inputs_lag(tmp_path):
    dates = sessions.resolve_scan_dates("2026-09-12", now=NOW)
    frame = pd.DataFrame({"open": 1., "high": 1., "low": 1., "close": 1.},
                         index=pd.bdate_range(end="2026-09-10", periods=80))
    frame.index.name = "date"
    for code in sessions.REGIME_INDEX_FILES.values():
        frame.to_csv(tmp_path / f"{code}.csv")
    with pytest.raises(ValueError, match="INDEX_EVIDENCE_UNAVAILABLE.*TRADING_DAY_COVERAGE"):
        sessions.index_coverage(tmp_path, dates)
