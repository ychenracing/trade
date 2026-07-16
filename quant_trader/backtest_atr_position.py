"""
ATR自适应仓位 vs 固定比例仓位 对比回测
- 同一套v5策略逻辑（动量评分）
- 只变仓位计算方式：
  A) 固定比例：当前逻辑（30%×趋势系数×高位降仓）
  B) ATR自适应：海龟式（每笔风险=账户×1.5%，股数=风险÷ATR÷100×100）
"""
import sys
import datetime
import pandas as pd
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from strategy.base import Signal
from strategy.combo import ComboStrategy
from execution.simulator import SimulatorExecutor
from risk.manager import RiskManager
from risk.position_sizer import PositionSizer, PositionInfo
from utils.helpers import (
    calc_ema, calc_sma, calc_macd, calc_donchian, calc_atr,
    calc_momentum_score, calc_trend_mode, round_lot,
)
from utils.logger import log, setup_logger
from config.settings import RISK_CONFIG, TRADE_CONFIG, INITIAL_CAPITAL

setup_logger()
log.remove()
log.add(sys.stderr, level="ERROR")

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

WARMUP = 70

# ATR仓位参数
ATR_RISK_PERCENT = 0.015       # 每笔风险=账户的1.5%
ATR_PERIOD = 20                # ATR计算周期
MAX_POSITION_RATIO = 0.30      # 单股仓位上限30%（跟固定模式一致）
MIN_POSITION_RATIO = 0.05      # 单股仓位下限5%


def get_hist_daily(code: str, days: int = 320) -> pd.DataFrame:
    import akshare as ak
    df = ak.stock_zh_a_daily(symbol=code, adjust="qfq")
    df = df.rename(columns={"date": "date", "open": "open", "high": "high",
                            "low": "low", "close": "close", "volume": "volume"})
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    df = df.tail(days).copy()
    df["ma60"] = calc_sma(df["close"], 60)
    df["atr"] = calc_atr(df["high"], df["low"], df["close"], ATR_PERIOD)
    return df


# ======================================================================
# 仓位计算 - A: 固定比例（当前逻辑）
# ======================================================================
def calc_fixed_shares(position_sizer, price, available_cash, current_positions,
                      position_value, price_gain_pct, trend_mode,
                      is_pyramid=False, base_shares=0):
    """固定比例仓位 - 复现当前PositionSizer逻辑"""
    return position_sizer.calc_buy_size(
        price, available_cash, current_positions,
        position_value=position_value,
        price_gain_pct=price_gain_pct,
        is_pyramid=is_pyramid,
        base_shares=base_shares,
        trend_mode=trend_mode,
    )


# ======================================================================
# 仓位计算 - B: ATR自适应（海龟式）
# ======================================================================
def calc_atr_shares(account_value, price, atr, available_cash, current_positions,
                    position_value, price_gain_pct, trend_mode,
                    is_pyramid=False, base_shares=0,
                    max_ratio=MAX_POSITION_RATIO, min_ratio=MIN_POSITION_RATIO):
    """
    ATR自适应仓位 - 海龟式改进版
    核心公式: 股数 = (账户 × 风险比例) ÷ (ATR × 每手股数) × 每手股数
    再叠加趋势系数、高位降仓、仓位上下限
    """
    if current_positions >= RISK_CONFIG["max_positions"] and position_value == 0:
        return 0

    if atr <= 0 or price <= 0:
        return 0

    # 趋势系数
    trend_mult = 1.0
    if trend_mode == "down":
        trend_mult = 0.40
    elif trend_mode == "mixed":
        trend_mult = 0.60

    # 高位降仓
    gain_mult = 1.0
    if price_gain_pct > 3.0:
        gain_mult = 0.6
    elif price_gain_pct > 2.0:
        gain_mult = 0.8

    # ATR自适应核心：每笔风险 = 账户 × 风险比例
    risk_amount = account_value * ATR_RISK_PERCENT * trend_mult * gain_mult

    # 止损距离 = ATR × 止损倍数（跟RiskManager的atr_stop_multiple一致）
    stop_distance = atr * RISK_CONFIG["atr_stop_multiple"]

    # 海龟式股数 = 风险金额 ÷ 止损距离
    if stop_distance <= 0:
        return 0
    raw_shares = int(risk_amount / stop_distance)
    shares = round_lot(raw_shares)

    # 仓位上限检查
    max_position_value = account_value * max_ratio * trend_mult * gain_mult
    if is_pyramid:
        if not RISK_CONFIG.get("pyramid_allowed", False):
            return 0
        if trend_mode == "down":
            return 0
        if base_shares <= 0:
            return 0
        target_shares = int(base_shares * RISK_CONFIG["pyramid_size_ratio"])
        target_shares = round_lot(target_shares)
        max_additional = max_position_value - position_value
        max_add_shares = int(max_additional / price) if price > 0 else 0
        shares = min(target_shares, max_add_shares)
        shares = round_lot(shares)
    else:
        # 仓位下限检查
        min_position_value = account_value * min_ratio
        max_by_cash = int((available_cash * 0.98) / price)
        max_by_ratio = int(max_position_value / price)
        if position_value > 0:
            max_by_ratio = int((max_position_value - position_value) / price)
        shares = min(shares, max_by_cash, max_by_ratio)
        shares = round_lot(shares)

        # 下限：如果计算出的仓位低于min_ratio，不建仓（避免太小的仓位）
        if shares * price < min_position_value and not is_pyramid:
            # 但如果资金不够就算了
            if available_cash * 0.98 >= min_position_value:
                shares = round_lot(int(min_position_value / price))

    if shares <= 0:
        return 0

    return shares


