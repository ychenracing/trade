"""交易引擎（模拟托管 / 实盘托管）。

- paper 模式：以「模拟券商」逐根 replay 行情，演示自动下单闭环，不产生真实交易。
- live 模式：预留实时轮询入口（需接入真实券商 API），默认不启用。

本引擎与回test共用同一套策略与风控逻辑，保证「回测即实盘」的一致性：
  - 信号在当根 bar 收盘后生成，订单于下一根开盘撮合（杜绝前视偏差，满足 T+1）；
  - 账户级风控（峰值最大回撤熔断）逐根检查并清仓。
"""
from typing import Dict

import pandas as pd

from . import indicators
from .backtest import build_strategy
from .broker import Bar, LiveBroker, PaperBroker
from .config import Config


class TradingEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        cfg.validate()
        if cfg.mode == "live":
            self.broker = LiveBroker()  # 未实现，将抛 NotImplementedError
        else:
            self.broker = PaperBroker(
                initial_cash=cfg.initial_capital,
                commission=cfg.commission,
                slippage=cfg.slippage,
                stamp_duty=cfg.stamp_duty,
                min_lot=cfg.min_lot,
            )
        # 每个 (标的, 策略) 一个独立策略实例
        self.strategies = []
        for symbol in cfg.universe:
            for sid in cfg.strategies:
                weight = cfg.strategy_capital_weight.get(sid, 1.0 / len(cfg.strategies))
                cap = cfg.initial_capital * weight
                self.strategies.append(build_strategy(sid, symbol, cap, cfg))
        # 以 (标的, 策略id) 唯一键索引，避免多标的下同名策略实例被互相覆盖（BUG-1 修复）
        self.st_by_id = {(st.symbol, st.id): st for st in self.strategies}

    def _equity(self) -> float:
        eq = self.broker.cash
        for (symbol, _), sh in self.broker.positions.items():
            if sh > 0:
                bar = self.broker.last_bars.get(symbol)
                if bar is not None:
                    eq += sh * bar.close
        return eq

    def run_paper(self, data: Dict[str, pd.DataFrame]) -> dict:
        """模拟托管：逐根回放行情并自动下单，返回成交与权益曲线。

        与回测引擎保持完全一致的事件时序：当根生成信号 → 下一根开盘撮合。
        """
        cfg = self.cfg
        enriched = {sym: indicators.add_indicators(df, cfg) for sym, df in data.items()}
        main_sym = cfg.universe[0]
        main = enriched[main_sym]

        # 各标的按日期建索引，支持多标的日期对齐（H2/H3 修复）
        bars_by_date = {
            sym: {row["date"]: row for _, row in df.iterrows()}
            for sym, df in enriched.items()
        }

        pending = []
        peak = float(cfg.initial_capital)
        halted = False
        equity_curve = []

        for i in range(len(main)):
            date = main.iloc[i]["date"]

            # 更新所有标的当根 bar 到券商
            for sym, df in enriched.items():
                row = bars_by_date[sym].get(date)
                if row is not None:
                    self.broker.update_bar(
                        Bar(sym, date, float(row["open"]), float(row["high"]),
                            float(row["low"]), float(row["close"]), float(row["volume"]))
                    )

            # 1) 撮合上一根挂单（本根开盘价）
            for order in pending:
                self.broker.submit_order(order)
                st = self.st_by_id.get((order.symbol, order.strategy))
                if st is not None:
                    st.sync_position(self.broker.get_position(order.symbol, order.strategy))
            pending = []

            if halted:
                equity_curve.append({"date": date, "equity": self._equity(), "cash": self.broker.cash})
                continue

            # 2) 生成新信号（每个策略读取其所属标的的当根 bar，H2 修复）
            for st in self.strategies:
                row = bars_by_date[st.symbol].get(date)
                if row is not None:
                    pending.extend(st.on_bar(row))

            # 3) 账户级风控：峰值最大回撤超过阈值则清仓并停机
            eq = self._equity()
            peak = max(peak, eq)
            basis = peak if cfg.max_drawdown_basis == "peak" else cfg.initial_capital
            if eq < basis * (1.0 - cfg.max_total_risk):
                halted = True
                self.broker.halted = True   # 拒绝停机后的任何新成交（BUG-2 修复）
                self.broker.liquidate_all()
                eq = self._equity()

            # 4) 记录权益（清算后口径）
            equity_curve.append({"date": date, "equity": eq, "cash": self.broker.cash})

        return {
            "equity_curve": pd.DataFrame(equity_curve).set_index("date"),
            "fills": self.broker.fills,
            "final_cash": self.broker.cash,
            "halted": halted,
        }
