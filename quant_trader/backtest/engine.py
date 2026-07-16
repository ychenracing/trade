"""
回测引擎
- 模拟历史数据上的策略表现
- 支持多股票回测
- 输出收益曲线、夏普比率、最大回撤等指标
- 使用 engine.trade_engine.run_trading_day() 作为单一交易循环入口
"""
import datetime
from typing import Optional
import pandas as pd
import numpy as np
from tqdm import tqdm

from data.data_fetcher import DataFetcher
from strategy.combo import ComboStrategy
from strategy.base import Signal
from execution.simulator import SimulatorExecutor
from risk.manager import RiskManager
from risk.position_sizer import PositionInfo, PositionSizer
from utils.helpers import format_code, pct_change, calc_atr, calc_sma, calc_trend_mode, round_lot
from utils.logger import log
from config.settings import INITIAL_CAPITAL, RISK_CONFIG
from engine.trade_engine import calc_daily_context, run_trading_day


class BacktestEngine:
    """回测引擎"""

    def __init__(self, initial_capital: float = INITIAL_CAPITAL):
        self.initial_capital = initial_capital
        self.fetcher = DataFetcher()
        self.strategy = ComboStrategy()
        self.simulator = SimulatorExecutor(initial_capital)
        self.risk_manager = RiskManager(initial_capital)
        self.position_sizer = PositionSizer(initial_capital)

        # 回测结果
        self.nav_history: list[dict] = []     # 净值曲线
        self.trade_history: list[dict] = []    # 交易记录
        self.daily_returns: list[float] = []

    def run(
        self,
        stock_codes: list,
        start_date: str,
        end_date: str,
    ) -> dict:
        """
        运行回测（使用 trade_engine.run_trading_day 作为单一交易循环入口）
        stock_codes: 股票代码列表
        start_date: "20230101"
        end_date: "20231231"
        """
        log.info(f"开始回测: {start_date} ~ {end_date}, 股票数={len(stock_codes)}")

        # 1. 获取所有股票的历史数据
        log.info("正在获取历史数据...")
        all_data = {}
        for code in tqdm(stock_codes, desc="获取数据"):
            code = format_code(code)
            df = self.fetcher.get_daily_data(code, start_date, end_date)
            if not df.empty and len(df) > self.strategy.min_data_days:
                all_data[code] = df
        log.info(f"获取完成: {len(all_data)}只股票有有效数据")

        if not all_data:
            log.error("无有效数据, 回测终止")
            return {}

        # 2. 获取交易日历
        trade_days = self.fetcher.get_trade_calendar(start_date, end_date)
        log.info(f"交易日历: {len(trade_days)}天")

        # 3. 逐日回测 — 每只股票调用 run_trading_day
        trade_log: list[dict] = []
        signal_log: list[dict] = []
        for trade_date in tqdm(trade_days, desc="回测进度"):
            date_str = trade_date.strftime("%Y%m%d") if hasattr(trade_date, "strftime") else str(trade_date)

            # T+1日期滚动
            self.simulator._check_date_rollover(trade_date)

            # 逐股运行交易循环
            for code, df in all_data.items():
                # 截取到当前交易日
                df_up_to = self._slice_data(df, trade_date)
                if df_up_to is None or len(df_up_to) < self.strategy.min_data_days:
                    continue

                # 找到当前日在df_up_to中的index
                if isinstance(trade_date, str):
                    day_mask = df_up_to["date"].dt.strftime("%Y%m%d") == trade_date
                else:
                    day_mask = df_up_to["date"].dt.date == trade_date
                if not day_mask.any():
                    continue
                i = len(df_up_to) - 1  # 最后一天就是当前日

                # 获取股票名
                stock_name = self._get_stock_name(code)

                try:
                    ctx = calc_daily_context(df_up_to, i)
                    run_trading_day(
                        ctx, code, stock_name,
                        self.simulator, self.risk_manager,
                        self.position_sizer, self.strategy,
                        trade_log, signal_log, self.nav_history,
                        verbose=False,
                    )
                except Exception as e:
                    log.warning(f"[{date_str}] {code} run_trading_day异常: {e}")

        # 4. 计算回测指标
        log.info("回测完成, 计算绩效指标...")
        self.trade_history = trade_log
        results = self._calc_performance()
        return results

    def _get_day_mask(self, df: pd.DataFrame, trade_date) -> pd.DataFrame:
        """获取某日数据"""
        if isinstance(trade_date, str):
            return df[df["date"].dt.strftime("%Y%m%d") == trade_date]
        else:
            return df[df["date"].dt.date == trade_date]

    def _slice_data(self, df: pd.DataFrame, trade_date) -> Optional[pd.DataFrame]:
        """截取到某日的数据"""
        if isinstance(trade_date, str):
            mask = df["date"].dt.strftime("%Y%m%d") <= trade_date
        else:
            mask = df["date"].dt.date <= trade_date
        sliced = df[mask]
        if sliced.empty:
            return None
        return sliced

    def _get_stock_name(self, code: str) -> str:
        """获取股票名称"""
        return code  # 简化处理

    def _calc_position(self, price: float, trend_mode: str = "mixed",
                       price_gain_pct: float = 0.0) -> int:
        """计算买入仓位（支持V2优化: 趋势感知仓位 + 高位降仓）"""
        acc = self.simulator.get_account()
        return self.position_sizer.calc_buy_size(
            price, acc.cash, len(acc.positions),
            trend_mode=trend_mode,
            price_gain_pct=price_gain_pct,
        )

    def _record_nav(self, date_str: str, nav: float):
        """记录每日净值"""
        prev_nav = self.nav_history[-1]["nav"] if self.nav_history else self.initial_capital
        daily_return = (nav - prev_nav) / prev_nav if prev_nav > 0 else 0
        self.nav_history.append({
            "date": date_str,
            "nav": nav,
            "daily_return": daily_return,
            "drawdown": (self.risk_manager.status.peak_nav - nav) / self.risk_manager.status.peak_nav if self.risk_manager.status.peak_nav > 0 else 0,
        })
        self.daily_returns.append(daily_return)

    def _calc_performance(self) -> dict:
        """计算绩效指标"""
        if not self.nav_history:
            return {}

        nav_df = pd.DataFrame(self.nav_history)
        final_nav = nav_df["nav"].iloc[-1]
        total_return = (final_nav - self.initial_capital) / self.initial_capital
        total_days = len(nav_df)

        # 年化收益
        years = total_days / 252
        annual_return = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0

        # 最大回撤
        peak = nav_df["nav"].cummax()
        drawdown = (nav_df["nav"] - peak) / peak
        max_drawdown = drawdown.min()

        # 夏普比率（假设无风险利率2.5%）
        daily_returns = nav_df["daily_return"].dropna()
        if daily_returns.std() > 0:
            sharpe = (daily_returns.mean() - 0.025/252) / daily_returns.std() * np.sqrt(252)
        else:
            sharpe = 0

        # 胜率
        trades = self.simulator.trade_log
        sell_trades = [t for t in trades if t["side"] == "sell"]
        wins = sum(1 for t in sell_trades if t.get("realized_pnl", 0) > 0)
        win_rate = wins / len(sell_trades) if sell_trades else 0

        # 盈亏比
        profits = [t["realized_pnl"] for t in sell_trades if t.get("realized_pnl", 0) > 0]
        losses = [abs(t["realized_pnl"]) for t in sell_trades if t.get("realized_pnl", 0) < 0]
        avg_profit = np.mean(profits) if profits else 0
        avg_loss = np.mean(losses) if losses else 0
        profit_loss_ratio = avg_profit / avg_loss if avg_loss > 0 else 0

        results = {
            "initial_capital": self.initial_capital,
            "final_nav": final_nav,
            "total_return": total_return,
            "annual_return": annual_return,
            "max_drawdown": max_drawdown,
            "sharpe_ratio": sharpe,
            "total_trades": len(trades),
            "win_rate": win_rate,
            "profit_loss_ratio": profit_loss_ratio,
            "avg_profit": avg_profit,
            "avg_loss": avg_loss,
            "trading_days": total_days,
            "nav_history": nav_df,
        }

        self._print_results(results)
        return results

    def _print_results(self, results: dict):
        """打印回测结果"""
        log.info("\n" + "=" * 60)
        log.info("                    回测绩效报告")
        log.info("=" * 60)
        log.info(f"  初始资金:     {results['initial_capital']:>15,.0f}")
        log.info(f"  最终净值:     {results['final_nav']:>15,.0f}")
        log.info(f"  总收益率:     {results['total_return']:>+15.2%}")
        log.info(f"  年化收益率:   {results['annual_return']:>+15.2%}")
        log.info(f"  最大回撤:     {results['max_drawdown']:>15.2%}")
        log.info(f"  夏普比率:     {results['sharpe_ratio']:>15.2f}")
        log.info(f"  总交易次数:   {results['total_trades']:>15}")
        log.info(f"  胜率:         {results['win_rate']:>15.2%}")
        log.info(f"  盈亏比:       {results['profit_loss_ratio']:>15.2f}")
        log.info(f"  平均盈利:     {results['avg_profit']:>15,.0f}")
        log.info(f"  平均亏损:     {results['avg_loss']:>15,.0f}")
        log.info(f"  交易天数:     {results['trading_days']:>15}")
        log.info("=" * 60)

    def export_results(self, filepath: str = "backtest_results.csv"):
        """导出净值曲线到CSV"""
        if self.nav_history:
            df = pd.DataFrame(self.nav_history)
            df.to_csv(filepath, index=False)
            log.info(f"净值曲线已导出: {filepath}")
