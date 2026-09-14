"""Source-bound read-only collection/fusion/open-execution diagnosis.

Every strategy and execution method is called exactly once. Records are copied,
not fed back into the engine. Outputs are noncanonical and never publishable.
"""
import argparse
import contextlib
import copy
import functools
import hashlib
import io
import json
import lzma
import pickle
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch


def wrap_once(method, before, after):
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        before(self, args, kwargs)
        result = method(self, *args, **kwargs)
        after(self, args, kwargs, result)
        return result
    return wrapped


def book_snapshot(sleeve):
    owners = {}
    for code, strategies in sleeve.strategy_instances.items():
        for strategy in strategies:
            pos = strategy.position
            if pos is not None:
                owners.setdefault(code, {})[strategy.name] = {
                    'position': copy.deepcopy(vars(pos)),
                    'matches_book': pos is sleeve.positions.get(code, {}).get(strategy.name),
                }
    return {'sleeve': sleeve.sleeve_name, 'cash': float(sleeve.cash),
            'positions': {code: {name: copy.deepcopy(vars(pos)) for name, pos in books.items()}
                          for code, books in sleeve.positions.items()}, 'owners': owners}


def signals(queue):
    return [copy.deepcopy(vars(signal)) for signal, _ in queue]


@contextlib.contextmanager
def observe_boundaries(rows):
    from quantfusion.engine.causal import _CausalBacktestEngine
    from quantfusion.engine.market_regime import MarketRegimeMixin
    from quantfusion.engine.sector_risk import CoreSectorRiskMixin
    from quantfusion.strategy.trend import ATRChannelStrategy, DualMAStrategy, TurtleBreakoutStrategy
    scope = []
    def collect_before(self, args, kw):
        scope.append(self)
        rows.append({'phase': 'collect_before', 'date': args[4],
                     'book': book_snapshot(self), 'pending': signals(args[6]),
                     'allow_buys': kw.get('allow_buys', args[7] if len(args) > 7 else None),
                     'selected': sorted(kw.get('top_symbols') or [])})
    def collect_after(self, args, kw, result):
        rows.append({'phase': 'collect_after', 'date': args[4],
                     'sleeve': self.sleeve_name, 'signals': signals(result)})
        scope.pop()
    def strategy_before(self, args, kw):
        ctx = args[0]
        if scope and self.position is not None:
            sleeve = scope[-1]
            rows.append({'phase': 'strategy_before', 'date': ctx.date,
                         'sleeve': sleeve.sleeve_name, 'symbol': ctx.symbol,
                         'strategy': self.name, 'position': copy.deepcopy(vars(self.position)),
                         'matches_book': self.position is sleeve.positions.get(ctx.symbol, {}).get(self.name),
                         'close': float(ctx.df['close'].iloc[ctx.i]),
                         'indicators': {k: float(v.iloc[ctx.i]) for k, v in ctx.indicators.items()}})
    def strategy_after(self, args, kw, result):
        if scope and result is not None:
            rows.append({'phase': 'strategy_signal', 'date': args[0].date,
                         'sleeve': scope[-1].sleeve_name, 'signal': copy.deepcopy(vars(result))})
    def fuse_before(self, args, kw):
        rows.append({'phase': 'fuse_before', 'date': args[1], 'sleeve': self.sleeve_name,
                     'signals': signals(args[0])})
    def fuse_after(self, args, kw, result):
        rows.append({'phase': 'fuse_after', 'date': args[1], 'sleeve': self.sleeve_name,
                     'signals': signals(result)})
    def open_before(self, args, kw):
        rows.append({'phase': 'open_before', 'date': str(args[2].date()),
                     'directions': sorted(kw.get('directions') or ['buy','sell']),
                     'pending': signals(args[0]), 'book': book_snapshot(self)})
    def open_after(self, args, kw, result):
        rows.append({'phase': 'open_after', 'date': str(args[2].date()),
                     'pending': signals(result), 'book': book_snapshot(self)})
    with contextlib.ExitStack() as stack:
        methods = [(CoreSectorRiskMixin, '_collect_strategy_signals', collect_before, collect_after),
                   (MarketRegimeMixin, '_fuse_daily_signals', fuse_before, fuse_after),
                   (_CausalBacktestEngine, '_execute_pending_signals', open_before, open_after)]
        methods += [(cls, 'on_bar', strategy_before, strategy_after)
                    for cls in (ATRChannelStrategy, DualMAStrategy, TurtleBreakoutStrategy)]
        for cls, name, before, after in methods:
            stack.enter_context(patch.object(cls, name, wrap_once(getattr(cls, name), before, after)))
        yield


