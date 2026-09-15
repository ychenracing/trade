from types import SimpleNamespace
import pandas as pd
from quantfusion.config.universe import (
    ESTABLISHED_BASE_CORE,
    ESTABLISHED_EXPANSION_CORE,
)
from quantfusion.domain.models import Signal
from quantfusion.engine.causal import CausalBacktestEngine
from quantfusion.engine.universe import BacktestEngine
def _series(values: dict[str, float]) -> pd.Series:
    return pd.Series(values, dtype="float64").set_axis(pd.to_datetime(list(values)))
def _scoring_engine() -> CausalBacktestEngine:
    engine = object.__new__(CausalBacktestEngine)
    engine.policy = SimpleNamespace(
        candidate_lookbacks=(10,),
        regime_symbols=("ref-a", "ref-b"),
    )
    engine._candidate_score_series = {
        "ref-a": {10: _series({"2026-01-02": 1.0})},
        "ref-b": {10: _series({"2026-01-02": 3.0})},
        "candidate": {10: _series({"2026-01-02": 2.0})},
    }
    return engine
def test_fixed_reference_denominator_ignores_unrelated_tradable_symbols() -> None:
    """Catch accidental pool-relative scoring in U admission."""
    engine = _scoring_engine()
    execution_date = pd.Timestamp("2026-01-05")
    before = engine._fixed_reference_scores(execution_date, {"candidate"})
    engine._candidate_score_series["unrelated"] = {
        10: _series({"2026-01-02": 100.0})
    }
    after = engine._fixed_reference_scores(execution_date, {"candidate"})
    assert before == after == {"candidate": 0.5}
def test_fixed_reference_uses_latest_close_strictly_before_execution() -> None:
    """Catch same-open or same-date evidence leaking into candidate strength."""
    engine = _scoring_engine()
    for code, execution_value in {
        "ref-a": 1.0,
        "ref-b": 3.0,
        "candidate": 100.0,
    }.items():
        engine._candidate_score_series[code][10].loc[pd.Timestamp("2026-01-05")] = (
            execution_value
        )
    scores = engine._fixed_reference_scores(
        pd.Timestamp("2026-01-05"), {"candidate"}
    )
    assert scores == {"candidate": 0.5}
def test_fixed_reference_fails_closed_when_a_required_sample_is_missing() -> None:
    """Catch a silently shrunken policy reference denominator."""
    engine = _scoring_engine()
    engine._candidate_score_series["ref-b"][10] = _series(
        {"2026-01-05": 3.0}
    )
    assert engine._fixed_reference_scores(
        pd.Timestamp("2026-01-05"), {"candidate"}
    ) == {}
def _signal(symbol: str, strategy: str, signal_date: str) -> Signal:
    return Signal(
        symbol=symbol,
        strategy_name=strategy,
        direction="buy",
        target_shares=100,
        signal_date=signal_date,
    )
def _emitting_state(
    candidate_value: float,
    pending: list[Signal],
    *,
    missing_reference: bool = False,
) -> SimpleNamespace:
    sleeve = object.__new__(CausalBacktestEngine)
    sleeve.policy = SimpleNamespace(
        candidate_lookbacks=(10,),
        regime_symbols=("r1", "r2", "r3", "r4", "r5"),
    )
    sleeve._candidate_score_series = {
        f"r{index}": {10: _series({"2026-01-02": float(index)})}
        for index in range(1, 6)
    }
    if missing_reference:
        sleeve._candidate_score_series.pop("r5")
    for symbol in {signal.symbol for signal in pending}:
        sleeve._candidate_score_series[symbol] = {
            10: _series({"2026-01-02": candidate_value})
        }
    sleeve.positions = {}
    sleeve._tradable_symbol_codes = {signal.symbol for signal in pending}
    sleeve.order_events = []
    return SimpleNamespace(
        sleeve=sleeve,
        data_map={
            signal.symbol: pd.DataFrame(
                {"close": [candidate_value, candidate_value]},
                index=pd.to_datetime(["2026-01-05", "2026-01-06"]),
            )
            for signal in pending
        },
        pending=[
            (signal, SimpleNamespace(name=signal.strategy_name)) for signal in pending
        ],
    )


