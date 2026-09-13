"""Stress preflight must include signal-only references before any worker starts."""

import sys

import pytest

from quantfusion.application import stress


def test_missing_out_of_pool_reference_stops_before_provenance(tmp_path, monkeypatch):
    market = tmp_path / "market"
    regime = tmp_path / "regime"
    market.mkdir()
    regime.mkdir()
    for code in stress.stress_scenarios.ORDERED_CODES:
        (market / f"{code}.csv").write_text("date,close\n2026-07-20,1\n")
    for code in stress.ra.REGIME_INDEX_FILES.values():
        (regime / f"{code}.csv").write_text("date,close\n2026-07-20,1\n")
    assert "688008" not in stress.stress_scenarios.ORDERED_CODES

    def unexpected_work(*args, **kwargs):
        pytest.fail("incomplete input reached provenance or economic scheduling")

    monkeypatch.setattr(stress.stress_artifacts, "_build_provenance", unexpected_work)
    monkeypatch.setattr(stress, "ProcessPoolExecutor", unexpected_work)
    checkpoint = tmp_path / "checkpoint.json"
    monkeypatch.setattr(sys, "argv", [
        "stress", "--data-dir", str(market), "--regime-data-dir", str(regime),
        "--checkpoint", str(checkpoint), "--source-revision", "0" * 40,
    ])
    with pytest.raises(ValueError, match="688008"):
        stress.main()
    assert not checkpoint.exists()
