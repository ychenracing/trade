"""The read-only report explains saved dates without certifying old inputs."""

from copy import deepcopy

import pytest

from quantfusion.application.daily_report import render_daily_report
from tests.unit.test_daily_report import account, simulation


@pytest.mark.parametrize("mode", ["account", "simulation"])
def test_report_distinguishes_requested_required_observed_and_next_dates(mode):
    data = account() if mode == "account" else simulation()
    data["scan_dates"] = {
        "requested_as_of": "2026-09-12",
        "required_evidence_date": "2026-09-11",
        "next_trading_date": "2026-09-14",
        "market_timezone": "Asia/Shanghai",
        "calendar_coverage_start": "2024-01-01",
        "calendar_coverage_end": "2026-12-31",
    }
    if mode == "account":
        data.update(as_of="2026-09-12", snapshot_date="2026-09-12", evidence_date="2026-09-11")
        data["market_evidence"] = {"300308": {"evidence_date": "2026-09-11"}}
        data["index_evidence"] = {"000300": {"evidence_date": "2026-09-11"}}
    else:
        data["scan_date"] = "2026-09-12"
        data["actual_evidence_dates"] = {"300308": "2026-09-11", "688008": "2026-09-11"}
        data["deployment"]["index_evidence"] = {"000300": {"evidence_date": "2026-09-11"}}
    before = deepcopy(data)
    text = render_daily_report(data)
    for phrase in ("请求截止日期：2026-09-12", "应覆盖交易日：2026-09-11",
                   "实际股票行情日期：2026-09-11", "实际指数日期：2026-09-11",
                   "下一可交易日：2026-09-14", "Asia/Shanghai", "2026-12-31"):
        assert phrase in text
    assert data == before


def test_old_report_does_not_silently_inherit_new_calendar_certification():
    data = simulation()
    text = render_daily_report(data)
    assert "未记录交易日覆盖校验" in text
    assert "应覆盖交易日：2026-07-30" not in text


def test_actual_mixed_evidence_dates_are_not_replaced_by_required_date():
    data = account()
    data["scan_dates"] = {"requested_as_of": "2026-09-12", "required_evidence_date": "2026-09-11"}
    data["market_evidence"] = {"300308": {"evidence_date": "2026-09-10"},
                               "300502": {"evidence_date": "2026-09-11"}}
    text = render_daily_report(data)
    assert "实际股票行情日期：2026-09-10、2026-09-11" in text
    assert "下一可交易日：未提供" in text
