"""
量化交易系统主程序
- 回测模式: python main.py --mode backtest
- 模拟盘:   python main.py --mode paper
- 实盘:     python main.py --mode live

启动方式:
  cd quant_trader
  python main.py --mode backtest --start 20230101 --end 20231231
  python main.py --mode paper
  python main.py --mode live --account YOUR_QMT_ACCOUNT
"""
import sys
import time
import argparse
import datetime
from pathlib import Path

# 确保项目根目录在Python路径中
sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils.logger import log
from utils.helpers import is_trading_day, is_market_open, format_code, calc_atr, calc_sma, calc_trend_mode, round_lot
from config.settings import (
    RUN_MODE, INITIAL_CAPITAL, RISK_CONFIG, STOCK_UNIVERSE,
    SCHEDULE_CONFIG, TRADE_CONFIG,
)
from config.strategy_config import COMBO_CONFIG, SCREENING_CONFIG
from data.data_fetcher import DataFetcher
from strategy.combo import ComboStrategy
from strategy.base import Signal
from execution.simulator import SimulatorExecutor
from execution.broker import QMTExecutor
from risk.manager import RiskManager
from risk.position_sizer import PositionSizer
from monitor.notifier import Notifier
from storage.models import Database


class QuantTrader:
    """量化交易主引擎"""

    def __init__(self, mode: str = "paper"):
        self.mode = mode
        log.info(f"{'='*60}")
        log.info(f"  A股量化交易系统启动 | 模式: {mode.upper()}")
        log.info(f"  初始资金: {INITIAL_CAPITAL:,.0f} | 最大回撤: {RISK_CONFIG['max_total_drawdown']:.0%}")
        log.info(f"  最大持仓: {RISK_CONFIG['max_positions']}只 | 策略: {COMBO_CONFIG['name']}")
        log.info(f"{'='*60}")

        # 核心组件
        self.fetcher = DataFetcher()
        self.strategy = ComboStrategy()
        self.db = Database()
        self.notifier = Notifier()
        self.risk_manager = RiskManager(INITIAL_CAPITAL)
        self.position_sizer = PositionSizer(INITIAL_CAPITAL)
        self.sold_today: set = set()  # 当日止损卖出的股票，防止重新买入

        # 根据模式选择执行器
        if mode == "live":
            account_id = getattr(self, "account_id", "")
            self.executor = QMTExecutor(account_id)
            if not self.executor.connected:
                log.error("实盘连接失败, 降级为模拟模式")
                self.executor = SimulatorExecutor(INITIAL_CAPITAL)
                self.mode = "paper"
        else:
            self.executor = SimulatorExecutor(INITIAL_CAPITAL)

        # 股票池
        self.stock_pool: list = []
        self._init_stock_pool()

    def _init_stock_pool(self):
        """初始化股票池"""
        log.info(f"初始化股票池: {STOCK_UNIVERSE['pool']}")
        stocks = self.fetcher.get_stock_list(STOCK_UNIVERSE["pool"])
        self.stock_pool = stocks["code"].tolist()
        log.info(f"股票池就绪: {len(self.stock_pool)}只股票")

    # ============ 回测模式 ============

    def run_backtest(self, start_date: str, end_date: str):
        """运行回测"""
        from backtest.engine import BacktestEngine
        engine = BacktestEngine(INITIAL_CAPITAL)

        # 使用前20只股票做快速回测（可调整）
        test_codes = self.stock_pool[:30]
        log.info(f"回测股票: {test_codes}")

        results = engine.run(test_codes, start_date, end_date)

        if results:
            engine.export_results("backtest_nav.csv")
            self.notifier.send("回测完成", f"总收益: {results['total_return']:.2%}, "
                                          f"最大回撤: {results['max_drawdown']:.2%}, "
                                          f"夏普: {results['sharpe_ratio']:.2f}")
        return results

    # ============ 模拟盘/实盘 ============

    def run_paper(self):
        """运行模拟盘"""
        log.info("启动模拟盘交易...")
        self.notifier.send("系统启动", f"模拟盘模式, 资金={INITIAL_CAPITAL:,.0f}")

        while True:
            try:
                now = datetime.datetime.now()

                # 非交易日跳过
                if not is_trading_day(now.date()):
                    log.debug(f"非交易日: {now.date()}")
                    time.sleep(3600)
                    continue

                # 交易时间内执行策略
                if is_market_open():
                    self._trading_loop()
                else:
                    # 收盘后处理
                    if now.time() > datetime.time(15, 0) and now.time() < datetime.time(16, 0):
                        self._post_market_process()
                        # 等到明天
                        time.sleep(3600)
                    else:
                        # 非交易时间等待
                        wait_min = SCHEDULE_CONFIG["run_interval"]
                        time.sleep(wait_min * 60)

            except KeyboardInterrupt:
                log.info("收到退出信号, 正在停止...")
                self.notifier.send("系统停止", "用户手动退出")
                break
            except Exception as e:
                log.error(f"主循环异常: {e}")
                self.notifier.notify_risk(f"主循环异常: {e}", "error")
                time.sleep(60)

    def _trading_loop(self):
        """交易循环"""
        log.info(f"--- 交易循环 {datetime.datetime.now().strftime('%H:%M:%S')} ---")

        # 1. 获取实时行情
        if not self.stock_pool:
            return

        # 2. 检查风控
        acc = self.executor.get_account()
        current_nav = acc.total_value
        # 每日更新仓位管理器的资金基准
        self.position_sizer.update_capital(current_nav)
        allowed, risk_reason = self.risk_manager.check_global_risk(
            current_nav, datetime.date.today()
        )
        if not allowed:
            self.notifier.notify_risk(
                f"触发全局风控熔断! {risk_reason} 当前净值={current_nav:,.0f}",
                "error"
            )
            return

        # 3. 获取持仓股票的实时价格并更新
        positions = self.executor.get_positions()
        if positions:
            pos_codes = list(positions.keys())
            realtime = self.fetcher.get_realtime_quotes(pos_codes)
            price_updates = {}
            for _, row in realtime.iterrows():
                code = format_code(row["code"])
                if code in positions:
                    price_updates[code] = float(row["price"])
            # 使用executor统一更新（含peak_price、trailing_active、profit_loss）
            if price_updates:
                # SimulatorExecutor.update_prices() 已封装全部状态更新逻辑
                # 外部执行器（QMT等）如果也有update_prices则优先使用
                if hasattr(self.executor, 'update_prices'):
                    self.executor.update_prices(price_updates)
                else:
                    # 兜底：直接操作PositionInfo（与update_prices逻辑一致）
                    from config.settings import RISK_CONFIG as _RC
                    for code, new_price in price_updates.items():
                        if code in positions:
                            pos = positions[code]
                            pos.current_price = new_price
                            pos.market_value = pos.shares * new_price
                            pos.profit_loss = pos.market_value - pos.cost_price * pos.shares
                            pos.profit_pct = (new_price - pos.cost_price) / pos.cost_price if pos.cost_price > 0 else 0
                            if new_price > pos.peak_price:
                                pos.peak_price = new_price
                            if pos.profit_pct >= _RC.get("trailing_stop_trigger", 0.15):
                                pos.trailing_active = True

        # 4. 检查持仓止损（传入ATR做自适应止损）
        for code, pos in list(positions.items()):
            # 获取该持仓股近期数据计算ATR
            pos_atr = 0.0
            try:
                atr_end = datetime.date.today().strftime("%Y%m%d")
                atr_start = (datetime.date.today() - datetime.timedelta(days=120)).strftime("%Y%m%d")
                pos_df = self.fetcher.get_daily_data(code, atr_start, atr_end)
                if pos_df is not None and not pos_df.empty and len(pos_df) >= 20:
                    pos_atr = float(calc_atr(
                        pos_df["high"], pos_df["low"], pos_df["close"], 20
                    ).iloc[-1])
            except Exception:
                pass

            should_stop, reason = self.risk_manager.check_stop_loss(pos, atr=pos_atr)
            if should_stop and code not in self.executor.today_bought:
                self.notifier.notify_risk(f"[{pos.name}] {reason}", "warning")
                order = self.executor.sell(code, pos.name, pos.current_price, pos.shares)
                if order.status.value == "filled":
                    loss = order.realized_pnl
                    if loss < 0:
                        self.risk_manager.record_realized_loss(loss)
                    self.notifier.notify_trade("sell", pos.name, code,
                                             order.filled_price, order.filled_shares, reason)
                    self.sold_today.add(code)  # 标记当日已卖出，防止重新买入

        # 5. 扫描股票池（每5分钟扫描一部分，避免频繁请求）
        # 优先扫描持仓股，确保卖出信号不被漏检
        held_codes = list(self.executor.get_positions().keys())
        scan_count = min(20, len(self.stock_pool))
        # 持仓股优先放入扫描列表
        scan_codes = [c for c in held_codes if c in self.stock_pool]
        # 补充非持仓股填满扫描批次（防止负索引）
        remaining_slots = max(0, scan_count - len(scan_codes))
        remaining = [c for c in self.stock_pool if c not in scan_codes]
        scan_codes += remaining[:remaining_slots]
        # 轮换股票池（仅轮换非持仓股部分）
        for c in scan_codes:
            if c in self.stock_pool:
                self.stock_pool.remove(c)
        self.stock_pool = self.stock_pool + scan_codes

        # 获取这些股票最近60天数据用于策略计算
        end_date = datetime.date.today().strftime("%Y%m%d")
        start_date = (datetime.date.today() - datetime.timedelta(days=120)).strftime("%Y%m%d")

        for code in scan_codes:
            # 跳过本周期刚止损卖出的股票，防止当日重新买入
            if code in self.sold_today:
                continue
            if len(self.executor.get_positions()) >= RISK_CONFIG["max_positions"]:
                break

            df = self.fetcher.get_daily_data(code, start_date, end_date)
            if df.empty or len(df) < self.strategy.min_data_days:
                continue

            result = self.strategy.generate_signal(df)
            if (result.signal == Signal.BUY and result.strength >= 0.5
                    and code not in self.executor.get_positions()):
                price = float(df.iloc[-1]["close"])
                stock_name = self._get_stock_name(code)
                # 计算趋势环境和涨幅（启用V2仓位优化）
                ma60_series = calc_sma(df["close"], 60)
                cur_ma60 = float(ma60_series.iloc[-1]) if len(ma60_series) > 0 else price
                trend_mode = calc_trend_mode(df)
                price_gain_pct = (price - cur_ma60) / cur_ma60 if cur_ma60 > 0 else 0.0
                # 计算ATR（用于买入时记录entry_atr）
                buy_atr = 0.0
                if len(df) >= 20:
                    try:
                        buy_atr = float(calc_atr(
                            df["high"], df["low"], df["close"], 20
                        ).iloc[-1])
                    except Exception:
                        pass

                shares = self.position_sizer.calc_buy_size(
                    price, acc.cash, len(self.executor.get_positions()),
                    trend_mode=trend_mode,
                    price_gain_pct=price_gain_pct,
                )
                if shares > 0:
                    order = self.executor.buy(code, stock_name, price, shares)
                    if order.status.value == "filled":
                        # 初始化持仓追踪
                        pos = self.executor.positions[code]
                        pos.peak_price = order.filled_price
                        pos.base_shares = shares
                        pos.entry_atr = buy_atr  # 与trade_engine保持一致
                        self.notifier.notify_trade(
                            "buy", stock_name, code,
                            order.filled_price, order.filled_shares,
                            result.reason
                        )
                        # 更新可用资金
                        acc = self.executor.get_account()

            # 持仓中的股票检查卖出信号 + 加仓机会
            if code in self.executor.get_positions():
                pos = self.executor.get_positions()[code]
                if result.signal == Signal.SELL:
                    # T+1前置检查：今日买入的不可卖
                    if code in self.executor.today_bought:
                        pass  # T+1限制，跳过卖出
                    else:
                        order = self.executor.sell(code, pos.name, pos.current_price, pos.shares)
                        if order.status.value == "filled":
                            # 记录实际盈亏
                            realized_pnl = order.realized_pnl
                            if realized_pnl < 0:
                                self.risk_manager.record_realized_loss(realized_pnl)
                            self.notifier.notify_trade(
                                "sell", pos.name, code,
                                order.filled_price, order.filled_shares,
                                result.reason
                            )
                elif RISK_CONFIG.get("pyramid_allowed", False):
                    # 独立加仓检查（每日检查，不依赖BUY信号）
                    profit_pct = (pos.current_price - pos.cost_price) / pos.cost_price if pos.cost_price > 0 else 0
                    if (profit_pct >= RISK_CONFIG.get("pyramid_trigger", 0.08)
                        and pos.pyramid_count < RISK_CONFIG.get("pyramid_max_adds", 2)
                        and code not in self.executor.today_bought
                        and not self.risk_manager.status.daily_halt):
                        # 计算趋势环境
                        ma60_s = calc_sma(df["close"], 60)
                        cur_ma60 = float(ma60_s.iloc[-1]) if len(ma60_s) > 0 else pos.current_price
                        py_trend = calc_trend_mode(df)
                        py_gain = (pos.current_price - cur_ma60) / cur_ma60 if cur_ma60 > 0 else 0.0
                        # 空头趋势不加仓
                        if py_trend != "down":
                            add_shares = self.position_sizer.calc_buy_size(
                                pos.current_price, acc.cash, len(self.executor.get_positions()),
                                trend_mode=py_trend, price_gain_pct=py_gain,
                                is_pyramid=True, base_shares=pos.base_shares,
                                position_value=pos.market_value,
                            )
                            add_shares = round_lot(add_shares)
                            if add_shares > 0:
                                order = self.executor.buy(code, pos.name, pos.current_price, add_shares)
                                if order.status.value == "filled":
                                    pos.pyramid_count += 1
                                    # simulator.buy()已更新pos.shares/cost_price/market_value，无需重复计算
                                    acc = self.executor.get_account()  # 更新账户
                                    self.notifier.notify_trade(
                                        "buy(加仓)", pos.name, code,
                                        order.filled_price, order.filled_shares,
                                        f"盈利{profit_pct:+.0%}加仓第{pos.pyramid_count}次"
                                    )

        log.info(self.risk_manager.get_status_summary())

    def _post_market_process(self):
        """收盘后处理"""
        log.info("=== 收盘后处理 ===")
        acc = self.executor.get_account()

        # 清空当日止损记录（次日允许重新买入）
        self.sold_today.clear()

        # 保存净值
        date_str = datetime.date.today().isoformat()
        position_value = sum(p.market_value for p in acc.positions.values())
        self.db.save_daily_nav(
            date_str, acc.total_value, acc.cash, position_value
        )

        # 保存持仓快照
        self.db.save_positions(date_str, acc.positions)

        # 保存交易记录
        for trade in acc.today_trades:
            self.db.save_trade(trade)

        # 发送收盘总结
        summary = (
            f"总资产: {acc.total_value:,.0f}\n"
            f"现金: {acc.cash:,.0f}\n"
            f"持仓: {position_value:,.0f} ({len(acc.positions)}只)\n"
            f"今日交易: {len(acc.today_trades)}笔\n"
            f"{self.risk_manager.get_status_summary()}"
        )
        self.notifier.notify_daily_summary(summary)

        log.info(self.executor.get_summary())

    def _get_stock_name(self, code: str) -> str:
        """获取股票名称（优先从持仓和行情缓存获取）"""
        # 尝试从持仓获取
        positions = self.executor.get_positions()
        if code in positions:
            return positions[code].name
        # 尝试从实时行情缓存获取
        try:
            realtime = self.fetcher.get_realtime_quotes([code])
            if not realtime.empty:
                return str(realtime.iloc[0].get("name", code))
        except Exception:
            pass
        return code

    def run(self):
        """主入口"""
        if self.mode == "backtest":
            # 回测需要在命令行指定日期
            self.run_backtest("20230101", "20231231")
        elif self.mode in ("paper", "live"):
            self.run_paper()


def main():
    parser = argparse.ArgumentParser(description="A股量化交易系统")
    parser.add_argument(
        "--mode", choices=["backtest", "paper", "live"],
        default="paper", help="运行模式"
    )
    parser.add_argument("--start", default="", help="回测开始日期 YYYYMMDD")
    parser.add_argument("--end", default="", help="回测结束日期 YYYYMMDD")
    parser.add_argument("--account", default="", help="实盘QMT账户ID")

    args = parser.parse_args()

    trader = QuantTrader(mode=args.mode)
    if args.mode == "backtest":
        start = args.start or "20230101"
        end = args.end or datetime.date.today().strftime("%Y%m%d")
        trader.run_backtest(start, end)
    else:
        if args.account:
            trader.account_id = args.account
        trader.run_paper()


if __name__ == "__main__":
    main()
