#!/usr/bin/env python3
"""Daily signal scan for the 26 AI-sector universe.

Fetches the latest forward-adjusted closing data via AKShare (with incremental
cache), runs the Quant Fusion backtest, and extracts the latest pending
signal (buy / sell / hold) for each symbol.

Usage:
    python daily_signal_scan.py [--end-date YYYY-MM-DD] [--cache-dir DIR]

If --end-date is omitted, today's date is used.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")

import pandas as pd

import quant_fusion as qf


# ── 26-stock AI sector universe ──────────────────────────────────────────
# 长鑫科技 (688825) listed 2026-07-27 on STAR Market.
SYMBOLS: dict[str, str] = {
    "300308": "中际旭创",
    "300502": "新易盛",
    "300394": "天孚通信",
    "688498": "源杰科技",
    "002281": "光迅科技",
    "601869": "长飞光纤",
    "688008": "澜起科技",
    "603986": "兆易创新",
    "300223": "北京君正",
    "688825": "长鑫科技",
    "688256": "寒武纪",
    "688041": "海光信息",
    "002371": "北方华创",
    "688012": "中微公司",
    "688072": "拓荆科技",
    "688082": "盛美上海",
    "688120": "华海清科",
    "688037": "芯源微",
    "688361": "中科飞测",
    "300604": "长川科技",
    "688019": "安集科技",
    "300054": "鼎龙股份",
    "002409": "雅克科技",
    "300666": "江丰电子",
    "688268": "华特气体",
    "688300": "联瑞新材",
}

START_DATE = "2026-07-01"
INITIAL_CAPITAL = 2_000_000.0
DEFAULT_CACHE_DIR = "/workspace/trade/data_cache"
DEFAULT_OUTPUT_DIR = "/workspace/trade/daily_signals"


def _today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


def _classify_signal(signal: Any) -> str:
    """Map a pending Signal direction to a Chinese label."""
    direction = getattr(signal, "direction", "")
    if direction == "buy":
        return "买入"
    if direction == "sell":
        return "卖出"
    return "持有"


def _extract_positions(trades: list[Any]) -> dict[str, int]:
    """Reconstruct net share count per symbol from the trade ledger."""
    held: dict[str, int] = defaultdict(int)
    for trade in trades:
        if trade.direction == "buy":
            held[trade.symbol] += trade.shares
        elif trade.direction == "sell":
            held[trade.symbol] -= trade.shares
    return {sym: max(0, sh) for sym, sh in held.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily AI-sector signal scan")
    parser.add_argument(
        "--end-date",
        default="",
        help="Backtest end date YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--cache-dir",
        default=DEFAULT_CACHE_DIR,
        help=f"Cache directory for incremental data (default: {DEFAULT_CACHE_DIR})",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory for results (default: {DEFAULT_OUTPUT_DIR})",
    )
    args = parser.parse_args()

    end_date = args.end_date or _today_str()

    print("=" * 72)
    print("  AI 板块 26 标的每日信号扫描")
    print("=" * 72)
    print(f"  标的数量:   {len(SYMBOLS)}")
    print(f"  回测区间:   {START_DATE} → {end_date}")
    print(f"  初始资金:   ¥{INITIAL_CAPITAL:,.0f}")
    print(f"  指标状态:   warm")
    print(f"  数据源:     AKShare 在线前复权 (增量缓存)")
    print(f"  缓存目录:   {args.cache_dir}")
    print("-" * 72)

    # Configure incremental cache for efficient daily updates
    qf.DataFetcher._cache_dir = args.cache_dir

    # ── Pre-screen: skip symbols with no data (e.g. not yet listed) ──
    # The engine loads all symbols at once and raises on any failure, so we
    # probe each symbol individually and build a tradable universe.
    tradable: dict[str, str] = {}
    skipped: list[tuple[str, str, str]] = []  # (code, name, reason)
    # Use the warmup start date (1 year before START_DATE) for the probe so
    # the engine has enough history for indicator calculation.
    probe_start = (
        pd.Timestamp(START_DATE) - pd.Timedelta(days=400)
    ).strftime("%Y-%m-%d")

    print("  正在检查标的可交易性...")
    print("-" * 72)
    for code, name in SYMBOLS.items():
        try:
            df = qf.DataFetcher.load_stock_data(
                code, probe_start, end_date, data_dir=None
            )
            if df is not None and not df.empty:
                tradable[code] = name
                print(f"  ✓ {code} {name}: {len(df)} 条数据")
            else:
                skipped.append((code, name, "无数据"))
                print(f"  ✗ {code} {name}: 无数据 (可能尚未上市)")
        except Exception as exc:
            skipped.append((code, name, str(exc)[:80]))
            print(f"  ✗ {code} {name}: 数据获取失败 — {str(exc)[:60]}")

    print("-" * 72)
    print(f"  可交易标的: {len(tradable)}  |  跳过: {len(skipped)}")
    if skipped:
        print("  跳过标的:")
        for code, name, reason in skipped:
            print(f"    {code} {name}: {reason}")
    print("-" * 72)

    if not tradable:
        print("  错误: 没有可交易的标的，退出。")
        return 1

    print("  正在运行回测，请稍候...")
    print("-" * 72)

    engine = qf.BacktestEngine(INITIAL_CAPITAL)
    result = engine.run(
        tradable,
        START_DATE,
        end_date,
        data_dir=None,  # online AKShare with cache
        indicator_state="warm",
        warmup_calendar_days=365,
    )

    # ── Extract latest pending signals ───────────────────────────────
    pending = result.get("pending_signals", [])
    # Group by symbol: collect all pending signals per symbol
    symbol_signals: dict[str, list[Any]] = defaultdict(list)
    for sig in pending:
        symbol_signals[sig.symbol].append(sig)

    # Reconstruct current positions from trade ledger
    positions = _extract_positions(result.get("trades", []))

    # ── Build per-symbol signal summary ──────────────────────────────
    # Determine the latest signal for each symbol
    # Priority: pending buy/sell > holding position > no position
    symbol_names = {code: name for code, name in SYMBOLS.items()}
    rows: list[dict[str, Any]] = []

    # Build a set of skipped codes for quick lookup
    skipped_codes = {code for code, _, _ in skipped}

    for code in sorted(SYMBOLS.keys()):
        name = symbol_names[code]

        # If the stock was skipped (no data), mark as "不可交易"
        if code in skipped_codes:
            rows.append({
                "code": code,
                "name": name,
                "signal": "不可交易",
                "held_shares": 0,
                "strategies": "无数据/未上市",
            })
            continue

        sigs = symbol_signals.get(code, [])
        held_shares = positions.get(code, 0)

        if sigs:
            # Has pending signal(s) — show the direction
            # If multiple signals (from different sleeves), show all unique directions
            directions = sorted({s.direction for s in sigs})
            if len(directions) == 1:
                signal_label = _classify_signal(sigs[0])
                strategies = ", ".join(sorted({s.strategy_name for s in sigs}))
            else:
                # Mixed signals (e.g., buy from one sleeve, sell from another)
                parts = []
                for d in directions:
                    d_sigs = [s for s in sigs if s.direction == d]
                    parts.append(
                        f"{_classify_signal(d_sigs[0])}"
                        f"({len(d_sigs)})"
                    )
                signal_label = " + ".join(parts)
                strategies = ", ".join(sorted({s.strategy_name for s in sigs}))
        elif held_shares > 0:
            signal_label = "持有"
            strategies = "—"
        else:
            signal_label = "观望"
            strategies = "—"

        rows.append({
            "code": code,
            "name": name,
            "signal": signal_label,
            "held_shares": held_shares,
            "strategies": strategies,
            "industry": qf._CoreBacktestEngine._SYMBOL_GROUP.get(code, "default"),
            "profile": qf._CoreBacktestEngine._SYMBOL_PROFILE.get(code, "default"),
        })

    # ── Print summary table ──────────────────────────────────────────
    print()
    print(f"{'代码':<10} {'名称':<10} {'信号':<12} {'行业':<20} {'Profile':<12} {'持仓股数':>12}  {'策略'}")
    print("─" * 100)

    buy_count = sell_count = hold_count = wait_count = untradeable_count = 0
    for row in rows:
        print(
            f"{row['code']:<10} {row['name']:<10} {row['signal']:<12} "
            f"{row['industry']:<20} {row['profile']:<12} "
            f"{row['held_shares']:>12,}  {row['strategies']}"
        )
        if "买入" in row["signal"]:
            buy_count += 1
        elif "卖出" in row["signal"]:
            sell_count += 1
        elif "不可交易" in row["signal"]:
            untradeable_count += 1
        elif "观望" in row["signal"]:
            wait_count += 1
        else:
            hold_count += 1

    print("─" * 72)
    print(
        f"信号汇总:  买入 {buy_count}  |  卖出 {sell_count}  |  "
        f"持有 {hold_count}  |  观望 {wait_count}  |  不可交易 {untradeable_count}"
    )
    print()

    # ── Portfolio metrics ────────────────────────────────────────────
    print("─" * 72)
    print("  组合绩效指标")
    print("─" * 72)
    print(f"  最终资产:     ¥{result['final_assets']:>15,.0f}")
    print(f"  总收益率:       {result['total_return']:>14.2%}")
    print(f"  最大回撤:       {result['max_drawdown']:>14.2%}")
    print(f"  Sharpe:         {result['sharpe']:>14.2f}")
    print(f"  总交易次数:     {result['total_trades']:>14}")
    print(
        f"  风险锁定:       "
        f"{'是' if result.get('terminal_risk_lock') else '否'}"
    )
    guard = result.get("sector_guard_active", False)
    print(f"  板块风控激活:   {'是' if guard else '否'}")
    print()

    # ── Stale data warning ──────────────────────────────────────────
    # If any symbol's data is marked stale (network fetch failed, using cache),
    # warn the user that signals may not reflect the latest session.
    stale_symbols = []
    # Check result for stale data markers
    for code in tradable:
        try:
            df = qf.DataFetcher.load_stock_data(
                code, probe_start, end_date, data_dir=None
            )
            if df is not None and not df.empty and df.attrs.get("_stale", False):
                stale_symbols.append((code, df.attrs.get("_cache_last_date", "?")))
        except Exception:
            pass
    if stale_symbols:
        print("─" * 72)
        print("  ⚠ 数据过期警告")
        print("─" * 72)
        for code, last_date in stale_symbols:
            print(f"  {code} {SYMBOLS.get(code, '?')}: 缓存截止 {last_date}（网络获取失败）")
        print("  信号可能不反映最新交易日，请勿直接用于实盘决策。")
        print()

    # ── Bear market position advisory ────────────────────────────────
    # Run a quick prior-period check to advise on position sizing
    if result.get("max_drawdown", 0) and abs(result["max_drawdown"]) > 0.15:
        dd = abs(result["max_drawdown"])
        if dd > 0.20:
            advisory = f"  ⚠ 当前组合最大回撤 {dd:.1%}，建议总仓位不超过50%"
        elif dd > 0.15:
            advisory = f"  ⚠ 当前组合最大回撤 {dd:.1%}，建议总仓位不超过70%"
        print("─" * 72)
        print("  弱市仓位建议")
        print("─" * 72)
        print(advisory)
        print()

    # ── Risk events (latest) ─────────────────────────────────────────
    risk_events = result.get("risk_events", [])
    if risk_events:
        latest_events = risk_events[-5:]
        print("─" * 72)
        print("  最近风控事件 (最多5条)")
        print("─" * 72)
        for ev in latest_events:
            print(
                f"  {ev.get('date', '?')}  {ev.get('event', '?')}  "
                f"[{ev.get('sleeve', 'portfolio')}]"
            )
        print()

    # ── Save JSON artifact ───────────────────────────────────────────
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"signals_{end_date}.json"

    # Serialize pending signals
    pending_serializable = []
    for sig in pending:
        try:
            pending_serializable.append(asdict(sig))
        except TypeError:
            pending_serializable.append({
                "symbol": getattr(sig, "symbol", ""),
                "direction": getattr(sig, "direction", ""),
                "strategy_name": getattr(sig, "strategy_name", ""),
                "target_shares": getattr(sig, "target_shares", 0),
                "price": getattr(sig, "price", 0.0),
                "reason": getattr(sig, "reason", ""),
                "signal_date": getattr(sig, "signal_date", ""),
            })

    artifact = {
        "scan_date": end_date,
        "symbols": SYMBOLS,
        "start_date": START_DATE,
        "initial_capital": INITIAL_CAPITAL,
        "signals": rows,
        "summary": {
            "buy": buy_count,
            "sell": sell_count,
            "hold": hold_count,
        },
        "portfolio": {
            "final_assets": float(result["final_assets"]),
            "total_return": float(result["total_return"]),
            "max_drawdown": float(result["max_drawdown"]),
            "sharpe": float(result["sharpe"]),
            "total_trades": int(result["total_trades"]),
            "sector_guard_active": bool(guard),
            "terminal_risk_lock": bool(result.get("terminal_risk_lock", False)),
        },
        "pending_signals": pending_serializable,
    }
    output_file.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"  结果已保存: {output_file}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
