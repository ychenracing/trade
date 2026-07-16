"""
午间策略扫描 - 用上午收盘数据跑策略
1. 拉取历史日K（新浪接口，稳定）
2. 拉取实时行情（腾讯接口，稳定）
3. 拼接成完整数据
4. 运行组合策略
"""
import sys
import datetime
import time
import requests
import pandas as pd
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from strategy.combo import ComboStrategy
from strategy.base import Signal
from utils.helpers import calc_sma, calc_trend_mode, calc_atr

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
    ("sh688535", "华海诚科"),
    ("sz300776", "帝尔激光"),
    ("sh688205", "德科立"),
    ("bj920045", "蘅东光"),
]


def get_realtime_quote_tencent(codes: list) -> dict:
    """腾讯实时行情接口"""
    url = f"https://qt.gtimg.cn/q={','.join(codes)}"
    resp = requests.get(url, timeout=10)
    resp.encoding = "gbk"
    quotes = {}
    for line in resp.text.strip().split(";"):
        line = line.strip()
        if not line:
            continue
        try:
            # v_sz300308="1~中际旭创~300308~100.00~99.00~..."
            prefix, data = line.split("=", 1)
            data = data.strip('"')
            fields = data.split("~")
            if len(fields) < 38:
                continue
            code = prefix.split("_")[-1]
            quotes[code] = {
                "name": fields[1],
                "price": float(fields[3]) if fields[3] else 0,
                "pre_close": float(fields[4]) if fields[4] else 0,
                "open": float(fields[5]) if fields[5] else 0,
                "volume": int(float(fields[6])) if fields[6] else 0,  # 成交量(手)
                "datetime": fields[30] if len(fields) > 30 else "",
                "change_pct": float(fields[32]) if fields[32] else 0,
                "high": float(fields[33]) if fields[33] else 0,
                "low": float(fields[34]) if fields[34] else 0,
                "amount": float(fields[37]) * 10000 if fields[37] else 0,  # 成交额(元)
                "turnover": float(fields[38]) if len(fields) > 38 and fields[38] else 0,
            }
        except Exception as e:
            print(f"  [warn] 解析行情失败: {e}, line={line[:80]}")
    return quotes


def get_hist_daily(code: str, days: int = 120) -> pd.DataFrame:
    """用akshare新浪接口拉历史日K（最稳定）"""
    import akshare as ak
    end = datetime.date.today().strftime("%Y%m%d")
    start = (datetime.date.today() - datetime.timedelta(days=days + 60)).strftime("%Y%m%d")
    try:
        df = ak.stock_zh_a_daily(symbol=code, start_date=start, end_date=end, adjust="qfq")
        if df.empty:
            return pd.DataFrame()
        # 新浪接口列名: date, open, high, low, close, volume, outstanding_share, turnover
        df = df.rename(columns={"date": "date"})
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df[["date", "open", "high", "low", "close", "volume"]].copy()
    except Exception as e:
        print(f"  [error] 拉取{code}历史数据失败: {e}")
        return pd.DataFrame()


def build_today_bar(quote: dict) -> dict:
    """用实时行情构造今日K线"""
    today = pd.Timestamp(datetime.date.today())
    return {
        "date": today,
        "open": quote["open"],
        "high": quote["high"],
        "low": quote["low"],
        "close": quote["price"],
        "volume": quote["volume"] * 100,  # 手 → 股
    }