# ======================================================================
# 回测引擎（共用）
# ======================================================================
def run_backtest(df, code, name, use_atr_position, initial_capital=INITIAL_CAPITAL):
    """
    单股票回测
    use_atr_position: True=ATR自适应仓位, False=固定比例仓位
    策略逻辑都用v5（ComboStrategy）
    """
    simulator = SimulatorExecutor(initial_capital)
    risk_mgr = RiskManager(initial_capital)
    position_sizer = PositionSizer(initial_capital)
    strategy = ComboStrategy()

    trade_log = []
    daily_nav = []

    if len(df) <= WARMUP:
        return _empty_result(initial_capital)

    for i in range(WARMUP, len(df)):
        window = df.iloc[:i+1].copy()
        current_price = float(window.iloc[-1]["close"])
        current_atr = float(window.iloc[-1]["atr"]) if not np.isnan(window.iloc[-1]["atr"]) else 0
        current_date = window.index[-1].date() if hasattr(window.index[-1], 'date') else window.index[-1]

        simulator._check_date_rollover(current_date)

        ma60 = float(window.iloc[-1]["ma60"]) if not np.isnan(window.iloc[-1]["ma60"]) else current_price
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
                        trade_log.append({"date": str(current_date), "action": "SELL(熔断)",
                                          "price": order.filled_price, "shares": order.filled_shares,
                                          "pnl": realized_pnl})
            account_after = simulator.get_account()
            daily_nav.append({"date": str(current_date), "nav": account_after.total_value,
                              "drawdown": risk_mgr.status.current_drawdown})
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
                    trade_log.append({"date": str(current_date), "action": action,
                                      "price": order.filled_price, "shares": order.filled_shares,
                                      "pnl": realized_pnl})
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
                    if use_atr_position:
                        add_shares = calc_atr_shares(
                            total_value, current_price, current_atr, simulator.cash,
                            len(simulator.positions), pos.market_value,
                            price_gain_pct, trend_mode,
                            is_pyramid=True, base_shares=pos.base_shares)
                    else:
                        add_shares = calc_fixed_shares(
                            position_sizer, current_price, simulator.cash,
                            len(simulator.positions), pos.market_value,
                            price_gain_pct, trend_mode,
                            is_pyramid=True, base_shares=pos.base_shares)
                    if add_shares > 0:
                        order = simulator.buy(code, name, current_price, add_shares, current_date)
                        if order.status.value == "filled":
                            pos.pyramid_count += 1
                            pos.profit_pct = (current_price - pos.cost_price) / pos.cost_price
                            trade_log.append({"date": str(current_date), "action": "BUY(加码)",
                                              "price": order.filled_price, "shares": add_shares})

        # 策略信号
        result = strategy.generate_signal(window)
        signal = result.signal

        if signal == Signal.BUY:
            if risk_mgr.status.daily_halt:
                continue
            if code not in simulator.positions:
                if use_atr_position:
                    shares = calc_atr_shares(
                        total_value, current_price, current_atr, simulator.cash,
                        len(simulator.positions), 0,
                        price_gain_pct, trend_mode)
                else:
                    shares = calc_fixed_shares(
                        position_sizer, current_price, simulator.cash,
                        len(simulator.positions), 0,
                        price_gain_pct, trend_mode)
                if shares > 0:
                    order = simulator.buy(code, name, current_price, shares, current_date)
                    if order.status.value == "filled":
                        pos = simulator.positions[code]
                        pos.peak_price = order.filled_price
                        pos.base_shares = shares
                        pos.entry_atr = current_atr
                        trade_log.append({"date": str(current_date), "action": "BUY",
                                          "price": order.filled_price, "shares": shares,
                                          "reason": result.reason,
                                          "position_pct": shares * current_price / total_value})

        elif signal == Signal.SELL and code in simulator.positions:
            if code not in simulator.today_bought:
                pos = simulator.positions[code]
                order = simulator.sell(code, name, current_price, pos.shares, current_date)
                if order.status.value == "filled":
                    realized_pnl = order.realized_pnl
                    risk_mgr.record_realized_loss(realized_pnl)
                    trade_log.append({"date": str(current_date), "action": "SELL",
                                      "price": order.filled_price, "shares": order.filled_shares,
                                      "pnl": realized_pnl})

        account = simulator.get_account()
        daily_nav.append({"date": str(current_date), "nav": account.total_value,
                          "drawdown": risk_mgr.status.current_drawdown})

    return _calc_stats(daily_nav, trade_log, df, initial_capital)


