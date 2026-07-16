"""单只标的回测入口（默认中际旭创 300308，2025-01~2026-07 真实数据）。

用法：
    python3.11 run_single.py                 # 默认 aggressive 预设
    python3.11 run_single.py balanced
    python3.11 run_single.py all --start 20250101 --end 20260708
    python3.11 run_single.py sweep           # 参数扫描，找 >800% 的配置

特点：全部 200 万资金压在单只标的上（无杠杆、T+1、qfq），海龟+均线共享现金池。
"""
import sys
import argparse
from dataclasses import replace
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, "/workspace")
from quant_turtle.config import Config
from quant_turtle.data_feed import load_daily
from quant_turtle.backtest import run_backtest_multi, compute_metrics
from quant_turtle.strategies.turtle import TurtleStrategy
from quant_turtle.strategies.ma_trend import MATrendStrategy

SYM = "300308"
NAME = "中际旭创"
START, END = "20250101", "20260708"

PRESETS = {
    "conservative": dict(entry=10, exit=80, stop=4.0, units=6, rt=0.02, halt=0.99),
    "balanced":     dict(entry=10, exit=70, stop=4.0, units=6, rt=0.02, halt=0.99),
    "aggressive":   dict(entry=10, exit=80, stop=4.0, units=6, rt=0.03, halt=0.99),
    # 单只中际旭创专用：提高风险预算，在回撤<20% 内把收益推过 800%
    "boost":        dict(entry=10, exit=70, stop=4.0, units=8, rt=0.06, halt=0.99),
    "turbo":        dict(entry=10, exit=70, stop=4.0, units=8, rt=0.075, halt=0.99),
    # 波动率目标化：用动态缩仓压低高波动回撤（新易盛最低回撤方案，收益约 500%）
    "vtarget":      dict(entry=10, exit=70, stop=4.0, units=8, rt=0.075, halt=0.99, vt=0.040),
    "vtarget2":     dict(entry=10, exit=70, stop=4.0, units=8, rt=0.075, halt=0.99, vt=0.035),
}


def load_all(start=START, end=END, sym=SYM):
    df = load_daily(sym, start, end, adjust="qfq")
    nm = {"300308": "中际旭创", "300502": "新易盛", "300394": "天孚通信"}.get(sym, sym)
    print(f"  {nm}({sym}) 数据 {df['date'].min().date()} ~ {df['date'].max().date()}  共 {len(df)} 交易日")
    return df


def build_strategies(cfg, sym):
    recipes = [{"kind": "turtle", "entry": cfg._entry, "exit": cfg._exit, "w": 0.5},
               {"kind": "ma", "w": 0.5}]
    total_w = sum(r["w"] for r in recipes)
    s = []
    for r in recipes:
        w = r["w"] / total_w
        cap = cfg.initial_capital * w   # 单标的：每策略拿满 50% 总资金
        if r["kind"] == "turtle":
            s.append(TurtleStrategy(sym, cap, cfg, r["entry"], r["exit"]))
        else:
            s.append(MATrendStrategy(sym, cap, cfg))
    return s