def _early_dual_state(candidate_value: float = 1.0, rsi: float = 54.0):
    signal = _signal("candidate", "dual_ma", "2026-01-02")
    state = _emitting_state(candidate_value, [signal])
    state.indicator_map = {
        "candidate": {"rsi": _series({"2026-01-02": rsi})}
    }
    return state
def _coordinator(*, runtime_size: int = 13, maximum: int = 1) -> BacktestEngine:
    engine = object.__new__(BacktestEngine)
    engine.cfg = {"max_positions": maximum, "adaptive_max_positions": False}
    engine._runtime_tradable_count = runtime_size
    engine._new_candidate_intent_streak = {}
    return engine
def test_emitting_sleeve_contributes_one_vote_per_symbol_and_batch() -> None:
    """Catch duplicate strategies or signal dates overweighting one sleeve."""
    low = _emitting_state(
        0.0,
        [
            _signal("candidate", "dual_ma", "2026-01-01"),
            _signal("candidate", "turtle_breakout", "2026-01-02"),
        ],
    )
    high = _emitting_state(
        6.0, [_signal("candidate", "atr_channel", "2026-01-02")]
    )
    _coordinator()._authorize_portfolio_buys(
        [low, high], pd.Timestamp("2026-01-05")
    )
    assert len(low.pending) == 2
    assert len(high.pending) == 1
def test_missing_emitting_sample_rejects_new_symbol_with_audit() -> None:
    """Catch missing fixed evidence being converted to a numeric zero vote."""
    state = _emitting_state(
        6.0,
        [_signal("candidate", "dual_ma", "2026-01-02")],
        missing_reference=True,
    )
    _coordinator()._authorize_portfolio_buys(
        [state], pd.Timestamp("2026-01-05")
    )
    assert state.pending == []
    assert state.sleeve.order_events[-1]["event"] == (
        "rejected_new_candidate_missing_fixed_reference_score"
    )
    assert state.sleeve.order_events[-1]["allocation_score"] is None
def test_u_off_uses_the_legacy_pool_relative_coordinator_score() -> None:
    state = _emitting_state(
        6.0,
        [_signal("candidate", "dual_ma", "2026-01-02")],
        missing_reference=True,
    )
    coordinator = _coordinator()
    coordinator._c6_diagnostic_request = {"intervention_id": "BASELINE"}
    state.sleeve._allocation_scores = lambda *_: {"candidate": 1.0}
    coordinator._authorize_portfolio_buys(
        [state], pd.Timestamp("2026-01-05")
    )
    assert len(state.pending) == 1
def test_route_migration_bypasses_missing_fixed_reference_score() -> None:
    """Catch U accidentally removing the existing outer-route bypass."""
    state = _emitting_state(
        6.0,
        [_signal("candidate", "positive_momentum_hold", "2026-01-02")],
        missing_reference=True,
    )
    _coordinator()._authorize_portfolio_buys(
        [state], pd.Timestamp("2026-01-05")
    )
    assert len(state.pending) == 1
    assert state.sleeve.order_events == []
def test_held_symbol_bypasses_missing_fixed_reference_score() -> None:
    """Catch U accidentally reclassifying a carried holding as a newcomer."""
    state = _emitting_state(
        6.0,
        [_signal("candidate", "dual_ma", "2026-01-02")],
        missing_reference=True,
    )
    state.sleeve.positions = {"candidate": {"dual_ma": object()}}
    _coordinator()._authorize_portfolio_buys(
        [state], pd.Timestamp("2026-01-05")
    )
    assert len(state.pending) == 1
