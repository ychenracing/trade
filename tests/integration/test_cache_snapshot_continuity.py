"""Cache continuity and immutable snapshot reuse must survive failure paths."""

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from quantfusion.config import paths
from quantfusion.data import contracts, snapshot
from quantfusion.data.providers import DataFetcher
from .test_input_evidence_boundaries import _frame, _snapshot_inputs


def test_existing_cache_survives_interrupted_write(tmp_path, monkeypatch):
    path = tmp_path / "300308.csv"
    path.write_bytes(b"previous cache bytes\n")
    monkeypatch.setattr(DataFetcher, "fetch_stock_data", lambda *args: _frame())

    def fail(frame, destination, *args, **kwargs):
        if hasattr(destination, "write"):
            destination.write("partial")
        else:
            Path(destination).write_text("partial")
        raise OSError("simulated disk failure")

    monkeypatch.setattr(pd.DataFrame, "to_csv", fail)
    with pytest.raises(OSError):
        DataFetcher.load_stock_data("300308", "2026-01-01", "2026-09-11",
                                    cache_dir=str(tmp_path))
    assert path.read_bytes() == b"previous cache bytes\n"


def test_stock_cache_cannot_overwrite_retained_market_data(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MARKET_DATA_DIR", tmp_path)
    path = tmp_path / "300308.csv"
    path.write_bytes(b"retained evidence\n")
    monkeypatch.setattr(DataFetcher, "fetch_stock_data", lambda *args: _frame())
    with pytest.raises(ValueError, match="frozen"):
        DataFetcher.load_stock_data("300308", "2026-01-01", "2026-09-11",
                                    cache_dir=str(tmp_path))
    assert path.read_bytes() == b"retained evidence\n"


def test_same_day_reuse_rejects_different_date_coverage(tmp_path):
    kwargs = _snapshot_inputs(tmp_path)
    snapshot.materialize_frozen_snapshot(**kwargs)
    root = kwargs["snapshot_dir"]
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    kwargs["frames"]["300308"] = kwargs["frames"]["300308"].iloc[:-1]
    with pytest.raises(ValueError, match="coverage"):
        snapshot.materialize_frozen_snapshot(**kwargs)
    assert all(p.read_bytes() == content for p, content in before.items())


def test_queried_start_prevents_repeated_prelisting_refetch(tmp_path, monkeypatch):
    calls = []

    def fetch(code, start, end):
        calls.append((code, start, end))
        return _frame("2026-08-03", end)

    monkeypatch.setattr(DataFetcher, "fetch_stock_data", fetch)
    for _ in range(2):
        value = DataFetcher.load_stock_data("300308", "2026-01-01", "2026-09-11",
                                           cache_dir=str(tmp_path))
        assert value.index[0] == pd.Timestamp("2026-08-03")
    assert len(calls) == 1


def test_prefix_refill_preserves_later_cache_and_bounds_result(tmp_path, monkeypatch):
    path = tmp_path / "300308.csv"
    _frame("2026-08-03").to_csv(path)
    DataFetcher._write_cache_contract(path)
    monkeypatch.setattr(DataFetcher, "fetch_stock_data",
                        lambda code, start, end: _frame(start, end))
    value = DataFetcher.load_stock_data("300308", "2026-01-01", "2026-09-02",
                                       cache_dir=str(tmp_path))
    assert value.index[-1] == pd.Timestamp("2026-09-02")
    assert pd.read_csv(path)["date"].iloc[-1] == "2026-09-11"


def test_failed_weekday_refresh_is_still_stale(tmp_path, monkeypatch):
    _frame(end="2026-09-10").to_csv(tmp_path / "300308.csv")
    DataFetcher._write_cache_contract(tmp_path / "300308.csv")

    def unavailable(*args):
        raise RuntimeError("network failed on Friday")

    monkeypatch.setattr(DataFetcher, "fetch_stock_data", unavailable)
    value = DataFetcher.load_stock_data("300308", "2026-01-01", "2026-09-12",
                                       cache_dir=str(tmp_path))
    assert value.attrs["_stale"] is True


def test_same_coverage_reuses_original_frozen_prices(tmp_path):
    kwargs = _snapshot_inputs(tmp_path)
    manifest = snapshot.materialize_frozen_snapshot(**kwargs)
    root = kwargs["snapshot_dir"]
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    kwargs["frames"]["300308"] *= 2
    assert snapshot.materialize_frozen_snapshot(**kwargs) == manifest
    assert all(p.read_bytes() == content for p, content in before.items())


def test_runtime_index_refresh_still_updates(tmp_path, monkeypatch):
    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    monkeypatch.setattr(contracts, "ak", SimpleNamespace(
        stock_zh_index_daily_em=lambda **kwargs: _frame(end=end).reset_index()))
    result = contracts.refresh_regime_indices(tmp_path, end_date=end)
    assert all(item["status"] == "updated" for item in result["indices"].values())
    assert (tmp_path / "live_refresh_manifest.json").is_file()


def test_published_snapshots_are_read_only_for_cache_and_indices(tmp_path):
    kwargs = _snapshot_inputs(tmp_path)
    snapshot.materialize_frozen_snapshot(**kwargs)
    root = kwargs["snapshot_dir"]
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="frozen"):
        DataFetcher.load_stock_data("300308", "2026-01-01", "2026-09-12",
                                    cache_dir=str(root / "market_data"))
    result = contracts.refresh_regime_indices(
        root / "regime_data", end_date=pd.Timestamp.today().strftime("%Y-%m-%d"))
    assert all(item["status"] == "frozen_read_only" for item in result["indices"].values())
    assert all(p.read_bytes() == content for p, content in before.items())


@pytest.mark.parametrize("unit", ["us", "ms", "s"])
def test_same_dates_at_different_precision_are_reusable(tmp_path, unit):
    kwargs = _snapshot_inputs(tmp_path)
    manifest = snapshot.materialize_frozen_snapshot(**kwargs)
    frame = kwargs["frames"]["300308"]
    frame.index = frame.index.as_unit(unit)
    assert snapshot.materialize_frozen_snapshot(**kwargs) == manifest
