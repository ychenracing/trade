#!/usr/bin/env python3
"""
针对性网格搜索 — 8只精选标的，围绕当前最优参数拓展搜索
目标：找到收益更高、回撤更小的参数组合
"""

import sys, os, time, itertools, pickle
import pandas as pd
import akshare as ak

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from a_share_turtle_v11 import (
    TurtleConfig, TurtleSystem, run_backtest, normalize_ohlcv_frame,
)
from dataclasses import replace

# ─── 8只标的 ───
SYMBOLS = {
    "300308.SZ": "中际旭创",
    "300502.SZ": "新易盛",
    "300394.SZ": "天孚通信",
    "688008.SH": "澜起科技",
    "603986.SH": "兆易创新",
    "002409.SZ": "雅克科技",
    "300054.SZ": "鼎龙股份",
    "688535.SH": "华海诚科",
}

START = "2025-01-02"
END   = "2026-06-30"
CAPITAL = 2_000_000

CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_cache_8.pkl")


def fetch_data():
    """一次性拉取全部标的数据"""
    data_map = {}
    for code, name in SYMBOLS.items():
        pure = code.split(".")[0]
        if pure.startswith(("0", "3")):
            ak_code = f"sz{pure}"
        elif pure.startswith("6"):
            ak_code = f"sh{pure}"
        else:
            ak_code = f"sh{pure}"

        for attempt in range(3):
            try:
                df = ak.stock_zh_a_daily(symbol=ak_code, start_date=START, end_date=END, adjust="qfq")
                if df is not None and not df.empty:
                    break
            except Exception as e:
                if attempt < 2:
                    time.sleep(1)
                else:
                    print(f"  ⚠ {code} 拉取失败: {e}")

        if df is None or df.empty:
            print(f"  ✗ {code} {name}: 无数据，跳过")
            continue

        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        try:
            df_clean = normalize_ohlcv_frame(df)
            data_map[code] = df_clean
            print(f"  ✓ {code} {name}: {len(df_clean)} bars")
        except Exception as e:
            print(f"  ✗ {code} {name}: normalize失败: {e}")

    # 缓存
    with open(CACHE_PATH, "wb") as f:
        pickle.dump(data_map, f)
    print(f"  数据已缓存到 {CACHE_PATH}")
    return data_map


def build_config(params):
    s1, s2 = params["s1"], params["s2"]
    systems = (
        TurtleSystem(f"S1_{s1[0]}_{s1[1]}", entry_window=s1[0], exit_window=s1[1],
                     risk_fraction=0.10),
        TurtleSystem(f"S2_{s2[0]}_{s2[1]}", entry_window=s2[0], exit_window=s2[1],
                     risk_fraction=0.10),
    )
    return TurtleConfig(
        initial_capital=CAPITAL,
        atr_stop_multiple=params["atr_stop"],
        max_symbol_weight=params["max_symbol"],
        max_total_stock_weight=0.98,
        max_units_per_symbol=6,
        pyramid_add_atr=params["pyramid_add"],
        max_drawdown=0.35,
        risk_off_cooldown_days=5,
        systems=systems,
        atr_window=20,
        use_atr_trailing_stop=True,
        use_donchian_exit=False,
        enable_pyramiding=True,
        pyramid_risk_decay=0.80,
        commission_rate=0.0003,
        stamp_tax_rate=0.0005,
        slippage_bps=5.0,
        lot_size=100,
        max_pending_buy_days=5,
        enable_dynamic_rebalance=False,
        close_position_on_data_end=True,
        force_close_on_end=False,
        allow_same_day_forced_close=False,
        count_forced_exits_in_stats=False,
    )


def run_one(data_map, params):
    config = build_config(params)
    try:
        result = run_backtest(data_map, config)
        s = result.summary
        return {
            "total_return": s.get("total_return", 0),
            "annual_return": s.get("annual_return", 0),
            "max_drawdown": s.get("max_drawdown", 0),
            "sharpe": s.get("sharpe_ratio", 0),
            "calmar": s.get("calmar_ratio", 0),
            "num_trades": s.get("num_trades", 0),
            "win_rate": s.get("win_rate", 0),
            "final_equity": s.get("final_equity", 0),
        }
    except Exception as e:
        return None


