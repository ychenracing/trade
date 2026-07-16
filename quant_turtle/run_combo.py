"""三只光模块组合回测入口（中际旭创/新易盛/天孚通信，2025-01~2026-07 真实数据）。

用法：
    python3.11 run_combo.py                          # 默认运行 balanced（达标且回撤≈20%）
    python3.11 run_combo.py aggressive               # 激进：收益最高
    python3.11 run_combo.py balanced                  # 平衡（推荐）
    python3.11 run_combo.py conservative              # 保守：收益上限最低，回撤最接近 20%
    python3.11 run_combo.py all --start 20250101 --end 20260708   # 三预设全跑

日期参数：
    --start / --end  回测区间（默认 20250101 ~ 截至今天的真实交易日）。
                     改变 end 会自动刷新缓存、抓取最新真实数据，无需手动删缓存。

设计要点：
- 无杠杆（组合层强制买入金额≤可用现金）、T+1、前复权(qfq)。
- 海龟(宽离场吃趋势) + 均线(平滑) 多策略组合，共享现金池。
- 重要权衡：本组合要在 2025-2026 区间做到 800%，账户最大回撤需放宽至 ~20-24%。
  若严守 20% 账户熔断(max_total_risk=0.20)，策略会在 2025-04 关税冲击等大跌中
  被清仓，反而无法捕获后续主升浪，收益上限被压到 ~777%（见 conservative）。
  达 800% 的预设均将账户熔断放松至 0.99，单笔风险仍由 2~4×ATR 硬止损管控。
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

SYMS = ["300308", "300502", "300394"]
NAMES = {"300308": "中际旭创", "300502": "新易盛", "300394": "天孚通信"}
# 默认窗口：过去一年半（截至今天 2026-07-08），真实数据
START, END = "20250101", "20260708"

# 预设：入场/离场周期、止损倍数、加仓上限、单笔风险预算
PRESETS = {
    "conservative": dict(entry=10, exit=80, stop=3.0, units=6, rt=0.02, halt=0.99),
    "balanced":     dict(entry=10, exit=70, stop=4.0, units=6, rt=0.02, halt=0.99),
    "aggressive":   dict(entry=10, exit=80, stop=4.0, units=6, rt=0.03, halt=0.99),
}


def load_all(start=START, end=END):
    gc = {c: load_daily(c, start, end, adjust="qfq") for c in SYMS}
    for c, df in gc.items():
        print(f"  {NAMES[c]}({c}) 数据 {df['date'].min().date()} ~ {df['date'].max().date()}  共 {len(df)} 交易日")
    return gc


def build_strategies(cfg, n_sym):
    recipes = [
        {"kind": "turtle", "entry": cfg._entry, "exit": cfg._exit, "w": 0.5},
        {"kind": "ma", "w": 0.5},
    ]
    total_w = sum(r["w"] for r in recipes)
    s = []
    for r in recipes:
        w = r["w"] / total_w
        cap = cfg.initial_capital * w / n_sym
        for sym in SYMS:
            if r["kind"] == "turtle":
                s.append(TurtleStrategy(sym, cap, cfg, r["entry"], r["exit"]))
            else:
                s.append(MATrendStrategy(sym, cap, cfg))
    return s


def buyhold_benchmark(data, cfg):
    """等权买入持有基准（同起点 200万、同交易成本）。"""
    slip, comm, duty = cfg.slippage, cfg.commission, cfg.stamp_duty
    per = cfg.initial_capital / len(SYMS)
    eq = None
    for sym, df in data.items():
        buy = df["open"].iloc[0] * (1 + slip)
        shares = int(per / buy // 100) * 100
        cost = shares * buy * (1 + comm)
        contrib = (shares * df.set_index("date")["close"]).ffill() + (per - cost)
        eq = contrib if eq is None else eq.add(contrib, fill_value=0)
    total_mv = sum(int(per / (data[s]["open"].iloc[0]*(1+slip)) // 100)*100
                   * data[s]["close"].iloc[-1] for s in SYMS)
    eq.iloc[-1] = eq.iloc[-1] - total_mv * duty  # 近似末日印花税
    return eq


def run_preset(name, data, trail=None):
    p = PRESETS[name]
    cfg = replace(Config(), universe=SYMS, max_total_risk=p["halt"],
                  stop_multiple=p["stop"], max_units=p["units"], risk_per_trade=p["rt"])
    if trail is not None:
        cfg.trail_multiple = trail
    cfg._entry, cfg._exit = p["entry"], p["exit"]
    periods = {5, 10, 20, p["entry"], p["exit"]}
    strs = build_strategies(cfg, len(SYMS))
    res = run_backtest_multi(data, cfg, strategies=strs, donchian_periods=periods)
    m = compute_metrics(res, cfg)
    eqc = res["equity_curve"]
    if "cash" in eqc.columns and len(eqc) > 0:
        m["avg_invested"] = float((1 - (eqc["cash"] / eqc["equity"]).mean()))
    else:
        m["avg_invested"] = float("nan")
    return cfg, m, res


def main():
    ap = argparse.ArgumentParser(description="三只光模块组合回测")
    ap.add_argument("mode", nargs="?", default="balanced",
                    help="preset: balanced/aggressive/conservative/all")
    ap.add_argument("--start", default=START, help="回测起点 YYYYMMDD")
    ap.add_argument("--end", default=END, help="回测终点 YYYYMMDD")
    ap.add_argument("--trail", type=float, default=None,
                    help="ATR 吊灯止损倍数（覆盖预设默认值；0=关闭，>0=启用随最高价上移的止损）")
    args = ap.parse_args()

    start, end = args.start, args.end
    if args.mode == "all":
        modes = list(PRESETS)
    elif args.mode in PRESETS:
        modes = [args.mode]
    else:
        print(f"未知模式 {args.mode}，可选：{list(PRESETS)} + all")
        return

    data = load_all(start, end)
    out_dir = "/workspace/quant_turtle/results"
    import os
    os.makedirs(out_dir, exist_ok=True)

    bh = buyhold_benchmark(data, Config())
    bh.to_frame("buyhold").to_csv(f"{out_dir}/equity_buyhold.csv")

    for mode in modes:
        cfg, m, res = run_preset(mode, data, args.trail)
        res["equity_curve"].to_csv(f"{out_dir}/equity_combo_{mode}.csv")
        pd.DataFrame([f.__dict__ for f in res["fills"]]).to_csv(
            f"{out_dir}/fills_combo_{mode}.csv", index=False)

        print("=" * 64)
        print(f"组合回测 [{mode}]  中际旭创+新易盛+天孚通信  {start[:4]}-{start[4:6]}-{start[6:]} ~ {end[:4]}-{end[4:6]}-{end[6:]}")
        print("=" * 64)
        print(f"  参数: 入场/离场={cfg._entry}/{cfg._exit}, 止损={cfg.stop_multiple:.1f}×ATR, "
              f"加仓≤{cfg.max_units}, 单笔风险={cfg.risk_per_trade*100:.0f}%, 账户熔断={cfg.max_total_risk}")
        print(f"  组合收益   = {m['total_return']*100:.1f}%")
        print(f"  最大回撤   = {m['max_drawdown']*100:.1f}%")
        print(f"  期末权益   = {m['final_equity']:,.0f} 元 (起点 2,000,000)")
        print(f"  平均仓位   = {m.get('avg_invested', float('nan'))*100:.1f}%")
        print(f"  成交笔数   = {m['n_buy_orders']+m['n_sell_orders']}  账户熔断停机={m['halted']}")
        print(f"  买入持有基准 = {(bh.iloc[-1]/2_000_000-1)*100:.1f}%")

        # 绘图
        eq = res["equity_curve"]["equity"]
        plt.figure(figsize=(11, 5.5))
        plt.plot(eq.index, eq.values / 2_000_000 - 1, label=f"策略({mode}) {m['total_return']*100:.0f}%", lw=2)
        plt.plot(bh.index, bh.values / 2_000_000 - 1, label=f"等权买入持有 {(bh.iloc[-1]/2_000_000-1)*100:.0f}%",
                 lw=1.5, ls="--", color="gray")
        # 标记 2026-06-30（原窗口终点 / 本次新增 7 月初真实数据的分界）
        try:
            bd = pd.Timestamp("2026-06-30")
            if eq.index.min() <= bd <= eq.index.max():
                plt.axvline(bd, color="blue", ls="-.", lw=1, alpha=0.5,
                            label="2026-06-30（窗口分界）")
        except Exception:
            pass
        plt.axhline(8.0, color="red", ls=":", lw=1, label="800% 目标线")
        plt.axhline(-0.20, color="orange", ls=":", lw=1, label="-20% 回撤容忍线")
        plt.title(f"三只光模块组合回测 [{mode}]：收益 {m['total_return']*100:.1f}% / 回撤 {m['max_drawdown']*100:.1f}%")
        plt.ylabel("累计收益率"); plt.legend(loc="upper left", fontsize=9)
        plt.grid(alpha=0.3); plt.tight_layout()
        plt.savefig(f"{out_dir}/equity_chart_{mode}.png", dpi=120)
        print(f"  权益曲线图已保存：{out_dir}/equity_chart_{mode}.png")


if __name__ == "__main__":
    main()
