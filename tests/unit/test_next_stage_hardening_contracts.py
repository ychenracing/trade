from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from quantfusion.data.feature_contract import validate_causal_feature_frame
from quantfusion.domain.health import HealthState
from quantfusion.engine.route_components import CashAllocator


class _RiskRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[float, float]] = []

    def rebase_after_cash_flow(self, assets_before: float, flow: float) -> None:
        self.calls.append((assets_before, flow))


def _state(cash: float) -> SimpleNamespace:
    sleeve = SimpleNamespace(
        cash=cash,
        equity_curve=[
            {
                "date": "2026-09-15",
                "assets": cash,
                "cash": cash,
                "position_value": 0.0,
            }
        ],
        risk=_RiskRecorder(),
    )
    return SimpleNamespace(sleeve=sleeve)


def test_causal_feature_contract_declares_required_metadata_and_ready_status() -> None:
    frame = pd.DataFrame(
        {"close": [10.0, 10.5, 11.0], "volume": [100.0, 110.0, 120.0]},
        index=pd.to_datetime(["2026-09-11", "2026-09-14", "2026-09-15"]),
    )

    contract = validate_causal_feature_frame(
        feature_name="leader_quality",
        as_of_date="2026-09-15",
        required_history=3,
        source_columns=("close", "volume"),
        frame=frame,
    )

    assert contract.feature_name == "leader_quality"
    assert contract.as_of_date == "2026-09-15"
    assert contract.required_history == 3
    assert contract.source_columns == ("close", "volume")
    assert contract.validation_status is HealthState.READY


def test_causal_feature_contract_rejects_future_observations() -> None:
    frame = pd.DataFrame(
        {"close": [10.0, 10.5], "volume": [100.0, 110.0]},
        index=pd.to_datetime(["2026-09-15", "2026-09-16"]),
    )

    contract = validate_causal_feature_frame(
        feature_name="leader_quality",
        as_of_date="2026-09-15",
        required_history=1,
        source_columns=("close", "volume"),
        frame=frame,
    )

    assert contract.validation_status is HealthState.INVALID


def test_causal_feature_contract_degrades_when_history_is_insufficient() -> None:
    frame = pd.DataFrame(
        {"close": [10.0, 10.5], "volume": [100.0, 110.0]},
        index=pd.to_datetime(["2026-09-14", "2026-09-15"]),
    )

    contract = validate_causal_feature_frame(
        feature_name="leader_quality",
        as_of_date="2026-09-15",
        required_history=3,
        source_columns=("close", "volume"),
        frame=frame,
    )

    assert contract.validation_status is HealthState.DEGRADED


def test_cash_allocator_preserves_total_cash_and_rebases_external_flows() -> None:
    states = [_state(1_500_000.0), _state(750_000.0), _state(750_000.0)]
    before_total = sum(float(state.sleeve.cash) for state in states)

    CashAllocator.shift_free_cash(states, (1.0, 0.0, 0.0))

    assert sum(float(state.sleeve.cash) for state in states) == before_total
    assert [state.sleeve.cash for state in states] == [3_000_000.0, 0.0, 0.0]
    assert [state.sleeve.risk.calls for state in states] == [
        [(1_500_000.0, 1_500_000.0)],
        [(750_000.0, -750_000.0)],
        [(750_000.0, -750_000.0)],
    ]
    assert sum(
        float(state.sleeve.equity_curve[-1]["assets"]) for state in states
    ) == before_total
