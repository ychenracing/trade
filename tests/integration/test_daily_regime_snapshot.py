"""Daily acquisition must freeze signal-only references without trading them."""

from collections import Counter
from pathlib import Path
from types import SimpleNamespace
import json
import sys

import pandas as pd
import pytest

from quantfusion.application import daily_scan as dss


@pytest.fixture
def scan_inputs(tmp_path, monkeypatch):
    dates = pd.bdate_range("2024-07-01", "2026-09-11")
    close = pd.Series([100.0 + i * 0.05 for i in range(len(dates))], index=dates)
    frame = pd.DataFrame({
        "open": close,
        "close": close,
        "high": close + 1.0,
        "low": close - 1.0,
        "volume": 10_000_000.0,
    })
    frame.index.name = "date"
    regime = tmp_path / "regime"
    regime.mkdir()
    for code in dss.ra.REGIME_INDEX_FILES.values():
        frame.to_csv(regime / f"{code}.csv")
    output = tmp_path / "output"
    cache = tmp_path / "cache"
    calls = Counter()
    failures = {}
    original_loader = dss.qf.DataFetcher.load_stock_data

    def load(symbol, start_date, end_date, data_dir=None, cache_dir=None):
        if data_dir is not None:
            # The real engine must consume frozen bytes, never mutable fallbacks.
            assert Path(data_dir).is_relative_to(output / "snapshots")
            return original_loader(symbol, start_date, end_date, data_dir=data_dir)
        calls[symbol] += 1
        value = frame.loc[start_date:end_date].copy()
        failure = failures.get(symbol)
        if failure == "error":
            raise OSError(f"provider unavailable for {symbol}")
        if failure == "empty":
            return value.iloc[:0]
        if failure == "stale":
            value.attrs["_stale"] = True
            value.attrs["_cache_last_date"] = "2026-09-10"
        if failure == "lagging":
            return value.iloc[:-1]
        return value

    monkeypatch.setattr(dss.qf.DataFetcher, "load_stock_data", load)
    monkeypatch.setattr(
        dss.market_data_contracts, "refresh_regime_indices", lambda *a, **k: {}
    )
    monkeypatch.setattr(sys, "argv", [
        "daily_scan", "--start-date", "2026-09-01", "--end-date", "2026-09-12",
        "--output-dir", str(output), "--cache-dir", str(cache),
        "--regime-data-dir", str(regime),
    ])
    return SimpleNamespace(
        frame=frame, regime=regime, output=output, cache=cache,
        calls=calls, failures=failures,
        snapshot=output / "snapshots" / "2026-09-12",
    )


def test_daily_scan_freezes_references_but_keeps_seventeen_trade_symbols(scan_inputs):
    inputs = scan_inputs
    sys.argv.append("--reset-risk-state")
    assert "688008" not in dss.SYMBOLS
    assert len(dss.SYMBOLS) == 17
    assert dss.main() == 0

    required = set(dss.SYMBOLS) | set(dss.qf.PortfolioPolicy().regime_symbols)
    manifest = dss._verify_frozen_snapshot(inputs.snapshot)
    assert set(manifest["symbols"]) == required
    assert len(required) == 18
    assert {p.stem for p in (inputs.snapshot / "market_data").glob("*.csv")} == required
    assert inputs.calls == Counter({code: 1 for code in required})
    artifact = json.loads((inputs.output / "signals_2026-09-12.json").read_text())
    assert artifact["status"] == "ok"
    assert set(artifact["symbols"]) == set(dss.SYMBOLS)
    assert {row["code"] for row in artifact["signals"]} == set(dss.SYMBOLS)
    for key in ("pending_signals", "blocked_signals"):
        assert all(signal["symbol"] != "688008" for signal in artifact.get(key, []))
    risk = json.loads((inputs.output / "risk_state.json").read_text())
    assert risk["total_symbols"] == 17
    fingerprint = (
        "start=2026-09-01|indicator=warm|capital=2000000.0"
        "|warmup=365|deployment=auto"
    )
    assert risk["symbols_hash"] == dss._compute_identity_hash(dss.SYMBOLS, fingerprint)


@pytest.mark.parametrize("failure,allow_stale", [
    ("error", False), ("empty", False), ("stale", False), ("lagging", False),
    ("error", True), ("empty", True),
])
def test_unusable_reference_fails_before_publication(
    scan_inputs, failure, allow_stale, capsys
):
    inputs = scan_inputs
    inputs.failures["688008"] = failure
    if allow_stale:
        sys.argv.append("--allow-stale")
    inputs.output.mkdir()
    previous_signal = inputs.output / "signals_2026-09-12.json"
    previous_signal.write_text('{"last_good": true}\n', encoding="utf-8")
    dss._save_risk_state(
        inputs.output, "2026-09-11",
        {"terminal_risk_lock": True, "sector_guard_active": False,
         "cycle_lock_count": 0, "max_drawdown": -0.18,
         "total_return": 0.0, "final_assets": 2_000_000.0},
        run_id="previous-good-run", tradable=dss.SYMBOLS,
    )
    previous_risk = inputs.output / "risk_state.json"
    original_signal = previous_signal.read_bytes()
    original_risk = previous_risk.read_bytes()
    assert dss.main() == 1
    assert previous_signal.read_bytes() == original_signal
    assert previous_risk.read_bytes() == original_risk
    assert not inputs.snapshot.exists()
    assert "688008" in capsys.readouterr().out


def test_incomplete_existing_snapshot_is_rejected_without_rewriting(scan_inputs):
    inputs = scan_inputs
    dss._materialize_frozen_snapshot(
        snapshot_dir=inputs.snapshot, cache_dir=inputs.cache,
        regime_data_dir=inputs.regime,
        frames={code: inputs.frame.copy() for code in dss.SYMBOLS},
        end_date="2026-09-12",
    )
    before = {p.relative_to(inputs.snapshot): p.read_bytes()
              for p in inputs.snapshot.rglob("*") if p.is_file()}
    assert dss.main() == 1
    after = {p.relative_to(inputs.snapshot): p.read_bytes()
             for p in inputs.snapshot.rglob("*") if p.is_file()}
    assert after == before
    assert not (inputs.output / "signals_2026-09-12.json").exists()
    assert not (inputs.output / "risk_state.json").exists()
