"""
新旧策略逻辑对比回测
- 拉取9只股票过去1年真实日K数据（akshare新浪接口）
- 同时跑旧逻辑（软买入）和新逻辑（动量评分）两套回测
- 输出收益率、回撤、夏普、胜率等对比
"""
import sys
import datetime
import pandas as pd
import numpy as np
from pathlib import Path
from copy import deepcopy

sys.path.insert(0, str(Path(__file__).resolve().parent))

from strategy.base import Signal, StrategyResult
from strategy.combo import ComboStrategy
from execution.simulator import SimulatorExecutor
from risk.manager import RiskManager
from risk.position_sizer import PositionSizer, PositionInfo
from utils.helpers import (
    calc_ema, calc_sma, calc_macd, calc_donchian, calc_atr,
    calc_momentum_score, calc_trend_mode, round_lot,
)
from utils.logger import log, setup_logger

setup_logger()
log.remove()
log.add(sys.stderr, level="ERROR")

# ======================================================================
# 股票池
# ======================================================================
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

# 回测参数
INITIAL_CAPITAL = 2_000_000
WARMUP = 70  # 预热期


# ======================================================================
# 数据获取
# ======================================================================
def fetch_data(code: str, name: str) -> pd.DataFrame:
    """用akshare新浪接口拉历史日K（前复权）"""
    import akshare as ak
    df = ak.stock_zh_a_daily(symbol=code, adjust="qfq")
    df = df.rename(columns={"date": "date", "open": "open", "high": "high",
                            "low": "low", "close": "close", "volume": "volume"})
    # 处理date列（可能是datetime.date对象）
    dates = []
    for d in df["date"]:
        s = str(d)[:10]
        dates.append(pd.Timestamp(s))
    df["date"] = dates
    df = df.set_index("date").sort_index()

    # 只取最近1年（约250个交易日），加warmup缓冲
    df = df.tail(320)

    # 预计算指标
    df["atr"] = calc_atr(df["high"], df["low"], df["close"], 20)
    df["ma60"] = calc_sma(df["close"], 60)
    df = df.dropna(subset=["close"])
    return df


