"""Exercise real acquisition, snapshot and publication boundaries offline."""

from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from quantfusion.application import daily_scan as dss
from quantfusion.config import paths
from quantfusion.config.daily import DEFAULT_REGIME_DATA_DIR
from quantfusion.data import contracts, snapshot
from quantfusion.data.providers import DataFetcher
from .test_daily_regime_snapshot import scan_inputs as scan_inputs


def _frame(start="2026-01-01", end="2026-09-11"):
    dates = pd.bdate_range(start, end)
    return pd.DataFrame(
        {"open": 10.0, "close": 10.0, "high": 11.0, "low": 9.0,
         "volume": 1000.0}, index=pd.DatetimeIndex(dates, name="date")
    )


def _snapshot_inputs(tmp_path):
    regime = tmp_path / "regime"
    regime.mkdir()
    frame = _frame()
    for code in dss.ra.REGIME_INDEX_FILES.values():
        frame.to_csv(regime / f"{code}.csv")
    return dict(snapshot_dir=tmp_path / "snapshot", cache_dir=tmp_path / "cache",
                regime_data_dir=regime, frames={"300308": frame},
                end_date="2026-09-12")


def _resign(root, manifest):
    payload = (json.dumps(manifest, indent=2) + "\n").encode()
    (root / "manifest.json").write_bytes(payload)
    (root / "manifest.sha256").write_text(hashlib.sha256(payload).hexdigest()+"\n")


def test_snapshot_freezes_validated_frame_not_mutable_cache(tmp_path):
    kwargs = _snapshot_inputs(tmp_path)
    kwargs["cache_dir"].mkdir()
    different = kwargs["frames"]["300308"] * 2
    different.to_csv(kwargs["cache_dir"] / "300308.csv")
    snapshot.materialize_frozen_snapshot(**kwargs)
    frozen = pd.read_csv(kwargs["snapshot_dir"] / "market_data/300308.csv")
    assert frozen["close"].iloc[-1] == 10.0


def test_snapshot_rejects_declared_symbol_without_evidence(tmp_path):
    kwargs = _snapshot_inputs(tmp_path)
    manifest = snapshot.materialize_frozen_snapshot(**kwargs)
    manifest["symbols"].append("688008")
    _resign(kwargs["snapshot_dir"], manifest)
    with pytest.raises(ValueError):
        snapshot.verify_frozen_snapshot(kwargs["snapshot_dir"])


def test_snapshot_rejects_missing_index_even_with_matching_hashes(tmp_path):
    kwargs = _snapshot_inputs(tmp_path)
    manifest = snapshot.materialize_frozen_snapshot(**kwargs)
    removed = "regime_data/000682.csv"
    (kwargs["snapshot_dir"] / removed).unlink()
    manifest["evidence"] = [x for x in manifest["evidence"] if x["path"] != removed]
    _resign(kwargs["snapshot_dir"], manifest)
    with pytest.raises(ValueError):
        snapshot.verify_frozen_snapshot(kwargs["snapshot_dir"])


@pytest.mark.parametrize("invalid", [[], None, "300308", ["300308", "300308"]])
def test_snapshot_rejects_malformed_symbol_inventory(tmp_path, invalid):
    kwargs = _snapshot_inputs(tmp_path)
    manifest = snapshot.materialize_frozen_snapshot(**kwargs)
    manifest["symbols"] = invalid
    _resign(kwargs["snapshot_dir"], manifest)
    with pytest.raises(ValueError):
        snapshot.verify_frozen_snapshot(kwargs["snapshot_dir"])


def test_snapshot_cannot_follow_symlinked_evidence_directory(tmp_path):
    kwargs = _snapshot_inputs(tmp_path)
    snapshot.materialize_frozen_snapshot(**kwargs)
    market = kwargs["snapshot_dir"] / "market_data"
    outside = tmp_path / "outside"
    market.rename(outside)
    market.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        snapshot.verify_frozen_snapshot(kwargs["snapshot_dir"])


@pytest.mark.parametrize("field", ["close", "volume"])
def test_market_provider_rejects_infinite_values(field):
    frame = _frame()
    frame.loc[frame.index[-1], field] = float("inf")
    if field == "close":
        frame.loc[frame.index[-1], "high"] = float("inf")
    with pytest.raises(ValueError, match="finite"):
        DataFetcher._normalize_columns(frame)


