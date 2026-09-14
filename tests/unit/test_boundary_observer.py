import importlib.util
from pathlib import Path
from types import SimpleNamespace
import pytest

spec = importlib.util.spec_from_file_location('boundary', Path(__file__).resolve().parents[2] / 'artifacts/diagnostics/no_waiver/boundary_replay.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_wrapper_calls_original_once_and_returns_the_identical_result():
    calls = []
    expected = object()
    def method(self, value):
        calls.append(('method', value))
        return expected
    wrapped = mod.wrap_once(method, lambda s,a,k: calls.append(('before', a[0])),
                            lambda s,a,k,result: calls.append(('after', result)))
    assert wrapped(object(), 7) is expected
    assert calls == [('before', 7), ('method', 7), ('after', expected)]


def test_exception_is_not_swallowed_or_retried():
    calls = []
    def method(self):
        calls.append('method')
        raise ValueError('original failure')
    wrapped = mod.wrap_once(method, lambda *a: None, lambda *a: calls.append('after'))
    with pytest.raises(ValueError, match='original failure'):
        wrapped(object())
    assert calls == ['method']


def test_book_snapshot_is_detached_and_distinguishes_zero_stale_owner():
    owner = SimpleNamespace(name='atr_channel', position=SimpleNamespace(shares=0))
    sleeve = SimpleNamespace(sleeve_name='s', cash=17., positions={}, strategy_instances={'x':[owner]})
    result = mod.book_snapshot(sleeve)
    owner.position.shares = 100
    assert result['owners']['x']['atr_channel']['position']['shares'] == 0
    assert result['owners']['x']['atr_channel']['matches_book'] is False
    assert sleeve.positions == {}