def main():
    print("=" * 80)
    print("  针对性网格搜索 — 8只精选标的")
    print("=" * 80)
    print(f"区间: {START} ~ {END}")
    print(f"初始资金: ¥{CAPITAL:,}")
    print(f"标的: {', '.join(SYMBOLS.values())}")

    # 加载数据
    print("\n[1/2] 加载行情数据...")
    data_map = None
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, "rb") as f:
            data_map = pickle.load(f)
        # 检查缓存日期范围
        first_code = list(data_map.keys())[0]
        cached_start = str(data_map[first_code]["date"].iloc[0])[:10]
        cached_end = str(data_map[first_code]["date"].iloc[-1])[:10]
        if cached_start == START and cached_end == END and len(data_map) == len(SYMBOLS):
            print(f"  使用缓存数据 ({len(data_map)} 只, {cached_start} ~ {cached_end})")
        else:
            print(f"  缓存日期不匹配 ({cached_start} ~ {cached_end})，重新拉取...")
            data_map = None

    if data_map is None:
        data_map = fetch_data()

    print(f"\n有效标的: {len(data_map)} 只")

    # 网格定义
    grids = {
        "s1": [(20, 15), (25, 20), (30, 20), (25, 15), (30, 25)],
        "s2": [(50, 20), (55, 20), (60, 20), (55, 25), (50, 25)],
        "atr_stop": [2.5, 3.0, 3.5, 4.0],
        "max_symbol": [0.40, 0.50, 0.60],
        "pyramid_add": [0.3, 0.5],
    }

    keys = list(grids.keys())
    combos = list(itertools.product(*[grids[k] for k in keys]))
    total = len(combos)
    print(f"\n[2/2] 网格搜索: {total} 个组合")
    print("=" * 80)

    results = []
    t0 = time.time()

    for i, combo in enumerate(combos):
        params = dict(zip(keys, combo))
        res = run_one(data_map, params)
        if res is None:
            continue
        res["params"] = params
        results.append(res)

        if (i + 1) % 50 == 0:
            elapsed = time.time() - t0
            speed = (i + 1) / elapsed
            eta = (total - i - 1) / speed
            print(f"  进度: {i+1}/{total} ({100*(i+1)/total:.0f}%) "
                  f"速度: {speed:.1f}/s ETA: {eta:.0f}s")

    elapsed = time.time() - t0
    print(f"\n完成！{len(results)} 个有效结果，耗时 {elapsed:.1f}s")

    # 报告
    if not results:
        print("无有效结果")
        return

    df = pd.DataFrame(results)
    df["calmar_calc"] = df["annual_return"] / df["max_drawdown"].abs().replace(0, 0.001)
    df["ret_dd_ratio"] = df["total_return"] / df["max_drawdown"].abs().replace(0, 0.001)

    # 综合评分
    df["norm_ret"] = (df["total_return"] - df["total_return"].min()) / (df["total_return"].max() - df["total_return"].min() + 1e-9)
    df["norm_dd"] = 1 - (df["max_drawdown"].abs() - df["max_drawdown"].abs().min()) / (df["max_drawdown"].abs().max() - df["max_drawdown"].abs().min() + 1e-9)
    df["norm_sharpe"] = (df["sharpe"] - df["sharpe"].min()) / (df["sharpe"].max() - df["sharpe"].min() + 1e-9)
    df["score"] = 0.40 * df["norm_ret"] + 0.30 * df["norm_dd"] + 0.30 * df["norm_sharpe"]

    print("\n" + "=" * 120)
    print("📊 TOP 20 — 综合评分（收益40% + 回撤30% + 夏普30%）")
    print("=" * 120)
    top = df.nlargest(20, "score")

    print(f"{'排名':>4} {'收益率':>8} {'年化':>8} {'回撤':>8} {'夏普':>6} {'Calmar':>7} {'交易':>4} {'胜率':>5} {'评分':>5}  参数")
    print("-" * 120)

    for rank, (_, row) in enumerate(top.iterrows(), 1):
        p = row["params"]
        param_str = (f"ATR={p['atr_stop']} "
                     f"sym={p['max_symbol']} "
                     f"add={p['pyramid_add']} "
                     f"S1={p['s1'][0]}/{p['s1'][1]} "
                     f"S2={p['s2'][0]}/{p['s2'][1]}")
        print(f"{rank:>4} {row['total_return']*100:>7.1f}% {row['annual_return']*100:>7.1f}% "
              f"{row['max_drawdown']*100:>7.1f}% {row['sharpe']:>6.2f} {row['calmar_calc']:>7.2f} "
              f"{row['num_trades']:>4.0f} {row['win_rate']*100:>4.0f}% {row['score']:>5.2f}  {param_str}")

    print("\n" + "=" * 120)
    print("📊 TOP 10 — 纯收益率")
    print("=" * 120)
    top_ret = df.nlargest(10, "total_return")
    for rank, (_, row) in enumerate(top_ret.iterrows(), 1):
        p = row["params"]
        param_str = f"ATR={p['atr_stop']} sym={p['max_symbol']} add={p['pyramid_add']} S1={p['s1']} S2={p['s2']}"
        print(f"#{rank}: 收益 {row['total_return']*100:.1f}% | 年化 {row['annual_return']*100:.1f}% | 回撤 {row['max_drawdown']*100:.1f}% | 夏普 {row['sharpe']:.2f} | Calmar {row['calmar_calc']:.2f} | 交易 {row['num_trades']:.0f}笔 | 胜率 {row['win_rate']*100:.0f}% | {param_str}")

    print("\n" + "=" * 120)
    print("📊 TOP 10 — 最小回撤（收益>100%）")
    print("=" * 120)
    positive = df[df["total_return"] > 1.0]
    if len(positive) > 0:
        top_dd = positive.nsmallest(10, "max_drawdown")
        for rank, (_, row) in enumerate(top_dd.iterrows(), 1):
            p = row["params"]
            param_str = f"ATR={p['atr_stop']} sym={p['max_symbol']} add={p['pyramid_add']} S1={p['s1']} S2={p['s2']}"
            print(f"#{rank}: 回撤 {row['max_drawdown']*100:.1f}% | 收益 {row['total_return']*100:.1f}% | 年化 {row['annual_return']*100:.1f}% | 夏普 {row['sharpe']:.2f} | Calmar {row['calmar_calc']:.2f} | 交易 {row['num_trades']:.0f}笔 | {param_str}")

    print("\n" + "=" * 120)
    print("📊 TOP 10 — Calmar比（年化收益/回撤）")
    print("=" * 120)
    top_cal = df.nlargest(10, "calmar_calc")
    for rank, (_, row) in enumerate(top_cal.iterrows(), 1):
        p = row["params"]
        param_str = f"ATR={p['atr_stop']} sym={p['max_symbol']} add={p['pyramid_add']} S1={p['s1']} S2={p['s2']}"
        print(f"#{rank}: Calmar {row['calmar_calc']:.2f} | 收益 {row['total_return']*100:.1f}% | 回撤 {row['max_drawdown']*100:.1f}% | 年化 {row['annual_return']*100:.1f}% | 夏普 {row['sharpe']:.2f} | 交易 {row['num_trades']:.0f}笔 | {param_str}")

    # 保存
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "v11_grid_search")
    os.makedirs(out_dir, exist_ok=True)
    save_df = df.copy()
    save_df["param_str"] = save_df["params"].apply(
        lambda p: f"ATR={p['atr_stop']} sym={p['max_symbol']} add={p['pyramid_add']} S1={p['s1']} S2={p['s2']}")
    cols = ["total_return", "annual_return", "max_drawdown", "sharpe", "calmar_calc",
            "num_trades", "win_rate", "score", "param_str"]
    save_df[cols].to_csv(os.path.join(out_dir, "grid_search_optimize.csv"),
                         index=False, encoding="utf-8-sig")
    print(f"\n全部结果已保存: {out_dir}/grid_search_optimize.csv ({len(df)} 组合)")

    # 推荐
    best = top.iloc[0]
    p = best["params"]
    print("\n" + "=" * 120)
    print("💡 推荐参数（综合评分最高）")
    print("=" * 120)
    print(f"""
  ATR止损倍数:      {p['atr_stop']}
  单票最大权重:     {p['max_symbol']}
  加仓间距(ATR):    {p['pyramid_add']}
  S1系统(短周期):   entry={p['s1'][0]}, exit={p['s1'][1]}
  S2系统(长周期):   entry={p['s2'][0]}, exit={p['s2'][1]}

  预期表现:
    收益率:   {best['total_return']*100:.1f}%
    年化:     {best['annual_return']*100:.1f}%
    回撤:     {best['max_drawdown']*100:.1f}%
    夏普:     {best['sharpe']:.2f}
    Calmar:  {best['calmar_calc']:.2f}
    交易笔数: {best['num_trades']:.0f}
    胜率:     {best['win_rate']*100:.0f}%
""")


if __name__ == "__main__":
    main()
