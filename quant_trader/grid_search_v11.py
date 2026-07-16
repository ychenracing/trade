#!/usr/bin/env python3
"""
AQuant v11 网格搜索 — 13只AI产业链标的
=========================================
目标：最大化年化收益率，同时控制回撤
"""

import sys, os, time, itertools, traceback
import pandas as pd
import akshare as ak

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from a_share_turtle_v11 import (
    TurtleConfig, TurtleSystem, run_backtest,
    normalize_ohlcv_frame,
)
from dataclasses import replace

# ─── 13只监控标的 ───
SYMBOLS = {
    "300308.SZ": "中际旭创",
    "300502.SZ": "新易盛",
    "300394.SZ": "天孚通信",
    "688008.SH": "澜起科技",
    "603986.SH": "兆易创新",
    "002409.SZ": "雅克科技",
    "688072.SH": "拓荆科技",
    "688110.SH": "联瑞新材",
    "300054.SZ": "鼎龙股份",
    "688535.SH": "华海诚科",
    "300776.SZ": "帝尔激光",
    "688205.SH": "德科立",
    "920045.BJ": "蘅东光",
}

START = "2025-01-02"
END   = "2026-07-08"
CAPITAL = 2_000_000


def load_cached_data():
    """从pickle加载缓存数据"""
    import pickle
    cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_cache.pkl")
    if os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            return pickle.load(f)
    return None


def fetch_data():
    """一次性拉取全部标的数据，缓存到全局变量"""
    data_map = {}
    for code, name in SYMBOLS.items():
        pure = code.split(".")[0]
        if pure.startswith(("0","3")):
            ak_code = f"sz{pure}"
        elif pure.startswith("6"):
            ak_code = f"sh{pure}"
        elif pure.startswith("9"):
            ak_code = f"bj{pure}"
        else:
            ak_code = f"sh{pure}"

        for attempt in range(3):
            try:
                df = ak.stock_zh_a_daily(symbol=ak_code, start_date=START, end_date=END, adjust="qfq")
                if df is not None and not df.empty:
                    break
            except Exception as e:
                if attempt < 2: time.sleep(1)
                else: print(f"  ⚠ {code} 拉取失败: {e}")
        
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
    return data_map