def main():
    print("=" * 70)
    print(f"  午间策略扫描 | {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  股票池: {len(STOCKS)}只 | 策略: Multi_Trend_Combo (EMA+Donchian+MACD)")
    print("=" * 70)

    # 1. 拉实时行情
    print("\n[1/3] 拉取实时行情（腾讯接口）...")
    codes = [c for c, _ in STOCKS]
    quotes = get_realtime_quote_tencent(codes)
    print(f"  成功获取 {len(quotes)}/{len(codes)} 只股票行情")
    for code, name in STOCKS:
        if code in quotes:
            q = quotes[code]
            print(f"    {name:6s} ({code})  现价:{q['price']:>8.2f}  "
                  f"涨跌:{q['change_pct']:>+6.2f}%  成交额:{q['amount']/1e8:.2f}亿")

    # 2. 拉历史数据 + 拼接今日bar
    print("\n[2/3] 拉取历史日K并拼接今日数据...")
    strategy = ComboStrategy()
    datasets = {}

    for code, name in STOCKS:
        if code not in quotes:
            print(f"  [skip] {name} ({code}) 无实时行情")
            continue

        df = get_hist_daily(code, days=120)
        if df.empty or len(df) < 60:
            print(f"  [skip] {name} ({code}) 历史数据不足 ({len(df)}条)")
            continue

        # 去掉今天的重复数据（如果新浪接口已包含今天）
        today_str = datetime.date.today().isoformat()
        df = df[df["date"].dt.strftime("%Y-%m-%d") != today_str].copy()

        # 拼接今日bar
        today_bar = build_today_bar(quotes[code])
        df = pd.concat([df, pd.DataFrame([today_bar])], ignore_index=True)
        df = df.sort_values("date").reset_index(drop=True)

        datasets[code] = (name, df)
        print(f"  {name:6s} ({code})  数据:{len(df)}条  最新:{df.iloc[-1]['date'].strftime('%Y-%m-%d')}  "
              f"close={df.iloc[-1]['close']:.2f}")

    # 3. 运行策略
    print(f"\n[3/3] 运行组合策略（{strategy.get_strategy_names()}）...")
    print("-" * 70)

    buy_list = []
    sell_list = []
    hold_list = []

    for code, (name, df) in datasets.items():
        result = strategy.generate_signal(df)

        # 计算辅助指标
        close = df["close"]
        ma20 = calc_sma(close, 20).iloc[-1]
        ma60 = calc_sma(close, 60).iloc[-1]
        atr = calc_atr(df["high"], df["low"], close, 20).iloc[-1]
        atr_pct = atr / close.iloc[-1] * 100 if close.iloc[-1] > 0 else 0
        trend = calc_trend_mode(df)

        # 20日涨幅
        if len(close) >= 21:
            gain_20d = (close.iloc[-1] / close.iloc[-21] - 1) * 100
        else:
            gain_20d = 0

        # 距MA60偏离
        dev_ma60 = (close.iloc[-1] / ma60 - 1) * 100 if ma60 > 0 else 0

        icon = {"buy": "🟢", "sell": "🔴", "hold": "⚪"}[result.signal.value]
        print(f"\n  {icon} {name} ({code})")
        print(f"     信号: {result.signal.value.upper()}  强度: {result.strength:.2f}")
        print(f"     原因: {result.reason}")
        print(f"     现价: {close.iloc[-1]:.2f}  MA20: {ma20:.2f}  MA60: {ma60:.2f}")
        print(f"     偏离MA60: {dev_ma60:+.1f}%  20日涨幅: {gain_20d:+.1f}%  ATR: {atr_pct:.2f}%")
        print(f"     趋势: {trend}")

        if result.signal == Signal.BUY:
            buy_list.append((code, name, result))
        elif result.signal == Signal.SELL:
            sell_list.append((code, name, result))
        else:
            hold_list.append((code, name, result))

    # 汇总
    print("\n" + "=" * 70)
    print("  扫描结果汇总")
    print("=" * 70)
    print(f"  🟢 买入信号: {len(buy_list)}只  |  🔴 卖出信号: {len(sell_list)}只  |  ⚪ 持有: {len(hold_list)}只")

    if buy_list:
        print("\n  🟢 买入候选:")
        for code, name, r in buy_list:
            print(f"     {name} ({code})  强度={r.strength:.2f}  {r.reason}")

    if sell_list:
        print("\n  🔴 卖出候选:")
        for code, name, r in sell_list:
            print(f"     {name} ({code})  强度={r.strength:.2f}  {r.reason}")

    if not buy_list and not sell_list:
        print("\n  ⚠️ 无明确买卖信号，市场可能处于震荡或趋势不明确状态")

    print("\n" + "=" * 70)
    print("  ⚠️ 以上为策略信号，不构成投资建议。实际交易需结合仓位管理和风控。")
    print("=" * 70)


if __name__ == "__main__":
    main()
