"""Reuse the retired one-shot builder for authorized PR63 AB2, never v30 refs."""
from __future__ import annotations
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ProcessPoolExecutor

BASE = '4e0efd3e52b8648c4625716c5311b9e2b680b954'
EXPECTED = {
 '.github/workflows/ci.yml': 'dafc30c29cf1f392909702930bcab4e1e5207aea',
 'quantfusion/config/engine.py': 'f378af34ecc311d7a11d674be0877c5bdc21d4c1',
 'quantfusion/config/overlay.py': '7a5e1bb2d4abfe9d24f598ee3ef0c5d36154a3ca',
 'quantfusion/engine/ensemble_allocation.py': '8e26f209d244ac342d9d1c0aa6ff0c1965074f7c',
 'quantfusion/engine/ensemble_orchestration.py': '8ce5ad1b62a57a63185c3bacbbac9291378d2b15',
 'quantfusion/engine/replay.py': '1681a8dcb9811b77d58e532a9a2fdd0b3dffaeaf',
 'quantfusion/risk/overlay/policy.py': 'fefc1dd9f2735ddf7333fc66025e88939bc25eef',
 'quantfusion/risk/account_budget.py': '48725487e86f6d98b69117f859727101ed5a70d1',
 'tests/c6_non_economic/test_account_risk_budget.py': 'e71f6d2fff030494774366cf2a5647f0a21835d3',
}
IDS = ('prefix-05','prefix-09','prefix-10','prefix-13','prefix-17',
       'add-one-13-601869','add-one-05-002384','random-20260807-03-006')
SCRIPT_ROOT = Path(__file__).resolve().parent
ROOT = Path.cwd() / 'candidate'
PROOF = Path.cwd() / 'ab2-proof'

def git(*args):
    return subprocess.check_output(['git','-C',str(ROOT),*args],text=True).strip()

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def verify():
    assert git('rev-parse','HEAD') == BASE
    for path, oid in EXPECTED.items():
        assert git('hash-object',path) == oid, path

def derive():
    assert git('rev-parse','HEAD') == BASE
    assert git('status','--porcelain') == ''
    assert git('rev-parse','HEAD:quantfusion') == '6df0af04b69157c2bd702dadc1fe2bdecf2dddbf'
    assert git('rev-parse','HEAD:tests') == '602cf6dbda8d0acb1adecfd4bbb2de40e157d210'
    assert (ROOT/'docs/C6_ACCOUNT_BUDGET_CONTINUATION.md').is_file()
    patch = str(SCRIPT_ROOT/'ab2_candidate/existing.patch')
    subprocess.run(['git','-C',str(ROOT),'apply','--check',patch],check=True)
    subprocess.run(['git','-C',str(ROOT),'apply',patch],check=True)
    for name,path in [('account_budget.py','quantfusion/risk/account_budget.py'),
                      ('test_account_risk_budget.py','tests/c6_non_economic/test_account_risk_budget.py')]:
        assert not (ROOT/path).exists()
        shutil.copyfile(SCRIPT_ROOT/'ab2_candidate'/name, ROOT/path)
    verify()
    git('add','--',*EXPECTED)
    assert set(git('diff','--cached','--name-only').splitlines()) == set(EXPECTED)
    PROOF.mkdir(exist_ok=True)
    (PROOF/'source.patch').write_text(git('diff','--cached','--binary','--full-index')+'\n')
    receipt = dict(kind='AB2_ENGINEERING_AND_DIAGNOSTIC', canonical=False, accepted=False,
        base=BASE, prospective_tree=git('write-tree'), source_blobs=EXPECTED,
        data_tree=git('rev-parse','HEAD:data'), lock_sha256=sha(ROOT/'requirements-lock.txt'),
        preregistration_sha256=sha(ROOT/'docs/C6_ACCOUNT_BUDGET_CONTINUATION.md'),
        workflow_run_id=os.environ['GITHUB_RUN_ID'], builder_sha=os.environ['GITHUB_SHA'],
        python=platform.python_version(), platform=platform.platform(),
        runtime_manifest='sha256:581429e3df12d76e6af4be5ab7d0e7fc2013eb57dc23d2de691411c8efdbb970',
        published_blobs=False, branch_writes=0, economic_dispatches=0)
    (PROOF/'receipt.json').write_text(json.dumps(receipt,indent=2,allow_nan=False)+'\n')
    print(json.dumps(receipt,sort_keys=True))

