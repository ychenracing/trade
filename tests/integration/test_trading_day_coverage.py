"""Exercise sourced-session coverage through both public scan paths offline."""

from dataclasses import asdict
from datetime import datetime
import json
import shutil
import sys
from zoneinfo import ZoneInfo

import pytest

from quantfusion.account.models import AccountSnapshot
from quantfusion.application import account_scan
from quantfusion.application import daily_scan as dss
from quantfusion.data import sessions
from tests.integration.test_daily_regime_snapshot import scan_inputs as scan_inputs


@pytest.fixture(autouse=True)
def fixed_market_clock(monkeypatch):
    monkeypatch.setattr(sessions, "market_now", lambda: datetime(
        2026, 9, 13, 18, tzinfo=ZoneInfo("Asia/Shanghai")
    ))


def _preserve_success(inputs):
    inputs.output.mkdir(exist_ok=True)
    signal = inputs.output / "signals_2026-09-12.json"
    signal.write_text('{"last_good":true}\n', encoding="utf-8")
    dss._save_risk_state(
        inputs.output, "2026-09-11",
        {"terminal_risk_lock": True, "sector_guard_active": False,
         "cycle_lock_count": 0, "max_drawdown": -0.18,
         "total_return": 0.0, "final_assets": 2_000_000.0},
        run_id="previous-good-run", tradable=dss.SYMBOLS,
    )
    files = [signal, inputs.output / "risk_state.json"]
    return {p: p.read_bytes() for p in files}


def test_daily_normal_weekend_records_four_dates(scan_inputs):
    assert dss.main() == 0
    result = json.loads((scan_inputs.output / "signals_2026-09-12.json").read_text())
    dates = result["scan_dates"]
    assert dates["requested_as_of"] == "2026-09-12"
    assert dates["required_evidence_date"] == "2026-09-11"
    assert dates["next_trading_date"] == "2026-09-14"
    assert dates["market_timezone"] == "Asia/Shanghai"
    assert set(result["actual_evidence_dates"].values()) == {"2026-09-11"}
    assert len(result["actual_evidence_dates"]) == 18


@pytest.mark.parametrize("failure", ["lagging", "stale"])
def test_all_inputs_one_day_late_cannot_certify_each_other(scan_inputs, failure):
    inputs = scan_inputs
    for symbol in set(dss.SYMBOLS) | set(dss.qf.PortfolioPolicy().regime_symbols):
        inputs.failures[symbol] = failure
    if failure == "lagging":
        for code in dss.ra.REGIME_INDEX_FILES.values():
            inputs.frame.iloc[:-1].to_csv(inputs.regime / f"{code}.csv")
    before = _preserve_success(inputs)
    sys.argv.extend(["--allow-stale", "--reset-risk-state"])
    assert dss.main() == 1
    assert all(p.read_bytes() == content for p, content in before.items())
    assert not inputs.snapshot.exists()


@pytest.mark.parametrize("code", ["300308", "688008"])
def test_missing_target_bar_preserves_state_even_with_reset(scan_inputs, code):
    inputs = scan_inputs
    inputs.failures[code] = "lagging"
    before = _preserve_success(inputs)
    sys.argv.extend(["--allow-stale", "--reset-risk-state"])
    assert dss.main() == 1
    assert all(p.read_bytes() == content for p, content in before.items())
    assert not inputs.snapshot.exists()


@pytest.mark.parametrize("invalid", ["missing", "old", "nonfinite"])
def test_index_coverage_failure_is_not_no_buy_signal(scan_inputs, invalid, capsys):
    inputs = scan_inputs
    path = inputs.regime / "000300.csv"
    if invalid == "missing":
        path.unlink()
    elif invalid == "old":
        inputs.frame.iloc[:-1].to_csv(path)
    else:
        frame = inputs.frame.copy()
        frame.loc[frame.index[-1], "close"] = float("inf")
        frame.to_csv(path)
    before = _preserve_success(inputs)
    sys.argv.append("--reset-risk-state")
    assert dss.main() == 1
    assert all(p.read_bytes() == content for p, content in before.items())
    assert "INDEX_EVIDENCE_UNAVAILABLE" in capsys.readouterr().out


