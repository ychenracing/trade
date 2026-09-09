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


def test_action_receipts_preserve_each_actual_batch_and_exact_book() -> None:
    """Later fills must not rewrite earlier partial/blocked action receipts."""
    from copy import deepcopy
    from quantfusion.application.c6_diagnostics import _action_records
    from quantfusion.risk.overlay.adapter import apply_risk_actions
    from quantfusion.risk.overlay.models import RiskAction

    sleeve, data_map, dates = _real_sleeve_with_position(500, 40_000.0)
    sleeve.positions["300308"]["other"] = Position("300308", "other", 900, 10, "2026-01-05")
    sleeve._c6_action_lifecycle = []
    sleeve._c6_action_by_signal = {}
    state = _state(sleeve, [])
    apply_risk_actions([RiskAction("300308", "fast", 500, 10, "2026-01-05", "sector_risk_trim", 40)], [state], date_str="2026-01-05")
    assert sleeve._c6_action_lifecycle[0]["current_shares"] == 500
    sleeve._start_trading_day()
    state.pending = sleeve._execute_pending_signals(state.pending, data_map, dates[1], {d: i for i, d in enumerate(dates)})
    first = deepcopy(_action_records({"_c6_states": [state], "trades": sleeve.trades}))
    receipt = first[-1]
    assert 0 < receipt["filled_shares"] < 500
    assert receipt["carry_to_next_batch"] is True
    assert receipt["filled_notional"] == pytest.approx(sleeve.trades[-1].gross_value)
    assert receipt["planned_notional"] == 500 * 10
    assert receipt["execution_timestamp"] == "2026-01-06"
    assert state.pending[0][0].target_shares == receipt["remainder_shares"]
    next_date = dates[1] + pd.offsets.BDay(1)
    data_map["300308"].loc[next_date] = data_map["300308"].iloc[-1]
    sleeve._start_trading_day()
    state.pending = sleeve._execute_pending_signals(state.pending, data_map, next_date, {d: i for i, d in enumerate(data_map["300308"].index)})
    final = _action_records({"_c6_states": [state], "trades": sleeve.trades})
    assert final[:len(first)] == first
    assert final[-1]["execution_timestamp"] == "2026-01-07"
    assert final[-1]["action_id"] == receipt["action_id"]
    assert final[-1]["planned_shares"] == receipt["remainder_shares"]
    assert final[-1]["filled_shares"] == sum(t.shares for t in sleeve.trades if t.date == "2026-01-07")


def test_order_receipt_keeps_requested_authorized_fill_and_blocked_attempts() -> None:
    sleeve, data_map, dates = _real_sleeve_with_position(500, 40_000.0)
    sleeve._c6_orders = []
    sleeve._c6_fills = []
    sleeve._c6_order_by_signal = {}
    sleeve._c6_state_index = 0
    signal = Signal("300308", "fast", "sell", target_shares=500, price=10.0,
                    reason="sector_risk_trim", signal_date="2026-01-05")
    pending = sleeve._execute_pending_signals([(signal, None)], data_map, dates[1], {d: i for i, d in enumerate(dates)})
    assert len(sleeve._c6_orders) == 1
    first = dict(sleeve._c6_orders[0])
    assert first["requested_shares"] == 500
    assert first["authorized_shares"] == 200
    assert first["filled_shares"] == 200
    assert first["status"] == "partial"
    fill = sleeve._c6_fills[0]
    assert fill["order_ordinal"] == first["order_ordinal"]
    assert fill["slippage"] == pytest.approx(2.0)
    assert fill["fee"] == pytest.approx(sleeve.trades[0].commission + sleeve.trades[0].stamp_duty_cost)
    next_date = dates[1] + pd.offsets.BDay(1)
    pending = sleeve._execute_pending_signals(pending, data_map, next_date, {d: i for i, d in enumerate(dates)})
    assert sleeve._c6_orders[0] == first
    second = sleeve._c6_orders[1]
    assert second["status"] == "blocked"
    assert second["blocked_reason"] == "blocked_missing_open"
    assert second["requested_shares"] == 300
    assert second["authorized_shares"] == 0
    assert second["filled_shares"] == 0
    assert pending[0][0].target_shares == 300


