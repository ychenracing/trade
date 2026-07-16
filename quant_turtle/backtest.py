"""事件驱动回测引擎 + 多策略组合调度。

执行时序（杜绝前视偏差、满足 T+1）：
  对第 i 根 bar：
    1) 以本根「开盘价」撮合上一根生成的挂单（pending）。
    2) 调用各策略 on_bar(本根) 生成新挂单，留待第 i+1 根开盘撮合。
    3) 记录本根权益曲线。
    4) 检查账户级最大回撤，必要时清仓停机。

止损单携带 limit_price：若下根开盘价 ≤ 止损价（跳空），按开盘价成交（更保守），
否则按止损价成交。
"""
from typing import Dict, List

import pandas as pd

from . import indicators
from .config import Config
from .portfolio import Portfolio
from .strategies.base import Order
from .strategies.ma_trend import MATrendStrategy
from .strategies.turtle import TurtleStrategy


def build_strategy(strategy_id: str, symbol: str, capital: float, cfg: Config):
    """根据策略 id 与分配到的资本，构造策略实例。"""
    if strategy_id == "turtle20":
        return TurtleStrategy(symbol, capital, cfg, entry_period=20, exit_period=10)
    if strategy_id == "turtle55":
        return TurtleStrategy(symbol, capital, cfg, entry_period=55, exit_period=20)
    if strategy_id == "ma_trend":
        return MATrendStrategy(symbol, capital, cfg)
    raise ValueError(f"未知策略 id：{strategy_id}")


def run_backtest(df: pd.DataFrame, cfg: Config, symbol: str = None) -> dict:
    """对单只标的运行多策略组合回测。

    返回包含权益曲线、成交、指标等结果的字典。
    """
    symbol = symbol or (cfg.universe[0] if cfg.universe else None)
    if symbol is None:
        raise ValueError("未指定回测标的")

    df = indicators.add_indicators(df, cfg)
    portfolio = Portfolio(cfg)

    strat_instances = []
    for sid in cfg.strategies:
        weight = cfg.strategy_capital_weight.get(sid, 1.0 / len(cfg.strategies))
        cap = cfg.initial_capital * weight
        strat_instances.append(build_strategy(sid, symbol, cap, cfg))

    pending: List[Order] = []
    st_by_id = {st.id: st for st in strat_instances}
    for i in range(len(df)):
        bar = df.iloc[i]
        date = bar["date"]
        prices = {symbol: float(bar["close"])}

        # 1) 撮合上一根挂单（本根开盘价）
        for order in pending:
            if order.limit_price is not None:
                # 止损单：下根开盘若已低于止损价，按开盘价成交（更保守）
                open_px = float(bar["open"])
                fill = open_px if open_px <= order.limit_price else order.limit_price
            else:
                fill = float(bar["open"])
            portfolio.execute(order, fill, date)
            # 成交后回填策略状态（H8：以组合实际持仓为唯一真相源）
            st = st_by_id.get(order.strategy)
            if st is not None:
                actual = portfolio.positions.get((order.symbol, order.strategy), 0)
                st.sync_position(actual)
        pending = []

        if portfolio.halted:
            portfolio.equity_curve.append(
                {"date": date, "equity": portfolio.equity(prices), "cash": portfolio.cash}
            )
            continue

        # 2) 生成新信号
        for st in strat_instances:
            pending.extend(st.on_bar(bar))

        # 3) 账户级风控（可能触发清仓）
        portfolio.check_drawdown_halt(prices)

        # 4) 记录权益（清算后口径，避免虚高，H7 修复）
        portfolio.equity_curve.append(
            {"date": date, "equity": portfolio.equity(prices), "cash": portfolio.cash}
        )

    result = {
        "symbol": symbol,
        "equity_curve": pd.DataFrame(portfolio.equity_curve).set_index("date"),
        "fills": portfolio.fills,
        "final_equity": portfolio.equity({symbol: float(df.iloc[-1]["close"])}),
        "cash": portfolio.cash,
        "halted": portfolio.halted,
        "data": df,
    }
    return result