def test_today_before_complete_bar_is_explicit(scan_inputs, monkeypatch, capsys):
    sys.argv[sys.argv.index("--end-date") + 1] = "2026-09-11"
    monkeypatch.setattr(sessions, "market_now", lambda: datetime(
        2026, 9, 11, 16, 0, tzinfo=ZoneInfo("Asia/Tokyo")
    ))
    before = _preserve_success(scan_inputs)
    sys.argv.append("--reset-risk-state")
    assert dss.main() == 1
    assert "当日收盘数据未就绪" in capsys.readouterr().out
    assert not scan_inputs.calls
    assert all(p.read_bytes() == content for p, content in before.items())


def test_unknown_calendar_rejects_before_loading(scan_inputs, tmp_path, capsys):
    sys.argv.extend(["--calendar-file", str(tmp_path / "missing-calendar.json")])
    assert dss.main() == 1
    assert "CALENDAR_UNAVAILABLE" in capsys.readouterr().out
    assert not scan_inputs.calls


def _account(tmp_path):
    snapshot = AccountSnapshot(3, "synthetic", "2026-09-12", 2_000_000.,
                               2_000_000., ())
    path = tmp_path / "synthetic_account.json"
    payload = {**asdict(snapshot), "positions": {}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path, snapshot


def _account_index_dir(inputs):
    # Retain the shared loader's strict frozen-directory assertion. Account
    # mode reads its supplied index directory directly, unlike simulation.
    target = inputs.output / "snapshots" / "account" / "regime_data"
    target.mkdir(parents=True)
    for source in inputs.regime.glob("*.csv"):
        shutil.copy2(source, target / source.name)
    return str(target)


def test_account_real_route_positive_and_identified(scan_inputs, tmp_path):
    path, snapshot = _account(tmp_path)
    result = account_scan.AccountSignalEngine(
        cache_dir=str(scan_inputs.cache), regime_data_dir=_account_index_dir(scan_inputs)
    ).run(snapshot, dss.SYMBOLS, as_of="2026-09-12", expected_account_id="synthetic")
    assert result["data_complete"]
    assert result["valuation_complete"]
    assert result["scan_dates"]["required_evidence_date"] == "2026-09-11"
    assert result["scan_dates"]["next_trading_date"] == "2026-09-14"
    assert result["index_evidence"]
    assert len(result["account_code_sha256"]) == 64
    assert snapshot.cash == 2_000_000.
    assert path.exists()


def test_account_missing_index_preserves_success_artifact(scan_inputs, tmp_path, capsys):
    path, _ = _account(tmp_path)
    (scan_inputs.regime / "000300.csv").unlink()
    scan_inputs.output.mkdir()
    output = scan_inputs.output / "account_signals_2026-09-12.json"
    output.write_text('{"last_good":true}\n')
    original = output.read_bytes()
    assert account_scan.run_account_scan(
        account_path=str(path), symbols=dss.SYMBOLS, end_date="2026-09-12",
        cache_dir=str(scan_inputs.cache), regime_data_dir=str(scan_inputs.regime),
        output_dir=str(scan_inputs.output), expected_account_id="synthetic",
    ) == 1
    assert output.read_bytes() == original
    assert "INDEX_EVIDENCE_UNAVAILABLE" in capsys.readouterr().out


def test_account_all_stocks_lagging_blocks_new_risk(scan_inputs, tmp_path):
    _, snapshot = _account(tmp_path)
    for symbol in set(dss.SYMBOLS) | set(dss.qf.PortfolioPolicy().regime_symbols):
        scan_inputs.failures[symbol] = "lagging"
    result = account_scan.AccountSignalEngine(
        cache_dir=str(scan_inputs.cache), regime_data_dir=_account_index_dir(scan_inputs)
    ).run(snapshot, dss.SYMBOLS, as_of="2026-09-12", expected_account_id="synthetic")
    assert result["buys_suppressed"]
    assert not any(row["action"] == "BUY_CANDIDATE" for row in result["actions"])
    assert result["market_data_errors"]
