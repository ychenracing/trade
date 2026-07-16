#!/usr/bin/env python3
"""AQuant 实时信号扫描 — 双标的池双参数配置，基于最新收盘数据生成交易建议"""

import sys
sys.path.insert(0, "/tmp/.pip-global/lib/python3.12/site-packages")

import pandas as pd
from datetime import datetime, timedelta
from aquant import DataFetcher, Indicators, BacktestEngine, BarContext, Position, parse_symbols

# ═══════════════════════════════════════════════════════════════════════
# 双标的池 + 双参数配置
# ═══════════════════════════════════════════════════════════════════════

# 池1：通信/光模块标的（趋势强，紧止损+加仓）
POOL_1_SYMBOLS = "300308,300502,300394,688008,603986,002409,688072,688300,300054,688205,920045,300776,688535"
POOL_1_OVERRIDES = {
    "trail_atr_mult": 3.0,   # 紧止损
    "max_units": 2,           # 允许加仓1次
}

# 池2：半导体设备/材料标的（波动大趋势弱，宽止损+不加仓）
POOL_2_SYMBOLS = "688249,688347,300666,600206,688409,688361,300604,688120,688082,688981"
POOL_2_OVERRIDES = {
    "trail_atr_mult": 5.0,   # 宽止损
    "max_units": 1,           # 禁止加仓
    "max_positions": 3,       # 半导体池最优值（网格搜索验证）
}

# 用户当前持仓（来自记忆）
HOLDINGS = {
    "300308": {"name": "中际旭创", "shares": 900, "cost": 415},
    "300054": {"name": "鼎龙股份", "shares": 6300, "cost": 88.10},
    "300394": {"name": "天孚通信", "shares": 2500, "cost": 301},
    "688300": {"name": "联瑞新材", "shares": 1985, "cost": 199},
    "300776": {"name": "帝尔激光", "shares": 600, "cost": 201},
}


