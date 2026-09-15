import pandas as pd

from quantfusion.domain.models import Signal
from quantfusion.engine.causal import CausalBacktestEngine


class _SignalStrategy:
    def __init__(self, name: str, signal: Signal) -> None:
        self.name = name
        self._signal = signal

    def on_bar(self, _ctx):
        return self._signal


def _collect(strategy_name: str, rsi: float, *, tradable_count: int = 7):
    date = pd.Timestamp("2026-04-24")
    signal = Signal(
        symbol="candidate",
        strategy_name=strategy_name,
        direction="buy",
        target_shares=100,
        price=10.0,
        signal_date=date.strftime("%Y-%m-%d"),
    )
    engine = object.__new__(CausalBacktestEngine)
    engine.positions = {}
    engine.cfg = {"max_positions": 6}
    engine._tradable_symbol_codes = {
        f"symbol-{index}" for index in range(tradable_count - 1)
    } | {"candidate"}
    engine.strategy_instances = {
        "candidate": [_SignalStrategy(strategy_name, signal)]
    }
    frame = pd.DataFrame({"close": [10.0]}, index=[date])
    indicators = {"candidate": {"rsi": pd.Series([rsi], index=[date])}}
    return engine._collect_strategy_signals(
        {"candidate": "Candidate"},
        {"candidate": frame},
        indicators,
        date,
        date.strftime("%Y-%m-%d"),
        100_000.0,
        [],
        allow_buys=True,
        top_symbols=set(),
    )


def test_early_dual_event_reaches_portfolio_selection_outside_momentum_table():
    pending = _collect("dual_ma", 54.0)
    assert [(signal.symbol, strategy.name) for signal, strategy in pending] == [
        ("candidate", "dual_ma")
    ]


def test_late_dual_event_still_requires_momentum_selection():
    assert _collect("dual_ma", 60.01) == []


def test_early_dual_event_does_not_bypass_when_portfolio_slots_are_not_scarce():
    assert _collect("dual_ma", 54.0, tradable_count=6) == []


def test_breakout_signal_still_requires_momentum_selection():
    assert _collect("atr_channel", 54.0) == []
