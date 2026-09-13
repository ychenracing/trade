"""Offline advice identity and account-capacity behavior, without human fills."""

from dataclasses import replace
import hashlib
import json

import pandas as pd
import pytest

from quantfusion.account.models import AccountSnapshot
from quantfusion.application import account_scan
from quantfusion.data.sessions import load_calendar
from quantfusion.research.fingerprints import account_source_sha, canonical_sequence_sha
from scripts import decision_diagnostics as diagnostic
from tests.unit.test_account_truth_boundary import (
    _BuyStrategy, _NoBuyStrategy, _decision, _frame, _indicators,
)


def _identified_advice(frame, day="2025-04-01"):
    prefix = frame.loc[(frame.index >= pd.Timestamp(day)-pd.Timedelta(days=700)) & (frame.index <= pd.Timestamp(day))]
    return {
        "as_of": day, "mode": "account_decision_support",
        "account_snapshot_sha256": hashlib.sha256(b"synthetic-account").hexdigest(),
        "account_code_sha256": account_source_sha(),
        "engine_config_sha256": canonical_sequence_sha(diagnostic.default_engine_config()),
        "scan_dates": {"calendar_sha256": load_calendar().sha256},
        "market_evidence": {"300308": {
            "frame_sha256": hashlib.sha256(prefix.to_csv(index=True).encode()).hexdigest(),
            "config_sha256": canonical_sequence_sha(diagnostic.symbol_config("300308")),
        }},
        "candidate_diagnostics": [{"symbol": "300308", "score": .5,
            "score_components": {"momentum": .25}, "confirmation_count": 1,
            "indicative_target_shares": 0, "constraint_reason": "insufficient lot cash"}],
        "actions": [],
    }


def test_saved_advice_matures_without_claiming_an_actual_fill(tmp_path):
    dates = pd.to_datetime([d for d in load_calendar().sessions if "2025-01-01" <= d <= "2025-08-01"])
    frame = pd.DataFrame({"open": 100., "close": 100., "high": 101., "low": 99., "volume": 1_000_000.}, index=dates)
    path = tmp_path / "account_signals.json"
    diagnostic.atomic_json(_identified_advice(frame), path)
    raw = path.read_bytes()
    result = diagnostic.evaluate_advice(json.loads(raw), {"300308": frame}, load_calendar(), "2025-07-20")
    assert result["identity"]["status"] == "MARKET_CODE_CONFIG_VERIFIED"
    row = result["rows"][0]
    assert row["indicative_target_shares"] == 0
    assert row["labels"]["5"]["status"] == "MATURE"
    assert row["labels"]["20"]["status"] == "MATURE"
    assert row["actual_human_fill"] == "UNKNOWN"
    assert path.read_bytes() == raw
    frame.loc[pd.Timestamp("2025-04-01"), "close"] = 99.
    rejected = diagnostic.evaluate_advice(json.loads(raw), {"300308": frame}, load_calendar(), "2025-07-20")
    assert rejected["identity"]["status"] == "IDENTITY_UNVERIFIED"
    assert rejected["rows"] == []


def test_weekend_advice_label_still_enters_at_next_exchange_open():
    dates = pd.to_datetime([d for d in load_calendar().sessions if "2025-01-01" <= d <= "2025-08-01"])
    frame = pd.DataFrame({"open": 100., "close": 100., "high": 101., "low": 99., "volume": 1_000_000.}, index=dates)
    result = diagnostic.execution_label("300308", frame, "2025-04-05", 5, load_calendar().sessions, "2025-07-20")
    assert result["entry_date"] == "2025-04-07"
    assert result["status"] == "MATURE"


@pytest.mark.parametrize("cash", [100_000., 1_000.])
def test_actual_score_selects_scarce_slot_and_cash_remains_a_real_constraint(monkeypatch, cash):
    config = account_scan.default_engine_config()
    monkeypatch.setattr(account_scan, "default_engine_config", lambda: {**config, "max_positions": 1})
    monkeypatch.setattr(account_scan, "index_coverage", lambda *a, **k: {})
    monkeypatch.setattr(account_scan.data_contracts, "refresh_regime_indices", lambda *a, **k: {})
    monkeypatch.setattr(account_scan.RegimeAdaptiveBacktestEngine, "decide_current", lambda *a, **k: _decision("frozen_trend_engine"))
    monkeypatch.setattr(account_scan, "TurtleBreakoutStrategy", _BuyStrategy)
    monkeypatch.setattr(account_scan, "DualMAStrategy", _NoBuyStrategy)
    monkeypatch.setattr(account_scan, "ATRChannelStrategy", _NoBuyStrategy)
    monkeypatch.setattr(account_scan.Indicators, "compute_all", _indicators)
    flat = _frame()
    rising = flat.copy()
    rising.loc[:, "close"] = [80. + 20.*i/(len(flat)-1) for i in range(len(flat))]
    rising.loc[:, "open"] = rising["close"]
    rising.loc[:, "high"] = rising["close"]+1.
    rising.loc[:, "low"] = rising["close"]-1.
    frames = {"300308": flat, "300502": rising}
    engine = account_scan.AccountSignalEngine(cache_dir="unused", regime_data_dir="unused")
    monkeypatch.setattr(engine, "_frame", lambda code, day: frames[code])
    snapshot = AccountSnapshot(3, "main", "2026-02-01", cash, cash, ())
    before = replace(snapshot)
    result = engine.run(snapshot, {"300308": "弱候选", "300502": "强候选"}, as_of="2026-02-01")
    candidates = result["candidate_diagnostics"]
    assert len(candidates) == 2
    assert result["candidate_slots"] == 1
    assert candidates[0]["symbol"] == "300502"  # Real score overrides lexical tie-break.
    assert candidates[0]["score"] > candidates[1]["score"]
    assert candidates[0]["selected_for_slots"]
    assert not candidates[1]["selected_for_slots"]
    assert candidates[1]["slot_constraint"] == "POSITION_SLOTS"
    quantity = candidates[0]["indicative_target_shares"]
    assert quantity > 0 if cash == 100_000. else quantity == 0
    assert snapshot == before