def scan_pool(pool_name, symbols_str, cfg_overrides):
    """扫描单个标的池，返回信号列表和动量数据"""
    symbols_dict = parse_symbols(symbols_str)

    # 用默认配置 + 覆盖参数
    base_engine = BacktestEngine(initial_capital=2_000_000)
    cfg = {**base_engine.cfg, **cfg_overrides}
    engine = BacktestEngine(initial_capital=2_000_000, cfg=cfg)

    # 获取最近6个月数据
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")

    print(f"  数据区间: {start_date} ~ {end_date}")
    print(f"  标的数量: {len(symbols_dict)}")
    print(f"  参数: trail={cfg['trail_atr_mult']} mu={cfg['max_units']} "
          f"ep={cfg['entry_period']} xp={cfg['exit_period']} adx={cfg['adx_threshold']} "
          f"mo={cfg['momentum_lookback']} mp={cfg['max_positions']}")
    print()

    # 获取数据 + 计算指标
    data_map = {}
    ind_map = {}
    latest_prices = {}
    for code, name in symbols_dict.items():
        try:
            df = DataFetcher.fetch_stock_data(code, start_date, end_date)
            if len(df) < 60:
                print(f"  ⚠ {name}({code}): 数据不足({len(df)}条), 跳过")
                continue
            data_map[code] = df
            ind_map[code] = Indicators.compute_all(df, engine.cfg)
            latest_prices[code] = df["close"].iloc[-1]
            latest_date = df.index[-1].strftime("%Y-%m-%d")
            print(f"  {name}({code}): {len(df)}条 | 最新价={df['close'].iloc[-1]:.2f} ({latest_date})")
        except Exception as e:
            print(f"  ✗ {name}({code}): 获取失败 - {e}")

    if not data_map:
        print("  无法获取任何数据!")
        return [], {}, {}, {}, engine.cfg, {}

    latest_date_str = list(data_map.values())[0].index[-1].strftime("%Y-%m-%d")
    total_assets = 2_000_000

    # 计算动量评分
    lookback = cfg.get("momentum_lookback", 5)
    max_positions = cfg.get("max_positions", 4)
    momentum_scores = {}

    for code, df in data_map.items():
        i = len(df) - 1
        if i >= lookback:
            ret = df["close"].iloc[i] / df["close"].iloc[i - lookback] - 1
            momentum_scores[code] = ret

    sorted_momentum = sorted(momentum_scores.items(), key=lambda x: x[1], reverse=True)
    top_symbols = {s[0] for s in sorted_momentum[:max_positions]}

    # 动量排名表
    print(f"\n  动量排名 (最近{lookback}日) — 决定能否新建仓")
    print(f"  规则: 动量排名前{max_positions}名 ✅可买入 | {max_positions+1}名以后 ❌不可买入 | 持仓标的不受限")
    print("  " + "-" * 88)
    print(f"  {'排名':<4} {'标的':<10} {'代码':<8} {'最新价':>10} {'动量':>10} {'能否买入':>10} {'状态':<8}")
    print("  " + "-" * 88)
    for rank, (code, score) in enumerate(sorted_momentum, 1):
        name = symbols_dict[code]
        held = "持仓中" if code in HOLDINGS else ""
        if code in top_symbols:
            buyable = "✅ 可买入"
        elif code in HOLDINGS:
            buyable = "— 持仓豁免"
        else:
            buyable = "❌ 不可买入"
        print(f"  {rank:<4} {name:<10} {code:<8} {latest_prices[code]:>10.2f} {score:>10.2%} {buyable:>10}  {held}")

    # 生成信号
    held_symbols = set(HOLDINGS.keys())
    signals = []

    for code, df in data_map.items():
        i = len(df) - 1
        strategies = [cls(engine.cfg) for cls in engine.strategy_templates]

        # 如果用户持有该标的，为策略注入持仓状态，使卖出逻辑能正确触发
        if code in HOLDINGS:
            h = HOLDINGS[code]
            entry_price = h["cost"]
            # 从历史数据估算入场以来最高价（取全段最高价，保守估计）
            highest = float(df["high"].iloc[max(0, i-60):i+1].max())
            atr_val = ind_map[code]["atr"].iloc[i]
            trail_mult = cfg.get("trail_atr_mult", 3.0)
            if not pd.isna(atr_val) and atr_val > 0:
                stop_loss = max(highest - trail_mult * atr_val,
                                entry_price - cfg.get("atr_multiplier", 0.5) * atr_val)
            else:
                stop_loss = entry_price * 0.9  # ATR不可用时退回10%止损
            pos = Position(
                symbol=code, strategy_name="holding", shares=h["shares"],
                entry_price=entry_price, entry_date="",
                stop_loss=stop_loss, highest_since_entry=highest, units=1,
            )
            for strategy in strategies:
                strategy.position = pos

        for strategy in strategies:
            ctx = BarContext(
                i=i, df=df, current_assets=total_assets,
                indicators=ind_map[code], available_cash=2_000_000,
                symbol=code, date=latest_date_str,
            )
            signal = strategy.on_bar(ctx)
            if signal is not None:
                signal.symbol_name = symbols_dict[code]
                signal.latest_price = latest_prices[code]
                signal.is_holding = code in HOLDINGS
                signal.holding_info = HOLDINGS.get(code, {})
                signals.append(signal)

    # 按信号类型分组
    buy_signals = [s for s in signals if s.direction == "buy"]
    sell_signals = [s for s in signals if s.direction == "sell"]

    # 输出信号
    print(f"\n  {'─' * 68}")
    if buy_signals:
        print(f"  📈 买入信号 ({len(buy_signals)}个):")
        print(f"  {'─' * 68}")
        print(f"  {'标的':<10} {'代码':<8} {'策略':<20} {'最新价':>10} {'动量':>8} {'信号价':>10} {'止损':>10} {'原因'}")
        for s in buy_signals:
            momentum = momentum_scores.get(s.symbol, 0)
            is_held = "持仓" if s.is_holding else "空仓"
            print(f"  {s.symbol_name:<10} {s.symbol:<8} {s.strategy_name:<20} "
                  f"{s.latest_price:>10.2f} {momentum:>8.2%} {s.price:>10.2f} {s.stop_loss:>10.2f} {s.reason} [{is_held}]")
    else:
        print("  无买入信号")

    print(f"\n  {'─' * 68}")
    if sell_signals:
        print(f"  📉 卖出信号 ({len(sell_signals)}个):")
        print(f"  {'─' * 68}")
        print(f"  {'标的':<10} {'代码':<8} {'策略':<20} {'最新价':>10} {'信号价':>10} {'原因'}")
        for s in sell_signals:
            print(f"  {s.symbol_name:<10} {s.symbol:<8} {s.strategy_name:<20} "
                  f"{s.latest_price:>10.2f} {s.price:>10.2f} {s.reason}")
    else:
        print("  无卖出信号")

    return signals, momentum_scores, latest_prices, top_symbols, engine.cfg, symbols_dict


