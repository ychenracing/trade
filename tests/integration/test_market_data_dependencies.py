"""Exercise market-data producers through frozen files and the native loader."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from quantfusion.application import daily_scan as dss
from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.config.universe import SYMBOL_NAMES
from scripts import download_eastmoney_qfq as download


class ReplayInputsLoaded(Exception):
    """Stop after native frozen-input loading, before any economic simulation."""


def _frame(last: str = "2026-09-11") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [10.0] * 90,
            "high": [11.0] * 90,
            "low": [9.0] * 90,
            "close": [10.0] * 90,
            "volume": [1_000_000.0] * 90,
        },
        index=pd.bdate_range(end=last, periods=90, name="date"),
    )


def _scan(monkeypatch, tmp_path: Path, *, failures=None, dates=None,
          stale=(), allow_stale=False, mode="auto"):
    cache = tmp_path / "cache"
    regime = tmp_path / "regime"
    output = tmp_path / "output"
    regime.mkdir()
    output.mkdir()
    for code in dss.ra.REGIME_INDEX_FILES.values():
        _frame().to_csv(regime / f"{code}.csv")
    argv = [
        "daily_scan", "--start-date", "2026-07-01", "--end-date", "2026-09-12",
        "--cache-dir", str(cache), "--regime-data-dir", str(regime),
        "--output-dir", str(output), "--deployment-mode", mode,
    ]
    if allow_stale:
        argv.append("--allow-stale")
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(dss.market_data_contracts, "refresh_regime_indices",
                        lambda *args, **kwargs: {})
    calls = Counter()
    native_load = dss.qf.DataFetcher.load_stock_data
    captured = {}

    def provider(code, start_date, end_date, **kwargs):
        calls[code] += 1
        failure = (failures or {}).get(code)
        if failure == "error":
            raise OSError(f"unavailable {code}")
        if failure == "empty":
            return pd.DataFrame()
        frame = _frame((dates or {}).get(code, "2026-09-11"))
        if code in stale:
            frame.attrs.update(_stale=True, _cache_last_date="2026-09-11")
        return frame

    class Engine:
        def __init__(self, capital):
            pass

        def decide_current(self, symbols, **kwargs):
            captured["current_symbols"] = dict(symbols)
            captured["as_of"] = kwargs["as_of"]
            return SimpleNamespace(name="trend", boundary=kwargs["as_of"], leaders=None)

        def run(self, symbols, start_date, end_date, **kwargs):
            captured["replay_symbols"] = dict(symbols)
            market_dir = Path(kwargs["data_dir"])
            captured["manifest"] = dss._verify_frozen_snapshot(market_dir.parent)
            for code in set(symbols) | set(PortfolioPolicy().regime_symbols):
                frame = native_load(code, start_date, end_date, data_dir=str(market_dir))
                assert not frame.empty, code
            raise ReplayInputsLoaded

    monkeypatch.setattr(dss.qf.DataFetcher, "load_stock_data", provider)
    monkeypatch.setattr(dss.ra, "RegimeAdaptiveBacktestEngine", Engine)
    return calls, captured, output, regime


@pytest.mark.parametrize("mode", ["auto", "trend", "weak"])
def test_daily_scan_freezes_references_without_trading_them(monkeypatch, tmp_path, mode):
    calls, captured, output, _ = _scan(monkeypatch, tmp_path, mode=mode)
    with pytest.raises(ReplayInputsLoaded):
        dss.main()
    required = set(SYMBOL_NAMES) | set(PortfolioPolicy().regime_symbols)
    assert len(SYMBOL_NAMES) == 17
    assert "688008" in required - set(SYMBOL_NAMES)
    assert captured["current_symbols"] == SYMBOL_NAMES
    assert captured["replay_symbols"] == SYMBOL_NAMES
    assert captured["manifest"]["symbols"] == sorted(required)
    market = output / "snapshots" / "2026-09-12" / "market_data"
    assert {path.stem for path in market.glob("*.csv")} == required
    assert calls == Counter({code: 1 for code in required})
    assert captured["as_of"] == "2026-09-12"


@pytest.mark.parametrize("code", ["688008", "300308", "688256"])
@pytest.mark.parametrize("failure", ["empty", "error"])
@pytest.mark.parametrize("allow_stale", [False, True])
def test_missing_required_data_never_shrinks_the_universe(
    monkeypatch, tmp_path, code, failure, allow_stale
):
    _, captured, output, _ = _scan(
        monkeypatch, tmp_path, failures={code: failure}, allow_stale=allow_stale
    )
    previous = output / "signals_2026-09-12.json"
    previous.write_bytes(b"previous-success")
    assert dss.main() == 1
    assert captured == {}
    assert previous.read_bytes() == b"previous-success"
    assert not (output / "snapshots").exists()
    assert not (output / "risk_state.json").exists()


@pytest.mark.parametrize("kind", ["stale", "lagging"])
@pytest.mark.parametrize("allow_stale", [False, True])
def test_reference_freshness_uses_the_same_simulation_override(
    monkeypatch, tmp_path, kind, allow_stale
):
    _, captured, _, _ = _scan(
        monkeypatch, tmp_path,
        stale=("688008",) if kind == "stale" else (),
        dates={"688008": "2026-09-10"} if kind == "lagging" else {},
        allow_stale=allow_stale,
    )
    if allow_stale:
        with pytest.raises(ReplayInputsLoaded):
            dss.main()
    else:
        assert dss.main() == 1
        assert captured == {}


def test_incomplete_existing_snapshot_is_rejected_without_rewriting(monkeypatch, tmp_path):
    _, captured, output, regime = _scan(monkeypatch, tmp_path)
    snapshot = output / "snapshots" / "2026-09-12"
    dss._materialize_frozen_snapshot(
        snapshot_dir=snapshot, cache_dir=tmp_path / "cache",
        regime_data_dir=regime, frames={code: _frame() for code in SYMBOL_NAMES},
        end_date="2026-09-12",
    )
    before = {str(p.relative_to(snapshot)): p.read_bytes()
              for p in snapshot.rglob("*") if p.is_file()}
    assert dss.main() == 1
    assert captured == {}
    assert before == {str(p.relative_to(snapshot)): p.read_bytes()
                      for p in snapshot.rglob("*") if p.is_file()}


def test_snapshot_freezes_validated_frames_not_mutable_cache(monkeypatch, tmp_path):
    _, _, _, regime = _scan(monkeypatch, tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    changed = _frame()
    changed["close"] = 10.5
    changed.to_csv(cache / "300308.csv")
    snapshot = tmp_path / "snapshot"
    dss._materialize_frozen_snapshot(
        snapshot_dir=snapshot, cache_dir=cache, regime_data_dir=regime,
        frames={"300308": _frame()}, end_date="2026-09-12",
    )
    frozen = pd.read_csv(snapshot / "market_data" / "300308.csv")
    assert frozen["close"].tolist() == _frame()["close"].tolist()


@pytest.mark.parametrize("explicit", [False, True])
def test_offline_download_default_includes_reference_only_data(
    monkeypatch, tmp_path, explicit
):
    requested = []

    def fake_download(code, start, end):
        requested.append(code)
        return _frame().reset_index(), code

    argv = ["download", "--output", str(tmp_path / "download")]
    if explicit:
        argv += ["--symbol", "300308"]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(download, "_download", fake_download)
    monkeypatch.setattr(download.time, "sleep", lambda seconds: None)
    assert download.main() == 0
    expected = ({"300308"} if explicit else
                set(SYMBOL_NAMES) | set(PortfolioPolicy().regime_symbols))
    assert set(requested) == expected
    assert len(requested) == len(expected)
    manifest = json.loads((tmp_path / "download" / "manifest.json").read_text())
    assert set(manifest["symbols"]) == expected
