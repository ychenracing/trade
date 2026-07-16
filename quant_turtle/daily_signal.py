"""每日收盘后策略信号（定时任务入口）。

对 13 只标的：抓取最新 qfq 日线（新浪源，北交所用 bj 前缀），将【海龟 turbo(entry=10/exit=40)】
与【均线(MA20/MA60)】两套策略严格按引擎时序重放到最新一根 bar，捕获 on_bar 在最新 bar 上
生成的订单——该订单将于【下一交易日开盘】撮合，即"后续策略动作"。

输出每只标的：当前持仓状态 + 海龟信号 + 均线信号 + 综合(买入/持有/卖出) + 触发价位。
同时落盘当日信号日志到 signals/ 目录。

用法：
    python3.11 daily_signal.py            # 用今天作为数据截止日
    python3.11 daily_signal.py 20260708   # 指定截止日（回溯验证信号逻辑用）
"""
import sys, os, warnings
sys.path.insert(0, "/workspace")
warnings.filterwarnings("ignore")
import akshare as ak
import pandas as pd
import numpy as np
from dataclasses import replace
from datetime import date

from quant_turtle.config import Config
from quant_turtle.strategies.turtle import TurtleStrategy
from quant_turtle.strategies.ma_trend import MATrendStrategy
from quant_turtle.indicators import add_indicators

NAMES = {
    "300308": "中际旭创", "300502": "新易盛", "300394": "天孚通信",
    "688008": "澜起科技", "603986": "兆易创新", "002409": "雅克科技",
    "688072": "拓荆科技", "688300": "联瑞新材", "300054": "鼎龙股份",
    "688205": "德科立", "920045": "蘅东光", "300776": "帝尔激光",
    "688535": "华海诚科",
}
DONCHIAN = {10, 40}   # 海龟入场上轨/离场下轨周期（约束最优口径B: exit=40）
CAP = 1_000_000.0     # 每策略分配资本（仅影响单位股数，不影响信号方向；与单名 turbo 回测一致）

def make_cfg():
    # 与约束最优版(optimized_backtest.py)信号逻辑保持一致：止损2ATR / 12单位
    cfg = replace(Config(), max_total_risk=0.99, stop_multiple=2.0,
                  max_units=12, risk_per_trade=0.075, target_atr_pct=0.0)
    return cfg

