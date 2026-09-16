from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from quantfusion.engine.strategy_lifecycle import StrategyLifecycleRegistry
from quantfusion.regime import evidence


def _regime_frame(*, include_close: bool = True) -> pd.DataFrame:
    dates = pd.bdate_range(end="2026-01-30", periods=80)
    frame = pd.DataFrame(index=dates)
    if include_close:
        frame["close"] = [100.0 + index for index in range(len(dates))]
    return frame


def test_regime_health_reports_valid_inputs() -> None:
    with patch.object(evidence, "_local_frame", return_value=_regime_frame()):
        result = evidence.detect_regime("unused", as_of="2026-01-30")

    assert result.status == "valid"
    assert asdict(result)["health"]["status"] == "valid"
    assert result.regime == "trending"


def test_regime_health_distinguishes_unavailable_input() -> None:
    with patch.object(
        evidence,
        "_local_frame",
        side_effect=FileNotFoundError("missing regime input"),
    ):
        result = evidence.detect_regime("unused", as_of="2026-01-30")

    assert result.regime == "unknown"
    assert result.status == "unavailable"
    assert result.health.issues


def test_regime_health_distinguishes_invalid_input() -> None:
    with patch.object(
        evidence,
        "_local_frame",
        return_value=_regime_frame(include_close=False),
    ):
        result = evidence.detect_regime("unused", as_of="2026-01-30")

    assert result.regime == "unknown"
    assert result.status == "invalid"
    assert all(issue.source.startswith("regime_index:") for issue in result.health.issues)


def test_strategy_registry_stays_bounded_across_long_running_rotation() -> None:
    registry = StrategyLifecycleRegistry()
    sleeve = SimpleNamespace(sleeve_name="fast", external_strategy_instances={})
    universe = tuple(f"{index:06d}" for index in range(17))

    for day in range(1_000):
        symbol = universe[day % len(universe)]
        entry = registry.acquire(
            "fast", symbol, lambda: SimpleNamespace(position=None)
        )
        registry.activate(sleeve, entry)
        registry.reconcile(
            sleeve,
            current_symbols=universe,
            active_symbols=(symbol,),
            pending=(),
            date=f"day-{day}",
        )
        assert sum(len(items) for items in sleeve.external_strategy_instances.values()) == 1

    assert len(registry.entries()) == len(universe)
    active = [entry for entry in registry.entries() if entry.state.value == "active"]
    assert len(active) == 1

    remaining = universe[-3:]
    registry.reconcile(
        sleeve,
        current_symbols=remaining,
        active_symbols=(),
        pending=(),
        date="universe-change",
    )
    snapshot = registry.snapshot()
    assert len(snapshot["entries"]) == 3
    assert len(snapshot["cleanup_events"]) == 14
    assert not sleeve.external_strategy_instances
