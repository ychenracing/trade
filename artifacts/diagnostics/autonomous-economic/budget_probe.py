"""Read-only Core17 probe; never install this instrument in production."""
from __future__ import annotations

import contextlib
import os
import dataclasses
import gzip
import hashlib
import json
import math
import pickle
import platform
import subprocess
from pathlib import Path

import pandas as pd

from quantfusion.engine.replay import ProductionReplayEngine
from quantfusion.engine.ensemble_allocation import EnsembleAllocationMixin
from quantfusion.risk import account_budget as budget
from quantfusion.domain.rules import floor_to_lot
from scripts.backtest_universes import DATA_DIR, NAMES, UNIVERSES
from quantfusion.config.paths import PROJECT_ROOT, REGIME_DATA_DIR

BASE = '5f797f7524cd5e012f1abdc96f26b0e724e6c039'
OUT = Path(os.environ.get('TRADE_PROBE_OUTPUT', '/tmp/trade-economic-probe'))
OUT.mkdir(exist_ok=True)
_original_apply = EnsembleAllocationMixin._apply_account_risk_budget
_original_plan = budget.plan_account_risk_budget
active = {}
traces = []


def action_identity(actions):
    return [dataclasses.asdict(action) for action in actions]


def plan(*args, **kwargs):
    receipt, actions = _original_plan(*args, **kwargs)
    buys = args[4]
    preexisting = active.get('defensive', set())
    newly_planned = {(a.state_index, a.symbol, a.strategy_name) for a in actions}
    variants = {}
    for name, excluded in (
        ('preexisting_defensive', preexisting),
        ('all_planned_defensive', preexisting | newly_planned),
    ):
        dead = [i for i, (state, signal, value) in enumerate(buys)
                if (state, signal.symbol, signal.strategy_name) in excluded]
        if not dead:
            continue
        retained = [i for i in range(len(buys)) if i not in dead]
        alternate_args = list(args)
        alternate_args[4] = [buys[i] for i in retained]
        alternate, alternate_actions = _original_plan(*alternate_args, **kwargs)
        changed = []
        for new_i, old_i in enumerate(retained):
            signal = buys[old_i][1]
            before = floor_to_lot(signal.target_shares * receipt['buy_scales'][old_i])
            after = floor_to_lot(signal.target_shares * alternate['buy_scales'][new_i])
            if before != after:
                changed.append({'buy_index': old_i, 'symbol': signal.symbol,
                                'strategy': signal.strategy_name,
                                'before': before, 'after': after,
                                'price': signal.price})
        variants[name] = {'excluded_indexes': dead, 'changed_eligible_buys': changed,
                         'required_actions_identical': action_identity(actions) == action_identity(alternate_actions),
                         'gross_cap_before': receipt['gross_cap'],
                         'gross_cap_after': alternate['gross_cap']}
    traces.append({'date': kwargs['date_str'], 'books': args[3],
                   'buys': [(state, dataclasses.asdict(signal), value) for state, signal, value in buys],
                   'preexisting_defensive': sorted(preexisting),
                   'receipt': receipt, 'actions': action_identity(actions),
                   'counterfactuals': variants})
    return receipt, actions


def apply(self, states, date, assets, peak, events, **kwargs):
    active['defensive'] = {
        (state_index, signal.symbol, signal.strategy_name)
        for state_index, state in enumerate(states)
        for signal, strategy in state.pending
        if signal.direction == 'sell' and strategy is None
    }
    try:
        return _original_apply(self, states, date, assets, peak, events, **kwargs)
    finally:
        active.clear()