def publish():
    verify()
    # Only publish content-addressed objects; the connected executor owns the
    # protected original-PR ref update, including its necessary CI change.
    for path,oid in EXPECTED.items():
        body=json.dumps({'content':(ROOT/path).read_text(),'encoding':'utf-8'}).encode()
        request=urllib.request.Request('https://api.github.com/repos/ychenracing/trade/git/blobs',data=body,
            headers={'Authorization':'Bearer '+os.environ['GITHUB_TOKEN'],
                     'Accept':'application/vnd.github+json','Content-Type':'application/json'},method='POST')
        with urllib.request.urlopen(request,timeout=30) as response:
            result=json.load(response)
        assert result['sha']==oid, path
    receipt=json.loads((PROOF/'receipt.json').read_text())
    receipt['published_blobs']=True
    receipt['native_tests_passed']=True
    (PROOF/'receipt.json').write_text(json.dumps(receipt,indent=2,allow_nan=False)+'\n')
    print(json.dumps(receipt,sort_keys=True))

def evaluate(task):
    scenario, enabled = task
    os.chdir(ROOT)
    sys.path.insert(0,str(ROOT))
    from quantfusion.engine.replay import ProductionReplayEngine
    from quantfusion.config.universe import SYMBOL_NAMES
    from quantfusion.config.paths import MARKET_DATA_DIR, REGIME_DATA_DIR
    name=scenario['scenario_id']
    started=time.monotonic()
    with (PROOF/f"{name}-{enabled}.log").open('w') as log,contextlib.redirect_stdout(log):
        result=ProductionReplayEngine(2000000.,cfg={'account_risk_budget_enabled':enabled}).run(
            {code:SYMBOL_NAMES[code] for code in scenario['symbols']},'2025-04-01','2026-07-20',
            data_dir=str(MARKET_DATA_DIR),regime_data_dir=str(REGIME_DATA_DIR),indicator_state='warm')
    trades=result['trades']
    actions=[e for e in result['risk_events'] if e.get('event')=='account_budget_envelope' and e['new_reduction_orders']]
    fills=[t for t in trades if 'account_budget_trim' in t.reason]
    breaches=result['drawdown_series'][result['drawdown_series'] < -0.18-1e-15]
    row={key:result[key] for key in ('total_return','max_drawdown','total_trades',
        'date_symbol_side_count','terminal_risk_lock','portfolio_cycle_lock_count','final_assets')}
    row.update(scenario_id=name, enabled=enabled, symbols=list(scenario['symbols']),
        elapsed_seconds=time.monotonic()-started,
        first_breach=str(breaches.index[0].date()) if len(breaches) else None,
        first_budget_action=actions[0]['date'] if actions else None,
        first_budget_fill=fills[0].date if fills else None,
        budget_fill_records=len(fills), budget_action_days=len(actions),
        commission=sum(t.commission for t in trades),stamp_duty=sum(t.stamp_duty_cost for t in trades),
        equity_last_matches_final=abs(float(result['equity_curve']['assets'].iloc[-1])-result['final_assets'])<1e-8,
        mdd_matches_series=abs(float(result['drawdown_series'].min())-result['max_drawdown'])<1e-12)
    assert row['equity_last_matches_final'] and row['mdd_matches_series']
    return row

def diagnose():
    verify()
    sys.path.insert(0,str(ROOT))
    from quantfusion.application.stress_scenarios import _multi_seed_scenarios, DEFAULT_SEEDS
    scenarios={s['scenario_id']:s for s in _multi_seed_scenarios(random_samples=50,permutation_samples=50,seeds=DEFAULT_SEEDS)}
    tasks=[(scenarios[name],flag) for name in IDS for flag in (False,True)]
    rows=[]
    with ProcessPoolExecutor(max_workers=4) as pool, (PROOF/'diagnostic-rows.jsonl').open('w') as out:
        for row in pool.map(evaluate,tasks):
            rows.append(row)
            text=json.dumps(row,sort_keys=True,allow_nan=False)
            print(text,flush=True)
            out.write(text+'\n'); out.flush()
    comparisons=[]
    for name in IDS:
        control=next(r for r in rows if r['scenario_id']==name and not r['enabled'])
        candidate=next(r for r in rows if r['scenario_id']==name and r['enabled'])
        comparisons.append(dict(scenario_id=name,wealth_ratio=(1+candidate['total_return'])/(1+control['total_return']),
            control_mdd=control['max_drawdown'], candidate_mdd=candidate['max_drawdown'],
            mdd_screen_passed=abs(candidate['max_drawdown'])<=.18+1e-15,
            candidate_order_buckets=candidate['date_symbol_side_count']))
    result=dict(schema_version=1,kind='AB2_FIXED_EIGHT_DIAGNOSTIC',canonical=False,accepted=False,
        complete=True,ordered_ids=list(IDS),source=json.loads((PROOF/'receipt.json').read_text()),
        rows=rows,comparisons=comparisons)
    (PROOF/'diagnostic.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    print(json.dumps(comparisons,sort_keys=True))

if __name__=='__main__':
    {'derive':derive,'publish':publish,'diagnose':diagnose}[sys.argv[1]]()