def run(args):
    sys.path.insert(0, str(args.root.resolve()))
    from quantfusion.engine.replay import ProductionReplayEngine
    from quantfusion.application import stress_artifacts as art, stress_scenarios as scen
    from quantfusion.config.paths import MARKET_DATA_DIR, REGIME_DATA_DIR
    from quantfusion.config.universe import SYMBOL_NAMES
    source = art._tree_fingerprint(art._source_files())
    data = art._tree_fingerprint(art._data_files(MARKET_DATA_DIR, REGIME_DATA_DIR))
    if source != args.source_fingerprint or data != 'aeeb96a94e84830033e8ad11180293fc982bb08e99322054d2c27dbb3f4b5975':
        raise ValueError(f'Source/data mismatch: {source} / {data}')
    plan = {s['scenario_id']: s for s in scen._multi_seed_scenarios(
        random_samples=50, permutation_samples=50, seeds=scen.DEFAULT_SEEDS)}
    if len(set(args.ids)) != len(args.ids) or not set(args.ids) <= set(plan):
        raise ValueError('Unknown or duplicate diagnostic scenario')
    args.out.mkdir(parents=True, exist_ok=False)
    revision = subprocess.check_output(['git','rev-parse','HEAD'],cwd=args.root,text=True).strip()
    diff = subprocess.check_output(['git','diff','--binary','HEAD'],cwd=args.root)
    (args.out/'source.patch').write_bytes(diff)
    identity = {'source_revision': revision, 'source_fingerprint': source, 'data_fingerprint': data,
                'observer_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'source_patch_sha256': hashlib.sha256(diff).hexdigest(), 'python': sys.version,
                'canonical': False, 'allow_publication': False, 'cfg_overrides': {}, 'scenario_ids': args.ids}
    (args.out/'identity.json').write_text(json.dumps(identity,indent=2)+'\n')
    results = []
    for sid in args.ids:
        rows = []
        with observe_boundaries(rows), contextlib.redirect_stdout(io.StringIO()):
            result = ProductionReplayEngine(2_000_000).run(
                {c: SYMBOL_NAMES[c] for c in plan[sid]['symbols']}, '2025-04-01','2026-07-20',
                data_dir=str(MARKET_DATA_DIR),regime_data_dir=str(REGIME_DATA_DIR),indicator_state='warm')
        result['boundary_trace'] = rows
        payload = pickle.dumps(result, protocol=pickle.HIGHEST_PROTOCOL)
        (args.out/(sid+'.pkl.xz')).write_bytes(lzma.compress(payload))
        row = {'scenario_id': sid, 'wealth': 1+result['total_return'], 'max_drawdown': result['max_drawdown'],
               'buckets': result['date_symbol_side_count'], 'raw_sha256': hashlib.sha256(payload).hexdigest(),
               'source_fingerprint':source, 'trace_rows':len(rows)}
        (args.out/(sid+'.json')).write_text(json.dumps(row,indent=2)+'\n')
        results.append(row)
        print(json.dumps(row),flush=True)
    (args.out/'results.json').write_text(json.dumps(results,indent=2)+'\n')
    manifest = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(args.out.iterdir()) if p.is_file()}
    (args.out/'SHA256SUMS.json').write_text(json.dumps(manifest,indent=2)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--source-fingerprint',required=True)
    parser.add_argument('--ids',nargs='+',required=True)
    run(parser.parse_args())