def save_pickle(name, value):
    path = OUT / name
    with path.open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', mtime=0) as stream:
        pickle.dump(value, stream, protocol=5)
    return {'file': name, 'bytes': path.stat().st_size,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


if __name__ == '__main__':
    snapshot_manifest = os.environ.get('TRADE_BASELINE_MANIFEST')
    if snapshot_manifest:
        manifest_bytes = Path(snapshot_manifest).read_bytes()
        if hashlib.sha256(manifest_bytes).hexdigest() != '372ad22de3871efb2662c96bba8ea739868bd1f64ef8622aef3d202571a6f818':
            raise RuntimeError('unverified baseline export manifest')
        manifest = json.loads(manifest_bytes)
        if manifest['source_revision'] != BASE:
            raise RuntimeError('snapshot does not match pinned baseline')
        for entry in manifest['entries']:
            local = PROJECT_ROOT / entry['path']
            if not local.resolve().is_relative_to(PROJECT_ROOT.resolve()):
                raise RuntimeError('unsafe snapshot member')
            data = local.read_bytes()
            oid = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
            if (len(data) != entry['bytes']
                    or hashlib.sha256(data).hexdigest() != entry['sha256']
                    or oid != entry['git_blob']):
                raise RuntimeError(f"snapshot source/data drift: {entry['path']}")
        fingerprint_text = json.dumps(manifest, sort_keys=True, indent=2)
    else:
        actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
        if actual != BASE:
            raise RuntimeError(f'probe must run exact baseline, not {actual}')
        subprocess.run(['git', 'diff', '--exit-code', BASE, '--', 'quantfusion', 'data'],
                       check=True, stdout=subprocess.DEVNULL)
        fingerprint_text = subprocess.check_output(
            ['git', 'ls-tree', '-r', 'HEAD', '--', 'quantfusion', 'data'], text=True)
    codes = next(codes for codes in UNIVERSES.values() if len(codes) == 17)
    budget.plan_account_risk_budget = plan
    EnsembleAllocationMixin._apply_account_risk_budget = apply
    try:
        with (OUT / 'replay.log').open('w') as log, contextlib.redirect_stdout(log):
            result = ProductionReplayEngine(2_000_000).run(
                {code: NAMES[code] for code in codes},
                '2025-04-01', '2026-07-20', data_dir=str(DATA_DIR), regime_data_dir=str(REGIME_DATA_DIR),
                indicator_state='warm')
    finally:
        budget.plan_account_risk_budget = _original_plan
        EnsembleAllocationMixin._apply_account_risk_budget = _original_apply
    raw_identity = save_pickle('B0-result.pkl.gz', result)
    trace_identity = save_pickle('B0-budget-trace.pkl.gz', traces)
    trades = result['trades']
    if isinstance(trades, pd.DataFrame):
        rows = trades.to_dict('records')
    else:
        rows = [dataclasses.asdict(t) if dataclasses.is_dataclass(t) else dict(t) for t in trades]
    fills = len(rows)
    buckets = len({(str(t['date']), t['symbol'], t['direction']) for t in rows})
    days = len({str(t['date']) for t in rows})
    fees = sum(float(t['commission']) + float(t['stamp_duty_cost']) for t in rows)
    notional = sum(float(t['gross_value']) for t in rows)
    slip = 0.001
    slippage = sum(float(t['gross_value']) * slip / (1 + slip if t['direction'] == 'buy' else 1 - slip) for t in rows)
    metrics = {'wealth': 1 + float(result['total_return']),
               'max_drawdown': abs(float(result['max_drawdown'])),
               'buckets': buckets, 'operation_days': days, 'fills': fills,
               'fees': fees, 'modeled_slippage': slippage,
               'gross_traded_notional': notional,
               'two_way_notional_over_initial_capital': notional / 2_000_000}
    assessments = {}
    for label in ('preexisting_defensive', 'all_planned_defensive'):
        entries = [(trace['date'], trace['counterfactuals'][label]) for trace in traces
                   if label in trace['counterfactuals']]
        changes = [(date, v) for date, v in entries if v['changed_eligible_buys']]
        assessments[label] = {'overlap_days': len(entries),
                              'changed_eligible_days': len(changes),
                              'all_required_actions_identical': all(v['required_actions_identical'] for _, v in entries),
                              'changed_rows': changes}
    (OUT / 'source-data-git-objects.txt').write_text(fingerprint_text)
    summary = {'source_revision': BASE, 'instrumentation_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               'python': platform.python_version(), 'pandas': pd.__version__,
               'snapshot_manifest_sha256': (hashlib.sha256(manifest_bytes).hexdigest()
                                            if snapshot_manifest else None),
               'engine': 'ProductionReplayEngine', 'window': ['2025-04-01', '2026-07-20'],
               'capital': 2_000_000, 'indicator_state': 'warm', 'codes': codes,
               'metrics': metrics, 'assessments': assessments,
               'raw': [raw_identity, trace_identity], 'status': 'read_only_diagnostic'}
    (OUT / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    print(json.dumps({'metrics': metrics, 'assessments': {k: {f: v for f, v in a.items() if f != 'changed_rows'} for k, a in assessments.items()}}, ensure_ascii=False, allow_nan=False))
    if not (math.isclose(metrics['wealth'], 9.610542744515504, rel_tol=1e-9)
            and math.isclose(metrics['max_drawdown'], 0.15104978428469945, rel_tol=1e-9)
            and buckets == 190 and fills == 645):
        raise RuntimeError('B0 fails existing release metric identity; do not select any candidate')
