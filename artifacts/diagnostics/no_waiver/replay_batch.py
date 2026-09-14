"""Run source-bound, noncanonical diagnosis through the unchanged production entry."""
from pathlib import Path
import argparse,contextlib,io,json,pickle,sys,time,subprocess,hashlib,collections
from concurrent.futures import ProcessPoolExecutor,as_completed
p=argparse.ArgumentParser();p.add_argument('--root',default='/mnt/data/trade');p.add_argument('--out',required=True);p.add_argument('--ids',nargs='+',required=True);p.add_argument('--workers',type=int,default=3);p.add_argument('--cfg',default='{}');args=p.parse_args()
sys.path.insert(0,args.root)
from quantfusion.engine.replay import ProductionReplayEngine
from quantfusion.application import stress_artifacts as a,stress_scenarios as s,stress_metrics as m
from quantfusion.application.stress import _diagnostic_telemetry,_reason_category
from quantfusion.config.paths import MARKET_DATA_DIR,REGIME_DATA_DIR
from quantfusion.config.universe import SYMBOL_NAMES
out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=args.root,text=True).strip()
fp=a._tree_fingerprint(a._source_files());df=a._tree_fingerprint(a._data_files(MARKET_DATA_DIR,REGIME_DATA_DIR));config=json.loads(args.cfg)
plan=s._multi_seed_scenarios(random_samples=50,permutation_samples=50,seeds=s.DEFAULT_SEEDS);byid={x['scenario_id']:x for x in plan}
ids=[sid for sid in args.ids if sid in byid];assert len(ids)==len(set(args.ids))
identity={'source_revision':revision,'source_fingerprint':fp,'data_fingerprint':df,'cfg_overrides':config,'diagnostic_noncanonical':True,'allow_publication':False,'scenario_ids':ids,'workers':args.workers,'python':sys.version}
identity_path=out/'identity.json'
if identity_path.exists() and json.loads(identity_path.read_text())!=identity:raise ValueError('Refuse mixed evidence identity')
identity_path.write_text(json.dumps(identity,indent=2)+'\n')
def run(sid):
 start=time.monotonic();scenario=byid[sid]
 with contextlib.redirect_stdout(io.StringIO()):
  r=ProductionReplayEngine(m.INITIAL_CAPITAL,cfg=config).run({c:SYMBOL_NAMES[c] for c in scenario['symbols']},m.START_DATE,m.END_DATE,data_dir=str(MARKET_DATA_DIR),regime_data_dir=str(REGIME_DATA_DIR),indicator_state='warm')
 raw=pickle.dumps(r);(out/(sid+'.pkl')).write_bytes(raw)
 attribution=collections.Counter(_reason_category(t) for t in r['trades'])
 row={**scenario,'symbol_count':len(scenario['symbols']),**{k:r[k] for k in ['total_return','max_drawdown','sharpe','calmar','total_trades','sleeve_fill_count','date_symbol_side_count','max_concurrent_symbols','terminal_risk_lock','deployment_policy']},'reason_attribution':{k:attribution[k] for k in m.ATTRIBUTION_CATEGORIES}}
 result={'result':row,'runtime_seconds':time.monotonic()-start,'raw_sha256':hashlib.sha256(raw).hexdigest(),'source_fingerprint':fp,'telemetry':_diagnostic_telemetry(r)}
 (out/(sid+'.json')).write_text(json.dumps(result,indent=2,allow_nan=False)+'\n');return row
if __name__=='__main__':
 pending=[sid for sid in ids if not (out/(sid+'.json')).exists()]
 with ProcessPoolExecutor(max_workers=args.workers) as ex:
  fs={ex.submit(run,sid):sid for sid in pending}
  for future in as_completed(fs):
   try:print(json.dumps(future.result(),allow_nan=False),flush=True)
   except Exception as e:print('FAILED',fs[future],repr(e),flush=True);raise
 results=[json.loads((out/(sid+'.json')).read_text())['result'] for sid in ids]
 (out/'results.json').write_text(json.dumps({'identity':identity,'results':results},indent=2,allow_nan=False)+'\n')
 print('COMPLETE',len(results),flush=True)
