#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AQuant v9 每日信号扫描
=====================
每个工作日收盘后运行，报告监控标的的三策略信号（买入/卖出/持有）。

三策略：
  1. TurtleBreakoutStrategy — 海龟突破（唐奇安通道+ADX过滤+ATR追踪止损）
  2. DualMAStrategy — 双均线趋势（MA金叉/死叉+ATR追踪止损）
  3. ATRChannelStrategy — ATR通道突破（MA±ATR通道+ADX过滤）

用法: python daily_scan.py
"""

import sys, os, time, urllib.request
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aquant import (
    BacktestEngine, DataFetcher, Indicators,
    TurtleBreakoutStrategy, DualMAStrategy, ATRChannelStrategy,
    Position, BarContext,
)

# ─── 配置 ───
# 组1：光通信+半导体（max_positions=6）
SYMBOLS_GROUP1 = {
    "300308": "中际旭创",
    "300502": "新易盛",
    "300394": "天孚通信",
    "688008": "澜起科技",
    "603986": "兆易创新",
    "002409": "雅克科技",
    "688300": "联瑞新材",
    "300054": "鼎龙股份",
    "300776": "帝尔激光",
    "688535": "华海诚科",
}

# 组2：半导体设备/材料（max_positions=3）
SYMBOLS_GROUP2 = {
    "688249": "晶合集成",
    "300666": "江丰电子",
    "600206": "有研新材",
    "688409": "富创精密",
    "688361": "中科飞测",
    "300604": "长川科技",
    "688120": "华海清科",
    "688082": "盛美上海",
}

# 兼容旧引用
SYMBOLS = {**SYMBOLS_GROUP1, **SYMBOLS_GROUP2}

# 用户当前实盘持仓（2026-07-10收盘更新）
USER_HOLDINGS = {
    "300308": {"shares": 700, "name": "中际旭创", "cost": 415},
    "300394": {"shares": 2500, "name": "天孚通信", "cost": 301},
    "300054": {"shares": 6300, "name": "鼎龙股份", "cost": 88.10},
    "300776": {"shares": 600, "name": "帝尔激光", "cost": 201},
    "688300": {"shares": 3303, "name": "联瑞新材", "cost": 172.21},
    # 300502新易盛已清仓
}

# 两组分别用不同配置
DEFAULT_CFG = BacktestEngine._default_config()  # max_positions=6（组1）
CFG_GROUP1 = {**DEFAULT_CFG}                     # max_positions=6
CFG_GROUP2 = {**DEFAULT_CFG, "max_positions": 3} # max_positions=3（组2）

LOOKBACK_DAYS = 120  # 拉取天数（足够算MA60+ADX+ATR+唐奇安通道）


def fetch_realtime_prices(codes: list[str]) -> dict[str, dict]:
    """通过腾讯接口拉取实时行情"""
    code_map = {}
    for code in codes:
        if code.startswith(("0", "3")):
            code_map[f"sz{code}"] = code
        elif code.startswith("6"):
            code_map[f"sh{code}"] = code
        elif code.startswith("9"):
            code_map[f"bj{code}"] = code

    url = f"https://qt.gtimg.cn/q={','.join(code_map.keys())}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    resp = urllib.request.urlopen(req, timeout=10)
    text = resp.read().decode("gbk")

    results = {}
    for line in text.strip().split(";"):
        line = line.strip()
        if not line:
            continue
        fields = line.split("~")
        if len(fields) < 38:
            continue
        ret_code = fields[2]
        ret_name = fields[1]
        price = float(fields[3]) if fields[3] else 0.0
        pct = float(fields[32]) if fields[32] else 0.0
        results[ret_code] = {"name": ret_name, "price": price, "pct": pct}
    return results


def fetch_recent_data(code: str, lookback: int = LOOKBACK_DAYS) -> pd.DataFrame:
    """拉取最近N天前复权日线数据"""
    end = pd.Timestamp.now().strftime("%Y-%m-%d")
    start = (pd.Timestamp.now() - pd.Timedelta(days=lookback + 90)).strftime("%Y-%m-%d")
    try:
        df = DataFetcher.fetch_stock_data(code, start, end)
        if df is not None and not df.empty:
            df = df.tail(lookback).reset_index()
            if "date" not in df.columns:
                df.rename(columns={df.columns[0]: "date"}, inplace=True)
            return df
    except Exception as e:
        print(f"  数据拉取失败 {code}: {e}")
    return pd.DataFrame()


def calc_indicators(df: pd.DataFrame, cfg: dict) -> dict:
    """计算所有技术指标"""
    return Indicators.compute_all(df, cfg)


def run_strategies_on_last_bar(code: str, name: str, df: pd.DataFrame,
                                rt_price: float, held_shares: int,
                                cfg: dict) -> list[dict]:
    """对最后一根bar运行三策略，返回信号列表"""
    if df.empty or len(df) < 65:
        return []

    ind = Indicators.compute_all(df, cfg)
    i = len(df) - 1
    last_date = str(df["date"].iloc[i])[:10]

    # 用实时价格替换最后一根bar的收盘价
    df_rt = df.copy()
    if rt_price > 0:
        df_rt.loc[df_rt.index[i], "close"] = rt_price
        # 重新计算指标（因为收盘价变了）
        ind = Indicators.compute_all(df_rt, cfg)

    atr_val = ind["atr"].iloc[i]
    adx_val = ind["adx"].iloc[i]
    rsi_val = ind["rsi"].iloc[i]
    ma_s = ind["ma_short"].iloc[i]
    ma_l = ind["ma_long"].iloc[i]
    upper = ind["donchian_upper"].iloc[i]
    lower = ind["donchian_lower"].iloc[i]
    close = df_rt["close"].iloc[i]
    high = df_rt["high"].iloc[i]

    if pd.isna(atr_val) or pd.isna(adx_val):
        atr_val = atr_val if not pd.isna(atr_val) else 0
        adx_val = adx_val if not pd.isna(adx_val) else 0

    signals = []

    # 为每个策略创建实例
    strategies = [
        TurtleBreakoutStrategy(cfg),
        DualMAStrategy(cfg),
        ATRChannelStrategy(cfg),
    ]

    # 如果用户持有该标的，设置position
    if held_shares > 0:
        # 估算入场价：用近期低点作为粗略估算
        recent = df_rt.tail(60)
        entry_price = recent["close"].min() * 0.95  # 保守估计
        highest = recent["high"].max()
        # ATR追踪止损
        trail_stop = highest - cfg["trail_atr_mult"] * atr_val if atr_val > 0 else 0
        initial_stop = entry_price - cfg["atr_multiplier"] * atr_val if atr_val > 0 else 0
        stop_loss = max(trail_stop, initial_stop, 0)

        for strat in strategies:
            strat.position = Position(
                symbol=code,
                strategy_name=strat.name,
                shares=held_shares,
                entry_price=entry_price,
                entry_date=str(df_rt["date"].iloc[max(0, i-30)])[:10],
                stop_loss=stop_loss,
                highest_since_entry=highest,
                units=1,
                last_add_price=close,
            )

    # 运行每个策略
    ctx = BarContext(
        i=i,
        df=df_rt,
        current_assets=2_600_000,  # 估算总资产
        indicators=ind,
        available_cash=0,
        symbol=code,
        date=last_date,
    )

    for strat in strategies:
        try:
            sig = strat.on_bar(ctx)
            if sig is not None:
                signals.append({
                    "strategy": strat.name,
                    "direction": sig.direction,
                    "reason": sig.reason,
                    "shares": sig.target_shares,
                    "price": sig.price,
                    "stop_loss": sig.stop_loss,
                    "atr": sig.atr,
                })
        except Exception as e:
            pass

    # 构建指标摘要
    indicator_summary = {
        "close": close,
        "atr": atr_val,
        "adx": adx_val,
        "rsi": rsi_val,
        "ma_short": ma_s,
        "ma_long": ma_l,
        "donchian_upper": upper,
        "donchian_lower": lower,
        "stop_loss": stop_loss if held_shares > 0 else None,
        "highest": highest if held_shares > 0 else None,
    }

    return signals, indicator_summary


def run_group(group_name: str, symbols: dict, cfg: dict, rt_data: dict):
    """扫描一组标的，返回信号汇总"""
    print(f"\n{'─' * 80}")
    print(f"  {group_name}（{len(symbols)}只标的, max_positions={cfg['max_positions']}）")
    print(f"{'─' * 80}")

    results = []
    for code, name in symbols.items():
        held = USER_HOLDINGS.get(code, {}).get("shares", 0)
        rt_price = rt_data.get(code, {}).get("price", 0)
        print(f"  拉取 {code} {name}...", end="  ")
        df = fetch_recent_data(code)
        if df.empty:
            print("FAIL")
            continue
        print(f"OK ({len(df)} bars)")
        ret = run_strategies_on_last_bar(code, name, df, rt_price, held, cfg)
        results.append((code, name, held, rt_price, ret))

    buy_list = []
    sell_list = []
    add_list = []
    hold_list = []

    for code, name, held, price, ret in results:
        if isinstance(ret, tuple):
            signals, ind = ret
        else:
            signals = ret
            ind = {}

        if not signals:
            held_tag = f" [持仓{held}股]" if held > 0 else ""
            if held > 0:
                stop_str = f"止损线={ind.get('stop_loss', 0):.2f}" if ind.get("stop_loss") else "无止损"
                hold_list.append({
                    "name": name, "code": code, "held_tag": held_tag,
                    "signal": "HOLD 持有",
                    "reason": f"未触发卖出或加仓",
                    "detail": f"现价{price:.2f} | {stop_str}",
                    "ind": ind,
                })
            else:
                upper = ind.get("donchian_upper", 0)
                gap = ((upper - price) / price * 100) if (upper > 0 and price > 0) else 0
                hold_list.append({
                    "name": name, "code": code, "held_tag": "",
                    "signal": "WAIT 空仓等待",
                    "reason": f"收盘{price:.2f}距突破{upper:.2f}差{gap:.1f}%",
                    "detail": "",
                    "ind": ind,
                })
            continue

        sells = [s for s in signals if s["direction"] == "sell"]
        buys = [s for s in signals if s["direction"] == "buy"]

        for s in sells:
            sell_list.append({
                "name": name, "code": code,
                "held_tag": f" [持仓{held}股]" if held > 0 else "",
                "strategy": s["strategy"],
                "reason": s["reason"],
                "detail": f"现价{price:.2f} | ATR={ind.get('atr',0):.2f} | ADX={ind.get('adx',0):.1f} | RSI={ind.get('rsi',0):.1f} | MA15={ind.get('ma_short',0):.2f} | MA60={ind.get('ma_long',0):.2f} | 唐奇安高={ind.get('donchian_upper',0):.2f} | 低={ind.get('donchian_lower',0):.2f} | 止损={ind.get('stop_loss',0):.2f}" if held > 0 else f"现价{price:.2f}",
                "ind": ind,
            })

        for b in buys:
            entry = "加仓" if held > 0 else "买入"
            add_list.append({
                "name": name, "code": code,
                "held_tag": f" [持仓{held}股]" if held > 0 else "",
                "strategy": b["strategy"],
                "reason": b["reason"],
                "detail": f"现价{price:.2f} | ATR={ind.get('atr',0):.2f} | ADX={ind.get('adx',0):.1f} | RSI={ind.get('rsi',0):.1f} | MA15={ind.get('ma_short',0):.2f} | MA60={ind.get('ma_long',0):.2f} | 唐奇安高={ind.get('donchian_upper',0):.2f} | 低={ind.get('donchian_lower',0):.2f} | 止损={ind.get('stop_loss',0):.2f}" if held > 0 else f"现价{price:.2f} | ATR={ind.get('atr',0):.2f} | ADX={ind.get('adx',0):.1f}",
                "ind": ind,
            })

    # 输出
    if sell_list:
        print("\n  🔴 卖出信号:")
        for r in sell_list:
            print(f"    {r['name']} {r['code']}{r.get('held_tag','')} — SELL [{r['strategy']}]")
            print(f"      {r['reason']}")
            if r['detail']:
                print(f"      {r['detail']}")

    if add_list:
        print("\n  🟢 买入/加仓信号:")
        for r in add_list:
            print(f"    {r['name']} {r['code']}{r.get('held_tag','')} — BUY [{r['strategy']}]")
            print(f"      {r['reason']}")
            if r['detail']:
                print(f"      {r['detail']}")

    if hold_list:
        print("\n  ⚪ 持有/等待:")
        for r in hold_list:
            print(f"    {r['name']} {r['code']}{r.get('held_tag','')} — {r['signal']}")
            print(f"      {r['reason']}")

    print(f"\n  [{group_name}] 总结: {len(add_list)}买入 | {len(sell_list)}卖出 | {len(hold_list)}持有/等待")
    return {"buy": len(add_list), "sell": len(sell_list), "hold": len(hold_list)}


def main():
    print("=" * 80)
    print("  AQuant v9 每日信号扫描（三策略组合 × 双组标的）")
    print("=" * 80)
    print(f"日期: {pd.Timestamp.now().strftime('%Y-%m-%d')}")
    print(f"组1: {len(SYMBOLS_GROUP1)}只 光通信+半导体 (max_positions={CFG_GROUP1['max_positions']})")
    print(f"组2: {len(SYMBOLS_GROUP2)}只 半导体设备/材料 (max_positions={CFG_GROUP2['max_positions']})")

    # 1. 拉取实时行情
    all_symbols = {**SYMBOLS_GROUP1, **SYMBOLS_GROUP2}
    print(f"\n[1/2] 拉取实时行情（{len(all_symbols)}只）...")
    codes = list(all_symbols.keys())
    try:
        rt_data = fetch_realtime_prices(codes)
        print(f"  实时价格: {len(rt_data)}只 (腾讯接口)")
        for code in codes:
            if code in rt_data:
                ret_name = rt_data[code]["name"]
                exp_name = all_symbols[code]
                if ret_name != exp_name:
                    print(f"  ⚠ {code} 返回名称={ret_name} 预期={exp_name}")
    except Exception as e:
        print(f"  实时行情拉取失败: {e}")
        rt_data = {}

    # 2. 分组扫描
    print(f"\n[2/2] 运行三策略扫描...")
    g1 = run_group("组1 光通信+半导体", SYMBOLS_GROUP1, CFG_GROUP1, rt_data)
    g2 = run_group("组2 半导体设备/材料", SYMBOLS_GROUP2, CFG_GROUP2, rt_data)

    # 总结
    print()
    print("=" * 80)
    print(f"  总总结: 组1({g1['buy']}买/{g1['sell']}卖/{g1['hold']}持) + 组2({g2['buy']}买/{g2['sell']}卖/{g2['hold']}持)")
    print(f"  策略: 海龟突破 + 双均线 + ATR通道 (三策略独立运行)")
    print("=" * 80)


if __name__ == "__main__":
    main()
