"""
对比"软买入"vs"动量评分"策略信号差异
同一批数据，跑两套逻辑，输出对比表
"""
import sys
import datetime
import pandas as pd
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from strategy.combo import ComboStrategy
from strategy.base import Signal
from utils.helpers import (
    calc_ema, calc_sma, calc_macd, calc_donchian, calc_atr,
    calc_momentum_score,
)

# 股票池
STOCKS = [
    ("sz300308", "中际旭创"),
    ("sz300502", "新易盛"),
    ("sz300394", "天孚通信"),
    ("sh688008", "澜起科技"),
    ("sh603986", "兆易创新"),
    ("sz002409", "雅克科技"),
    ("sh688072", "拓荆科技"),
    ("sh688110", "联瑞新材"),
    ("sz300054", "鼎龙股份"),
]


def get_hist_daily(code: str, days: int = 180) -> pd.DataFrame:
    """用akshare新浪接口拉历史日K（前复权）"""
    import akshare as ak
    df = ak.stock_zh_a_daily(symbol=code, adjust="qfq")
    df = df.rename(columns={"date": "date", "open": "open", "high": "high",
                            "low": "low", "close": "close", "volume": "volume"})
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").tail(days).reset_index(drop=True)
    return df


# ============================================================
# 旧逻辑：复现3个策略的"软买入"条件（改动前）
# ============================================================

def old_ema_signal(df: pd.DataFrame) -> tuple:
    """旧EMA策略信号 (ma_cross.py 改动前)"""
    p = {"ema_short": 10, "ema_long": 30, "ema_trend": 60, "volume_ratio": 1.5}
    df = df.copy()
    df["ema_short"] = calc_ema(df["close"], p["ema_short"])
    df["ema_long"] = calc_ema(df["close"], p["ema_long"])
    df["ema_trend"] = calc_ema(df["close"], p["ema_trend"])
    df["vol_ma5"] = df["volume"].rolling(5).mean()
    df["cross"] = 0
    prev_above = df["ema_short"].shift(1) > df["ema_long"].shift(1)
    curr_above = df["ema_short"] > df["ema_long"]
    df.loc[~prev_above & curr_above, "cross"] = 1
    df.loc[prev_above & ~curr_above, "cross"] = -1

    latest = df.iloc[-1]
    close = latest["close"]
    ema_s, ema_l, ema_t = latest["ema_short"], latest["ema_long"], latest["ema_trend"]
    vol, vol_ma5 = latest["volume"], latest["vol_ma5"]
    cross = latest["cross"]

    is_above = ema_s > ema_l
    trend_up = close > ema_t
    bullish_alignment = ema_s > ema_l > ema_t
    volume_confirm = vol > vol_ma5 * p["volume_ratio"] if vol_ma5 > 0 else False

    # 标准买入：金叉+趋势+放量
    if cross == 1 and trend_up and volume_confirm:
        strength = 0.8 if bullish_alignment else 0.6
        return "BUY", strength, "EMA金叉+放量"

    # 软买入：多头排列就买
    if is_above and trend_up and bullish_alignment:
        return "BUY", 0.4, "EMA多头排列"

    if cross == -1:
        return "SELL", 0.8, "EMA死叉"
    if close < ema_t and df.iloc[-2]["close"] > df.iloc[-2]["ema_trend"]:
        return "SELL", 0.7, "跌破趋势线"

    return "HOLD", 0, "无明显信号"


