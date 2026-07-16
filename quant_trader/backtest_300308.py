"""
中际旭创(300308) 单股回测脚本
使用yfinance真实历史数据
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
from utils.helpers import calc_commission, calc_stamp_tax, calc_atr, calc_sma, calc_trend_mode, round_lot, pct_change
from utils.logger import log, setup_logger
from engine.trade_engine import calc_daily_context, run_trading_day
import logging

# 降低日志级别，只显示WARNING以上
setup_logger()
log.remove()
log.add(sys.stderr, level="WARNING",
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>")


def fetch_real_data(symbol: str, start: str, end: str) -> pd.DataFrame:
    """通过yfinance获取真实A股数据"""
    ticker = yf.Ticker(symbol)
    df = ticker.history(start=start, end=end)

    if df.empty:
        raise ValueError(f"无法获取 {symbol} 数据")

    # 转换列名以匹配策略引擎格式
    df = df.rename(columns={
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume",
    })

    # 添加必要字段
    df["date"] = df.index
    df["amount"] = df["close"] * df["volume"]
    df = df.reset_index(drop=True)

    # 去除volume为0的异常数据
    df = df[df["volume"] > 0].reset_index(drop=True)

    log.info(f"获取 {symbol} 数据: {len(df)}条, "
             f"{df.iloc[0]['date'].date()} ~ {df.iloc[-1]['date'].date()}")
    log.info(f"期初收盘: {df.iloc[0]['close']:.2f}, "
             f"期末收盘: {df.iloc[-1]['close']:.2f}, "
             f"期间涨跌: {pct_change(df.iloc[-1]['close'], df.iloc[0]['close']):+.2%}")
    log.info(f"最高价: {df['high'].max():.2f}, 最低价: {df['low'].min():.2f}")

    return df


def run_backtest(symbol: str, name: str, start: str, end: str,
                 initial_capital: float = 2_000_000):
    """运行单股回测"""

    print("\n" + "=" * 70)
    print(f"  回测标的: {name}({symbol})")
    print(f"  回测区间: {start} ~ {end}")
    print(f"  初始资金: {initial_capital:,.0f}")
    print("=" * 70)

    # 1. 获取数据
    df = fetch_real_data(symbol, start, end)

    # 2. 初始化组件
    strategy = ComboStrategy()
    simulator = SimulatorExecutor(initial_capital)
    risk_mgr = RiskManager(initial_capital)
    position_sizer = PositionSizer(initial_capital)

    # 3. 回测变量
    trade_log = []          # 交易记录
    daily_nav = []          # 每日净值
    signal_log = []         # 每日信号记录
    warmup_days = 70           # 策略需要至少60+天数据来计算指标

    print(f"\n策略预热期: {warmup_days} 个交易日")
    print(f"实际交易期: 第{warmup_days+1}天 ~ 第{len(df)}天 ({len(df)-warmup_days}天)")
    print(f"策略组合: {strategy.get_strategy_names()}")
    print(f"投票模式: 多数表决(至少2票一致)")
    print()

    # 4. 逐日回测（调用公共交易引擎）
    for i in range(warmup_days, len(df)):
        ctx = calc_daily_context(df, i)
        run_trading_day(
            ctx, symbol, name,
            simulator, risk_mgr, position_sizer, strategy,
            trade_log, signal_log, daily_nav,
            verbose=True,
        )

    # 5. 计算回测指标
    print("\n" + "=" * 70)
    print("  回测结果")
    print("=" * 70)

    if not daily_nav:
        print("  无交易数据")
        return

    nav_df = pd.DataFrame(daily_nav)
    trade_df = pd.DataFrame(trade_log)
    signal_df = pd.DataFrame(signal_log)

    final_nav = nav_df.iloc[-1]["nav"]
    total_return = pct_change(final_nav, initial_capital)
    trading_days = len(nav_df)

    # 年化收益率
    years = trading_days / 252
    annual_return = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0

    # 最大回撤
    nav_df["peak"] = nav_df["nav"].cummax()
    nav_df["drawdown"] = (nav_df["nav"] - nav_df["peak"]) / nav_df["peak"]
    max_drawdown = nav_df["drawdown"].min()

    # 日收益率
    nav_df["daily_return"] = nav_df["nav"].pct_change()
    daily_returns = nav_df["daily_return"].dropna()

    # 夏普比率（无风险利率2.5%）
    if len(daily_returns) > 1 and daily_returns.std() > 0:
        sharpe = (daily_returns.mean() - 0.025/252) / daily_returns.std() * np.sqrt(252)
    else:
        sharpe = 0

    # 交易统计
    buy_trades = trade_df[trade_df["action"].str.contains("BUY")] if len(trade_df) > 0 else pd.DataFrame()
    sell_trades = trade_df[trade_df["action"].str.contains("SELL")] if len(trade_df) > 0 else pd.DataFrame()

    win_trades = sell_trades[sell_trades.get("pnl", pd.Series([0])) > 0] if len(sell_trades) > 0 else pd.DataFrame()
    lose_trades = sell_trades[sell_trades.get("pnl", pd.Series([0])) <= 0] if len(sell_trades) > 0 else pd.DataFrame()

    win_rate = len(win_trades) / len(sell_trades) if len(sell_trades) > 0 else 0
    avg_profit = win_trades["pnl"].mean() if len(win_trades) > 0 else 0
    avg_loss = lose_trades["pnl"].mean() if len(lose_trades) > 0 else 0
    profit_loss_ratio = abs(avg_profit / avg_loss) if avg_loss != 0 else float('inf')

    # 买入持有对比
    buy_hold_shares = round_lot(int(initial_capital * 0.20 / df.iloc[0]["close"]))  # 20%仓位
    buy_hold_value = buy_hold_shares * df.iloc[-1]["close"]
    buy_hold_return = pct_change(buy_hold_value, buy_hold_shares * df.iloc[0]["close"])

    print(f"\n  📊 绩效指标")
    print(f"  {'─' * 50}")
    print(f"  初始资金:         {initial_capital:>15,.0f}")
    print(f"  最终净值:         {final_nav:>15,.0f}")
    print(f"  总收益率:         {total_return:>+15.2%}")
    print(f"  年化收益率:       {annual_return:>+15.2%}")
    print(f"  最大回撤:         {max_drawdown:>+15.2%}")
    print(f"  夏普比率:         {sharpe:>15.2f}")
    print(f"  交易天数:         {trading_days:>15}")
    print()
    print(f"  📈 交易统计")
    print(f"  {'─' * 50}")
    print(f"  总交易次数:       {len(trade_df):>15}")
    print(f"  买入次数:         {len(buy_trades):>15}")
    print(f"  卖出次数:         {len(sell_trades):>15}")
    print(f"  胜率:             {win_rate:>15.2%}")
    print(f"  平均盈利:         {avg_profit:>+15,.0f}")
    print(f"  平均亏损:         {avg_loss:>+15,.0f}")
    print(f"  盈亏比:           {profit_loss_ratio:>15.2f}")
    print()
    print(f"  📉 对比基准")
    print(f"  {'─' * 50}")
    print(f"  股票期间涨跌:     {pct_change(df.iloc[-1]['close'], df.iloc[0]['close']):>+15.2%}")
    print(f"  买入持有(20%仓):  {buy_hold_return:>+15.2%}")
    print(f"  策略 vs 买入持有: {total_return - buy_hold_return:>+15.2%}")

    # 打印交易明细
    if len(trade_df) > 0:
        print(f"\n  📋 交易明细")
        print(f"  {'─' * 66}")
        print(f"  {'日期':<12} {'操作':<14} {'价格':>8} {'股数':>8} {'盈亏':>12} {'原因'}")
        print(f"  {'─' * 66}")
        for _, t in trade_df.iterrows():
            pnl_str = f"{t.get('pnl', 0):>+12,.0f}" if 'pnl' in t and pd.notna(t.get('pnl')) else ""
            print(f"  {str(t['date']):<12} {t['action']:<14} {t['price']:>8.2f} "
                  f"{t['shares']:>8} {pnl_str}  {t.get('reason', '')[:30]}")

    # 信号分布
    if len(signal_df) > 0:
        print(f"\n  📊 信号分布")
        print(f"  {'─' * 50}")
        signal_counts = signal_df["signal"].value_counts()
        for sig, count in signal_counts.items():
            pct = count / len(signal_df)
            bar = "█" * int(pct * 30)
            print(f"  {sig:<8} {count:>4}次 ({pct:>5.1%}) {bar}")

    # 净值曲线概览（按月汇总）
    print(f"\n  📈 月度净值")
    print(f"  {'─' * 50}")
    nav_df["month"] = pd.to_datetime(nav_df["date"]).dt.to_period("M")
    monthly = nav_df.groupby("month").agg(
        start_nav=("nav", "first"),
        end_nav=("nav", "last"),
        max_dd=("drawdown", "min"),
    )
    monthly["return"] = monthly["end_nav"] / monthly["start_nav"] - 1
    for m, row in monthly.iterrows():
        ret_str = f"{row['return']:+.2%}"
        dd_str = f"{row['max_dd']:+.2%}"
        print(f"  {m}  净值={row['end_nav']:>12,.0f}  月收益={ret_str:>8}  最大回撤={dd_str:>8}")

    print(f"\n{'=' * 70}")

    # 导出数据
    nav_df.to_csv("backtest_nav_300308.csv", index=False)
    if len(trade_df) > 0:
        trade_df.to_csv("backtest_trades_300308.csv", index=False)
    signal_df.to_csv("backtest_signals_300308.csv", index=False)
    print(f"\n  数据已导出: backtest_nav_300308.csv, backtest_trades_300308.csv, backtest_signals_300308.csv")

    return {
        "final_nav": final_nav,
        "total_return": total_return,
        "annual_return": annual_return,
        "max_drawdown": max_drawdown,
        "sharpe": sharpe,
        "win_rate": win_rate,
        "trades": len(trade_df),
        "buy_hold_return": buy_hold_return,
    }


if __name__ == "__main__":
    # 注意: 文件名为300308(中际旭创)，实际回测标的请按需修改
    # 如需回测中际旭创，将symbol改为 "300308.SZ", name改为 "中际旭创"
    run_backtest(
        symbol="300308.SZ",
        name="中际旭创",
        start="2024-01-01",
        end="2026-05-31",
        initial_capital=2_000_000,
    )