def run_backtest_multi(data: Dict[str, pd.DataFrame], cfg: Config, strategies=None,
                       donchian_periods=None) -> dict:
    """对「多标的组合」运行多策略回测（共享现金池、账户级风控）。

    与单标的回测保持完全一致的事件时序（当根信号 → 下一根开盘撮合），
    并按日期对齐多标的行情。各 (标的, 策略) 子仓位共享同一现金池，
    由组合层强制无杠杆（买入金额不得超过可用现金）。

    参数：
      data              : {symbol: 原始日线 DataFrame}（前复权，未经指标增强）
      cfg               : 全局配置
      strategies        : 可选，预构建的策略实例列表；为 None 时按默认权重自动构建
                          （每个 (标的, 策略) 分配 capital = initial * 权重 / 标的数量）
      donchian_periods  : 可选，需计算的唐奇安通道周期集合；用于自定义参数扫描
    """
    enriched = {sym: indicators.add_indicators(df.copy(), cfg, donchian_periods)
                for sym, df in data.items()}

    if strategies is None:
        strategies = []
        n_sym = max(len(enriched), 1)
        for symbol in cfg.universe:
            for sid in cfg.strategies:
                weight = cfg.strategy_capital_weight.get(sid, 1.0 / len(cfg.strategies))
                cap = cfg.initial_capital * weight / n_sym
                strategies.append(build_strategy(sid, symbol, cap, cfg))

    portfolio = Portfolio(cfg)
    # 以 (标的, 策略id) 唯一键索引：同一策略 id（如 turtle20）在多个标的下各有一个实例，
    # 若仅以 st.id 为键会发生「后者覆盖前者」，导致 sync_position 跨标的污染状态（BUG-1 修复）。
    st_by_id = {(st.symbol, st.id): st for st in strategies}

    # 各标的按日期建索引，取交集作为主时间轴（A 股同交易日历）
    bars_by_date = {
        sym: {row["date"]: row for _, row in df.iterrows()}
        for sym, df in enriched.items()
    }
    master = None
    for sym in enriched:
        idx = set(bars_by_date[sym].keys())
        master = idx if master is None else (master & idx)
    all_dates = sorted(master)

    last_close = {sym: None for sym in enriched}
    pending: List[Order] = []
    equity_curve = []

    for date in all_dates:
        # 估值价：优先用当根收盘，缺失则沿用最近已知收盘
        prices = {}
        for sym in enriched:
            row = bars_by_date[sym].get(date)
            if row is not None:
                last_close[sym] = float(row["close"])
            prices[sym] = last_close[sym]

        # 1) 撮合上一根挂单（本根开盘价）
        for order in pending:
            row = bars_by_date[order.symbol].get(date)
            if row is None:
                continue  # 该标的当日无行情，递延（极少见）
            open_px = float(row["open"])
            if order.limit_price is not None:
                fill = open_px if open_px <= order.limit_price else order.limit_price
            else:
                fill = open_px
            portfolio.execute(order, fill, date)
            st = st_by_id.get((order.symbol, order.strategy))
            if st is not None:
                actual = portfolio.positions.get((order.symbol, order.strategy), 0)
                st.sync_position(actual)
        pending = []

        if portfolio.halted:
            equity_curve.append(
                {"date": date, "equity": portfolio.equity(prices), "cash": portfolio.cash}
            )
            continue

        # 2) 生成新信号（每个策略读取其所属标的的当根 bar）
        for st in strategies:
            row = bars_by_date[st.symbol].get(date)
            if row is not None:
                pending.extend(st.on_bar(row))

        # 3) 账户级风控（可能触发清仓）
        portfolio.check_drawdown_halt(prices)

        # 4) 记录权益（清算后口径）
        equity_curve.append(
            {"date": date, "equity": portfolio.equity(prices), "cash": portfolio.cash}
        )

    result = {
        "symbol": "+".join(cfg.universe),
        "equity_curve": pd.DataFrame(equity_curve).set_index("date"),
        "fills": portfolio.fills,
        "final_equity": portfolio.equity({sym: last_close[sym] for sym in enriched}),
        "cash": portfolio.cash,
        "halted": portfolio.halted,
        "data": enriched,
    }
    return result


def compute_metrics(result: dict, cfg: Config) -> dict:
    """由权益曲线计算绩效指标。"""
    eq = result["equity_curve"]["equity"]
    initial = cfg.initial_capital
    total_return = eq.iloc[-1] / initial - 1.0
    peak = eq.cummax()
    drawdown = eq / peak - 1.0
    max_drawdown = drawdown.min()

    # 年化（按 252 个交易日）
    n_days = max(len(eq) - 1, 1)
    annual_return = (1.0 + total_return) ** (252.0 / n_days) - 1.0

    # 单笔成交统计
    buys = [f for f in result["fills"] if f.side == "BUY"]
    sells = [f for f in result["fills"] if f.side == "SELL"]
    halt_fills = [f for f in sells if f.reason == "max_drawdown_halt"]

    return {
        "initial_capital": initial,
        "final_equity": float(eq.iloc[-1]),
        "total_return": float(total_return),
        "annual_return": float(annual_return),
        "max_drawdown": float(max_drawdown),
        "n_bars": int(len(eq)),
        "n_buy_orders": len(buys),
        "n_sell_orders": len(sells),
        "n_halt_liquidations": len(halt_fills),
        "halted": bool(result["halted"]),
    }