def test_candidate_order_score_does_not_zero_ordinary_held_buy_ordering(monkeypatch):
    state = _emitting_state(3.0, [_signal('held', 'dual_ma', '2026-01-02'),
                                  _signal('candidate', 'dual_ma', '2026-01-02')])
    sleeve = state.sleeve
    seen = []
    monkeypatch.setattr(sleeve, '_allocation_scores', lambda data, date: {'held': .9, 'candidate': .2})
    monkeypatch.setattr(sleeve, '_prepare_open_signal', lambda signal, *args: (seen.append(signal.symbol) or None, False))
    sleeve._execute_pending_signals(state.pending, {}, pd.Timestamp('2026-01-05'), {}, buy_scores={'candidate': .6})
    assert seen == ['held', 'candidate']


def test_base_core_keeps_its_existing_six_to_twelve_name_bypass() -> None:
    """Catch U imposing the new score gate on the established five-name core."""
    symbol = sorted(ESTABLISHED_BASE_CORE)[0]
    state = _emitting_state(
        0.0, [_signal(symbol, "dual_ma", "2026-01-02")]
    )
    state.sleeve._tradable_symbol_codes = set(ESTABLISHED_BASE_CORE) | {"extra"}
    _coordinator(runtime_size=6)._authorize_portfolio_buys(
        [state], pd.Timestamp("2026-01-05")
    )
    assert len(state.pending) == 1
def test_noncore_candidate_keeps_two_day_confirmation_at_size_six() -> None:
    """Catch U bypassing the existing confirmation streak."""
    signal = _signal("candidate", "dual_ma", "2026-01-02")
    state = _emitting_state(3.0, [signal])
    state.sleeve._tradable_symbol_codes = set(ESTABLISHED_BASE_CORE) | {"candidate"}
    coordinator = _coordinator(runtime_size=6)
    coordinator._authorize_portfolio_buys(
        [state], pd.Timestamp("2026-01-05")
    )
    assert state.pending == []
    assert state.sleeve.order_events[-1]["event"] == (
        "rejected_new_candidate_confirmation"
    )
    state.pending = [(signal, SimpleNamespace(name=signal.strategy_name))]
    coordinator._authorize_portfolio_buys(
        [state], pd.Timestamp("2026-01-06")
    )
    assert len(state.pending) == 1


def test_cross_sleeve_early_dual_event_bypasses_persistent_signal_confirmation() -> None:
    """A one-day cross is confirmed by sleeves, not impossible four-day persistence."""
    states = [_early_dual_state() for _ in range(3)]
    coordinator = _coordinator(runtime_size=11, maximum=1)
    coordinator._authorize_portfolio_buys(states, pd.Timestamp("2026-01-05"))
    assert [[signal.symbol for signal, _ in state.pending] for state in states] == [
        ["candidate"], ["candidate"], ["candidate"],
    ]
    assert all(
        state.sleeve.order_events[-1]["event"]
        == "admitted_cross_sleeve_early_dual_transition"
        for state in states
    )


def test_single_sleeve_early_dual_event_keeps_existing_score_gate() -> None:
    state = _early_dual_state()
    _coordinator(runtime_size=11, maximum=1)._authorize_portfolio_buys(
        [state], pd.Timestamp("2026-01-05")
    )
    assert state.pending == []
    assert state.sleeve.order_events[-1]["event"] == (
        "rejected_new_candidate_allocation_score"
    )


def test_cross_sleeve_late_dual_event_keeps_existing_score_gate() -> None:
    states = [_early_dual_state(rsi=60.01) for _ in range(3)]
    _coordinator(runtime_size=11, maximum=1)._authorize_portfolio_buys(
        states, pd.Timestamp("2026-01-05")
    )
    assert all(state.pending == [] for state in states)
    assert all(
        state.sleeve.order_events[-1]["event"]
        == "rejected_new_candidate_allocation_score"
        for state in states
    )