def test_later_consolidation_does_not_rewrite_executed_receipts() -> None:
    from copy import deepcopy
    from quantfusion.risk.overlay.adapter import apply_risk_actions
    from quantfusion.risk.overlay.models import RiskAction
    sleeve, data_map, dates = _real_sleeve_with_position(500, 40_000.)
    sleeve._c6_action_lifecycle, sleeve._c6_orders, sleeve._c6_fills = [], [], []
    sleeve._c6_action_by_signal, sleeve._c6_order_by_signal = {}, {}
    sleeve._c6_state_index = 0
    state = _state(sleeve, [])
    apply_risk_actions([RiskAction("300308", "fast", 500, 10, "2026-01-05", "sector_risk_trim", 40)], [state], date_str="2026-01-05")
    state.pending = sleeve._execute_pending_signals(state.pending, data_map, dates[1], {d: i for i, d in enumerate(dates)})
    old_actions, old_orders = deepcopy(sleeve._c6_action_lifecycle), deepcopy(sleeve._c6_orders)
    apply_risk_actions([RiskAction("300308", "fast", 300, 10, "2026-01-06", "catastrophe_stop", 100)], [state], date_str="2026-01-06")
    assert sleeve._c6_action_lifecycle[:len(old_actions)] == old_actions
    assert sleeve._c6_orders[:len(old_orders)] == old_orders
    suppressed = [x for x in sleeve._c6_orders if x['status'] == 'suppressed']
    assert len(suppressed) == 1
    winner = sleeve._c6_orders[suppressed[0]['suppression_winner_order_ordinal']]
    assert winner['reason'] == 'catastrophe_stop'
    assert winner['requested_shares'] == 300


def test_global_lock_records_cancelled_buy_before_next_open() -> None:
    sleeve, _, _ = _real_sleeve_with_position(500, 40_000.)
    sleeve._c6_orders, sleeve._c6_fills, sleeve._c6_order_by_signal = [], [], {}
    sleeve._c6_state_index = 0
    state = _state(sleeve, [(_signal('buy', symbol='300308'), _Strategy('fast'))])
    BacktestEngine._apply_global_risk_lock([state], pd.Timestamp('2026-01-06'))
    assert state.pending == []
    assert len(sleeve._c6_orders) == 1
    receipt = sleeve._c6_orders[0]
    assert receipt['status'] == 'cancelled'
    assert receipt['execution_timestamp'] is None
    assert receipt['blocked_reason'] == 'merged_account_lock'
    assert receipt['authorized_shares'] == receipt['filled_shares'] == 0


def test_exposure_receipt_uses_side_neutral_marks_and_real_book_inventory() -> None:
    sleeve, data_map, dates = _real_sleeve_with_position(500, 40_000.)
    state = _state(sleeve, [])
    state.data_map = data_map
    coordinator = BacktestEngine()
    coordinator._c6_exposure_trace = []
    coordinator._record_c6_exposure([state], dates[1], 'batch_start')
    snapshot = coordinator._c6_exposure_trace[0]
    assert snapshot['gross_notional'] == 5000.
    assert snapshot['assets'] == sleeve.cash + 5000.
    assert snapshot['positions'][0]['shares'] == 500
    assert snapshot['positions'][0]['mark_price'] == 10.
    assert snapshot['symbol_notionals'] == {'300308': 5000.}
    assert sum(snapshot['cluster_notionals'].values()) == 5000.
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


def test_pending_remainder_deduplication_preserves_actual_partial_fill_status():
    sleeve, data_map, dates = _real_sleeve_with_position(500, 40000.)
    sleeve._c6_orders, sleeve._c6_fills, sleeve._c6_order_by_signal = [], [], {}
    sleeve._c6_state_index = 0
    first = Signal('300308', 'fast', 'sell', target_shares=500, price=10., reason='ordinary_exit', signal_date='2026-01-05')
    second = Signal('300308', 'fast', 'sell', target_shares=500, price=10., reason='sector_risk_trim', signal_date='2026-01-05')
    pending = sleeve._execute_pending_signals([(first, _Strategy('fast')), (second, None)], data_map, dates[1], {d: i for i, d in enumerate(dates)})
    assert len(pending) == 1 and pending[0][0].reason == 'sector_risk_trim'
    actual = sleeve._c6_orders[0]
    assert actual['filled_shares'] == 200
    assert actual['status'] == 'partial'
    assert any(event['event'] == 'pending_remainder_suppressed' and event['shares'] == 300 for event in actual['events'])
