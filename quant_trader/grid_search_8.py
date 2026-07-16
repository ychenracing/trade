#!/usr/bin/env python3
"""8只标的网格搜索 — 聚焦核心参数"""
import sys, os, time, itertools, pickle
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from a_share_turtle_v11 import TurtleConfig, TurtleSystem, run_backtest
from dataclasses import replace

CAPITAL = 2_000_000
NAMES = {
    "300308.SZ":"中际旭创","300502.SZ":"新易盛","300394.SZ":"天孚通信",
    "688008.SH":"澜起科技","603986.SH":"兆易创新","002409.SZ":"雅克科技",
    "300054.SZ":"鼎龙股份","688535.SH":"华海诚科",
}

def build_config(p):
    sy1 = TurtleSystem(name="S1", entry_window=p["s1"][0], exit_window=p["s1"][1], risk_fraction=p["rf"])
    sy2 = TurtleSystem(name="S2", entry_window=55, exit_window=20, risk_fraction=p["rf"])
    return TurtleConfig(
        initial_capital=CAPITAL, max_drawdown=p["dd"],
        atr_stop_multiple=p["atr"], max_symbol_weight=p["sym"],
        max_total_stock_weight=0.98, max_units_per_symbol=6,
        pyramid_add_atr=0.3, risk_off_cooldown_days=5,
        systems=[sy1, sy2],
    )

def run_one(data_map, p):
    cfg = build_config(p)
    try:
        r = run_backtest(data_map, cfg)
        s = r.summary
        return {
            "total_return": s.get("total_return", 0),
            "annual_return": s.get("annual_return", 0),
            "max_drawdown": s.get("max_drawdown", 0),
            "calmar_calc": s.get("calmar_calc", s.get("annual_return",0)/abs(s.get("max_drawdown",1))) if s.get("max_drawdown",0)!=0 else 0,
            "win_rate": s.get("win_rate", 0),
            "num_trades": s.get("num_trades", 0),
            "param_str": f"ATR={p['atr']} sym={p['sym']} rf={p['rf']} S1={p['s1']} dd={p['dd']}",
        }
    except Exception as e:
        return None

def main():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_cache_8.pkl"), "rb") as f:
        data_map = pickle.load(f)
    
    print(f"标的: {', '.join(NAMES.values())} ({len(data_map)}只)")
    
    grids = {
        "s1": [(10,5), (20,15), (25,20)],
        "atr": [3.0, 5.0, 10.0],
        "sym": [0.25, 0.50, 0.95],
        "rf": [0.05, 0.10],
        "dd": [0.25, 0.99],
    }
    keys = list(grids.keys())
    combos = list(itertools.product(*[grids[k] for k in keys]))
    total = len(combos)
    print(f"参数组合: {total}个\n")
    
    results = []
    t0 = time.time()
    for i, vals in enumerate(combos):
        p = dict(zip(keys, vals))
        r = run_one(data_map, p)
        if r:
            results.append(r)
        if (i+1) % 20 == 0 or i == total-1:
            elapsed = time.time() - t0
            eta = (total-i-1) / ((i+1)/elapsed) if elapsed > 0 else 0
            print(f"  {i+1}/{total} ({(i+1)/total*100:.0f}%) ETA {eta:.0f}s", flush=True)
    
    # 排序
    results.sort(key=lambda x: x["calmar_calc"], reverse=True)
    
    print("\n" + "="*100)
    print("TOP 15 — Calmar比排序")
    print("="*100)
    for i, r in enumerate(results[:15]):
        print(f"#{i+1}: Calmar {r['calmar_calc']:.2f} | 收益 {r['total_return']*100:.1f}% | 回撤 {r['max_drawdown']*100:.1f}% | 年化 {r['annual_return']*100:.1f}% | 胜率 {r['win_rate']*100:.0f}% | {r['param_str']}")
    
    # sym=0.5 专项
    r50 = [r for r in results if "sym=0.5" in r["param_str"]]
    r50.sort(key=lambda x: x["calmar_calc"], reverse=True)
    print("\n" + "="*100)
    print("TOP 10 — sym=0.5 专项")
    print("="*100)
    for i, r in enumerate(r50[:10]):
        print(f"#{i+1}: Calmar {r['calmar_calc']:.2f} | 收益 {r['total_return']*100:.1f}% | 回撤 {r['max_drawdown']*100:.1f}% | 年化 {r['annual_return']*100:.1f}% | 胜率 {r['win_rate']*100:.0f}% | {r['param_str']}")
    
    # sym对比
    print("\n" + "="*100)
    print("sym对比（每组最优Calmar）")
    print("="*100)
    for sym in [0.25, 0.50, 0.95]:
        rs = [r for r in results if f"sym={sym}" in r["param_str"]]
        if rs:
            b = max(rs, key=lambda x: x["calmar_calc"])
            print(f"sym={sym}: 收益{b['total_return']*100:.1f}% 回撤{b['max_drawdown']*100:.1f}% Calmar{b['calmar_calc']:.2f} 胜率{b['win_rate']*100:.0f}% | {b['param_str']}")
    
    # 存CSV
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "v11_grid_8")
    os.makedirs(out_dir, exist_ok=True)
    pd.DataFrame(results).to_csv(os.path.join(out_dir, "grid_search_8.csv"), index=False, encoding="utf-8-sig")
    print(f"\n结果已保存: {out_dir}/grid_search_8.csv")

if __name__ == "__main__":
    main()