def test_cross_sleeve_event_keeps_score_gate_without_candidate_scarcity() -> None:
    states = [_early_dual_state() for _ in range(3)]
    _coordinator(runtime_size=6, maximum=6)._authorize_portfolio_buys(
        states, pd.Timestamp("2026-01-05")
    )
    assert all(state.pending == [] for state in states)
    assert all(
        state.sleeve.order_events[-1]["event"]
        == "rejected_new_candidate_allocation_score"
        for state in states
    )
def test_exact_fourteen_keeps_expansion_score_and_confirmation_rules() -> None:
    """Catch U changing the exact-14 newcomer contract."""
    signal = _signal("candidate", "dual_ma", "2026-01-02")
    state = _emitting_state(4.0, [signal])
    state.sleeve._tradable_symbol_codes = set(ESTABLISHED_EXPANSION_CORE) | {
        "candidate"
    }
    coordinator = _coordinator(runtime_size=14)
    coordinator._authorize_portfolio_buys(
        [state], pd.Timestamp("2026-01-05")
    )
    assert state.pending == []
    state.pending = [(signal, SimpleNamespace(name=signal.strategy_name))]
    coordinator._authorize_portfolio_buys(
        [state], pd.Timestamp("2026-01-06")
    )
    assert len(state.pending) == 1


def test_budget_preview_excludes_unconfirmed_candidate_without_mutating_queue() -> None:
    """A candidate rejected at the next open cannot dilute close risk capacity."""
    signal = _signal("candidate", "atr_channel", "2026-01-02")
    state = _emitting_state(6.0, [signal])
    state.sleeve._tradable_symbol_codes = set(ESTABLISHED_EXPANSION_CORE) | {
        "candidate"
    }
    coordinator = _coordinator(runtime_size=14, maximum=6)

    scores, evidence_symbols = coordinator._authorize_portfolio_buys(
        [state], pd.Timestamp("2026-01-05"), preview_only=True
    )

    assert scores == {"candidate": 1.0}
    assert evidence_symbols == set()
    assert state.pending == [(signal, state.pending[0][1])]
    assert state.sleeve.order_events == []
    assert coordinator._new_candidate_intent_streak == {}


def test_equal_score_capacity_tie_break_is_input_permutation_invariant() -> None:
    """Catch U changing max-position capacity or deterministic rank ties."""
    winners = []
    for symbols in (("300502", "300308"), ("300308", "300502")):
        signals = [_signal(symbol, "dual_ma", "2026-01-02") for symbol in symbols]
        state = _emitting_state(3.0, signals)
        _coordinator(maximum=1)._authorize_portfolio_buys(
            [state], pd.Timestamp("2026-01-05")
        )
        winners.append([signal.symbol for signal, _ in state.pending])
    assert winners == [["300308"], ["300308"]]
def test_fully_sold_symbol_without_buy_does_not_reserve_candidate_capacity() -> None:
    signal = _signal("candidate", "dual_ma", "2026-01-02")
    state = _emitting_state(6.0, [signal])
    _coordinator(maximum=1)._authorize_portfolio_buys(
        [state],
        pd.Timestamp("2026-01-05"),
        carried_symbols={"sold-before-authorization"},
    )
    assert [item[0].symbol for item in state.pending] == ["candidate"]
def test_authorization_returns_global_mean_scores_for_execution_order() -> None:
    low = _emitting_state(2.0, [_signal("candidate", "dual_ma", "2026-01-02")])
    high = _emitting_state(6.0, [_signal("candidate", "atr_channel", "2026-01-02")])
    scores = _coordinator()._authorize_portfolio_buys(
        [low, high], pd.Timestamp("2026-01-05")
    )
    assert scores == {"candidate": 0.7}


class _RecordingExecutionEngine(CausalBacktestEngine):
    def _allocation_scores(self, data_map, date):
        del data_map, date
        return {"strong": 0.0, "weak": 1.0}
    def _prepare_open_signal(self, signal, data_map, date, date_to_pos):
        del data_map, date, date_to_pos
        return signal, False
    def _execute_buy_batch(self, items, date_str, data_map, date):
        del date_str, data_map, date
        self.executed_batches.append(items[0][0].symbol)
    @staticmethod
    def _dedupe_pending_signals(pending):
        return pending
