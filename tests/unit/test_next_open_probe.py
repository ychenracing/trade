"""A local execution probe must use native liquidity, fees and close-known books."""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from quantfusion.config.engine import default_engine_config
from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.domain.models import Position


def _load_probe():
    path = Path(__file__).parents[2] / 'artifacts/diagnostics/no_waiver/next_open_probe.py'
    assert path.is_file(), 'The isolated native execution probe is not implemented'
    spec = importlib.util.spec_from_file_location('next_open_probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.probe_next_open


def _inputs(*, open_price=10., shares=500, volume=100000.):
    dates = pd.bdate_range('2026-01-05', periods=3)
    frame = pd.DataFrame({'open': [10., open_price, 10.], 'close': [10., 9., 10.],
                          'high': [10., 10., 10.], 'low': [10., 8., 10.],
                          'volume': [volume, 999999999., 999999999.]}, index=dates)
    rows = [{'sleeve': 'base', 'cash': 1000., 'book_before': {
        '300308': {'atr_channel': vars(Position('300308', 'atr_channel', shares, 10., '2026-01-05'))}}}]
    return rows, {'300308': frame}, PortfolioPolicy(), default_engine_config()


def test_native_fills_conserve_cash_inventory_and_do_not_mutate_inputs():
    probe = _load_probe()
    rows, frames, policy, cfg = _inputs()
    before = copy.deepcopy(rows)
    out = probe(rows, frames, policy, cfg, '2026-01-05', '2026-01-06')
    assert rows == before
    assert sum(x['shares'] for x in out['fills']) == 500
    assert out['residual_shares'] == {}
    assert out['ending_cash'] - out['starting_cash'] == pytest.approx(sum(x['net_cash_flow'] for x in out['fills']))
    assert out['fills'][0]['price'] == pytest.approx(9.99)
    assert out['fills'][0]['commission'] == 5.
    assert out['fills'][0]['signal_date'] == '2026-01-05'


def test_limit_down_keeps_the_obligation_without_crediting_cash():
    rows, frames, policy, cfg = _inputs(open_price=8.)
    out = _load_probe()(rows, frames, policy, cfg, '2026-01-05', '2026-01-06')
    assert not out['fills']
    assert out['ending_cash'] == out['starting_cash']
    assert out['residual_shares'] == {'300308': 500}
    assert out['unfilled'][0]['target_shares'] == 500


def test_adv_is_prior_only_and_shared_across_sleeves():
    rows, frames, policy, cfg = _inputs()
    second = copy.deepcopy(rows[0])
    second['sleeve'] = 'slow'
    rows.append(second)
    out = _load_probe()(rows, frames, policy, cfg, '2026-01-05', '2026-01-06')
    assert sum(x['shares'] for x in out['fills']) == 500
    assert out['residual_shares'] == {'300308': 500}


@pytest.mark.parametrize('close,execution', [('2026-01-05', '2026-01-05'), ('2026-01-05', '2026-01-07')])
def test_rejects_same_day_or_skipped_next_session(close, execution):
    rows, frames, policy, cfg = _inputs()
    with pytest.raises(ValueError, match='next trading session'):
        _load_probe()(rows, frames, policy, cfg, close, execution)


def test_rejects_incomplete_snapshot_instead_of_inventing_a_mark():
    rows, frames, policy, cfg = _inputs()
    frames['300308'] = frames['300308'].iloc[1:]
    with pytest.raises(ValueError, match='closing mark'):
        _load_probe()(rows, frames, policy, cfg, '2026-01-05', '2026-01-06')