def old_donchian_signal(df: pd.DataFrame) -> tuple:
    """旧Donchian策略信号 (donchian.py 改动前)"""
    p = {"entry_period": 20, "exit_period": 10, "atr_period": 20, "atr_filter_multiple": 0.5}
    df = df.copy()
    upper, lower, _ = calc_donchian(df["high"], df["low"], p["entry_period"])
    _, exit_lower, _ = calc_donchian(df["high"], df["low"], p["exit_period"])
    df["dc_upper"] = upper
    df["dc_lower"] = lower
    df["dc_exit_lower"] = exit_lower
    df["atr"] = calc_atr(df["high"], df["low"], df["close"], p["atr_period"])

    latest = df.iloc[-1]
    prev = df.iloc[-2]
    close = latest["close"]
    dc_upper, dc_lower = latest["dc_upper"], latest["dc_lower"]
    dc_exit_lower = latest["dc_exit_lower"]
    atr = latest["atr"]

    # 标准买入：突破N日新高
    if not pd.isna(dc_upper) and close > dc_upper:
        breakout_pct = (close - dc_upper) / close if close > 0 else 0
        atr_pct = (atr * p["atr_filter_multiple"]) / close if close > 0 else 0
        if atr_pct == 0 or breakout_pct >= atr_pct:
            strength = 0.9 if close > prev["close"] else 0.6
            return "BUY", strength, f"突破{p['entry_period']}日新高"

    if not pd.isna(dc_exit_lower) and close < dc_exit_lower:
        return "SELL", 0.8, f"跌破{p['exit_period']}日新低"

    # 软买入：通道上部>70%就买
    if not pd.isna(dc_upper) and not pd.isna(dc_lower):
        pos = (close - dc_lower) / (dc_upper - dc_lower) if dc_upper > dc_lower else 0.5
        if pos > 0.7:
            return "BUY", 0.3, f"通道上部({pos:.0%})"

    return "HOLD", 0, "通道中部"


def old_macd_signal(df: pd.DataFrame) -> tuple:
    """旧MACD策略信号 (macd_trend.py 改动前)"""
    p = {"fast": 12, "slow": 26, "signal": 9, "ma_filter": 60, "hist_threshold": 0}
    df = df.copy()
    dif, dea, hist = calc_macd(df["close"], p["fast"], p["slow"], p["signal"])
    df["dif"], df["dea"], df["hist"] = dif, dea, hist
    df["ma_filter"] = calc_sma(df["close"], p["ma_filter"])
    df["macd_cross"] = 0
    prev_above = df["dif"].shift(1) > df["dea"].shift(1)
    curr_above = df["dif"] > df["dea"]
    df.loc[~prev_above & curr_above, "macd_cross"] = 1
    df.loc[prev_above & ~curr_above, "macd_cross"] = -1
    df["hist_change"] = df["hist"].diff()

    latest = df.iloc[-1]
    close = latest["close"]
    dif_v, dea_v, hist_v = latest["dif"], latest["dea"], latest["hist"]
    ma = latest["ma_filter"]
    macd_cross = latest["macd_cross"]
    hist_change = latest["hist_change"]

    trend_up = close > ma if not pd.isna(ma) else False

    # 标准买入：金叉+趋势+柱状图
    if macd_cross == 1 and trend_up and hist_v > p["hist_threshold"]:
        strength = 0.85 if dif_v > 0 else 0.6
        return "BUY", strength, "MACD金叉"

    # 软买入：零轴上方就买
    if dif_v > 0 and dea_v > 0 and dif_v > dea_v and trend_up:
        return "BUY", 0.5, "MACD零轴上方"

    if macd_cross == -1:
        return "SELL", 0.8, "MACD死叉"
    if len(df) >= 4:
        hist_shrinking = all(df["hist_change"].iloc[-3:] < 0)
        if hist_shrinking and hist_v > 0:
            return "SELL", 0.6, "柱状图连续缩短"
    if close < ma and df.iloc[-2]["close"] > df.iloc[-2]["ma_filter"]:
        return "SELL", 0.5, "跌破MA"

    return "HOLD", 0, "无明显信号"


def old_combo_signal(df: pd.DataFrame) -> tuple:
    """旧组合策略：3策略投票"""
    signals = []
    for name, fn in [("EMA", old_ema_signal), ("Don", old_donchian_signal), ("MACD", old_macd_signal)]:
        sig, strength, reason = fn(df)
        signals.append((name, sig, strength, reason))

    buy_count = sum(1 for _, s, _, _ in signals if s == "BUY")
    sell_count = sum(1 for _, s, _, _ in signals if s == "SELL")

    if buy_count >= 2:
        avg_str = np.mean([s for _, sig, s, _ in signals if sig == "BUY"])
        return "BUY", avg_str, signals, f"{buy_count}/3票买入"
    if sell_count >= 2:
        avg_str = np.mean([s for _, sig, s, _ in signals if sig == "SELL"])
        return "SELL", avg_str, signals, f"{sell_count}/3票卖出"
    if buy_count == 3:
        avg_str = np.mean([s for _, sig, s, _ in signals if sig == "BUY"])
        return "BUY", avg_str, signals, "全票看多"

    return "HOLD", 0, signals, f"信号不一致(买{buy_count}/卖{sell_count}/持{3-buy_count-sell_count})"


