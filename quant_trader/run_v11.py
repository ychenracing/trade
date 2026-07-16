#!/usr/bin/env python3
"""
AQuant v11 回测运行器
=====================
拉取A股日线数据，用v11海龟策略回测，输出结果。
"""

import sys
import os
import time
import traceback

import pandas as pd
import akshare as ak

# 确保能 import 同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from a_share_turtle_v11 import (
    TurtleConfig,
    run_backtest,
    save_result,
    normalize_ohlcv_frame,
)

# ─── 配置 ───
SYMBOLS = {
    "300308.SZ": "中际旭创",
    "300502.SZ": "新易盛",
    "300394.SZ": "天孚通信",
    "688008.SH": "澜起科技",
    "603986.SH": "兆易创新",
    "002409.SZ": "雅克科技",
    "688300.SH": "联瑞新材",
    "300054.SZ": "鼎龙股份",
    "300776.SZ": "帝尔激光",
    "688535.SH": "华海诚科",
}

START_DATE = "2025-01-02"
END_DATE = "2026-06-30"
INITIAL_CAPITAL = 2_000_000

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "v11_results")


def fetch_daily_sina(code: str, start: str, end: str) -> pd.DataFrame:
    """用新浪接口拉前复权日线（TOOLS.md确认最稳定）"""
    # akshare 新浪格式：sz+代码 / sh+代码
    if code.startswith(("0", "3")):
        ak_code = f"sz{code}"
    elif code.startswith(("6",)):
        ak_code = f"sh{code}"
    else:
        ak_code = f"bj{code}"

    print(f"  拉取 {ak_code} ({start} ~ {end})...", end=" ", flush=True)
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_daily(symbol=ak_code, start_date=start, end_date=end, adjust="qfq")
            if df is not None and not df.empty:
                print(f"OK ({len(df)} bars)")
                return df
        except Exception as e:
            print(f"retry({attempt+1}/3) {e}", end=" ", flush=True)
            time.sleep(2)
    print("FAILED")
    return pd.DataFrame()


def build_data_map(symbols: dict, start: str, end: str) -> dict:
    """构建 {symbol: DataFrame} 数据字典"""
    data_map = {}
    for code, name in symbols.items():
        df = fetch_daily_sina(code.split(".")[0], start, end)
        if df.empty:
            print(f"  ⚠ {code} {name} 数据拉取失败，跳过")
            continue

        # 新浪接口返回列：date, open, high, low, close, volume
        # date 可能是 datetime.date 对象
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])

        # 标准化列名（v11 的 normalize_ohlcv_frame 会处理）
        # 新浪接口已经是英文列名，直接传
        try:
            df_clean = normalize_ohlcv_frame(df)
            data_map[code] = df_clean
            print(f"  ✓ {code} {name}: {len(df_clean)} bars ({df_clean['date'].iloc[0].date()} ~ {df_clean['date'].iloc[-1].date()})")
        except Exception as e:
            print(f"  ✗ {code} {name} 标准化失败: {e}")

    return data_map


