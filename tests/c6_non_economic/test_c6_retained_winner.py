"""Synthetic contracts for C6 retained defensive-sell winners."""
from __future__ import annotations
# ruff: noqa: E501
from unittest import mock
import pandas as pd
import pytest
from quantfusion.config.engine import default_engine_config
from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.domain.models import Position, Signal
from quantfusion.engine.ensemble import PreparedSleeveRun
from quantfusion.engine.universe import BacktestEngine, SleeveBacktestEngine
class _Strategy:
    def __init__(self, name: str) -> None:
        self.name = name
class _Sleeve:
    def __init__(self, name: str, *, carry_overlay_sell: bool = False) -> None:
        self.sleeve_name = name
        self.carry_overlay_sell = carry_overlay_sell
        self.positions: dict[str, dict[str, object]] = {"AAA": {"fast": object()}}
        self.order_events: list[dict[str, object]] = []
        self.calls: list[str] = []
        self.executed_buys: list[Signal] = []
    def _start_trading_day(self) -> None:
        self.calls.append("start")
    def _execute_pending_signals(
        self,
        pending: list[tuple[Signal, object | None]],
        _data_map: dict,
        _date: pd.Timestamp,
        _date_to_pos: dict,
        directions: frozenset[str],
        *,
        buy_scores: dict[str, float] | None = None,
    ) -> list[tuple[Signal, object | None]]:
        del buy_scores
        side = next(iter(directions))
        self.calls.append(side)
        retained: list[tuple[Signal, object | None]] = []
        for signal, strategy in pending:
            if signal.direction not in directions:
                retained.append((signal, strategy))
            elif side == "sell" and strategy is None and self.carry_overlay_sell:
                retained.append((signal, strategy))
            elif side == "buy":
                self.executed_buys.append(signal)
        return retained
    def _record_order_event(self, **event: object) -> None:
        self.order_events.append(event)
def _signal(
    direction: str,
    *,
    symbol: str = "AAA",
    strategy_name: str = "fast",
    reason: str = "catastrophe_stop",
) -> Signal:
    return Signal(
        symbol=symbol,
        strategy_name=strategy_name,
        direction=direction,
        target_shares=100,
        price=10.0,
        reason=reason,
        signal_date="2026-01-05",
    )
def _state(
    sleeve: _Sleeve, pending: list[tuple[Signal, object | None]]
) -> PreparedSleeveRun:
    date = pd.Timestamp("2026-01-06")
    return PreparedSleeveRun(
        sleeve=sleeve,  # type: ignore[arg-type]
        data_map={},
        indicator_map={},
        all_dates=[date],
        date_to_pos={date: 0},
        pending=pending,  # type: ignore[arg-type]
    )
def _run(
    states: list[PreparedSleeveRun], *, intervention_id: str | None = None
) -> list[str]:
    coordinator = BacktestEngine()
    if intervention_id is not None:
        coordinator._c6_diagnostic_request = {"intervention_id": intervention_id}
    calls: list[str] = []
    with (
        mock.patch.object(
            coordinator,
            "_rebalance_free_sleeve_cash",
            side_effect=lambda *_: calls.append("rebalance"),
        ),
        mock.patch.object(
            coordinator,
            "_authorize_portfolio_buys",
            side_effect=lambda *_: calls.append("authorize"),
        ),
    ):
        coordinator._execute_ensemble_open(states, pd.Timestamp("2026-01-06"))
    return calls
@pytest.mark.parametrize(
    "case,carry",
    [
        ("limit-blocked", True),
        ("suspended", True),
        ("missing-open", True),
        ("adv-zero", True),
        ("partial-fill", True),
        ("partial-sublot", False),
        ("odd-lot-full-liquidation", False),
    ],
)
def test_retained_winner_vetoes_same_batch_buy(case: str, carry: bool) -> None:
    """The pre-sell winner owns the identical book through the buy phase."""
    sleeve = _Sleeve(case, carry_overlay_sell=carry)
    state = _state(
        sleeve,
        [(_signal("sell"), None), (_signal("buy"), _Strategy("fast"))],
    )
    _run([state])
    assert sleeve.executed_buys == []
    assert [item[0].direction for item in state.pending] == (["sell"] if carry else [])
    assert any(
        event.get("event") == "blocked_retained_defensive_sell"
        and event.get("state_index") == 0
        and event.get("sleeve_name") == case
        for event in sleeve.order_events
    )
