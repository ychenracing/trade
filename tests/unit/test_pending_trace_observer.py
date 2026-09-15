"""The diagnostic observer must expose filtering without changing decisions."""
import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from quantfusion.domain.models import Signal
from quantfusion.engine.sector_risk import CoreSectorRiskMixin
from quantfusion.engine.signals import CoreSignalMixin
from quantfusion.strategy.trend import ATRChannelStrategy


@pytest.fixture
def observer_module():
    path = Path(__file__).resolve().parents[2] / 'artifacts/diagnostics/no_waiver/pending_trace.py'
    assert path.is_file(), 'read-only pending observer is missing'
    spec = importlib.util.spec_from_file_location('pending_trace', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('keyword_arguments', [False, True])
def test_exposes_pending_suppression_without_evaluating_strategy_twice(observer_module, monkeypatch, keyword_arguments):
    calls = []
    emitted = Signal('300308', 'atr_channel', 'sell', 100, 100., reason='native exit', signal_date='2026-02-06')
    def on_bar(self, ctx):
        calls.append(ctx.date)
        return emitted
    monkeypatch.setattr(ATRChannelStrategy, 'on_bar', on_bar)
    strategy = ATRChannelStrategy({})
    class Engine(CoreSectorRiskMixin, CoreSignalMixin):
        pass
    engine = Engine()
    engine.sleeve_name = 'test'
    engine.cash = 10.
    engine.positions = {}
    engine.strategy_instances = {'300308': [strategy]}
    frame = pd.DataFrame({'close': [100.]}, index=pd.to_datetime(['2026-02-06']))
    pending = [(emitted, strategy)]
    args = ({'300308': 'symbol'}, {'300308': frame}, {'300308': {}}, frame.index[0], '2026-02-06', 10000., pending)
    original = CoreSectorRiskMixin._collect_strategy_signals
    observer = observer_module.PendingTrace({'300308'})
    with observer:
        result = engine._collect_strategy_signals(*args, allow_buys=False, top_symbols=set()) if keyword_arguments else engine._collect_strategy_signals(*args, False, set())
    assert calls == ['2026-02-06']
    assert result == []
    assert pending[0][0] is emitted and pending[0][1] is strategy
    assert CoreSectorRiskMixin._collect_strategy_signals is original
    record = next(row for row in observer.rows if row['stage'] == 'collect')
    assert record['allow_buys'] is False
    assert record['pending_checks'][0]['has_pending_sell'] is True
    assert record['emitted'][0]['signal']['reason'] == 'native exit'
    assert record['collected'] == []
    record['pending_before'][0]['signal']['reason'] = 'mutate saved copy'
    assert emitted.reason == 'native exit'


def test_restores_instrumented_methods_on_exception(observer_module):
    original = CoreSectorRiskMixin._collect_strategy_signals
    observer = observer_module.PendingTrace({'300308'})
    with pytest.raises(RuntimeError, match='deliberate'):
        with observer:
            raise RuntimeError('deliberate')
    assert CoreSectorRiskMixin._collect_strategy_signals is original