def test_candidate_strength_execution_order_uses_bound_global_scores() -> None:
    """Catch downstream ordering recomputing a sleeve-local admission score."""
    engine = object.__new__(_RecordingExecutionEngine)
    engine.policy = SimpleNamespace(
        candidate_lookbacks=(10,),
        regime_symbols=("r1", "r2", "r3", "r4", "r5"),
    )
    engine._candidate_score_series = {
        f"r{index}": {10: _series({"2026-01-02": float(index)})}
        for index in range(1, 6)
    }
    engine._candidate_score_series.update(
        {
            "strong": {10: _series({"2026-01-02": 6.0})},
            "weak": {10: _series({"2026-01-02": 2.0})},
        }
    )
    engine.executed_batches = []
    pending = [
        (_signal("weak", "dual_ma", "2026-01-02"), SimpleNamespace()),
        (_signal("strong", "dual_ma", "2026-01-02"), SimpleNamespace()),
    ]
    engine._execute_pending_signals(
        pending,
        data_map={},
        date=pd.Timestamp("2026-01-05"),
        date_to_pos={},
        directions=frozenset({"buy"}),
        buy_scores={"strong": 1.0, "weak": 0.0},
    )
    assert engine.executed_batches == ["strong", "weak"]
def test_overlay_weakness_score_stays_pool_relative() -> None:
    """Catch U leaking fixed-reference semantics into overlay weakness."""
    sleeve = SimpleNamespace(
        positions={},
        _allocation_scores=lambda data_map, date: {"candidate": 0.2},
        _fixed_reference_scores=lambda date, symbols: {"candidate": 1.0},
    )
    state = SimpleNamespace(sleeve=sleeve, data_map={})
    score = BacktestEngine._overlay_allocation_score(
        [state], pd.Timestamp("2026-01-05")
    )
    assert score("candidate") == 0.2


def test_overlay_weakness_excludes_symbols_outside_the_live_account_book() -> None:
    """An unheld add-one candidate cannot change which held name is trimmed."""
    ranked_universes = []

    def allocation_scores(data_map, date):
        del date
        ranked_universes.append(set(data_map))
        return {"held": 0.75, "unheld": 1.0}

    sleeve = SimpleNamespace(
        positions={"held": {"dual_ma": SimpleNamespace(shares=100)}},
        _allocation_scores=allocation_scores,
    )
    state = SimpleNamespace(
        sleeve=sleeve,
        data_map={"held": object(), "unheld": object()},
    )

    score = BacktestEngine._overlay_allocation_score(
        [state], pd.Timestamp("2026-01-05")
    )

    assert score("held") == 0.75
    assert ranked_universes == [{"held"}]


def test_pool_relative_score_cache_is_scoped_to_ranked_universe() -> None:
    """A prior full-pool query cannot poison a held-book-only risk query."""
    engine = object.__new__(CausalBacktestEngine)
    engine._c6_intervention = None
    engine._allocation_score_cache = {}
    engine._allocation_raw_series = {
        "weak": {
            window: _series({"2026-01-02": 1.0})
            for window in engine.ALLOCATION_LOOKBACKS
        },
        "strong": {
            window: _series({"2026-01-02": 2.0})
            for window in engine.ALLOCATION_LOOKBACKS
        },
        "unheld": {
            window: _series({"2026-01-02": 100.0})
            for window in engine.ALLOCATION_LOOKBACKS
        },
    }
    full = {symbol: object() for symbol in ("weak", "strong", "unheld")}
    held = {symbol: full[symbol] for symbol in ("weak", "strong")}

    full_scores = engine._allocation_scores(full, pd.Timestamp("2026-01-05"))
    held_scores = engine._allocation_scores(held, pd.Timestamp("2026-01-05"))

    assert full_scores["strong"] == 2 / 3
    assert held_scores == {"weak": 0.5, "strong": 1.0}
