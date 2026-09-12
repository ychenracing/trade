"""Current decisions must honor the existing fixed-index evidence gate."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from quantfusion.config.regime import (
    MAX_EVIDENCE_STALENESS_DAYS,
    REGIME_INDEX_FILES,
)
from quantfusion.engine import replay
from quantfusion.regime import evidence, state_machine
from quantfusion.regime.models import LeaderSelection, RegimeEvidence, RegimeRoute


@pytest.mark.parametrize("route", list(RegimeRoute))
def test_unknown_evidence_cannot_reuse_any_non_cash_route(route: RegimeRoute) -> None:
    as_of = "2026-09-08"
    unknown = RegimeEvidence(as_of=as_of, regime="unknown", observations=())
    leaders = LeaderSelection(as_of, ("300308",), 1, ("300308",), (0.1,))
    with (
        patch.object(replay, "boundary_route", return_value=route),
        patch.object(replay, "detect_regime", return_value=unknown),
        patch.object(replay, "select_positive_momentum_leaders", return_value=leaders),
    ):
        decision = replay.RegimeAdaptiveBacktestEngine().decide_current(
            {"300308": "example"}, as_of=as_of, data_dir="unused"
        )
    assert decision.name == "cash_preservation"
    assert decision.boundary == as_of
    assert decision.regime == unknown
    assert decision.leaders is None


@pytest.mark.parametrize("regime_name", ["trending", "choppy"])
@pytest.mark.parametrize("route", list(RegimeRoute))
def test_known_evidence_preserves_existing_route_selection(
    route: RegimeRoute, regime_name: str
) -> None:
    as_of = "2026-09-08"
    known = RegimeEvidence(as_of=as_of, regime=regime_name, observations=())
    leaders = LeaderSelection(as_of, ("300308",), 1, ("300308",), (0.1,))
    expected = {
        RegimeRoute.CASH: "cash_preservation",
        RegimeRoute.TREND: "frozen_trend_engine",
        RegimeRoute.TRANSITION_TO_TREND: "frozen_trend_engine",
        RegimeRoute.WEAK: "positive_momentum_hold",
        RegimeRoute.TRANSITION_TO_WEAK: "positive_momentum_hold",
    }[route]
    with (
        patch.object(replay, "boundary_route", return_value=route),
        patch.object(replay, "detect_regime", return_value=known),
        patch.object(replay, "select_positive_momentum_leaders", return_value=leaders),
    ):
        decision = replay.RegimeAdaptiveBacktestEngine().decide_current(
            {"300308": "example"}, as_of=as_of, data_dir="unused"
        )
    assert decision.name == expected
    assert decision.regime == known
    assert decision.leaders == (leaders if expected == "positive_momentum_hold" else None)


@pytest.mark.parametrize(
    "age_days",
    [0, 2, MAX_EVIDENCE_STALENESS_DAYS, MAX_EVIDENCE_STALENESS_DAYS + 1],
)
def test_real_evidence_gate_rejects_expired_indices_without_changing_tolerance(
    age_days: int,
) -> None:
    # Friday observations remain valid on Sunday; the frozen natural-day
    # tolerance is preserved, including its inclusive upper boundary.
    dates = pd.bdate_range(end="2026-08-28", periods=240)
    frame = pd.DataFrame({"close": range(100, 340)}, index=dates)
    as_of = str((dates[-1] + pd.Timedelta(days=age_days)).date())

    def load_frame(data_dir: str | Path, code: str, end_date: str) -> pd.DataFrame:
        assert code in REGIME_INDEX_FILES.values()
        return frame.loc[frame.index <= pd.Timestamp(end_date)].copy()

    with (
        patch.object(evidence, "_local_frame", side_effect=load_frame),
        patch.object(state_machine, "_local_frame", side_effect=load_frame),
    ):
        # This is the original defect: a historical TREND still exists even
        # after both series have exceeded the current evidence tolerance.
        assert state_machine.boundary_route("unused", as_of=as_of) is RegimeRoute.TREND
        decision = replay.RegimeAdaptiveBacktestEngine().decide_current(
            {"300308": "example"}, as_of=as_of, data_dir="unused"
        )
    expired = age_days > MAX_EVIDENCE_STALENESS_DAYS
    assert decision.regime.regime == ("unknown" if expired else "trending")
    assert decision.name == ("cash_preservation" if expired else "frozen_trend_engine")
    assert decision.leaders is None


@pytest.mark.parametrize("technology_input", ["missing", "short", "older"])
def test_incomplete_or_misaligned_index_pair_stays_defensive(
    technology_input: str,
) -> None:
    dates = pd.bdate_range(end="2026-08-28", periods=240)
    frame = pd.DataFrame({"close": range(100, 340)}, index=dates)

    def load_frame(data_dir: str | Path, code: str, end_date: str) -> pd.DataFrame:
        current = frame.loc[frame.index <= pd.Timestamp(end_date)].copy()
        if code == REGIME_INDEX_FILES["technology"]:
            if technology_input == "missing":
                raise OSError("index refresh failed; no usable file")
            if technology_input == "short":
                return current.tail(59)
            return current.iloc[:-1]
        return current

    with (
        patch.object(evidence, "_local_frame", side_effect=load_frame),
        patch.object(state_machine, "_local_frame", side_effect=load_frame),
    ):
        decision = replay.RegimeAdaptiveBacktestEngine().decide_current(
            {"300308": "example"}, as_of="2026-08-28", data_dir="unused"
        )
    assert decision.name == "cash_preservation"
    assert decision.leaders is None
