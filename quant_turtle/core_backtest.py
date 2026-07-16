"""核心仓回测（验证代码审查后策略未被改坏）。

窗口：2025-01-01 ~ 2026-06-30（真实 qfq 数据）。
参数：turbo —— 海龟(entry=10/exit=70/stop=4/units=8/rt=7.5%) + 均线(MA20/MA60)，各 50% 资金。
标的：核心持仓（中际旭创/德科立/雅克科技/天孚通信/鼎龙股份）+ 重点跟踪（新易盛）。
输出：每只标的收益率、最大回撤、成交、买入持有基准；以及核心仓等权组合账户级收益/回撤。
"""
import sys, warnings
sys.path.insert(0, "/workspace")
warnings.filterwarnings("ignore")
import pandas as pd
from dataclasses import replace

from quant_turtle.config import Config
from quant_turtle.backtest import run_backtest_multi, compute_metrics
from quant_turtle.strategies.turtle import TurtleStrategy
from quant_turtle.strategies.ma_trend import MATrendStrategy
from quant_turtle.data_feed import load_daily

START, END = "20250101", "20260630"
# 核心持仓 + 重点跟踪
CORE = {
    "300308": "中际旭创(持)", "688205": "德科立(持)", "002409": "雅克科技(持)",
    "300394": "天孚通信(持)", "300054": "鼎龙股份(持)", "300502": "新易盛(跟踪)",
}

def buyhold(df, cfg):
    slip, comm, duty = cfg.slippage, cfg.commission, cfg.stamp_duty
    buy = df["open"].iloc[0] * (1 + slip)
    shares = int(cfg.initial_capital / buy // 100) * 100
    cost = shares * buy * (1 + comm)
    eq = shares * df.set_index("date")["close"] + (cfg.initial_capital - cost)
    return eq.iloc[-1] / cfg.initial_capital - 1.0

def make_cfg():
    cfg = replace(Config(), max_total_risk=0.99, stop_multiple=4.0,
                  max_units=8, risk_per_trade=0.075)
    cfg._entry, cfg._exit = 10, 70
    return cfg

def load(code):
    return load_daily(code, START, END, adjust="qfq", use_cache=False)

def run_one(code, cfg):
    df = load(code)
    strs = [TurtleStrategy(code, cfg.initial_capital*0.5, cfg, 10, 70),
            MATrendStrategy(code, cfg.initial_capital*0.5, cfg)]
    res = run_backtest_multi({code: df}, cfg, strategies=strs, donchian_periods={5,10,20,10,70})
    m = compute_metrics(res, cfg)
    return m, buyhold(df, cfg), df

def main():
    cfg = make_cfg()
    print("=" * 92)
    print(f"  核心仓回测  窗口 {START}~{END}  turbo: 海龟(10/70/止损4ATR/8单位) + 均线(MA20/MA60)")
    print("=" * 92)
    print(f"{'标的':14s} {'收益%':>9s} {'回撤%':>8s} {'双达标':>5s} {'成交':>5s} {'买持%':>9s} {'期末权益':>14s}")
    print("-" * 92)
    rows = []
    for code, name in CORE.items():
        m, bh, df = run_one(code, cfg)
        dual = "✅" if m["total_return"] >= 8 and m["max_drawdown"] > -0.20 else "❌"
        print(f"  {name:14s} {m['total_return']*100:8.1f}  {m['max_drawdown']*100:7.1f}  {dual:>5s} "
              f"{m['n_buy_orders']+m['n_sell_orders']:5d} {bh*100:8.1f}  {m['final_equity']:>14,.0f}")
        rows.append((code, name, m["total_return"], m["max_drawdown"], bh))
    print("-" * 92)
    # 核心仓等权组合（共享现金池，各标的 50/50 海龟+均线，按标的数平分资本）
    n = len(CORE)
    data = {c: load(c) for c in CORE}
    strs = []
    for c in CORE:
        strs.append(TurtleStrategy(c, cfg.initial_capital*0.5/n, cfg, 10, 70))
        strs.append(MATrendStrategy(c, cfg.initial_capital*0.5/n, cfg))
    res = run_backtest_multi(data, cfg, strategies=strs, donchian_periods={5,10,20,10,70})
    mc = compute_metrics(res, cfg)
    print(f"  {'核心仓等权组合('+str(n)+'只)':14s} {mc['total_return']*100:8.1f}  {mc['max_drawdown']*100:7.1f}  "
          f"{'✅' if mc['total_return']>=8 and mc['max_drawdown']>-0.20 else '❌':>5s} "
          f"{mc['n_buy_orders']+mc['n_sell_orders']:5d} {'—':>9s}  {mc['final_equity']:>14,.0f}")
    print("=" * 92)
    print("说明：单只回测为 200 万满仓 turbo 口径（与历史报告一致），便于横向对比；")
    print("      组合行为各标的平分资本的账户级结果（稀释效应已在此前报告中论证）。")

if __name__ == "__main__":
    main()