# ======================================================================
# 旧逻辑信号生成（inline复现改动前的代码）
# ======================================================================
def old_generate_signal(df: pd.DataFrame) -> StrategyResult:
    """复现改动前的3个策略投票逻辑"""

    # ---- EMA_Cross 旧逻辑 ----
    def ema_old(df):
        p = {"ema_short": 10, "ema_long": 30, "ema_trend": 60, "volume_ratio": 1.5}
        df = df.copy()
        df["ema_short"] = calc_ema(df["close"], p["ema_short"])
        df["ema_long"] = calc_ema(df["close"], p["ema_long"])
        df["ema_trend"] = calc_ema(df["close"], p["ema_trend"])
        df["vol_ma5"] = df["volume"].rolling(5).mean()

        if len(df) < 70:
            return Signal.HOLD, 0, "数据不足"

        cur = df.iloc[-1]
        prev = df.iloc[-2]
        close = cur["close"]
        ema_s = cur["ema_short"]
        ema_l = cur["ema_long"]
        ema_t = cur["ema_trend"]

        cross = 0
        if prev["ema_short"] <= prev["ema_long"] and ema_s > ema_l:
            cross = 1
        elif prev["ema_short"] >= prev["ema_long"] and ema_s < ema_l:
            cross = -1

        is_above = close > ema_t
        trend_up = ema_s > ema_l
        bullish_alignment = ema_s > ema_l > ema_t
        vol_ok = cur["volume"] > cur["vol_ma5"] * p["volume_ratio"]

        # 金叉买入
        if cross == 1 and is_above and vol_ok:
            return Signal.BUY, 0.7, "EMA金叉, 放量确认"

        # 软买入：多头排列+趋势向上 → BUY 0.4
        if is_above and trend_up and bullish_alignment:
            return Signal.BUY, 0.4, "EMA多头排列, 趋势向上"

        # 死叉卖出
        if cross == -1:
            return Signal.SELL, 0.8, "EMA死叉"

        # 跌破趋势线
        if close < ema_t and prev["close"] > prev["ema_trend"]:
            return Signal.SELL, 0.7, f"跌破EMA{p['ema_trend']}"

        return Signal.HOLD, 0, "无明显信号"

    # ---- Donchian 旧逻辑 ----
    def don_old(df):
        p = {"entry_period": 20, "exit_period": 10, "atr_period": 20, "atr_filter_multiple": 0.5}
        df = df.copy()
        upper, lower, middle = calc_donchian(df["high"], df["low"], p["entry_period"])
        df["dc_upper"] = upper
        df["dc_lower"] = lower
        df["dc_middle"] = middle
        df["atr"] = calc_atr(df["high"], df["low"], df["close"], p["atr_period"])

        exit_upper, exit_lower, _ = calc_donchian(df["high"], df["low"], p["exit_period"])

        if len(df) < 30:
            return Signal.HOLD, 0, "数据不足"

        cur = df.iloc[-1]
        close = cur["close"]
        dc_upper = cur["dc_upper"]
        dc_lower = cur["dc_lower"]
        atr = cur["atr"]
        dc_exit_lower = exit_lower.iloc[-1] if len(exit_lower) > 0 else np.nan

        # 突破买入
        if not pd.isna(dc_upper) and close > dc_upper and atr > 0:
            breakout = close - dc_upper
            if breakout > atr * p["atr_filter_multiple"]:
                return Signal.BUY, 0.9, f"突破{p['entry_period']}日新高"

        # 跌破卖出
        if not pd.isna(dc_exit_lower) and close < dc_exit_lower:
            return Signal.SELL, 0.8, f"跌破{p['exit_period']}日低点"

        # 软买入：通道上部>70% → BUY 0.3
        if not pd.isna(dc_upper) and not pd.isna(dc_lower):
            pos = (close - dc_lower) / (dc_upper - dc_lower) if dc_upper > dc_lower else 0.5
            if pos > 0.7:
                return Signal.BUY, 0.3, f"价格在通道上部({pos:.0%})"

        return Signal.HOLD, 0, "价格在通道中部"

    # ---- MACD 旧逻辑 ----
    def macd_old(df):
        p = {"fast": 12, "slow": 26, "signal": 9, "ma_filter": 60, "hist_threshold": 0.01}
        df = df.copy()
        dif, dea, hist = calc_macd(df["close"], p["fast"], p["slow"], p["signal"])
        df["dif"] = dif
        df["dea"] = dea
        df["hist"] = hist
        df["ma_filter"] = calc_sma(df["close"], p["ma_filter"])
        df["hist_change"] = df["hist"].diff()

        if len(df) < 70:
            return Signal.HOLD, 0, "数据不足"

        cur = df.iloc[-1]
        prev = df.iloc[-2]
        close = cur["close"]
        dif = cur["dif"]
        dea = cur["dea"]
        hist = cur["hist"]
        ma = cur["ma_filter"]
        trend_up = close > ma

        macd_cross = 0
        if prev["dif"] <= prev["dea"] and dif > dea:
            macd_cross = 1
        elif prev["dif"] >= prev["dea"] and dif < dea:
            macd_cross = -1

        # 金叉买入
        if macd_cross == 1 and trend_up and hist > p["hist_threshold"]:
            return Signal.BUY, 0.8, "MACD金叉"

        # 软买入：DIF>0且DEA>0且DIF>DEA → BUY 0.5
        if dif > 0 and dea > 0 and dif > dea and trend_up:
            return Signal.BUY, 0.5, "MACD零轴上方运行"

        # 死叉卖出
        if macd_cross == -1:
            return Signal.SELL, 0.8, "MACD死叉"

        # 柱状图连续缩短
        if len(df) >= 4:
            hist_shrinking = all(df["hist_change"].iloc[-3:] < 0)
            if hist_shrinking and hist > 0:
                return Signal.SELL, 0.6, "MACD柱状图连续缩短"

        # 跌破MA
        prev_close = df.iloc[-2]["close"]
        prev_ma = df.iloc[-2]["ma_filter"]
        if close < ma and prev_close > prev_ma:
            return Signal.SELL, 0.5, f"跌破MA{p['ma_filter']}"

        return Signal.HOLD, 0, "MACD无明显信号"

    # ---- 投票 ----
    signals = []
    for fn, name in [(ema_old, "EMA"), (don_old, "Don"), (macd_old, "MACD")]:
        try:
            sig, str_val, reason = fn(df)
            signals.append((sig, str_val, reason, name))
        except Exception:
            signals.append((Signal.HOLD, 0, "error", name))

    buy_count = sum(1 for s, _, _, _ in signals if s == Signal.BUY)
    sell_count = sum(1 for s, _, _, _ in signals if s == Signal.SELL)
    total = len(signals)

    if buy_count >= 2:
        avg_str = sum(s for sig, s, _, _ in signals if sig == Signal.BUY) / buy_count
        return StrategyResult(Signal.BUY, avg_str, f"旧逻辑多数看多({buy_count}/{total})", {})
    elif sell_count >= 2:
        avg_str = sum(s for sig, s, _, _ in signals if sig == Signal.SELL) / sell_count
        return StrategyResult(Signal.SELL, avg_str, f"旧逻辑多数看空({sell_count}/{total})", {})
    elif buy_count == total:
        avg_str = sum(s for sig, s, _, _ in signals if sig == Signal.BUY) / buy_count
        return StrategyResult(Signal.BUY, avg_str, f"旧逻辑全票看多({buy_count}/{total})", {})
    elif sell_count == total:
        avg_str = sum(s for sig, s, _, _ in signals if sig == Signal.SELL) / sell_count
        return StrategyResult(Signal.SELL, avg_str, f"旧逻辑全票看空({sell_count}/{total})", {})
    else:
        return StrategyResult(Signal.HOLD, 0, f"旧逻辑不一致(买{buy_count}/卖{sell_count})", {})