def _empty_result(initial_capital):
    return {"final_nav": initial_capital, "total_return": 0, "max_drawdown": 0,
            "sharpe": 0, "trades": 0, "win_rate": 0, "daily_nav": [], "avg_position_pct": 0}


def _calc_stats(daily_nav, trade_log, df, initial_capital):
    if not daily_nav:
        return _empty_result(initial_capital)

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

    trade_df = pd.DataFrame(trade_log) if trade_log else pd.DataFrame()
    sell_trades = trade_df[trade_df["action"].str.contains("SELL", na=False)] if len(trade_df) > 0 else pd.DataFrame()
    if len(sell_trades) > 0 and "pnl" in sell_trades.columns:
        wins = sell_trades[sell_trades["pnl"] > 0]
        win_rate = len(wins) / len(sell_trades) if len(sell_trades) > 0 else 0
    else:
        win_rate = 0

    bh_return = (df.iloc[-1]["close"] / df.iloc[WARMUP]["close"] - 1)

    # 平均仓位比例
    buy_trades = trade_df[trade_df["action"] == "BUY"] if len(trade_df) > 0 else pd.DataFrame()
    avg_pos_pct = buy_trades["position_pct"].mean() if len(buy_trades) > 0 and "position_pct" in buy_trades.columns else 0

    return {
        "final_nav": final_nav, "total_return": total_return,
        "annual_return": annual_return, "max_drawdown": max_drawdown,
        "sharpe": sharpe, "trades": len(trade_log),
        "sell_count": len(sell_trades), "win_rate": win_rate,
        "buy_hold": bh_return, "daily_nav": daily_nav,
        "avg_position_pct": avg_pos_pct,
    }