def build_config(params):
    """根据网格参数构建TurtleConfig"""
    s1, s2 = params["s1"], params["s2"]
    systems = (
        TurtleSystem(f"S1_{s1[0]}_{s1[1]}", entry_window=s1[0], exit_window=s1[1], 
                     risk_fraction=params["risk_fraction"]),
        TurtleSystem(f"S2_{s2[0]}_{s2[1]}", entry_window=s2[0], exit_window=s2[1], 
                     risk_fraction=params["risk_fraction"]),
    )
    return TurtleConfig(
        initial_capital=CAPITAL,
        atr_stop_multiple=params["atr_stop"],
        max_symbol_weight=params["max_symbol"],
        max_total_stock_weight=params["max_total"],
        max_units_per_symbol=params["max_units"],
        pyramid_add_atr=params["pyramid_add"],
        max_drawdown=params["max_dd"],
        risk_off_cooldown_days=params["cooldown"],
        systems=systems,
        # 固定参数
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
    """跑一次回测，返回关键指标"""
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


def grid_search(data_map):
    """网格搜索"""
    # ─── 精简参数网格（108组合）───
    grids = {
        # 窗口组合（精选3组：短/中/长）
        "s1": [(20, 15), (10, 5), (25, 20)],
        "s2": [(55, 20), (40, 10), (70, 30)],
        # ATR止损倍数（3档：紧/中/松）
        "atr_stop": [3.0, 5.0, 10.0],
        # 单票最大权重（3档：分散/平衡/集中）
        "max_symbol": [0.25, 0.50, 0.95],
        # 风险比例（2档）
        "risk_fraction": [0.05, 0.10],
        # 最大回撤熔断（2档：有保护/无保护）
        "max_dd": [0.25, 0.99],
        # 固定参数
        "max_total": [0.98],
        "pyramid_add": [0.3],
        "cooldown": [5],
        "max_units": [6],
    }
    
    # 生成所有组合
    keys = list(grids.keys())
    combos = list(itertools.product(*[grids[k] for k in keys]))
    total = len(combos)
    print(f"\n网格搜索: {total} 个组合 × {len(data_map)} 只标的")
    print("=" * 80)
    
    results = []
    t0 = time.time()
    
    for i, combo in enumerate(combos):
        params = dict(zip(keys, combo))
        
        # 跳过明显不合理的组合
        if params["max_symbol"] > params["max_total"]:
            continue
        
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
    return results


def report(results):
    """输出排行榜"""
    if not results:
        print("无有效结果")
        return
    
    df = pd.DataFrame(results)
    
    # Calmar ratio (年化收益/最大回撤)
    df["calmar_calc"] = df["annual_return"] / df["max_drawdown"].abs().replace(0, 0.001)
    
    # 收益/回撤比
    df["ret_dd_ratio"] = df["total_return"] / df["max_drawdown"].abs().replace(0, 0.001)
    
    print("\n" + "=" * 100)
    print("📊 TOP 15 — 综合评分（收益+回撤+夏普）")
    print("=" * 100)
    
    # 综合评分: 标准化收益(40%) + 标准化回撤(30%, 越小越好) + 标准化夏普(30%)
    df["norm_ret"] = (df["total_return"] - df["total_return"].min()) / (df["total_return"].max() - df["total_return"].min() + 1e-9)
    df["norm_dd"] = 1 - (df["max_drawdown"].abs() - df["max_drawdown"].abs().min()) / (df["max_drawdown"].abs().max() - df["max_drawdown"].abs().min() + 1e-9)
    df["norm_sharpe"] = (df["sharpe"] - df["sharpe"].min()) / (df["sharpe"].max() - df["sharpe"].min() + 1e-9)
    df["score"] = 0.40 * df["norm_ret"] + 0.30 * df["norm_dd"] + 0.30 * df["norm_sharpe"]
    
    top = df.nlargest(15, "score")
    
    print(f"{'排名':>4} {'收益率':>8} {'年化':>8} {'回撤':>8} {'夏普':>6} {'Calmar':>7} {'交易':>4} {'胜率':>5} {'评分':>5}  参数")
    print("-" * 100)
    
    for rank, (_, row) in enumerate(top.iterrows(), 1):
        p = row["params"]
        param_str = (f"ATR={p['atr_stop']} "
                     f"sym={p['max_symbol']} "
                     f"total={p['max_total']} "
                     f"rf={p['risk_fraction']} "
                     f"add={p['pyramid_add']} "
                     f"dd={p['max_dd']} "
                     f"units={p['max_units']} "
                     f"cool={p['cooldown']} "
                     f"S1={p['s1'][0]}/{p['s1'][1]} "
                     f"S2={p['s2'][0]}/{p['s2'][1]}")
        print(f"{rank:>4} {row['total_return']*100:>7.1f}% {row['annual_return']*100:>7.1f}% "
              f"{row['max_drawdown']*100:>7.1f}% {row['sharpe']:>6.2f} {row['calmar_calc']:>7.2f} "
              f"{row['num_trades']:>4.0f} {row['win_rate']*100:>4.0f}% {row['score']:>5.2f}  {param_str}")
    
    print("\n" + "=" * 100)
    print("📊 TOP 10 — 纯收益率")
    print("=" * 100)
    top_ret = df.nlargest(10, "total_return")
    for rank, (_, row) in enumerate(top_ret.iterrows(), 1):
        p = row["params"]
        param_str = f"ATR={p['atr_stop']} sym={p['max_symbol']} rf={p['risk_fraction']} add={p['pyramid_add']} dd={p['max_dd']} S1={p['s1']} S2={p['s2']}"
        print(f"#{rank}: 收益 {row['total_return']*100:.1f}% | 年化 {row['annual_return']*100:.1f}% | 回撤 {row['max_drawdown']*100:.1f}% | 夏普 {row['sharpe']:.2f} | 交易 {row['num_trades']:.0f}笔 | {param_str}")
    
    print("\n" + "=" * 100)
    print("📊 TOP 10 — 最小回撤（收益>0）")
    print("=" * 100)
    positive = df[df["total_return"] > 0]
    if len(positive) > 0:
        top_dd = positive.nsmallest(10, "max_drawdown", )
        for rank, (_, row) in enumerate(top_dd.iterrows(), 1):
            p = row["params"]
            param_str = f"ATR={p['atr_stop']} sym={p['max_symbol']} rf={p['risk_fraction']} add={p['pyramid_add']} dd={p['max_dd']} S1={p['s1']} S2={p['s2']}"
            print(f"#{rank}: 回撤 {row['max_drawdown']*100:.1f}% | 收益 {row['total_return']*100:.1f}% | 年化 {row['annual_return']*100:.1f}% | 夏普 {row['sharpe']:.2f} | 交易 {row['num_trades']:.0f}笔 | {param_str}")
    
    print("\n" + "=" * 100)
    print("📊 TOP 10 — Calmar比（年化收益/回撤）")
    print("=" * 100)
    top_cal = df.nlargest(10, "calmar_calc")
    for rank, (_, row) in enumerate(top_cal.iterrows(), 1):
        p = row["params"]
        param_str = f"ATR={p['atr_stop']} sym={p['max_symbol']} rf={p['risk_fraction']} add={p['pyramid_add']} dd={p['max_dd']} S1={p['s1']} S2={p['s2']}"
        print(f"#{rank}: Calmar {row['calmar_calc']:.2f} | 收益 {row['total_return']*100:.1f}% | 回撤 {row['max_drawdown']*100:.1f}% | 年化 {row['annual_return']*100:.1f}% | 夏普 {row['sharpe']:.2f} | {param_str}")
    
    # 保存全部结果
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "v11_grid_search")
    os.makedirs(out_dir, exist_ok=True)
    
    save_df = df.copy()
    save_df["param_str"] = save_df["params"].apply(lambda p: f"ATR={p['atr_stop']} sym={p['max_symbol']} rf={p['risk_fraction']} add={p['pyramid_add']} dd={p['max_dd']} units={p['max_units']} cool={p['cooldown']} S1={p['s1']} S2={p['s2']}")
    cols = ["total_return","annual_return","max_drawdown","sharpe","calmar_calc","num_trades","win_rate","score","param_str"]
    save_df[cols].to_csv(os.path.join(out_dir, "grid_search_results.csv"), index=False, encoding="utf-8-sig")
    print(f"\n全部结果已保存: {out_dir}/grid_search_results.csv ({len(df)} 组合)")
    
    # 推荐参数
    best = top.iloc[0]
    p = best["params"]
    print("\n" + "=" * 100)
    print("💡 推荐参数（综合评分最高）")
    print("=" * 100)
    print(f"""
  ATR止损倍数:      {p['atr_stop']}
  单票最大权重:     {p['max_symbol']}
  总仓位上限:       {p['max_total']}
  风险比例:         {p['risk_fraction']}
  加仓间距(ATR):    {p['pyramid_add']}
  最大加仓单位:     {p['max_units']}
  熔断回撤阈值:     {p['max_dd']}
  冷却天数:         {p['cooldown']}
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


def main():
    print("=" * 80)
    print("  AQuant v11 网格搜索 — 13只AI产业链标的")
    print("=" * 80)
    print(f"区间: {START} ~ {END}")
    print(f"初始资金: ¥{CAPITAL:,}")
    print(f"标的: {', '.join(SYMBOLS.values())}")
    
    print("\n[1/2] 加载行情数据...")
    data_map = load_cached_data()
    if data_map is None:
        print("  缓存不存在，在线拉取...")
        data_map = fetch_data()
    if data_map is None or len(data_map) < 5:
        print(f"⚠ 只拉到{len(data_map) if data_map else 0}只标的数据，结果可能不可靠")
    
    print(f"\n有效标的: {len(data_map)} 只")
    
    print("\n[2/2] 网格搜索...")
    results = grid_search(data_map)
    
    if results:
        report(results)


if __name__ == "__main__":
    main()