# ======================================================================
# 回测引擎
# ======================================================================
def run_single_backtest(df: pd.DataFrame, code: str, name: str,
                        use_new_logic: bool, initial_capital: float = INITIAL_CAPITAL):
    """
    单股票回测
    use_new_logic: True=用新策略(import), False=用旧逻辑(inline)
    """
    simulator = SimulatorExecutor(initial_capital)
    risk_mgr = RiskManager(initial_capital)
    position_sizer = PositionSizer(initial_capital)

    new_strategy = ComboStrategy() if use_new_logic else None

    trade_log = []
    daily_nav = []
    warmup = WARMUP

    if len(df) <= warmup:
        return {"final_nav": initial_capital, "total_return": 0, "max_drawdown": 0,
                "sharpe": 0, "trades": 0, "win_rate": 0, "daily_nav": []}

    for i in range(warmup, len(df)):
        window = df.iloc[:i+1].copy()
        current_price = window.iloc[-1]["close"]
        current_atr = window.iloc[-1]["atr"] if not np.isnan(window.iloc[-1]["atr"]) else 0
        current_date = window.index[-1].date() if hasattr(window.index[-1], 'date') else window.index[-1]

        simulator._check_date_rollover(current_date)

        # 趋势环境
        ma60 = window.iloc[-1]["ma60"] if not np.isnan(window.iloc[-1]["ma60"]) else current_price
        price_gain_pct = (current_price - ma60) / ma60 if ma60 > 0 else 0
        trend_mode = calc_trend_mode(window)

        # 更新持仓
        if code in simulator.positions:
            pos = simulator.positions[code]
            pos.current_price = current_price
            pos.market_value = current_price * pos.shares
            pos.profit_loss = pos.market_value - pos.cost_price * pos.shares
            pos.profit_pct = (current_price - pos.cost_price) / pos.cost_price
            if current_price > pos.peak_price:
                pos.peak_price = current_price

        account = simulator.get_account()
        total_value = account.total_value
        risk_mgr.update_nav(total_value, current_date)
        position_sizer.update_capital(total_value)

        # 全局风控
        allowed, risk_reason = risk_mgr.check_global_risk(total_value, current_date)
        if not allowed:
            if simulator.positions:
                for c in list(simulator.positions.keys()):
                    pos = simulator.positions[c]
                    order = simulator.sell(c, name, pos.current_price, pos.shares, current_date)
                    if order.status.value == "filled":
                        realized_pnl = order.realized_pnl
                        if realized_pnl < 0:
                            risk_mgr.record_realized_loss(realized_pnl)
                        trade_log.append({
                            "date": str(current_date), "action": "SELL(熔断)",
                            "price": order.filled_price, "shares": order.filled_shares,
                            "pnl": realized_pnl,
                        })
            account_after = simulator.get_account()
            daily_nav.append({
                "date": str(current_date), "nav": account_after.total_value,
                "drawdown": risk_mgr.status.current_drawdown,
            })
            continue

        # 持仓风控
        if code in simulator.positions:
            pos = simulator.positions[code]
            should_stop, stop_reason = risk_mgr.check_stop_loss(pos, atr=current_atr)
            if should_stop and code not in simulator.today_bought:
                order = simulator.sell(code, name, current_price, pos.shares, current_date)
                if order.status.value == "filled":
                    realized_pnl = order.realized_pnl
                    risk_mgr.record_realized_loss(realized_pnl)
                    action = "SELL(止盈)" if realized_pnl > 0 else "SELL(止损)"
                    trade_log.append({
                        "date": str(current_date), "action": action,
                        "price": order.filled_price, "shares": order.filled_shares,
                        "pnl": realized_pnl,
                    })
                continue

        # 加仓
        if (code in simulator.positions
            and risk_mgr.config.get("pyramid_allowed", False)
            and code not in simulator.today_bought
            and not risk_mgr.status.daily_halt):
            pos = simulator.positions[code]
            if (pos.pyramid_count < risk_mgr.config["pyramid_max_adds"]
                and pos.profit_pct >= risk_mgr.config["pyramid_trigger"]):
                if trend_mode != "down":
                    add_shares = position_sizer.calc_buy_size(
                        current_price, simulator.cash, len(simulator.positions),
                        position_value=pos.market_value,
                        price_gain_pct=price_gain_pct,
                        is_pyramid=True, base_shares=pos.base_shares,
                        trend_mode=trend_mode,
                    )
                    if add_shares > 0:
                        order = simulator.buy(code, name, current_price, add_shares, current_date)
                        if order.status.value == "filled":
                            pos.pyramid_count += 1
                            pos.profit_pct = (current_price - pos.cost_price) / pos.cost_price
                            trade_log.append({
                                "date": str(current_date), "action": "BUY(加码)",
                                "price": order.filled_price, "shares": add_shares,
                            })

        # 策略信号
        if use_new_logic:
            result = new_strategy.generate_signal(window)
        else:
            result = old_generate_signal(window)
        signal = result.signal

        if signal == Signal.BUY:
            if risk_mgr.status.daily_halt:
                continue
            if code not in simulator.positions:
                shares = position_sizer.calc_buy_size(
                    current_price, simulator.cash, len(simulator.positions),
                    price_gain_pct=price_gain_pct, trend_mode=trend_mode,
                )
                if shares > 0:
                    order = simulator.buy(code, name, current_price, shares, current_date)
                    if order.status.value == "filled":
                        pos = simulator.positions[code]
                        pos.peak_price = order.filled_price
                        pos.base_shares = shares
                        pos.entry_atr = current_atr
                        trade_log.append({
                            "date": str(current_date), "action": "BUY",
                            "price": order.filled_price, "shares": shares,
                            "reason": result.reason,
                        })

        elif signal == Signal.SELL and code in simulator.positions:
            if code not in simulator.today_bought:
                pos = simulator.positions[code]
                order = simulator.sell(code, name, current_price, pos.shares, current_date)
                if order.status.value == "filled":
                    realized_pnl = order.realized_pnl
                    risk_mgr.record_realized_loss(realized_pnl)
                    trade_log.append({
                        "date": str(current_date), "action": "SELL",
                        "price": order.filled_price, "shares": order.filled_shares,
                        "pnl": realized_pnl,
                    })

        # 记录净值
        account = simulator.get_account()
        daily_nav.append({
            "date": str(current_date), "nav": account.total_value,
            "drawdown": risk_mgr.status.current_drawdown,
        })

    # 统计
    if not daily_nav:
        return {"final_nav": initial_capital, "total_return": 0, "max_drawdown": 0,
                "sharpe": 0, "trades": 0, "win_rate": 0, "daily_nav": []}

    nav_df = pd.DataFrame(daily_nav)
    final_nav = daily_nav[-1]["nav"]
    total_return = (final_nav - initial_capital) / initial_capital
    days = len(daily_nav)
    annual_return = ((1 + total_return) ** (252 / days) - 1) if days > 0 and total_return > -1 else 0

    nav_df["peak"] = nav_df["nav"].cummax()
    nav_df["dd"] = (nav_df["peak"] - nav_df["nav"]) / nav_df["peak"]
    max_drawdown = nav_df["dd"].max()

    nav_df["daily_ret"] = nav_df["nav"].pct_change()
    if nav_df["daily_ret"].std() > 0:
        sharpe = (nav_df["daily_ret"].mean() - 0.025/252) / nav_df["daily_ret"].std() * (252 ** 0.5)
    else:
        sharpe = 0

    # 交易统计
    trade_df = pd.DataFrame(trade_log) if trade_log else pd.DataFrame()
    sell_trades = trade_df[trade_df["action"].str.contains("SELL", na=False)] if len(trade_df) > 0 else pd.DataFrame()
    if len(sell_trades) > 0 and "pnl" in sell_trades.columns:
        wins = sell_trades[sell_trades["pnl"] > 0]
        losses = sell_trades[sell_trades["pnl"] <= 0]
        win_rate = len(wins) / len(sell_trades) if len(sell_trades) > 0 else 0
        avg_profit = wins["pnl"].mean() if len(wins) > 0 else 0
        avg_loss = losses["pnl"].mean() if len(losses) > 0 else 0
        profit_loss_ratio = abs(avg_profit / avg_loss) if avg_loss != 0 else 0
    else:
        win_rate = 0
        profit_loss_ratio = 0

    # 买入持有基准
    bh_return = (df.iloc[-1]["close"] / df.iloc[warmup]["close"] - 1)

    return {
        "final_nav": final_nav, "total_return": total_return,
        "annual_return": annual_return, "max_drawdown": max_drawdown,
        "sharpe": sharpe, "trades": len(trade_log),
        "sell_count": len(sell_trades), "win_rate": win_rate,
        "profit_loss_ratio": profit_loss_ratio,
        "buy_hold": bh_return, "daily_nav": daily_nav,
    }