def fetch(code, end):
    if code == "920045":
        df = ak.stock_zh_a_daily(symbol="bj920045", start_date="20250101",
                                 end_date=end, adjust="qfq")
        df["date"] = pd.to_datetime(df["date"])  # 与 load_daily 的 Timestamp 对齐，避免跨源类型不一致
    else:
        from quant_turtle.data_feed import load_daily
        df = load_daily(code, "20250101", end, adjust="qfq", use_cache=False)
    for c in ["open", "high", "low", "close", "volume", "amount"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values("date").reset_index(drop=True)

def snapshot(strat, bar):
    """抓取重放至某 bar 后（执行完挂单、未生成新信号前）的策略状态与当根指标。"""
    ep = strat.entry_prices
    avg_entry = float(np.mean(ep)) if ep else float("nan")
    return {
        "in_position": strat.in_position,
        "units": strat.units,
        "avg_entry": avg_entry,
        "stop": strat.stop_price,
        "close": float(bar["close"]),
        "upper10": bar.get("donchian_upper_10"),
        "lower40": bar.get("donchian_lower_40"),
        "ma_fast": bar.get("ma_fast"),
        "ma_slow": bar.get("ma_slow"),
        "atr": float(bar["atr"]) if not pd.isna(bar.get("atr")) else float("nan"),
    }

def replay(strategy, df):
    """重放单策略至最后一根 bar，返回 (最新bar产生的订单, 最新收盘时点的状态快照)。"""
    enr = add_indicators(df, strategy.cfg, donchian_periods=DONCHIAN)
    enr = enr.reset_index(drop=True)
    pos = 0
    pending = []
    last_snap = None
    for i in range(len(enr)):
        bar = enr.iloc[i]
        # 1) 撮合上一根挂单（本根开盘价）——T+1 天然满足
        for order in pending:
            open_px = float(bar["open"])
            if order.limit_price is not None:
                fill = open_px if open_px <= order.limit_price else order.limit_price
            else:
                fill = open_px
            pos = pos + order.shares if order.action == "BUY" else max(0, pos - order.shares)
            strategy.sync_position(pos)
        # 2) 生成新信号
        if i == len(enr) - 1:
            last_snap = snapshot(strategy, bar)   # 最新收盘时点的持仓状态
        pending = strategy.on_bar(bar)
    return pending, last_snap

def classify(pending):
    if not pending:
        return "持有", ""
    buys = [o for o in pending if o.action == "BUY"]
    sells = [o for o in pending if o.action == "SELL"]
    if sells:
        # 取第一个卖出单的理由
        r = sells[0].reason
        return "卖出", r
    if buys:
        r = buys[0].reason
        return "买入", r
    return "持有", ""

def reason_text(strat_name, sig, reason, snap):
    if sig == "买入":
        if "entry" in reason:
            if strat_name == "ma":
                lvl = snap.get("ma_fast")
                return f"站上MA20建仓(>{lvl:.2f})" if (lvl is not None and pd.notna(lvl)) else "MA20建仓"
            lvl = snap["upper10"]
            return f"突破建仓(>{lvl:.2f})" if pd.notna(lvl) else "突破建仓"
        return "加仓(金字塔)"
    if sig == "卖出":
        if "stop" in reason:
            lvl = snap.get("stop")
            return f"止损@{lvl:.2f}" if (lvl is not None and pd.notna(lvl)) else "止损"
        if "exit_breakdown" in reason:
            lvl = snap.get("lower40")
            return f"跌破40日低({lvl:.2f})" if (lvl is not None and pd.notna(lvl)) else "跌破40日低"
        if "exit_ma" in reason:
            lvl = snap.get("ma_fast")
            return f"跌破MA20({lvl:.2f})" if (lvl is not None and pd.notna(lvl)) else "跌破MA20"
        if "trim" in reason:
            return "波动减仓"
        return reason
    # 持有
    if snap["in_position"]:
        return f"持仓{int(snap['units'])}单位@均价{snap['avg_entry']:.2f}"
    return "空仓观望"

def run_one(code, end):
    cfg = make_cfg()
    df = fetch(code, end)
    if df is None or len(df) < 80:
        return {"code": code, "name": NAMES[code], "err": f"数据不足({len(df) if df is not None else 0}根)"}
    turtle = TurtleStrategy(code, CAP, cfg, 10, 40)
    ma = MATrendStrategy(code, CAP, cfg)
    pt, st = replay(turtle, df)
    pm, sm = replay(ma, df)
    t_sig, t_reason = classify(pt)
    m_sig, m_reason = classify(pm)
    # 综合：任一看空→卖出；任一买入→买入；否则持有
    if t_sig == "卖出" or m_sig == "卖出":
        overall = "卖出"
    elif t_sig == "买入" or m_sig == "买入":
        overall = "买入"
    else:
        overall = "持有"
    return {
        "code": code, "name": NAMES[code],
        "close": st["close"], "in_pos": st["in_position"] or sm["in_position"],
        "units": max(st["units"], sm["units"]),
        "avg_entry": st["avg_entry"] if st["in_position"] else (sm["avg_entry"] if sm["in_position"] else float("nan")),
        "stop": st["stop"] if st["in_position"] else sm["stop"],
        "t_sig": t_sig, "t_reason": reason_text("turtle", t_sig, t_reason, st),
        "m_sig": m_sig, "m_reason": reason_text("ma", m_sig, m_reason, sm),
        "overall": overall,
        "upper10": st["upper10"], "lower40": st["lower40"], "ma_fast": st["ma_fast"], "ma_slow": st["ma_slow"],
    }

def main():
    end = sys.argv[1] if len(sys.argv) > 1 else date.today().strftime("%Y%m%d")
    print("=" * 96)
    print(f"  每日策略信号  数据截至 {end} 收盘  参数: 海龟(突破10/离场40/止损2ATR/12单位) + 均线(MA20/MA60) + 组合严格单票≤60%(持仓峰值减仓)")
    print("=" * 96)
    header = f"{'标的':8s} {'代码':7s} {'现价':>9s} {'持仓':>10s} {'海龟信号':>10s} {'均线信号':>10s} {'综合':>6s}  触发/理由"
    print(header)
    print("-" * 96)
    rows = []
    for code in NAMES:
        try:
            r = run_one(code, end)
        except Exception as e:
            r = {"code": code, "name": NAMES[code], "err": f"异常:{repr(e)[:60]}"}
        if "err" in r:
            print(f"  {r['name']:8s} {r['code']:7s}  {r['err']}")
            continue
        pos = f"{'持仓' if r['in_pos'] else '空仓'}{int(r['units'])}单位" if r['in_pos'] else "空仓"
        tcol = {"买入": "🔼买入", "卖出": "🔻卖出", "持有": "⚪持有"}[r["t_sig"]]
        mcol = {"买入": "🔼买入", "卖出": "🔻卖出", "持有": "⚪持有"}[r["m_sig"]]
        ocol = {"买入": "🔼买入", "卖出": "🔻卖出", "持有": "⚪持有"}[r["overall"]]
        reason = r["t_reason"] if r["t_sig"] != "持有" else (r["m_reason"] if r["m_sig"] != "持有" else r["t_reason"])
        print(f"  {r['name']:8s} {r['code']:7s} {r['close']:9.2f} {pos:>10s} {tcol:>10s} {mcol:>10s} {ocol:>6s}  {reason}")
        rows.append(r)
    print("-" * 96)
    buys = [r["name"] for r in rows if r["overall"] == "买入"]
    sells = [r["name"] for r in rows if r["overall"] == "卖出"]
    holds = [r["name"] for r in rows if r["overall"] == "持有"]
    print(f"  🔼 买入({len(buys)}): {', '.join(buys) if buys else '—'}")
    print(f"  🔻 卖出({len(sells)}): {', '.join(sells) if sells else '—'}")
    print(f"  ⚪ 持有({len(holds)}): {', '.join(holds) if holds else '—'}")
    print("=" * 96)
    # 落盘日志
    os.makedirs("/workspace/quant_turtle/signals", exist_ok=True)
    log = pd.DataFrame([{
        "date": end, "code": r["code"], "name": r["name"], "close": r["close"],
        "in_position": r["in_pos"], "units": r["units"],
        "avg_entry": r["avg_entry"], "stop": r["stop"],
        "turtle_sig": r["t_sig"], "ma_sig": r["m_sig"], "overall": r["overall"],
        "upper10": r["upper10"], "lower40": r["lower40"], "ma_fast": r["ma_fast"], "ma_slow": r["ma_slow"],
    } for r in rows])
    path = f"/workspace/quant_turtle/signals/signal_{end}.csv"
    log.to_csv(path, index=False)
    print(f"  信号日志: {path}")

if __name__ == "__main__":
    main()