def print_holdings_analysis(signals_1, signals_2, momentum_1, momentum_2,
                             prices_1, prices_2, top_1, top_2):
    """打印持仓标的操作建议"""
    all_signals = signals_1 + signals_2
    all_momentum = {**momentum_1, **momentum_2}
    all_prices = {**prices_1, **prices_2}
    all_top = top_1 | top_2

    print(f"\n{'=' * 70}")
    print("持仓标的操作建议")
    print(f"{'=' * 70}")

    for code, info in HOLDINGS.items():
        name = info["name"]
        shares = info["shares"]
        cost = info["cost"]
        current_price = all_prices.get(code)
        if current_price is None:
            print(f"  {name}({code}): 无数据")
            continue

        pnl_pct = (current_price - cost) / cost
        has_buy = any(s.symbol == code and s.direction == "buy" for s in all_signals)
        has_sell = any(s.symbol == code and s.direction == "sell" for s in all_signals)
        market_value = shares * current_price

        if has_sell and not has_buy:
            action = "🔴 建议卖出"
        elif has_buy and not has_sell:
            action = "🟢 建议加仓"
        elif has_buy and has_sell:
            action = "🟡 策略分歧（一个买一个卖）"
        else:
            action = "🟡 继续持有（无信号）"

        stop_losses = [s.stop_loss for s in all_signals if s.symbol == code and s.direction == "buy"]
        stop_str = f"止损={stop_losses[0]:.2f}" if stop_losses else ""

        momentum = all_momentum.get(code, 0)
        momentum_rank = list(all_momentum.keys()).index(code) + 1 if code in all_momentum else "-"

        print(f"  {name}({code}): {shares}股 @ {cost:.2f} | 现价 {current_price:.2f} | "
              f"盈亏 {pnl_pct:+.2%} | 市值 {market_value:,.0f} | "
              f"动量排名#{momentum_rank} | {action} {stop_str}")


def print_empty_analysis(signals_1, signals_2, momentum_1, momentum_2,
                          prices_1, prices_2, top_1, top_2, symbols_1, symbols_2):
    """打印空仓标的监控"""
    all_signals = signals_1 + signals_2
    all_momentum = {**momentum_1, **momentum_2}
    all_prices = {**prices_1, **prices_2}
    all_top = top_1 | top_2
    all_symbols = {**symbols_1, **symbols_2}

    print(f"\n{'=' * 70}")
    print("空仓标的监控")
    print(f"{'=' * 70}")

    empty_codes = [c for c in all_symbols if c not in HOLDINGS]
    for code in empty_codes:
        name = all_symbols[code]
        current_price = all_prices.get(code)
        if current_price is None:
            continue

        has_buy = any(s.symbol == code and s.direction == "buy" for s in all_signals)
        momentum = all_momentum.get(code, 0)
        momentum_rank = list(all_momentum.keys()).index(code) + 1 if code in all_momentum else "-"
        is_top = "★Top" if code in all_top else ""

        if has_buy:
            buy_signal = [s for s in all_signals if s.symbol == code and s.direction == "buy"][0]
            action = f"🟢 买入信号: {buy_signal.reason} 止损={buy_signal.stop_loss:.2f}"
        else:
            action = "⬜ 观望"

        print(f"  {name}({code}): 现价 {current_price:.2f} | 动量排名#{momentum_rank} {is_top} | {action}")


# ═══════════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════════

today_str = datetime.now().strftime("%Y年%m月%d日")
print(f"\n{today_str} 文心强趋震荡量化（海龟双均ATR）光4半3双池策略")
print("=" * 70)
print("AQuant 实时信号扫描（双池双参数）")
print("=" * 70)

# 池1：通信/光模块（13标的，trail=3.0/mu=2）
print(f"\n{'━' * 70}")
print(f"池1：通信/光模块标的（trail=3.0 / max_units=2）")
print(f"{'━' * 70}")
signals_1, momentum_1, prices_1, top_1, cfg_1, syms_1 = scan_pool(
    "通信光模块", POOL_1_SYMBOLS, POOL_1_OVERRIDES
)

# 池2：半导体设备/材料（10标的，trail=5.0/mu=1）
print(f"\n{'━' * 70}")
print(f"池2：半导体设备/材料标的（trail=5.0 / max_units=1）")
print(f"{'━' * 70}")
signals_2, momentum_2, prices_2, top_2, cfg_2, syms_2 = scan_pool(
    "半导体", POOL_2_SYMBOLS, POOL_2_OVERRIDES
)

# 汇总持仓和空仓分析
print_holdings_analysis(signals_1, signals_2, momentum_1, momentum_2,
                         prices_1, prices_2, top_1, top_2)

print_empty_analysis(signals_1, signals_2, momentum_1, momentum_2,
                      prices_1, prices_2, top_1, top_2, syms_1, syms_2)

print(f"\n{'=' * 70}")
print("注意: 以上信号基于策略指标自动生成，仅供参考，不构成投资建议。")
print("交易决策请结合基本面、市场情绪和个人风险承受能力综合判断。")
print(f"{'=' * 70}")
