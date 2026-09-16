from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from quantfusion.data.feature_contract import validate_causal_feature_frame
from quantfusion.domain.health import HealthState
from quantfusion.domain.models import Signal
from quantfusion.engine.route_components import CashAllocator, ExecutionGuard
from quantfusion.engine.signals import CoreSignalMixin
from quantfusion.strategy.weak import PositiveMomentumHoldStrategy


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


def _sell(symbol: str, strategy_name: str) -> Signal:
    return Signal(
        symbol,
        strategy_name,
        "sell",
        100,
        10.0,
        reason="route liquidation",
        signal_date="2026-09-16",
    )


def _buy(symbol: str, strategy_name: str) -> Signal:
    return Signal(
        symbol,
        strategy_name,
        "buy",
        100,
        10.0,
        reason="existing buy",
        signal_date="2026-09-16",
    )


class _LiquidationSleeve:
    _dedupe_pending_signals = staticmethod(CoreSignalMixin._dedupe_pending_signals)

    def __init__(self) -> None:
        self.positions = {
            "trend": {"dual_ma": SimpleNamespace(shares=100)},
            "weak": {
                PositiveMomentumHoldStrategy.name: SimpleNamespace(shares=100)
            },
        }
        self._generated = [
            (_sell("trend", "dual_ma"), SimpleNamespace(name="dual_ma")),
            (
                _sell("weak", PositiveMomentumHoldStrategy.name),
                SimpleNamespace(name=PositiveMomentumHoldStrategy.name),
            ),
        ]

    def _generate_liquidation_signals(self, date_str: str, *, reason: str):
        assert date_str == "2026-09-16"
        assert reason == "production outer-route migration"
        return list(self._generated)


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


def test_route_cash_migration_preserves_total_cash_and_rebases_external_flows() -> None:
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


def test_route_liquidation_keeps_sell_queue_unique_and_positions_owned() -> None:
    sleeve = _LiquidationSleeve()
    positions = sleeve.positions
    existing_trend_sell = (_sell("trend", "dual_ma"), SimpleNamespace(name="dual_ma"))
    existing_buy = (_buy("candidate", "dual_ma"), SimpleNamespace(name="dual_ma"))
    state = SimpleNamespace(
        sleeve=sleeve,
        pending=[existing_trend_sell, existing_buy],
    )

    ExecutionGuard.queue_liquidations(
        [state], "2026-09-16", weak_only=False
    )
    ExecutionGuard.queue_liquidations(
        [state], "2026-09-16", weak_only=True
    )

    keys = [
        (signal.symbol, signal.strategy_name, signal.direction)
        for signal, _strategy in state.pending
    ]
    assert keys == [
        ("trend", "dual_ma", "sell"),
        ("weak", PositiveMomentumHoldStrategy.name, "sell"),
    ]
    assert len(keys) == len(set(keys))
    assert sleeve.positions is positions
    assert sleeve.positions["trend"]["dual_ma"].shares == 100
    assert sleeve.positions["weak"][PositiveMomentumHoldStrategy.name].shares == 100