def print_summary(result, symbols):
    """打印回测摘要"""
    s = result.summary
    print("\n" + "=" * 60)
    print("  AQuant v11 回测结果摘要")
    print("=" * 60)
    print(f"  标的:          {', '.join(f'{c}({n})' for c, n in symbols.items())}")
    print(f"  回测区间:      {s.get('start_date', '?')} ~ {s.get('end_date', '?')}")
    print(f"  初始资金:      ¥{s.get('initial_capital', 0):,.0f}")
    print(f"  最终权益:      ¥{s.get('final_equity', 0):,.2f}")
    print(f"  总收益率:      {s.get('total_return', 0)*100:+.2f}%")
    print(f"  年化收益率:    {s.get('annual_return', 0)*100:+.2f}%")
    print(f"  最大回撤:      {s.get('max_drawdown', 0)*100:.2f}%")
    print(f"  交易笔数:      {s.get('trade_count', 0)}")
    if pd.notna(s.get('win_rate', float('nan'))):
        print(f"  胜率:          {s.get('win_rate', 0)*100:.1f}%")
    print(f"  熔断次数:      {s.get('risk_off_events', 0)}")
    print(f"  强制退出数:    {s.get('forced_exit_count', 0)}")
    print(f"  期末持仓数:    {s.get('open_positions', 0)}")
    print("=" * 60)

    # 打印交易明细
    if not result.trades.empty:
        print("\n交易明细:")
        print("-" * 80)
        for _, t in result.trades.iterrows():
            forced = " [强制退出]" if t.get("is_forced_exit", False) else ""
            print(f"  {t['symbol']:10s} {t['system']:10s} "
                  f"买:{str(t['entry_date'])[:10]}@{t['entry_price']:.2f} → "
                  f"卖:{str(t['exit_date'])[:10]}@{t['exit_price']:.2f} "
                  f"{t['shares']:>6d}股 PnL:{t['pnl']:>+12.2f} ({t['return_pct']*100:>+7.2f}%) "
                  f"[{t['reason']}]{forced}")
        print("-" * 80)

    # 打印订单明细
    if not result.orders.empty:
        print(f"\n订单总数: {len(result.orders)}")
        # 只打印前10条和后5条
        if len(result.orders) > 15:
            print("前10条:")
            for _, o in result.orders.head(10).iterrows():
                print(f"  {str(o['date'])[:10]} {o['symbol']:10s} {o['side']:4s} "
                      f"{o['shares']:>6d}股@{o['price']:.2f} "
                      f"现金后:{o['cash_after']:>12.0f} [{o['reason']}]")
            print(f"... 省略 {len(result.orders) - 15} 条 ...")
            print("后5条:")
            for _, o in result.orders.tail(5).iterrows():
                print(f"  {str(o['date'])[:10]} {o['symbol']:10s} {o['side']:4s} "
                      f"{o['shares']:>6d}股@{o['price']:.2f} "
                      f"现金后:{o['cash_after']:>12.0f} [{o['reason']}]")
        else:
            for _, o in result.orders.iterrows():
                print(f"  {str(o['date'])[:10]} {o['symbol']:10s} {o['side']:4s} "
                      f"{o['shares']:>6d}股@{o['price']:.2f} "
                      f"现金后:{o['cash_after']:>12.0f} [{o['reason']}]")


def main():
    print("=" * 60)
    print("  AQuant v11 海龟趋势跟踪回测")
    print("=" * 60)
    print(f"\n标的: {', '.join(f'{c}({n})' for c, n in SYMBOLS.items())}")
    print(f"区间: {START_DATE} ~ {END_DATE}")
    print(f"初始资金: ¥{INITIAL_CAPITAL:,.0f}\n")

    # 1. 拉数据
    print("[1/3] 拉取行情数据...")
    data_map = build_data_map(SYMBOLS, START_DATE, END_DATE)
    if not data_map:
        print("❌ 无可用数据，退出")
        return

    # 2. 跑回测
    print(f"\n[2/3] 运行回测 ({len(data_map)} 只标的)...")
    config = TurtleConfig(initial_capital=INITIAL_CAPITAL)
    print(f"  配置: atr_stop={config.atr_stop_multiple}x, "
          f"max_symbol={config.max_symbol_weight}, "
          f"max_total={config.max_total_stock_weight}, "
          f"max_units={config.max_units_per_symbol}, "
          f"pyramid_add={config.pyramid_add_atr}ATR, "
          f"max_dd={config.max_drawdown}")
    print(f"  系统: {', '.join(f'{s.name}(entry={s.entry_window},exit={s.exit_window},rf={s.risk_fraction})' for s in config.systems)}")

    t0 = time.time()
    try:
        result = run_backtest(data_map, config)
        elapsed = time.time() - t0
        print(f"  回测完成，耗时 {elapsed:.1f}s")
    except Exception as e:
        print(f"  ❌ 回测失败: {e}")
        traceback.print_exc()
        return

    # 3. 输出结果
    print(f"\n[3/3] 输出结果...")
    print_summary(result, SYMBOLS)

    # 保存到CSV
    save_result(result, OUTPUT_DIR)
    print(f"\n结果已保存到: {OUTPUT_DIR}/")
    print(f"  - equity_curve.csv (权益曲线)")
    print(f"  - trades.csv (交易记录)")
    print(f"  - orders.csv (订单记录)")
    print(f"  - summary.csv (摘要)")


if __name__ == "__main__":
    main()