def buyhold_benchmark(df, cfg):
    slip, comm, duty = cfg.slippage, cfg.commission, cfg.stamp_duty
    buy = df["open"].iloc[0] * (1 + slip)
    shares = int(cfg.initial_capital / buy // 100) * 100
    cost = shares * buy * (1 + comm)
    eq = shares * df.set_index("date")["close"] + (cfg.initial_capital - cost)
    return eq, shares


def run_preset(name, df, sym, trail=None, vt=None):
    p = PRESETS[name]
    cfg = replace(Config(), universe=[sym], max_total_risk=p["halt"],
                  stop_multiple=p["stop"], max_units=p["units"], risk_per_trade=p["rt"])
    if trail is not None:
        cfg.trail_multiple = trail
    if vt is not None:
        cfg.target_atr_pct = vt
    elif "vt" in p:
        cfg.target_atr_pct = p["vt"]
    cfg._entry, cfg._exit = p["entry"], p["exit"]
    periods = {5, 10, 20, p["entry"], p["exit"]}
    strs = build_strategies(cfg, sym)
    res = run_backtest_multi({sym: df}, cfg, strategies=strs, donchian_periods=periods)
    m = compute_metrics(res, cfg)
    eqc = res["equity_curve"]
    if "cash" in eqc.columns and len(eqc) > 0:
        m["avg_invested"] = float((1 - (eqc["cash"] / eqc["equity"]).mean()))
    else:
        m["avg_invested"] = float("nan")
    return cfg, m, res


def main():
    ap = argparse.ArgumentParser(description="单只标的回测（默认中际旭创）")
    ap.add_argument("mode", nargs="?", default="aggressive")
    ap.add_argument("--start", default=START)
    ap.add_argument("--end", default=END)
    ap.add_argument("--trail", type=float, default=None)
    ap.add_argument("--sym", default=SYM, help="标的代码（默认 300308 中际旭创）")
    args = ap.parse_args()

    start, end = args.start, args.end
    sym = args.sym
    name = {"300308": "中际旭创", "300502": "新易盛", "300394": "天孚通信"}.get(sym, sym)

    df = load_all(start, end, sym)
    out_dir = "/workspace/quant_turtle/results"
    import os
    os.makedirs(out_dir, exist_ok=True)

    bh, _ = buyhold_benchmark(df, Config())
    bh.to_frame("buyhold").to_csv(f"{out_dir}/equity_buyhold_single_{sym}.csv")

    if args.mode == "sweep":
        sweep(df, sym)
        return
    if args.mode == "vsweep":
        vsweep(df, sym)
        return

    modes = list(PRESETS) if args.mode == "all" else [args.mode]
    for mode in modes:
        cfg, m, res = run_preset(mode, df, sym, args.trail)
        res["equity_curve"].to_csv(f"{out_dir}/equity_single_{sym}_{mode}.csv")
        pd.DataFrame([f.__dict__ for f in res["fills"]]).to_csv(
            f"{out_dir}/fills_single_{sym}_{mode}.csv", index=False)
        print("=" * 64)
        print(f"单只回测 [{mode}]  {name}({sym})  {start[:4]}-{start[4:6]}-{start[6:]} ~ {end[:4]}-{end[4:6]}-{end[6:]}")
        print("=" * 64)
        print(f"  参数: 入场/离场={cfg._entry}/{cfg._exit}, 止损={cfg.stop_multiple:.1f}×ATR, "
              f"加仓≤{cfg.max_units}, 风险={cfg.risk_per_trade*100:.0f}%, 吊灯={cfg.trail_multiple}, "
              f"波动率目标={cfg.target_atr_pct}")
        print(f"  组合收益   = {m['total_return']*100:.1f}%  目标800%: {'✅' if m['total_return']>=8.0 else '❌'}")
        print(f"  最大回撤   = {m['max_drawdown']*100:.1f}%  {'✅<20%' if m['max_drawdown']>-0.20 else '❌≥20%'}")
        print(f"  期末权益   = {m['final_equity']:,.0f} 元 (起点 2,000,000)")
        print(f"  平均仓位   = {m.get('avg_invested', float('nan'))*100:.1f}%")
        print(f"  成交笔数   = {m['n_buy_orders']+m['n_sell_orders']}  熔断={m['halted']}")
        print(f"  买入持有基准 = {(bh.iloc[-1]/2_000_000-1)*100:.1f}%")
        # 绘图
        eq = res["equity_curve"]["equity"]
        plt.figure(figsize=(11, 5.5))
        plt.plot(eq.index, eq.values/2_000_000-1, label=f"策略({mode}) {m['total_return']*100:.0f}%", lw=2)
        plt.plot(bh.index, bh.values/2_000_000-1, label=f"买入持有 {(bh.iloc[-1]/2_000_000-1)*100:.0f}%", lw=1.5, ls="--", color="gray")
        plt.axhline(8.0, color="red", ls=":", lw=1, label="800% 目标线")
        plt.title(f"{name} 单只回测 [{mode}]：收益 {m['total_return']*100:.1f}% / 回撤 {m['max_drawdown']*100:.1f}%")
        plt.ylabel("累计收益率"); plt.legend(loc="upper left", fontsize=9)
        plt.grid(alpha=0.3); plt.tight_layout()
        chart_path = f"{out_dir}/equity_single_chart_{sym}_{mode}.png"
        plt.savefig(chart_path, dpi=120)
        print(f"  图表: {chart_path}")


def sweep(df, sym=SYM):
    entries=[10,20]; exits=[40,55,70,80]; stops=[2.5,3.0,4.0]; units=[6,8]; rts=[0.02,0.03]
    rows=[]
    for e in entries:
        for x in exits:
            for s in stops:
                for u in units:
                    for r in rts:
                        cfg=replace(Config(),universe=[sym],max_total_risk=0.99,stop_multiple=s,max_units=u,risk_per_trade=r)
                        cfg._entry,cfg._exit=e,x
                        res=run_backtest_multi({sym:df},cfg,strategies=build_strategies(cfg, sym),donchian_periods={5,10,20,e,x})
                        m=compute_metrics(res,cfg)
                        rows.append(dict(entry=e,exit=x,stop=s,units=u,rt=r,ret=m['total_return'],dd=m['max_drawdown'],final=m['final_equity']))
    d=pd.DataFrame(rows)
    d['ret_pct']=d['ret']*100; d['dd_pct']=d['dd']*100
    d.to_csv("/root/.codebuddy/artifact/sweep_single_results.csv",index=False)
    print(f"扫描 {len(d)} 组；收益≥800% 的配置数 = {(d['ret']>=8.0).sum()}")
    print("\n收益最高 Top10:")
    for _,r in d.sort_values('ret',ascending=False).head(10).iterrows():
        print(f"  入{r.entry}/离{r.exit} 止损{r.stop} 单位{r.units} 风险{r.rt}: 收益={r.ret_pct:.1f}%  回撤={r.dd_pct:.1f}%")
    print("\n回撤≤20% 且收益最高 Top10:")
    sub=d[d['dd']>-0.20].sort_values('ret',ascending=False)
    if sub.empty: print("  (无)" )
    else:
        for _,r in sub.head(10).iterrows():
            print(f"  入{r.entry}/离{r.exit} 止损{r.stop} 单位{r.units} 风险{r.rt}: 收益={r.ret_pct:.1f}%  回撤={r.dd_pct:.1f}%")
    print(f"\n全局收益上限={d['ret'].max()*100:.1f}% (回撤 {d.loc[d['ret'].idxmax(),'dd']*100:.1f}%)")


def vsweep(df, sym=SYM):
    """波动率目标化扫描：固定 turbo 基线参数，扫描 target_atr_pct，找 >800% 且 <20% 回撤的配置。"""
    vts=[0.0,0.040,0.045,0.050,0.055,0.060,0.070]
    rows=[]
    for vt in vts:
        cfg=replace(Config(),universe=[sym],max_total_risk=0.99,stop_multiple=4.0,max_units=8,risk_per_trade=0.075)
        cfg.target_atr_pct=vt
        cfg._entry,cfg._exit=10,70
        res=run_backtest_multi({sym:df},cfg,strategies=build_strategies(cfg, sym),donchian_periods={5,10,20,10,70})
        m=compute_metrics(res,cfg)
        eqc=res["equity_curve"]
        avg_inv=(1-(eqc["cash"]/eqc["equity"]).mean()) if "cash" in eqc.columns else float("nan")
        rows.append(dict(vt=vt,ret=m['total_return'],dd=m['max_drawdown'],final=m['final_equity'],avg=avg_inv,
                         trades=m['n_buy_orders']+m['n_sell_orders']))
    d=pd.DataFrame(rows)
    d['ret_pct']=d['ret']*100; d['dd_pct']=d['dd']*100; d['avg_pct']=d['avg']*100
    d.to_csv("/root/.codebuddy/artifact/vsweep_single_results.csv",index=False)
    print(f"波动率目标化扫描 {sym}（基线 entry=10/exit=70/stop=4/units=8/rt=7.5%）:")
    print(f"{'target%':>8} {'收益%':>10} {'回撤%':>9} {'平均仓位%':>11} {'成交':>6}  双达标")
    for _,r in d.iterrows():
        flag='✅' if (r['ret']>=8.0 and r['dd']>-0.20) else ''
        print(f"{r.vt*100:>8.1f} {r.ret_pct:>10.1f} {r.dd_pct:>9.1f} {r.avg_pct:>11.1f} {int(r.trades):>6}  {flag}")
    dual=d[(d['ret']>=8.0)&(d['dd']>-0.20)]
    print(f"\n双达标(>800% & <20%)配置数 = {len(dual)}")
    if not dual.empty:
        best=dual.sort_values('ret',ascending=False).iloc[0]
        print(f"  最优 target%={best.vt*100:.1f}  收益={best.ret_pct:.1f}%  回撤={best.dd_pct:.1f}%")


if __name__ == "__main__":
    main()