@pytest.mark.parametrize("invalid", [[], None, "metadata"])
def test_invalid_cache_metadata_is_not_a_valid_contract(tmp_path, invalid):
    cache = tmp_path / "300308.csv"
    cache.with_suffix(".csv.meta.json").write_text(json.dumps(invalid))
    assert DataFetcher._cache_has_share_volume_contract(cache) is False


def test_cache_fetches_required_history_before_existing_prefix(tmp_path, monkeypatch):
    _frame("2026-08-03").to_csv(tmp_path / "300308.csv")
    DataFetcher._write_cache_contract(tmp_path / "300308.csv")
    calls = []

    def fetch(code, start, end):
        calls.append((code, start, end))
        return _frame(start, end)

    monkeypatch.setattr(DataFetcher, "fetch_stock_data", fetch)
    result = DataFetcher.load_stock_data("300308", "2026-01-01", "2026-09-11",
                                         cache_dir=str(tmp_path))
    assert result.index[0] == pd.Timestamp("2026-01-01")
    assert calls == [("300308", "2026-01-01", "2026-09-11")]


def test_weekend_only_tail_does_not_mark_friday_cache_stale(tmp_path, monkeypatch):
    _frame().to_csv(tmp_path / "300308.csv")
    DataFetcher._write_cache_contract(tmp_path / "300308.csv")

    def unavailable(*args, **kwargs):
        raise RuntimeError("no bars exist for a Saturday")

    monkeypatch.setattr(DataFetcher, "fetch_stock_data", unavailable)
    result = DataFetcher.load_stock_data("300308", "2026-01-01", "2026-09-12",
                                         cache_dir=str(tmp_path))
    assert not result.attrs.get("_stale", False)
    assert result.index[-1] == pd.Timestamp("2026-09-11")


def test_daily_default_indices_are_outside_retained_evidence():
    default = Path(DEFAULT_REGIME_DATA_DIR).resolve()
    assert not default.is_relative_to(paths.REGIME_DATA_DIR.resolve())
    assert not default.is_relative_to(paths.MARKET_DATA_DIR.resolve())


def test_index_refresh_preserves_retained_evidence(tmp_path, monkeypatch):
    protected = tmp_path / "retained"
    protected.mkdir()
    monkeypatch.setattr(paths, "REGIME_DATA_DIR", protected)
    before = {}
    for code in contracts.INDEX_SYMBOLS:
        path = protected / f"{code}.csv"
        path.write_bytes(b"retained immutable evidence\n")
        before[path] = path.read_bytes()
    calls = Counter()

    def fetch(**kwargs):
        calls[kwargs["symbol"]] += 1
        return _frame(end=pd.Timestamp.today().strftime("%Y-%m-%d")).reset_index()

    monkeypatch.setattr(contracts, "ak", SimpleNamespace(stock_zh_index_daily_em=fetch))
    contracts.refresh_regime_indices(protected, end_date=pd.Timestamp.today().strftime("%Y-%m-%d"))
    assert not calls
    assert all(path.read_bytes() == content for path, content in before.items())
    assert not (protected / "live_refresh_manifest.json").exists()


@pytest.mark.parametrize("failure", ["error", "empty"])
def test_allow_stale_does_not_allow_missing_trade_symbols(scan_inputs, failure):
    scan_inputs.failures["688072"] = failure
    sys.argv.append("--allow-stale")
    assert dss.main() == 1
    assert not (scan_inputs.output / "signals_2026-09-12.json").exists()


def test_daily_decision_keeps_requested_boundary(scan_inputs, monkeypatch):
    boundaries = []
    original = dss.ra.RegimeAdaptiveBacktestEngine.decide_current

    def observe(self, symbols, **kwargs):
        boundaries.append(kwargs["as_of"])
        return original(self, symbols, **kwargs)

    monkeypatch.setattr(dss.ra.RegimeAdaptiveBacktestEngine, "decide_current", observe)
    assert dss.main() == 0
    assert boundaries and set(boundaries) == {"2026-09-12"}


def test_daily_rejects_old_unmarked_data_instead_of_backdating(scan_inputs, monkeypatch):
    original = dss.qf.DataFetcher.load_stock_data

    def old_data(code, start, end, **kwargs):
        frame = original(code, start, end, **kwargs)
        if kwargs.get("data_dir") is None:
            return frame.loc[:"2026-08-28"].copy()
        return frame

    monkeypatch.setattr(dss.qf.DataFetcher, "load_stock_data", old_data)
    assert dss.main() == 1
    assert not (scan_inputs.output / "signals_2026-09-12.json").exists()
