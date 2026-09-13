"""Unknown indicator observations remain explicit without masking bad money."""

from copy import deepcopy
import math

import pytest

from scripts.decision_diagnostics import replay_evidence


def test_missing_indicator_has_no_fabricated_native_fingerprint():
    raw = {"regime_state_series": [{"date": "2024-02-01", "hurst": float("nan"),
                                    "vol_percentile": 0.5, "route": "unknown"}]}
    evidence = replay_evidence(raw)
    assert math.isnan(raw["regime_state_series"][0]["hurst"])
    assert evidence["records"]["regime_state_series"][0]["hurst"] is None
    assert evidence["regime_observation_gaps"] == [
        {"index": 0, "date": "2024-02-01", "field": "hurst", "original": "NaN"}]
    assert evidence["fingerprints"]["regime_state_series_sha256"] is None
    assert evidence["fingerprints"]["regime_state_count"] == 1
    assert evidence["fingerprint_status"] == "PARTIAL_MISSING_REGIME_OBSERVATIONS"


def test_finite_replay_retains_native_fingerprints():
    from quantfusion.research.fingerprints import economic_sequence_fingerprints

    raw = {"regime_state_series": [{"date": "2024-02-01", "hurst": 0.6}]}
    before = deepcopy(raw)
    evidence = replay_evidence(raw)
    assert evidence["fingerprints"] == economic_sequence_fingerprints(raw)
    assert evidence["regime_observation_gaps"] == []
    assert evidence["fingerprint_status"] == "COMPLETE"
    assert raw == before


@pytest.mark.parametrize("payload", [
    {"risk_events": [{"date": "2024-02-01", "gross_cap": float("nan")}]},
    {"trades": [{"net_cash_flow": float("nan")}]},
    {"regime_state_series": [{"date": "2024-02-01", "hurst": float("inf")}]},
    {"regime_state_series": [{"date": "2024-02-01", "unexpected": float("nan")}]},
])
def test_nonfinite_economic_or_unrecognized_values_still_fail(payload):
    with pytest.raises(ValueError):
        replay_evidence(payload)
