import contextlib,io,json,pickle,sys,hashlib,dataclasses
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'trade-candidate'))
from quantfusion.engine.execution_flow import CoreExecutionMixin
from quantfusion.engine.replay import ProductionReplayEngine
from quantfusion.config.paths import MARKET_DATA_DIR,REGIME_DATA_DIR
from quantfusion.config.universe import SYMBOL_NAMES
rows=[]
original=CoreExecutionMixin._apply_buy_to_position

def observe(self,signal,strategy,date_str,shares,exec_price,total_cost):
 pos=self.positions.get(signal.symbol,{}).get(strategy.name)
 before=dataclasses.asdict(pos) if pos is not None else None
 original(self,signal,strategy,date_str,shares,exec_price,total_cost)
 if before:
  rows.append({'date':date_str,'symbol':signal.symbol,'strategy':strategy.name,'sleeve':getattr(self,'sleeve_name',''),'before':before,'after':dataclasses.asdict(strategy.position),'signal':dataclasses.asdict(signal),'added_shares':shares,'exec_price':exec_price,'stop_transfer':before['shares']*(strategy.position.stop_loss-before['stop_loss'])})
CoreExecutionMixin._apply_buy_to_position=observe
with contextlib.redirect_stdout(io.StringIO()):
 r=ProductionReplayEngine(2_000_000).run({c:SYMBOL_NAMES[c] for c in ['300308','300502','300394']},'2025-04-01','2026-07-20',data_dir=str(MARKET_DATA_DIR),regime_data_dir=str(REGIME_DATA_DIR),indicator_state='warm')
old=pickle.loads((Path(__file__).parent/'tradable-results/prefix-03.pkl').read_bytes())
for k in ['total_return','max_drawdown','date_symbol_side_count','final_assets']:
 assert r[k]==old[k],k
assert r['trades']==old['trades']
assert r['equity_curve'].equals(old['equity_curve'])
for row in rows:
 matching=[t for t in r['trades'] if t.date>=row['date'] and t.direction=='sell' and t.symbol==row['symbol'] and t.strategy_name==row['sleeve']+':'+row['strategy']]
 row['next_sell']=dataclasses.asdict(matching[0]) if matching else None
p=Path(__file__).parent/'add-stop-observations.json'
p.write_text(json.dumps({'status':'OBSERVATIONAL_EQUIVALENCE_VERIFIED','metrics_trades_equity_unchanged':True,'raw_input_sha256':hashlib.sha256((Path(__file__).parent/'tradable-results/prefix-03.pkl').read_bytes()).hexdigest(),'rows':rows},indent=2)+'\n')
print('observed',len(rows),'adds, metrics/trades/full equity identical')
for row in sorted(rows,key=lambda x:-x['stop_transfer'])[:12]:
 print(row['date'],row['symbol'],row['sleeve'],row['before']['shares'],row['added_shares'],round(row['before']['stop_loss'],2),round(row['after']['stop_loss'],2),round(row['stop_transfer']),row['next_sell'])