# ============================================================
# 新逻辑：import修改后的策略模块
# ============================================================

def new_combo_signal(df: pd.DataFrame, combo: ComboStrategy) -> tuple:
    """新组合策略信号"""
    result = combo.generate_signal(df)
    # 也获取各子策略投票
    vote_detail = []
    for strategy in combo.strategies:
        r = strategy.generate_signal(df)
        vote_detail.append((strategy.name, r.signal.value, r.strength, r.reason))
    return result.signal.value, result.strength, vote_detail, result.reason


# ============================================================
# 主流程
# ============================================================

def main():
    print("=" * 90)
    print("  📊 软买入 vs 动量评分 策略信号对比")
    print(f"  日期: {datetime.date.today()}")
    print("=" * 90)

    combo = ComboStrategy()

    results = []
    failed = []

    for code, name in STOCKS:
        print(f"\n  [{code}] {name} 拉取数据中...", end=" ")
        try:
            df = get_hist_daily(code, 180)
            print(f"数据{len(df)}条, 最新:{df.iloc[-1]['date'].strftime('%Y-%m-%d')} close={df.iloc[-1]['close']:.2f}")

            # 旧逻辑
            old_sig, old_str, old_votes, old_reason = old_combo_signal(df)

            # 新逻辑
            new_sig, new_str, new_votes, new_reason = new_combo_signal(df, combo)

            # 动量评分
            mom_score, r2 = calc_momentum_score(df["close"], 25)

            # 距MA60
            ma60 = df["close"].rolling(60).mean().iloc[-1]
            dev_ma60 = (df["close"].iloc[-1] - ma60) / ma60 * 100 if ma60 > 0 else 0

            # 距52周高点回撤
            high_252 = df["close"].rolling(min(252, len(df))).max().iloc[-1]
            drawdown = (df["close"].iloc[-1] - high_252) / high_252 * 100

            results.append({
                "code": code, "name": name,
                "close": df["close"].iloc[-1],
                "mom_score": mom_score, "r2": r2,
                "dev_ma60": dev_ma60, "drawdown": drawdown,
                "old_sig": old_sig, "old_str": old_str,
                "old_votes": old_votes, "old_reason": old_reason,
                "new_sig": new_sig, "new_str": new_str,
                "new_votes": new_votes, "new_reason": new_reason,
            })
        except Exception as e:
            print(f"失败: {e}")
            failed.append((code, name, str(e)))

    # ============================================================
    # 输出对比表
    # ============================================================
    print("\n" + "=" * 90)
    print("  📋 信号对比总表")
    print("=" * 90)

    print(f"\n{'代码':<10} {'名称':<8} {'动量评分':>8} {'R²':>5} {'距MA60':>7} {'回撤':>6} │ {'旧信号':<6} {'旧强度':>6} {'旧投票':<12} │ {'新信号':<6} {'新强度':>6} {'新投票':<12} │ {'变化':<8}")
    print("─" * 130)

    for r in results:
        old_vote_str = "/".join([f"{n[0]}={s[0].upper()}" for n, s, _, _ in r["old_votes"]])
        new_vote_str = "/".join([f"{n[:3]}={s[0].upper()}" for n, s, _, _ in r["new_votes"]])

        # 变化标记
        if r["old_sig"] != r["new_sig"]:
            change = f"{r['old_sig']}→{r['new_sig']}"
        else:
            change = "不变" if abs(r["old_str"] - r["new_str"]) < 0.01 else f"强度{r['old_str']:.2f}→{r['new_str']:.2f}"

        print(f"{r['code']:<10} {r['name']:<8} {r['mom_score']:>8.2f} {r['r2']:>5.2f} {r['dev_ma60']:>+6.1f}% {r['drawdown']:>+5.1f}% │ "
              f"{r['old_sig']:<6} {r['old_str']:>6.2f} {old_vote_str:<12} │ "
              f"{r['new_sig']:<6} {r['new_str']:>6.2f} {new_vote_str:<12} │ {change}")

    # ============================================================
    # 变化详情
    # ============================================================
    changed = [r for r in results if r["old_sig"] != r["new_sig"]]
    if changed:
        print(f"\n{'='*90}")
        print(f"  🔀 信号变化的股票（{len(changed)}只）")
        print(f"{'='*90}")
        for r in changed:
            print(f"\n  ■ {r['name']}({r['code']}) {r['old_sig']} → {r['new_sig']}")
            print(f"    动量评分={r['mom_score']:.2f}  R²={r['r2']:.2f}  现价={r['close']:.2f}  距MA60={r['dev_ma60']:+.1f}%  回撤={r['drawdown']:+.1f}%")
            print(f"    旧: {r['old_reason']}")
            for n, s, st, reason in r["old_votes"]:
                print(f"       {n}: {s}(strength={st:.2f}) - {reason}")
            print(f"    新: {r['new_reason']}")
            for n, s, st, reason in r["new_votes"]:
                print(f"       {n}: {s}(strength={st:.2f}) - {reason}")
    else:
        print(f"\n  ✅ 所有股票信号未发生变化（仅strength可能调整）")

    # ============================================================
    # 汇总统计
    # ============================================================
    print(f"\n{'='*90}")
    print("  📈 汇总统计")
    print(f"{'='*90}")

    old_buys = sum(1 for r in results if r["old_sig"].upper() == "BUY")
    old_sells = sum(1 for r in results if r["old_sig"].upper() == "SELL")
    old_holds = sum(1 for r in results if r["old_sig"].upper() == "HOLD")
    new_buys = sum(1 for r in results if r["new_sig"].upper() == "BUY")
    new_sells = sum(1 for r in results if r["new_sig"].upper() == "SELL")
    new_holds = sum(1 for r in results if r["new_sig"].upper() == "HOLD")

    print(f"\n  {'':>12} {'BUY':>6} {'SELL':>6} {'HOLD':>6}")
    print(f"  {'旧(软买入)':>12} {old_buys:>6} {old_sells:>6} {old_holds:>6}")
    print(f"  {'新(动量评分)':>12} {new_buys:>6} {new_sells:>6} {new_holds:>6}")
    print(f"  {'变化':>12} {new_buys-old_buys:>+6} {new_sells-old_sells:>+6} {new_holds-old_holds:>+6}")

    # strength对比
    print(f"\n  强度对比（仅BUY信号）:")
    print(f"  {'股票':<12} {'旧强度':>8} {'新强度':>8} {'变化':>8} {'动量评分':>8} {'R²':>6}")
    for r in results:
        old_is_buy = r["old_sig"].upper() == "BUY"
        new_is_buy = r["new_sig"].upper() == "BUY"
        if old_is_buy or new_is_buy:
            old_s = r["old_str"] if old_is_buy else 0
            new_s = r["new_str"] if new_is_buy else 0
            if old_is_buy and new_is_buy:
                delta_str = f"{new_s - old_s:+.2f}"
            else:
                delta_str = "N/A"
            print(f"  {r['name']:<12} {old_s:>8.2f} {new_s:>8.2f} {delta_str:>8} {r['mom_score']:>8.2f} {r['r2']:>6.2f}")

    if failed:
        print(f"\n⚠️ 数据拉取失败（{len(failed)}只）：")
        for code, name, reason in failed:
            print(f"  {name}({code}): {reason}")

    print("\n" + "=" * 90)
    print("  ⚠️ 以上为策略信号对比，基于收盘价生成，不构成投资建议。仅供研究参考。")
    print("=" * 90)


if __name__ == "__main__":
    main()
