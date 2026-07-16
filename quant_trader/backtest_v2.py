"""
多股票回测脚本 V2 - 优化版
优化1: 移动止盈 50% → 70%
优化2: ATR自适应止损
优化3: 高位动态降仓（涨幅>100%降30%, >200%降50%）
优化4: 突破确认加码（盈利10%后加仓50%）
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import datetime
import pandas as pd
import numpy as np
import yfinance as yf

from strategy.combo import ComboStrategy
from strategy.base import Signal
from execution.simulator import SimulatorExecutor
from risk.manager import RiskManager
from risk.position_sizer import PositionSizer, PositionInfo
from utils.helpers import calc_atr, calc_sma, round_lot, calc_trend_mode, pct_change
from utils.logger import log, setup_logger
from engine.trade_engine import calc_daily_context, run_trading_day

setup_logger()
log.remove()
log.add(sys.stderr, level="WARNING",
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>")


def fetch_real_data(symbol: str, start: str, end: str) -> pd.DataFrame:
    ticker = yf.Ticker(symbol)
    df = ticker.history(start=start, end=end)
    if df.empty:
        raise ValueError(f"无法获取 {symbol} 数据")
    df = df.rename(columns={
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume",
    })
    df["date"] = df.index
    df["amount"] = df["volume"] * df["close"]
    # 预计算ATR
    df["atr"] = calc_atr(df["high"], df["low"], df["close"], 20)
    # 预计算MA60用于趋势起点判断
    df["ma60"] = calc_sma(df["close"], 60)
    df = df.dropna(subset=["close"])
    print(f"获取 {symbol} 数据: {len(df)}条, {df.index[0].date()} ~ {df.index[-1].date()}")
    print(f"  期初收盘: {df.iloc[0]['close']:.2f}, 期末收盘: {df.iloc[-1]['close']:.2f}, "
          f"期间涨跌: {(df.iloc[-1]['close']/df.iloc[0]['close']-1)*100:.2f}%")
    return df


def run_backtest(symbol: str, name: str, start: str, end: str,
                 initial_capital: float = 2_000_000):
    print(f"\n{'='*70}")
    print(f"  回测标的: {name}({symbol})")
    print(f"  回测区间: {start} ~ {end}")
    print(f"  初始资金: {initial_capital:,.0f}")
    print(f"  优化版本: V2 (ATR止损+放宽止盈+高位降仓+加码)")
    print(f"{'='*70}")

    df = fetch_real_data(symbol, start, end)

    strategy = ComboStrategy()
    simulator = SimulatorExecutor(initial_capital)
    risk_mgr = RiskManager(initial_capital)
    position_sizer = PositionSizer(initial_capital)

    warmup = 70
    if len(df) <= warmup:
        print("数据不足")
        return

    print(f"\n策略预热期: {warmup} 个交易日")
    print(f"实际交易期: 第{warmup+1}天 ~ 第{len(df)}天 ({len(df)-warmup}天)")
    print(f"策略组合: {strategy.get_strategy_names()}")

    trade_log = []
    daily_nav = []
    signal_log = []

    for i in range(warmup, len(df)):
        ctx = calc_daily_context(df, i)
        run_trading_day(
            ctx, symbol, name,
            simulator, risk_mgr, position_sizer, strategy,
            trade_log, signal_log, daily_nav,
            verbose=True,
        )

    # ============ 统计结果 ============
    nav_df = pd.DataFrame(daily_nav)
    trade_df = pd.DataFrame(trade_log)

    final_nav = daily_nav[-1]["nav"] if daily_nav else initial_capital
    total_return = (final_nav - initial_capital) / initial_capital
    days = len(daily_nav)
    annual_return = ((1 + total_return) ** (252 / days) - 1) if days > 0 else 0

    nav_df["peak"] = nav_df["nav"].cummax()
    nav_df["dd"] = (nav_df["peak"] - nav_df["nav"]) / nav_df["peak"]
    max_drawdown = nav_df["dd"].max()

    nav_df["daily_ret"] = nav_df["nav"].pct_change()
    if nav_df["daily_ret"].std() > 0:
        sharpe = (nav_df["daily_ret"].mean() - 0.025/252) / nav_df["daily_ret"].std() * (252 ** 0.5)
    else:
        sharpe = 0

    # 交易统计
    sell_trades = trade_df[trade_df["action"].str.contains("SELL", na=False)] if len(trade_df) > 0 else pd.DataFrame()
    if len(sell_trades) > 0:
        wins = sell_trades[sell_trades["pnl"] > 0]
        losses = sell_trades[sell_trades["pnl"] <= 0]
        win_rate = len(wins) / len(sell_trades) if len(sell_trades) > 0 else 0
        avg_profit = wins["pnl"].mean() if len(wins) > 0 else 0
        avg_loss = losses["pnl"].mean() if len(losses) > 0 else 0
        profit_loss_ratio = abs(avg_profit / avg_loss) if avg_loss != 0 else float('inf')
    else:
        win_rate = 0
        avg_profit = 0
        avg_loss = 0
        profit_loss_ratio = 0

    # 买入持有
    # 买入持有（20%仓位，用实际股数计算）
    bh_shares = round_lot(int(initial_capital * 0.2 / df.iloc[0]["close"]))
    if bh_shares > 0:
        buy_hold_return = pct_change(bh_shares * df.iloc[-1]["close"], bh_shares * df.iloc[0]["close"])
    else:
        buy_hold_return = 0.0

    # ============ 输出 ============
    print(f"\n{'='*70}")
    print(f"  回测结果 V2")
    print(f"{'='*70}")
    print(f"\n  📊 绩效指标")
    print(f"  {'─'*50}")
    print(f"  初始资金:         {initial_capital:>15,.0f}")
    print(f"  最终净值:         {final_nav:>15,.0f}")
    print(f"  总收益率:         {total_return:>+15.2%}")
    print(f"  年化收益率:       {annual_return:>+15.2%}")
    print(f"  最大回撤:         {max_drawdown:>15.2%}")
    print(f"  夏普比率:         {sharpe:>15.2f}")
    print(f"  交易天数:         {days:>15}")
    print(f"  买入持有(20%仓):  {buy_hold_return:>+15.2%}")
    print(f"  超越买入持有:     {total_return - buy_hold_return:>+15.2%}")
    print(f"\n  📈 交易统计")
    print(f"  {'─'*50}")
    print(f"  总交易次数:       {len(trade_df):>15}")
    if len(trade_df) > 0:
        buys = trade_df[trade_df["action"].str.contains("BUY", na=False)]
        sells = trade_df[trade_df["action"].str.contains("SELL", na=False)]
        print(f"  买入次数:         {len(buys):>15}")
        print(f"  卖出次数:         {len(sells):>15}")
    print(f"  胜率:             {win_rate:>15.2%}")
    print(f"  盈亏比:           {profit_loss_ratio:>15.2f}")
    print(f"  平均盈利:         {avg_profit:>+15,.0f}")
    print(f"  平均亏损:         {avg_loss:>+15,.0f}")

    # 交易明细
    if len(trade_df) > 0:
        print(f"\n  📋 交易明细")
        print(f"  {'─'*50}")
        for _, r in trade_df.iterrows():
            pnl_str = f"{r.get('pnl', 0):>+10,.0f}" if pd.notna(r.get('pnl', None)) else ""
            gain_str = f"涨幅={r.get('price_gain', 0):.0%}" if pd.notna(r.get('price_gain', None)) else ""
            print(f"  {r['date']}  {r['action']:<12} {r['price']:>8.2f}  "
                  f"{r['shares']:>6}股  {pnl_str}  {gain_str}")

    # 月度
    nav_df["month"] = pd.to_datetime(nav_df["date"]).dt.to_period("M")
    monthly = nav_df.groupby("month").agg(
        end_nav=("nav", "last"),
        max_dd=("dd", "max"),
    )
    monthly["ret"] = monthly["end_nav"].pct_change()
    print(f"\n  📅 月度净值")
    print(f"  {'─'*50}")
    for m, row in monthly.iterrows():
        ret_str = f"{row['ret']:+.2%}" if pd.notna(row['ret']) else "   —"
        print(f"  {m}  净值={row['end_nav']:>12,.0f}  月收益={ret_str:>8}  最大回撤={row['max_dd']:+.2%}")

    print(f"\n{'='*70}")

    # 导出
    nav_df.to_csv(f"backtest_nav_{symbol.replace('.', '_')}_v2.csv", index=False)
    if len(trade_df) > 0:
        trade_df.to_csv(f"backtest_trades_{symbol.replace('.', '_')}_v2.csv", index=False)

    return {
        "name": name, "symbol": symbol,
        "final_nav": final_nav, "total_return": total_return,
        "annual_return": annual_return, "max_drawdown": max_drawdown,
        "sharpe": sharpe, "win_rate": win_rate,
        "trades": len(trade_df), "buy_hold": buy_hold_return,
        "profit_loss_ratio": profit_loss_ratio,
    }


def run_all():
    """批量回测多只股票"""
    stocks = [
        ("002028.SZ", "思源电气", "2025-01-01", "2026-05-31"),
        ("300394.SZ", "天孚通信", "2025-01-01", "2026-05-31"),
    ]

    results = []
    for symbol, name, start, end in stocks:
        result = run_backtest(symbol, name, start, end)
        results.append(result)

    # 汇总对比
    print(f"\n\n{'='*70}")
    print(f"  📊 多只股票 V1 vs V2 对比汇总")
    print(f"{'='*70}")
    print(f"\n  {'股票':<10} {'V1收益':>8} {'V2收益':>8} {'提升':>8} {'V1回撤':>8} {'V2回撤':>8} {'V1夏普':>8} {'V2夏普':>8}")
    print(f"  {'─'*70}")

    v1_results = {
        "思源电气": {"return": 0.5975, "dd": 0.0795, "sharpe": 2.46},
        "天孚通信": {"return": 0.3090, "dd": 0.0952, "sharpe": 1.43},
    }
    v2_results = {
        "思源电气": {"return": 0.8988, "dd": 0.1094, "sharpe": 2.58},
        "天孚通信": {"return": 0.4633, "dd": 0.1242, "sharpe": 1.64},
    }

    print(f"  {'股票':<10} {'V1收益':>8} {'V2收益':>8} {'当前收益':>8} {'V1→当前':>8} {'V1回撤':>8} {'当前回撤':>8} {'V1夏普':>7} {'当前夏普':>7}")
    print(f"  {'─'*78}")
    for r in results:
        v1 = v1_results.get(r["name"], {})
        v2 = v2_results.get(r["name"], {})
        v1_ret = v1.get("return", 0)
        v2_ret = v2.get("return", 0)
        improvement = r["total_return"] - v1_ret
        print(f"  {r['name']:<10} {v1_ret:>+7.2%} {v2_ret:>+7.2%} {r['total_return']:>+7.2%} "
              f"{improvement:>+7.2%} {v1.get('dd',0):>7.2%} {r['max_drawdown']:>7.2%} "
              f"{v1.get('sharpe',0):>7.2f} {r['sharpe']:>7.2f}")

    print(f"  {'─'*78}")
    print(f"\n  ✅ V1→V2 优化效果:")
    for r in results:
        v1 = v1_results.get(r["name"], {})
        v1_ret = v1.get("return", 0)
        diff = r["total_return"] - v1_ret
        emoji = "📈" if diff > 0 else ("📉" if diff < 0 else "➡️")
        print(f"  {emoji} {r['name']}: {v1_ret:>+.2%} → {r['total_return']:>+.2%} ({diff:>+.2%})")


if __name__ == "__main__":
    run_all()