def test_veto_is_exact_book_identity_and_sell_precedes_authorization() -> None:
    first = _Sleeve("first", carry_overlay_sell=True)
    second = _Sleeve("second")
    first_state = _state(
        first,
        [
            (_signal("sell"), None),
            (_signal("buy"), _Strategy("fast")),
            (_signal("buy", strategy_name="slow"), _Strategy("slow")),
            (_signal("buy", symbol="BBB"), _Strategy("fast")),
        ],
    )
    second_state = _state(
        second,
        [(_signal("buy"), _Strategy("fast"))],
    )
    coordinator_calls = _run([first_state, second_state])
    assert [(signal.symbol, signal.strategy_name) for signal in first.executed_buys] == [
        ("AAA", "slow"),
        ("BBB", "fast"),
    ]
    assert [(signal.symbol, signal.strategy_name) for signal in second.executed_buys] == [
        ("AAA", "fast")
    ]
    assert first.calls == ["start", "sell", "buy"]
    assert second.calls == ["start", "sell", "buy"]
    assert coordinator_calls == ["rebalance", "authorize"]
def test_ordinary_full_sell_cannot_revive_buy_after_overlay_zero_fill() -> None:
    sleeve = _Sleeve("ordinary-first")
    strategy = _Strategy("fast")
    state = _state(
        sleeve,
        [
            (_signal("sell", reason="ordinary_exit"), strategy),
            (_signal("sell"), None),
            (_signal("buy"), strategy),
        ],
    )
    _run([state])
    assert sleeve.executed_buys == []
def test_veto_is_released_for_the_next_execution_batch() -> None:
    sleeve = _Sleeve("batch-local")
    strategy = _Strategy("fast")
    state = _state(
        sleeve,
        [(_signal("sell"), None), (_signal("buy"), strategy)],
    )
    _run([state])
    assert sleeve.executed_buys == []
    state.pending.append((_signal("buy"), strategy))
    _run([state])
    assert [(signal.symbol, signal.strategy_name) for signal in sleeve.executed_buys] == [
        ("AAA", "fast")
    ]
def test_f1_off_reproduces_legacy_same_batch_buy_authorization() -> None:
    sleeve = _Sleeve("baseline", carry_overlay_sell=True)
    strategy = _Strategy("fast")
    state = _state(
        sleeve,
        [(_signal("sell"), None), (_signal("buy"), strategy)],
    )
    _run([state], intervention_id="BASELINE")
    assert [(signal.symbol, signal.strategy_name) for signal in sleeve.executed_buys] == [
        ("AAA", "fast")
    ]
def _real_sleeve_with_position(shares: int, prior_volume: float) -> tuple[
    SleeveBacktestEngine, dict[str, pd.DataFrame], pd.DatetimeIndex
]:
    policy = PortfolioPolicy(allocation_mode="single")
    sleeve = SleeveBacktestEngine(
        100_000.0,
        cfg=default_engine_config(),
        policy=policy,
        allocation_lookbacks=policy.single_lookbacks,
        sleeve_name="real",
    )
    position = Position("300308", "fast", shares, 10.0, "2026-01-05")
    sleeve.positions = {"300308": {"fast": position}}
    dates = pd.bdate_range("2026-01-05", periods=2)
    frame = pd.DataFrame(
        {
            "open": [10.0, 10.0],
            "close": [10.0, 10.0],
            "high": [10.0, 10.0],
            "low": [10.0, 10.0],
            "volume": [prior_volume, prior_volume],
        },
        index=dates,
    )
    return sleeve, {"300308": frame}, dates
def test_partial_sublot_remainder_is_released_in_real_execution() -> None:
    sleeve, data_map, dates = _real_sleeve_with_position(250, 1_000_000.0)
    pending = [
        (
            _signal(
                "sell", symbol="300308", reason="sector_risk_trim"
            ),
            None,
        )
    ]
    pending[0] = (
        Signal(**{**vars(pending[0][0]), "target_shares": 50}),
        None,
    )
    remaining = sleeve._execute_pending_signals(
        pending,
        data_map,
        dates[-1],
        {date: index for index, date in enumerate(dates)},
        frozenset({"sell"}),
    )
    assert remaining == []
    assert sleeve.positions["300308"]["fast"].shares == 250
def test_adv_zero_keeps_odd_lot_full_liquidation_executable() -> None:
    sleeve, data_map, dates = _real_sleeve_with_position(50, 0.0)
    sell = Signal(
        **{
            **vars(_signal("sell", symbol="300308")),
            "target_shares": 50,
        }
    )
    remaining = sleeve._execute_pending_signals(
        [(sell, None)],
        data_map,
        dates[-1],
        {date: index for index, date in enumerate(dates)},
        frozenset({"sell"}),
    )
    assert remaining == [(sell, None)]
    assert sleeve.positions["300308"]["fast"].shares == 50
