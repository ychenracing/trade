from types import SimpleNamespace

import pandas as pd
import pytest

from quantfusion.engine.replay import ProductionRouteController
from quantfusion.engine.universe import BacktestEngine
from quantfusion.regime.evidence import select_positive_momentum_leaders
from quantfusion.regime.models import LeaderSelection


def _leader_frame(*, rising: bool) -> pd.DataFrame:
    dates = pd.bdate_range(end="2026-01-30", periods=260)
    if rising:
        close = pd.Series(
            [100.0 + index * 0.2 for index in range(len(dates))], index=dates
        )
    else:
        close = pd.Series(
            [160.0 - index * 0.2 for index in range(len(dates))], index=dates
        )
    return pd.DataFrame(
        {
            "close": close,
            "volume": 10_000_000.0,
        },
        index=dates,
    )


def test_leader_selection_distinguishes_valid_empty_result() -> None:
    def load(code: str, boundary: str) -> pd.DataFrame:
        del boundary
        return _leader_frame(rising=code != "600000")

    selection = select_positive_momentum_leaders(
        ("600000",),
        data_dir="unused",
        as_of="2026-01-30",
        frame_loader=load,
    )

    assert selection.selected_symbols == ()
    assert getattr(selection, "status", None) == "valid"


def test_leader_selection_distinguishes_unavailable_data() -> None:
    def load(code: str, boundary: str) -> pd.DataFrame:
        del boundary
        if code == "600000":
            raise FileNotFoundError("missing requested symbol")
        return _leader_frame(rising=True)

    selection = select_positive_momentum_leaders(
        ("600000",),
        data_dir="unused",
        as_of="2026-01-30",
        frame_loader=load,
    )

    assert selection.selected_symbols == ()
    assert selection.unavailable_symbols == ("600000",)
    assert getattr(selection, "status", None) == "unavailable"


def test_leader_selection_distinguishes_invalid_data() -> None:
    def load(code: str, boundary: str) -> pd.DataFrame:
        del boundary
        if code == "600000":
            raise ValueError("malformed requested symbol")
        return _leader_frame(rising=True)

    selection = select_positive_momentum_leaders(
        ("600000",),
        data_dir="unused",
        as_of="2026-01-30",
        frame_loader=load,
    )

    assert selection.selected_symbols == ()
    assert getattr(selection, "status", None) == "invalid"


def test_production_route_rejects_unavailable_leader_evidence(tmp_path) -> None:
    controller = ProductionRouteController([], leader_data_dir=tmp_path)

    with pytest.raises(RuntimeError, match="leader"):
        controller._leaders(("600000",), "2026-01-30")


def test_production_route_keeps_valid_leader_behavior(monkeypatch, tmp_path) -> None:
    selection = LeaderSelection(
        as_of="2026-01-30",
        requested_symbols=("600000",),
        observed_symbols=1,
        selected_symbols=("600000",),
        selected_returns=(0.7,),
    )
    monkeypatch.setattr(
        "quantfusion.engine.replay.select_positive_momentum_leaders",
        lambda *args, **kwargs: selection,
    )
    controller = ProductionRouteController([], leader_data_dir=tmp_path)

    assert controller._leaders(("600000",), "2026-01-30") == ("600000",)
    assert controller._leaders(("600000",), "2026-01-30") == ("600000",)


def test_overlay_allocation_failure_retains_fallback_but_is_observable() -> None:
    def fail_scores(data_map, date):
        del data_map, date
        raise ValueError("invalid allocation evidence")

    sleeve = SimpleNamespace(
        sleeve_name="fast",
        positions={"held": {"dual_ma": SimpleNamespace(shares=100)}},
        _allocation_scores=fail_scores,
    )
    state = SimpleNamespace(sleeve=sleeve, data_map={"held": object()})

    score = BacktestEngine._overlay_allocation_score(
        [state], pd.Timestamp("2026-01-05")
    )

    assert score("held") == 0.0
    assert getattr(score, "status", None) == "degraded"
    event = score.as_event("2026-01-05")
    assert event["event"] == "allocation_score_degraded"
    assert event["failed_sleeves"] == ["fast"]
    assert "invalid allocation evidence" in event["failures"][0]["message"]


def test_route_controller_owns_weak_strategy_lifecycle(tmp_path) -> None:
    controller = ProductionRouteController([], leader_data_dir=tmp_path)

    assert getattr(controller, "_weak_strategy_registry", None) is not None
    snapshot = controller.result_snapshot()
    assert snapshot["weak_strategy_lifecycle"]["entries"] == []
    assert snapshot["weak_strategy_lifecycle"]["cleanup_events"] == []
