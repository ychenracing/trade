#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
组合级回测脚本 - 13只AI产业链股票共享资金池
使用现有策略(EMA+Donchian+MACD组合) + 现有风控(ATR止损+峰值trailing+熔断)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import datetime
import json
import pandas as pd
import numpy as np

from execution.simulator import SimulatorExecutor
from risk.manager import RiskManager
from risk.position_sizer import PositionSizer
from strategy.combo import ComboStrategy
from utils.helpers import calc_atr, calc_sma, format_code
from utils.logger import log, setup_logger
from engine.portfolio_engine import run_portfolio_trading_day

setup_logger()
log.remove()
log.add(sys.stderr, level="WARNING",
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>")


# ─── 股票池 (13只AI产业链) ───
STOCK_POOL = [
    ("300308", "中际旭创"),
    ("300502", "新易盛"),
    ("300394", "天孚通信"),
    ("688008", "澜起科技"),
    ("603986", "兆易创新"),
    ("002409", "雅克科技"),
    ("688072", "拓荆科技"),
    ("688110", "联瑞新材"),
    ("300054", "鼎龙股份"),
    ("688535", "华海诚科"),
    ("300776", "帝尔激光"),
    ("688205", "德科立"),
    # ("920045", "蘅东光"),  # 北交所，akshare可能不支持
]


def fetch_stock_data(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """使用akshare新浪接口获取日K数据（最稳定，见TOOLS.md）"""
    import akshare as ak

    code = format_code(code)
    if code.startswith("6"):
        symbol = f"sh{code}"
    elif code.startswith("8") or code.startswith("9"):
        symbol = f"bj{code}"
    else:
        symbol = f"sz{code}"

    # stock_zh_a_daily 接受 YYYYMMDD 格式
    start = start_date.replace("-", "")
    end = end_date.replace("-", "")

    try:
        df = ak.stock_zh_a_daily(symbol=symbol, start_date=start, end_date=end, adjust="qfq")
        if df.empty:
            print(f"  数据为空: {code}")
            return pd.DataFrame()

        # 统一列名（新浪接口返回的是英文列名）
        # date列可能是datetime.date对象，需要转换（见TOOLS.md）
        df["date"] = pd.to_datetime(df["date"].astype(str))
        df = df.sort_values("date").reset_index(drop=True)

        # 确保数值类型
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna(subset=["close"])
        return df
    except Exception as e:
        print(f"  获取失败({code}): {e}")
        return pd.DataFrame()


def run_portfolio_backtest(
    stock_pool: list,
    start_date: str,
    end_date: str,
    initial_capital: float = 2_000_000,
    warmup: int = 70,
    verbose: bool = True,
):
    """运行组合回测"""
    print(f"\n{'='*70}")
    print(f"  组合级回测")
    print(f"  回测区间: {start_date} ~ {end_date}")
    print(f"  初始资金: {initial_capital:,.0f}")
    print(f"  股票数量: {len(stock_pool)}")
    print(f"{'='*70}")

    # 1. 获取所有股票数据
    print(f"\n获取数据...")
    stocks_data = {}
    stock_names = {}
    for code, name in stock_pool:
        code = format_code(code)
        print(f"  {name}({code})...", end=" ", flush=True)
        df = fetch_stock_data(code, start_date, end_date)
        if not df.empty:
            stocks_data[code] = df
            stock_names[code] = name
            print(f"{len(df)}条, {df.iloc[0]['date'].date()} ~ {df.iloc[-1]['date'].date()}")
        else:
            print("失败")

    print(f"\n成功获取: {len(stocks_data)}/{len(stock_pool)}只")
    if not stocks_data:
        print("无可用数据")
        return None

    # 2. 构建日期索引
    # 每只股票的 date→row_idx 映射
    stock_date_map = {}  # {symbol: {date: row_idx}}
    for symbol, df in stocks_data.items():
        stock_date_map[symbol] = {}
        for i in range(len(df)):
            d = df.iloc[i]["date"]
            if hasattr(d, 'date'):
                d = d.date()
            elif isinstance(d, str):
                d = pd.to_datetime(d).date()
            stock_date_map[symbol][d] = i

    # 所有日期的并集（按交易日排序）
    all_dates = sorted(set().union(*[set(m.keys()) for m in stock_date_map.values()]))
    print(f"交易日总数: {len(all_dates)}")

    # 3. 初始化共享组件
    simulator = SimulatorExecutor(initial_capital)
    risk_mgr = RiskManager(initial_capital)
    position_sizer = PositionSizer(initial_capital)
    strategies = {symbol: ComboStrategy() for symbol in stocks_data}

    trade_log = []
    daily_nav = []
    signal_log = []

    # 4. 逐日回测
    print(f"\n开始回测 (warmup={warmup}天)...")
    circuit_breaker_count = 0

    for date in all_dates:
        # 找出当日有数据且过warmup的股票
        active_stocks = {}
        for symbol, date_map in stock_date_map.items():
            if date in date_map:
                idx = date_map[date]
                if idx >= warmup:
                    active_stocks[symbol] = idx

        if not active_stocks:
            continue

        # 每周一打印进度
        if verbose and date.weekday() == 0:
            account = simulator.get_account()
            print(f"  {date} | NAV={account.total_value:>12,.0f} | "
                  f"持仓={len(simulator.positions)}只 | "
                  f"现金={simulator.cash:>12,.0f}")

        ok = run_portfolio_trading_day(
            date=date,
            stocks_data=stocks_data,
            stock_indices=active_stocks,
            stock_names=stock_names,
            simulator=simulator,
            risk_mgr=risk_mgr,
            position_sizer=position_sizer,
            strategies=strategies,
            trade_log=trade_log,
            signal_log=signal_log,
            daily_nav=daily_nav,
            verbose=verbose,
        )

        if not ok:
            circuit_breaker_count += 1
            account = simulator.get_account()
            print(f"  [熔断] {date} | NAV={account.total_value:,.0f} | "
                  f"回撤={risk_mgr.status.current_drawdown:.2%}")

    # 5. 统计结果
    result = _print_portfolio_results(
        daily_nav, trade_log, signal_log,
        initial_capital, stocks_data, stock_names,
        circuit_breaker_count,
    )

    # 6. 保存结果
    _save_results(daily_nav, trade_log, signal_log, result, initial_capital)

    return {
        "daily_nav": daily_nav,
        "trade_log": trade_log,
        "signal_log": signal_log,
        "stats": result,
        "initial_capital": initial_capital,
    }


def _print_portfolio_results(
    daily_nav, trade_log, signal_log,
    initial_capital, stocks_data, stock_names,
    circuit_breaker_count,
):
    """打印组合回测结果"""
    if not daily_nav:
        print("无回测数据")
        return {}

    nav_df = pd.DataFrame(daily_nav)
    trade_df = pd.DataFrame(trade_log) if trade_log else pd.DataFrame()

    final_nav = daily_nav[-1]["nav"]
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
    if len(trade_df) > 0:
        sell_trades = trade_df[trade_df["action"].str.contains("SELL", na=False)]
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
    else:
        sell_trades = pd.DataFrame()
        win_rate = 0
        avg_profit = 0
        avg_loss = 0
        profit_loss_ratio = 0

    # 买入持有基准（等权组合）
    bh_returns = []
    for symbol, df in stocks_data.items():
        if len(df) > 0:
            bh_return = (df.iloc[-1]["close"] / df.iloc[0]["close"] - 1)
            bh_returns.append(bh_return)
    bh_avg = np.mean(bh_returns) if bh_returns else 0

    # 输出
    print(f"\n{'='*70}")
    print(f"  组合级回测结果")
    print(f"{'='*70}")

    print(f"\n  绩效指标")
    print(f"  {'-'*50}")
    print(f"  初始资金:       {initial_capital:>15,.0f}")
    print(f"  最终净值:       {final_nav:>15,.0f}")
    print(f"  总收益率:       {total_return:>+15.2%}")
    print(f"  年化收益率:     {annual_return:>+15.2%}")
    print(f"  最大回撤:       {max_drawdown:>15.2%}")
    print(f"  夏普比率:       {sharpe:>15.2f}")
    print(f"  交易天数:       {days:>15}")
    print(f"  熔断次数:       {circuit_breaker_count:>15}")
    print(f"  买入持有(等权): {bh_avg:>+15.2%}")
    print(f"  超越买入持有:   {total_return - bh_avg:>+15.2%}")

    print(f"\n  交易统计")
    print(f"  {'-'*50}")
    print(f"  总交易次数:     {len(trade_df):>15}")
    if len(trade_df) > 0:
        buys = trade_df[trade_df["action"].str.contains("BUY", na=False)]
        sells = trade_df[trade_df["action"].str.contains("SELL", na=False)]
        print(f"  买入次数:       {len(buys):>15}")
        print(f"  卖出次数:       {len(sells):>15}")
    print(f"  胜率:           {win_rate:>15.2%}")
    print(f"  盈亏比:         {profit_loss_ratio:>15.2f}")
    print(f"  平均盈利:       {avg_profit:>+15,.0f}")
    print(f"  平均亏损:       {avg_loss:>+15,.0f}")

    # 每只股票的贡献
    if len(trade_df) > 0 and "symbol" in trade_df.columns:
        print(f"\n  各股票盈亏贡献")
        print(f"  {'-'*50}")
        sell_by_stock = trade_df[trade_df["action"].str.contains("SELL", na=False)]
        if len(sell_by_stock) > 0:
            stock_pnl = sell_by_stock.groupby("name").agg(
                total_pnl=("pnl", "sum"),
                trade_count=("pnl", "count"),
                win_count=("pnl", lambda x: (x > 0).sum()),
            )
            stock_pnl = stock_pnl.sort_values("total_pnl", ascending=False)
            for name, row in stock_pnl.iterrows():
                wr = row["win_count"] / row["trade_count"] if row["trade_count"] > 0 else 0
                print(f"  {name:<10} 盈亏={row['total_pnl']:>+12,.0f}  "
                      f"交易={int(row['trade_count']):>3}笔  胜率={wr:.0%}")

    # 月度净值
    nav_df["month"] = pd.to_datetime(nav_df["date"]).dt.to_period("M")
    monthly = nav_df.groupby("month").agg(
        end_nav=("nav", "last"),
        max_dd=("dd", "max"),
        pos_count=("position_count", "mean"),
    )
    monthly["ret"] = monthly["end_nav"].pct_change()
    print(f"\n  月度净值")
    print(f"  {'-'*60}")
    for m, row in monthly.iterrows():
        ret_str = f"{row['ret']:+.2%}" if pd.notna(row['ret']) else "   ---"
        print(f"  {m}  净值={row['end_nav']:>12,.0f}  月收益={ret_str:>8}  "
              f"回撤={row['max_dd']:+.2%}  持仓={row['pos_count']:.1f}只")

    print(f"\n{'='*70}")

    return {
        "final_nav": final_nav,
        "total_return": total_return,
        "annual_return": annual_return,
        "max_drawdown": max_drawdown,
        "sharpe": sharpe,
        "win_rate": win_rate,
        "profit_loss_ratio": profit_loss_ratio,
        "trade_count": len(trade_df),
        "circuit_breaker_count": circuit_breaker_count,
        "buy_hold_avg": bh_avg,
    }


def _save_results(daily_nav, trade_log, signal_log, stats, initial_capital):
    """保存结果到JSON"""
    result = {
        "stats": stats,
        "initial_capital": initial_capital,
        "trade_count": len(trade_log),
        "trades": trade_log,
        "nav_count": len(daily_nav),
    }
    output_file = "backtest_portfolio_result.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n结果已保存: {output_file}")


if __name__ == "__main__":
    run_portfolio_backtest(
        stock_pool=STOCK_POOL,
        start_date="2024-07-01",
        end_date="2026-07-04",
        initial_capital=2_000_000,
        warmup=70,
        verbose=True,
    )
