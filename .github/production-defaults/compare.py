"""Native isolated reference comparison; no formal stress rerun or fitting."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys

BASE = '314254fe04a5fc8bc0fd9bc91bb3a2c2ed5af796'


def evaluate(root, case, explicit):
    sys.path.insert(0, str(Path(root).resolve()))
    os.chdir(root)
    from quantfusion.engine import BacktestEngine
    from quantfusion.engine.replay import RegimeAdaptiveBacktestEngine, ProductionReplayEngine
    from quantfusion.research.fingerprints import economic_sequence_fingerprints, _canonical_sequence_sha
    from tests.regression.test_quant_fusion import regime_route_fingerprint
    from scripts.backtest_universes import UNIVERSES, NAMES
    cfg = {'account_risk_budget_enabled': True} if explicit else None
    kwargs = {'data_dir': str(Path('data/market').resolve()), 'indicator_state': 'warm'}
    end = '2026-07-20'
    if case == 'adaptive':
        engine = RegimeAdaptiveBacktestEngine(2_000_000, cfg=cfg)
        codes = ['300308']
        kwargs['regime_data_dir'] = str(Path('data/regime').resolve())
        end = '2026-06-30'
    elif case == 'replay17':
        engine = ProductionReplayEngine(2_000_000, cfg=cfg)
        codes = next(v for v in UNIVERSES.values() if len(v) == 17)
        kwargs['regime_data_dir'] = str(Path('data/regime').resolve())
        kwargs['leader_data_dir'] = kwargs['data_dir']
    else:
        engine = BacktestEngine(2_000_000, cfg=cfg)
        codes = next(v for v in UNIVERSES.values() if len(v) == int(case))
    with contextlib.redirect_stdout(io.StringIO()):
        result = engine.run({code: NAMES[code] for code in codes}, '2025-04-01', end, **kwargs)
    metrics = {key: result[key] for key in ('total_return', 'max_drawdown', 'total_trades',
        'sell_trades','sleeve_fill_count','sleeve_sell_fill_count',
        'date_symbol_side_count','date_symbol_sell_side_count')}
    metrics.update(economic_sequence_fingerprints(result))
    metrics['regime_route_sha256'] = regime_route_fingerprint(result)
    output = {'metrics': metrics, 'equity_sha256': _canonical_sequence_sha(result['equity_curve']),
              'pending_sha256': _canonical_sequence_sha(result.get('pending_signals', []))}
    if not explicit:
        status = result['account_risk_budget']
        assert status['enabled'] is True and status['status'] == 'APPLIED', status
        assert status['evaluation_count'] == len(result['equity_curve']), (status['evaluation_count'], len(result['equity_curve']))
    print(json.dumps(output, sort_keys=True, allow_nan=False))


def compare(reference, candidate, output):
    reference, candidate, output = map(lambda p: Path(p).resolve(), (reference,candidate,output))
    output.mkdir(parents=True, exist_ok=True)
    cases = ['1','3','5','13','17','adaptive','replay17']
    def run(item):
        case, kind = item
        path = reference if kind == 'reference' else candidate
        cmd = [sys.executable, __file__, 'evaluate', str(path), case, str(kind == 'reference')]
        result = subprocess.run(cmd, capture_output=True, text=True)
        (output / f'{case}-{kind}.stderr').write_text(result.stderr)
        result.check_returncode()
        value = json.loads(result.stdout)
        (output / f'{case}-{kind}.json').write_text(json.dumps(value, sort_keys=True, indent=2)+'\n')
        print(case, kind, 'completed', flush=True)
        return (case, kind), value
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = dict(pool.map(run, [(c,k) for c in cases for k in ('reference','candidate')]))
    for case in cases:
        assert results[(case,'reference')] == results[(case,'candidate')], f'Actual AB5 drift: {case}'
    proof = {'kind': 'native_explicit_ab5_equals_production_defaults', 'reference_source': BASE,
             'cases': cases, 'all_equal': True, 'scope': 'orders/fills/equity/events/pending; no formal rerun',
             'patch_sha256': os.environ['PATCH_SHA256'],
             'results': {case: results[(case,'reference')] for case in cases}}
    proof_raw = (json.dumps(proof, sort_keys=True, indent=2, allow_nan=False)+'\n').encode()
    (output/'comparison.json').write_bytes(proof_raw)
    golden_path = candidate/'tests/fixtures/backtest_golden_metrics.json'
    old_raw = golden_path.read_bytes()
    old = json.loads(old_raw)
    golden = {case: results[(case,'reference')]['metrics'] for case in cases if case.isdigit()}
    golden['_adaptive_bull'] = {**results[('adaptive','reference')]['metrics'],
        'source_revision': BASE, 'engine': 'RegimeAdaptiveBacktestEngine',
        'symbols':['300308'], 'start_date':'2025-04-01','end_date':'2026-06-30'}
    golden['_previous_default'] = old
    golden['_source_binding'] = {'kind': proof['kind'], 'expected_from_source':BASE,
        'expected_config':{'account_risk_budget_enabled':True},
        'account_risk_budget_enabled':True, 'all_current_equal_frozen':True,
        'case_count':6, 'additional_replay_cases':1, 'initial_capital':2000000,
        'comparison_patch_sha256': os.environ['PATCH_SHA256'],
        'comparison_sha256':hashlib.sha256(proof_raw).hexdigest(),
        'source_workflow_run':int(os.environ['GITHUB_RUN_ID']),
        'original_golden_sha256':hashlib.sha256(old_raw).hexdigest(),
        'data_directory':'data/market','indicator_state':'warm',
        'start_date':'2025-04-01','end_date':'2026-07-20'}
    golden_path.write_text(json.dumps(golden, sort_keys=True, indent=2, allow_nan=False)+'\n')
    doc=candidate/'docs/VALIDATION.md'
    text=doc.read_text()
    table='''## 当前默认风险预算回归

正常命令默认启用 AB5。当前五池黄金预期来自不可变 main `314254fe04a5fc8bc0fd9bc91bb3a2c2ed5af796`
显式开启预算的独立回放；新默认入口在相同锁定环境逐项匹配完整成交、订单、净值与事件。
另验证单股自适应与 17 股生产回放，默认预算评估次数与真实权益采样日数一致。
旧关闭预算预期保留在黄金文件 `_previous_default`，原文件哈希和本次验证运行写入 `_source_binding`。
这不改写原 958 正式工件，不构成新增样本外证据；真实账户快照仅通过独立的时点建议测试。

| 股票数量 | 总收益 | 最大回撤 | 实际成交记录 | 日期/股票/方向桶 |
|---:|---:|---:|---:|---:|
'''
    for case in ['1','3','5','13','17']:
        m=golden[case]
        table+=f"| {case} | {m['total_return']*100:.6f}% | {m['max_drawdown']*100:.6f}% | {m['total_trades']} | {m['date_symbol_side_count']} |\n"
    text=text.replace('## 历史 C6 关闭预算的共享引擎回归', table+'\n## 历史 C6 关闭预算的共享引擎回归')
    doc.write_text(text)
    print('SEVEN PAIRS EXACTLY EQUAL; goldens from immutable reference, not candidate', flush=True)


if __name__ == '__main__':
    if sys.argv[1]=='evaluate':
        evaluate(sys.argv[2],sys.argv[3],sys.argv[4]=='True')
    else:
        compare(*sys.argv[2:5])