# ======================================================================
# 主函数
# ======================================================================
def main():
    print("=" * 110)
    print("  📊 ATR自适应仓位 vs 固定比例仓位 对比回测")
    print(f"  策略: v5动量评分 | 资金: {INITIAL_CAPITAL:,.0f} | 回测区间: 过去~16个月")
    print(f"  固定模式: 单股30%×趋势系数×高位降仓")
    print(f"  ATR模式: 风险={ATR_RISK_PERCENT:.1%}×趋势系数÷(ATR×{RISK_CONFIG['atr_stop_multiple']}) + 上下限{MIN_POSITION_RATIO:.0%}~{MAX_POSITION_RATIO:.0%}")
    print("=" * 110)

    all_data = {}
    failed = []

    for code, name in STOCKS:
        print(f"  [{code}] {name} 拉取数据中...", end=" ")
        try:
            df = get_hist_daily(code)
            all_data[code] = df
            print(f"数据{len(df)}条, {df.index[0].date()} ~ {df.index[-1].date()}, close={df.iloc[-1]['close']:.2f} ATR={df.iloc[-1]['atr']:.2f}")
        except Exception as e:
            print(f"失败: {e}")
            failed.append((code, name, str(e)))

    results = []
    for code, name in STOCKS:
        if code not in all_data:
            continue
        df = all_data[code]
        print(f"\n  ── {name}({code}) ──")

        # A: 固定比例
        r_fixed = run_backtest(df, code, name, use_atr_position=False)
        print(f"    固定: 收益={r_fixed['total_return']:+.2%} 回撤={r_fixed['max_drawdown']:.2%} 夏普={r_fixed['sharpe']:.2f} 交易={r_fixed['trades']}次 胜率={r_fixed['win_rate']:.0%} 仓位={r_fixed['avg_position_pct']:.1%}")

        # B: ATR自适应
        r_atr = run_backtest(df, code, name, use_atr_position=True)
        print(f"    ATR:  收益={r_atr['total_return']:+.2%} 回撤={r_atr['max_drawdown']:.2%} 夏普={r_atr['sharpe']:.2f} 交易={r_atr['trades']}次 胜率={r_atr['win_rate']:.0%} 仓位={r_atr['avg_position_pct']:.1%}")

        results.append({"code": code, "name": name, "fixed": r_fixed, "atr": r_atr})

    # 汇总
    print(f"\n{'='*110}")
    print("  📈 对比汇总")
    print(f"{'='*110}")
    print(f"\n  {'股票':<12} {'固定收益':>8} {'ATR收益':>8} {'差异':>8} {'固定回撤':>8} {'ATR回撤':>8} {'差异':>8} {'固定夏普':>8} {'ATR夏普':>8} {'固定仓位':>8} {'ATR仓位':>8} {'买入持有':>10}")
    print(f"  {'─'*120}")

    for r in results:
        f, a = r["fixed"], r["atr"]
        ret_diff = a["total_return"] - f["total_return"]
        dd_diff = a["max_drawdown"] - f["max_drawdown"]
        print(f"  {r['name']:<12} {f['total_return']:>+7.2%} {a['total_return']:>+7.2%} {ret_diff:>+7.2%} "
              f"{f['max_drawdown']:>7.2%} {a['max_drawdown']:>7.2%} {dd_diff:>+7.2%} "
              f"{f['sharpe']:>7.2f} {a['sharpe']:>7.2f} "
              f"{f['avg_position_pct']:>7.1%} {a['avg_position_pct']:>7.1%} "
              f"{f['buy_hold']:>+9.2%}")

    print(f"  {'─'*120}")
    avg_f_ret = np.mean([r["fixed"]["total_return"] for r in results])
    avg_a_ret = np.mean([r["atr"]["total_return"] for r in results])
    avg_f_dd = np.mean([r["fixed"]["max_drawdown"] for r in results])
    avg_a_dd = np.mean([r["atr"]["max_drawdown"] for r in results])
    avg_f_sharpe = np.mean([r["fixed"]["sharpe"] for r in results])
    avg_a_sharpe = np.mean([r["atr"]["sharpe"] for r in results])
    avg_f_pos = np.mean([r["fixed"]["avg_position_pct"] for r in results])
    avg_a_pos = np.mean([r["atr"]["avg_position_pct"] for r in results])
    print(f"  {'平均':<12} {avg_f_ret:>+7.2%} {avg_a_ret:>+7.2%} {avg_a_ret-avg_f_ret:>+7.2%} "
          f"{avg_f_dd:>7.2%} {avg_a_dd:>7.2%} {avg_a_dd-avg_f_dd:>+7.2%} "
          f"{avg_f_sharpe:>7.2f} {avg_a_sharpe:>7.2f} "
          f"{avg_f_pos:>7.1%} {avg_a_pos:>7.1%}")

    # 分析
    n = len(results)
    better_ret = sum(1 for r in results if r["atr"]["total_return"] > r["fixed"]["total_return"])
    worse_ret = sum(1 for r in results if r["atr"]["total_return"] < r["fixed"]["total_return"])
    better_dd = sum(1 for r in results if r["atr"]["max_drawdown"] < r["fixed"]["max_drawdown"])
    worse_dd = sum(1 for r in results if r["atr"]["max_drawdown"] > r["fixed"]["max_drawdown"])
    better_sharpe = sum(1 for r in results if r["atr"]["sharpe"] > r["fixed"]["sharpe"])
    worse_sharpe = sum(1 for r in results if r["atr"]["sharpe"] < r["fixed"]["sharpe"])

    print(f"\n  {'='*80}")
    print(f"  📋 分析")
    print(f"  {'='*80}")
    print(f"  收益率: ATR>固定 {better_ret}只, ATR<固定 {worse_ret}只, 持平 {n-better_ret-worse_ret}只")
    print(f"  回撤:   ATR<固定(改善) {better_dd}只, ATR>固定(恶化) {worse_dd}只, 持平 {n-better_dd-worse_dd}只")
    print(f"  夏普:   ATR>固定 {better_sharpe}只, ATR<固定 {worse_sharpe}只, 持平 {n-better_sharpe-worse_sharpe}只")
    print(f"\n  平均收益率: 固定={avg_f_ret:+.2%} → ATR={avg_a_ret:+.2%} (差异={avg_a_ret-avg_f_ret:+.2%})")
    print(f"  平均回撤:   固定={avg_f_dd:.2%} → ATR={avg_a_dd:.2%} (差异={avg_a_dd-avg_f_dd:+.2%})")
    print(f"  平均夏普:   固定={avg_f_sharpe:.2f} → ATR={avg_a_sharpe:.2f} (差异={avg_a_sharpe-avg_f_sharpe:+.2f})")
    print(f"  平均仓位:   固定={avg_f_pos:.1%} → ATR={avg_a_pos:.1%} (差异={avg_a_pos-avg_f_pos:+.1%})")

    if failed:
        print(f"\n  ⚠️ 失败（{len(failed)}只）：")
        for code, name, reason in failed:
            print(f"    {name}({code}): {reason}")

    print(f"\n  {'='*110}")
    print(f"  ⚠️ 以上为回测结果，基于历史数据，不代表未来收益。仅供研究参考。")
    print(f"  {'='*110}")


if __name__ == "__main__":
    main()