# ======================================================================
# 主函数
# ======================================================================
def main():
    print("=" * 100)
    print("  📊 新旧策略逻辑对比回测 (过去1年)")
    print("  旧逻辑: 软买入（多头排列/通道上部/MACD零轴上方 → 固定strength BUY）")
    print("  新逻辑: 动量评分（25日加权对数回归斜率×年化×R² > 0.1 + R² > 0.3 → BUY）")
    print("=" * 100)

    # 拉取数据
    all_data = {}
    failed = []
    for code, name in STOCKS:
        print(f"  [{code}] {name} 拉取数据中... ", end="", flush=True)
        try:
            df = fetch_data(code, name)
            all_data[code] = (df, name)
            print(f"数据{len(df)}条, {df.index[0].date()} ~ {df.index[-1].date()}, "
                  f"close={df.iloc[-1]['close']:.2f}")
        except Exception as e:
            print(f"失败: {e}")
            failed.append((code, name, str(e)))

    if not all_data:
        print("\n❌ 无可用数据")
        return

    # 跑回测
    results = []
    for code, (df, name) in all_data.items():
        print(f"\n  ── {name}({code}) ──")
        try:
            old_res = run_single_backtest(df, code, name, use_new_logic=False)
            new_res = run_single_backtest(df, code, name, use_new_logic=True)
            results.append({
                "code": code, "name": name,
                "old": old_res, "new": new_res,
            })
            print(f"    旧: 收益={old_res['total_return']:+.2%} 回撤={old_res['max_drawdown']:.2%} "
                  f"夏普={old_res['sharpe']:.2f} 交易={old_res['trades']}次 胜率={old_res['win_rate']:.0%}")
            print(f"    新: 收益={new_res['total_return']:+.2%} 回撤={new_res['max_drawdown']:.2%} "
                  f"夏普={new_res['sharpe']:.2f} 交易={new_res['trades']}次 胜率={new_res['win_rate']:.0%}")
        except Exception as e:
            print(f"    ❌ 回测失败: {e}")
            import traceback
            traceback.print_exc()
            failed.append((code, name, f"回测失败: {e}"))

    # 汇总
    print(f"\n\n{'='*100}")
    print(f"  📈 对比汇总")
    print(f"{'='*100}")

    # 逐股对比
    print(f"\n  {'股票':<10} {'旧收益':>8} {'新收益':>8} {'差异':>8} {'旧回撤':>8} {'新回撤':>8} {'差异':>8} "
          f"{'旧夏普':>7} {'新夏普':>7} {'旧交易':>6} {'新交易':>6} {'旧胜率':>6} {'新胜率':>6} {'买入持有':>8}")
    print(f"  {'─'*135}")

    old_returns = []
    new_returns = []
    old_drawdowns = []
    new_drawdowns = []
    old_sharpes = []
    new_sharpes = []

    for r in results:
        o = r["old"]
        n = r["new"]
        ret_diff = n["total_return"] - o["total_return"]
        dd_diff = n["max_drawdown"] - o["max_drawdown"]
        print(f"  {r['name']:<10} {o['total_return']:>+7.2%} {n['total_return']:>+7.2%} {ret_diff:>+7.2%} "
              f"{o['max_drawdown']:>7.2%} {n['max_drawdown']:>7.2%} {dd_diff:>+7.2%} "
              f"{o['sharpe']:>7.2f} {n['sharpe']:>7.2f} "
              f"{o['trades']:>6} {n['trades']:>6} "
              f"{o['win_rate']:>5.0%} {n['win_rate']:>5.0%} "
              f"{o['buy_hold']:>+7.2%}")
        old_returns.append(o["total_return"])
        new_returns.append(n["total_return"])
        old_drawdowns.append(o["max_drawdown"])
        new_drawdowns.append(n["max_drawdown"])
        old_sharpes.append(o["sharpe"])
        new_sharpes.append(n["sharpe"])

    # 平均
    print(f"  {'─'*135}")
    n_stocks = len(results)
    print(f"  {'平均':<10} {np.mean(old_returns):>+7.2%} {np.mean(new_returns):>+7.2%} "
          f"{np.mean(new_returns)-np.mean(old_returns):>+7.2%} "
          f"{np.mean(old_drawdowns):>7.2%} {np.mean(new_drawdowns):>7.2%} "
          f"{np.mean(new_drawdowns)-np.mean(old_drawdowns):>+7.2%} "
          f"{np.mean(old_sharpes):>7.2f} {np.mean(new_sharpes):>7.2f} "
          f"{'':>6} {'':>6} {'':>6} {'':>6}")

    # 分析
    print(f"\n  {'='*80}")
    print(f"  📋 分析")
    print(f"  {'='*80}")

    better_return = sum(1 for r in results if r["new"]["total_return"] > r["old"]["total_return"])
    worse_return = sum(1 for r in results if r["new"]["total_return"] < r["old"]["total_return"])
    better_dd = sum(1 for r in results if r["new"]["max_drawdown"] < r["old"]["max_drawdown"])
    worse_dd = sum(1 for r in results if r["new"]["max_drawdown"] > r["old"]["max_drawdown"])
    better_sharpe = sum(1 for r in results if r["new"]["sharpe"] > r["old"]["sharpe"])
    worse_sharpe = sum(1 for r in results if r["new"]["sharpe"] < r["old"]["sharpe"])
    fewer_trades = sum(1 for r in results if r["new"]["trades"] < r["old"]["trades"])
    more_trades = sum(1 for r in results if r["new"]["trades"] > r["old"]["trades"])

    print(f"  收益率: 新>旧 {better_return}只, 新<旧 {worse_return}只, 持平 {n_stocks-better_return-worse_return}只")
    print(f"  回撤:   新<旧(改善) {better_dd}只, 新>旧(恶化) {worse_dd}只, 持平 {n_stocks-better_dd-worse_dd}只")
    print(f"  夏普:   新>旧 {better_sharpe}只, 新<旧 {worse_sharpe}只, 持平 {n_stocks-better_sharpe-worse_sharpe}只")
    print(f"  交易数: 新<旧(减少) {fewer_trades}只, 新>旧(增加) {more_trades}只, 持平 {n_stocks-fewer_trades-more_trades}只")

    avg_old_ret = np.mean(old_returns)
    avg_new_ret = np.mean(new_returns)
    avg_old_dd = np.mean(old_drawdowns)
    avg_new_dd = np.mean(new_drawdowns)
    print(f"\n  平均收益率: 旧={avg_old_ret:+.2%} → 新={avg_new_ret:+.2%} (差异={avg_new_ret-avg_old_ret:+.2%})")
    print(f"  平均回撤:   旧={avg_old_dd:.2%} → 新={avg_new_dd:.2%} (差异={avg_new_dd-avg_old_dd:+.2%})")
    print(f"  平均夏普:   旧={np.mean(old_sharpes):.2f} → 新={np.mean(new_sharpes):.2f} (差异={np.mean(new_sharpes)-np.mean(old_sharpes):+.2f})")

    if failed:
        print(f"\n⚠️ 失败（{len(failed)}只）：")
        for code, name, reason in failed:
            print(f"  {name}({code}): {reason}")

    print(f"\n  {'='*100}")
    print(f"  ⚠️ 以上为回测结果，基于历史数据，不代表未来收益。仅供研究参考。")
    print(f"  {'='*100}")


if __name__ == "__main__":
    main()
