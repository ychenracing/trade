"""Read-only collection, pending-order and fill observer; never a trading policy."""
from __future__ import annotations

import argparse
import copy
import hashlib
import inspect
import json
import pickle
import subprocess
import sys
from functools import wraps
from pathlib import Path

from quantfusion.engine.ensemble import _EnsembleSleeveBacktestEngine
from quantfusion.engine.sector_risk import CoreSectorRiskMixin
from quantfusion.engine.signals import CoreSignalMixin
from quantfusion.strategy.trend import ATRChannelStrategy, DualMAStrategy, TurtleBreakoutStrategy


class PendingTrace:
    """Observe each real call once, copying evidence and restoring all methods."""

    def __init__(self, symbols: set[str]):
        self.symbols = frozenset(symbols)
        self.rows: list[dict] = []
        self._originals: list[tuple] = []
        self._active: list[dict] = []

    def _queue(self, items):
        return [
            {'signal': copy.deepcopy(vars(signal)),
             'strategy_object': strategy.name if strategy is not None else None}
            for signal, strategy in items if signal.symbol in self.symbols
        ]

    def _book(self, engine):
        return {symbol: {name: copy.deepcopy(vars(position)) for name, position in positions.items()}
                for symbol, positions in engine.positions.items() if symbol in self.symbols}

    def _patch(self, cls, name, factory):
        original = getattr(cls, name)
        self._originals.append((cls, name, original))
        setattr(cls, name, factory(original))

    def __enter__(self):
        if self._originals:
            raise RuntimeError('Observer is already installed')
        try:
            self._patch(CoreSectorRiskMixin, '_collect_strategy_signals', self._collect)
            self._patch(CoreSignalMixin, '_pending_has_sell', self._pending)
            self._patch(CoreSignalMixin, '_fuse_daily_signals', self._fusion)
            self._patch(_EnsembleSleeveBacktestEngine, '_execute_pending_signals', self._execute)
            for cls in (ATRChannelStrategy, DualMAStrategy, TurtleBreakoutStrategy):
                self._patch(cls, 'on_bar', self._on_bar)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        for cls, name, original in reversed(self._originals):
            setattr(cls, name, original)
        self._originals.clear()
        self._active.clear()
        return False

    def _collect(self, original):
        signature = inspect.signature(original)
        @wraps(original)
        def call(engine, *args, **kwargs):
            bound = signature.bind(engine, *args, **kwargs)
            bound.apply_defaults()
            values = bound.arguments
            row = {'stage': 'collect', 'date': values['date_str'],
                   'sleeve': engine.sleeve_name, 'cash': float(engine.cash),
                   'allow_buys': bool(values['allow_buys']),
                   'selected': None if values['top_symbols'] is None else sorted(values['top_symbols']),
                   'book_before': self._book(engine), 'pending_before': self._queue(values['pending']),
                   'emitted': [], 'pending_checks': []}
            self._active.append(row)
            try:
                result = original(engine, *args, **kwargs)
                row['collected'] = self._queue(result)
                self.rows.append(row)
                return result
            finally:
                self._active.pop()
        return call

    def _on_bar(self, original):
        @wraps(original)
        def call(strategy, ctx):
            observe = bool(self._active) and ctx.symbol in self.symbols
            before = copy.deepcopy(vars(strategy.position)) if observe and strategy.position is not None else None
            signal = original(strategy, ctx)
            if observe:
                self._active[-1]['emitted'].append({
                    'symbol': ctx.symbol, 'strategy': strategy.name, 'position_before': before,
                    'signal': None if signal is None else copy.deepcopy(vars(signal))})
            return signal
        return call

    def _pending(self, original):
        @wraps(original)
        def call(engine, pending, code, strategy_name):
            result = original(engine, pending, code, strategy_name)
            if self._active and code in self.symbols:
                self._active[-1]['pending_checks'].append({
                    'symbol': code, 'strategy': strategy_name, 'has_pending_sell': bool(result)})
            return result
        return call

    def _fusion(self, original):
        @wraps(original)
        def call(engine, daily, date_str):
            before = self._queue(daily)
            result = original(engine, daily, date_str)
            after = self._queue(result)
            if before or after:
                self.rows.append({'stage': 'fusion', 'date': date_str, 'sleeve': engine.sleeve_name,
                                  'input': before, 'output': after})
            return result
        return call

    def _execute(self, original):
        signature = inspect.signature(original)
        @wraps(original)
        def call(engine, *args, **kwargs):
            bound = signature.bind(engine, *args, **kwargs)
            bound.apply_defaults()
            values = bound.arguments
            before = self._queue(values['pending'])
            book = self._book(engine)
            start = len(engine.trades)
            event_start = len(getattr(engine, 'order_events', []))
            result = original(engine, *args, **kwargs)
            after = self._queue(result)
            fills = [copy.deepcopy(vars(trade)) for trade in engine.trades[start:]
                     if trade.symbol in self.symbols]
            if before or after or fills:
                self.rows.append({'stage': 'open', 'date': str(values['date'].date()),
                                  'sleeve': engine.sleeve_name,
                                  'directions': sorted(values['directions'] or {'buy', 'sell'}),
                                  'pending_before': before, 'pending_after': after,
                                  'book_before': book, 'book_after': self._book(engine), 'fills': fills,
                                  'order_events': copy.deepcopy(getattr(engine, 'order_events', [])[event_start:])})
            return result
        return call


def main():
    from quantfusion.application import stress_artifacts as a, stress_scenarios as s, stress_metrics as m
    from quantfusion.config.paths import PROJECT_ROOT, MARKET_DATA_DIR, REGIME_DATA_DIR
    from quantfusion.config.universe import SYMBOL_NAMES
    from quantfusion.engine.replay import ProductionReplayEngine
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--id', required=True)
    parser.add_argument('--symbols', nargs='+', required=True)
    args = parser.parse_args()
    scenarios = {item['scenario_id']: item for item in s._multi_seed_scenarios(random_samples=50, permutation_samples=50, seeds=s.DEFAULT_SEEDS)}
    scenario = scenarios[args.id]
    source = a._tree_fingerprint(a._source_files())
    data = a._tree_fingerprint(a._data_files(MARKET_DATA_DIR, REGIME_DATA_DIR))
    assert data == 'aeeb96a94e84830033e8ad11180293fc982bb08e99322054d2c27dbb3f4b5975'
    patch = subprocess.check_output(['git', 'diff', 'HEAD', '--', 'quantfusion'], cwd=PROJECT_ROOT)
    identity = {'source_revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=PROJECT_ROOT, text=True).strip(),
                'source_fingerprint': source, 'source_patch_sha256': hashlib.sha256(patch).hexdigest(),
                'data_fingerprint': data, 'scenario': scenario, 'cfg_overrides': {},
                'observer_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'python': sys.version, 'diagnostic_noncanonical': True, 'allow_publication': False}
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / 'identity.json').write_text(json.dumps(identity, indent=2) + '\n')
    (args.out / 'source.patch').write_bytes(patch)
    with PendingTrace(set(args.symbols)) as observer:
        result = ProductionReplayEngine(m.INITIAL_CAPITAL).run(
            {code: SYMBOL_NAMES[code] for code in scenario['symbols']}, m.START_DATE, m.END_DATE,
            data_dir=str(MARKET_DATA_DIR), regime_data_dir=str(REGIME_DATA_DIR), indicator_state='warm')
    assert source == a._tree_fingerprint(a._source_files())
    result['pending_trace'] = observer.rows
    raw = pickle.dumps(result)
    (args.out / 'result.pkl').write_bytes(raw)
    summary = {'identity': identity, 'raw_sha256': hashlib.sha256(raw).hexdigest(),
               'wealth': 1 + result['total_return'], 'max_drawdown': result['max_drawdown'],
               'buckets': result['date_symbol_side_count'], 'trace_rows': len(observer.rows)}
    (args.out / 'summary.json').write_text(json.dumps(summary, indent=2, allow_nan=False) + '\n')
    print(json.dumps(summary, allow_nan=False))


if __name__ == '__main__':
    main()
