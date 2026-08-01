#!/usr/bin/env python3
"""Daily signal scan for the 26 AI-sector universe.

Fetches the latest forward-adjusted closing data via AKShare (with incremental
cache), runs the Quant Fusion backtest, and extracts the latest pending
signal (buy / sell / hold) for each symbol.

Two modes:
  - Simulation mode (default): runs a fresh backtest from --start-date.
    Signals reflect what the strategy *would have* done, not your real portfolio.
  - Account mode (--account account.json): overlays real cash, positions, and
    risk state on top of the backtest. Displays simulated vs real holdings side
    by side with discrepancy warnings.

Risk state (terminal_risk_lock, sector_guard_active, drawdown) is persisted to
risk_state.json with enhanced identity fields (symbols_hash, run_id, account_path)
after each run and restored on the next run for continuity. Old risk state files
without symbols_hash are rejected (fail-closed) to prevent cross-contamination.

Stale data fail-closed: if any symbol's cached data is stale (network fetch
failed) or data end dates are inconsistent across symbols, the scan refuses to
produce signals and exits with code 1. Override with --allow-stale only when
you understand the risk; stale-data signals must not be used for live trading.

Usage:
    # Simulation mode (default)
    python daily_signal_scan.py [--end-date YYYY-MM-DD] [--cache-dir DIR] [--capital N]

    # Account-aware mode
    python daily_signal_scan.py --account account.json [--start-date DATE] [--capital N]

If --end-date is omitted, today's date is used.
"""

from __future__ import annotations

import argparse
import hashlib
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
DEFAULT_CACHE_DIR = "data_cache"
DEFAULT_OUTPUT_DIR = "daily_signals"


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


def _load_account(path: str) -> dict[str, Any] | None:
    """Load a real-account JSON snapshot for live-signal mode.

    Expected format::

        {
          "cash": 500000.0,
          "peak_equity": 2500000.0,
          "positions": {
            "300308": {"shares": 900, "avg_cost": 980.50, "entry_date": "2026-03-18"}
          },
          "risk_state": {
            "terminal_risk_lock": false,
            "sector_guard_active": false,
            "cycle_lock_count": 0
          }
        }
    """
    p = Path(path)
    if not p.exists():
        print(f"  错误: 账户文件不存在: {path}")
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  错误: 账户文件解析失败: {exc}")
        return None
    if "cash" not in data:
        print("  错误: 账户文件缺少 'cash' 字段")
        return None
    data.setdefault("positions", {})
    data.setdefault("risk_state", {})
    if "peak_equity" not in data:
        # Use cash as a fallback when peak_equity is not provided
        data["peak_equity"] = float(data.get("cash", 0))
    return data


def _load_prev_risk_state(output_dir: str, end_date: str) -> dict[str, Any] | None:
    """Load the risk state saved by a previous daily scan run."""
    state_file = Path(output_dir) / "risk_state.json"
    if not state_file.exists():
        return None
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        if data.get("scan_date") == end_date:
            return None  # same-day state — not "previous"
        return data
    except (OSError, json.JSONDecodeError):
        return None


def _save_risk_state(
    output_dir: str, end_date: str, result: dict[str, Any],
    tradable: dict[str, str] | None = None,
    config_hash: str = "",
    account_path: str = "",
) -> None:
    """Persist risk state for restoration by the next daily scan run.

    The ``symbols_hash`` now includes symbol set, count, account fingerprint,
    and an optional config fingerprint so that the same symbol set with
    different configuration or account is treated as a different identity.
    """
    state: dict[str, Any] = {
        "scan_date": end_date,
        "terminal_risk_lock": bool(result.get("terminal_risk_lock", False)),
        "sector_guard_active": bool(result.get("sector_guard_active", False)),
        "cycle_lock_count": int(result.get("cycle_lock_count") or 0),
        "max_drawdown": float(result.get("max_drawdown", 0.0)),
        "total_return": float(result.get("total_return", 0.0)),
        "final_assets": float(result.get("final_assets", 0.0)),
    }
    if tradable:
        # Build identity hash from multiple identity fields to prevent
        # cross-contamination between different configurations.
        identity_parts = [
            "trade",
            str(len(tradable)),
            ",".join(sorted(tradable.keys())),
        ]
        if config_hash:
            identity_parts.append(config_hash)
        if account_path:
            identity_parts.append(account_path)
        state["symbols_hash"] = hashlib.sha256(
            "|".join(identity_parts).encode("utf-8")
        ).hexdigest()[:16]
        state["total_symbols"] = len(tradable)
        state["run_id"] = f"trade_{end_date}"
        state["account_path"] = account_path
    state_file = Path(output_dir) / "risk_state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


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
    parser.add_argument(
        "--allow-stale",
        action="store_true",
        help="Proceed even if cached data is stale (network fetch failed). "
        "By default, stale data causes immediate exit to prevent misleading signals.",
    )
    parser.add_argument(
        "--account",
        default="",
        help="Path to a real-account JSON file (cash, positions, risk_state). "
        "When provided, signals are overlaid with real holdings. "
        "When omitted, signals are from a fresh simulation (warning shown).",
    )
    parser.add_argument(
        "--start-date",
        default="",
        help=f"Backtest start date YYYY-MM-DD (default: {START_DATE})",
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=0.0,
        help=f"Initial capital (default: {INITIAL_CAPITAL:,.0f}). "
        "Overridden by account.cash when --account is used.",
    )
    args = parser.parse_args()

    end_date = args.end_date or _today_str()
    start_date = args.start_date or START_DATE

    # Load real account state if provided
    account: dict[str, Any] | None = None
    if args.account:
        account = _load_account(args.account)
        if account is None:
            return 1

    # ── Resolve capital: account.cash > --capital > default ──
    # P0 fix: use cash alone as engine capital because positions are
    # injected separately via account_state. Using total_equity would
    # double-count position value (once in cash, once as positions).
    if account is not None:
        cash = float(account["cash"])
        positions = account.get("positions", {})
        position_value = 0.0
        for v in positions.values():
            shares = float(v.get("shares", 0))
            # Fall back to avg_cost when price is absent — account files
            # typically store avg_cost, not the current market price.
            price = float(v.get("price", 0) or v.get("avg_cost", 0))
            if shares > 0 and price > 0:
                position_value += shares * price
        total_equity = cash + position_value
        capital = cash  # engine cash; positions injected separately
        # Store derived values for later reporting
        account["_position_value"] = position_value
        account["_total_equity"] = total_equity
    elif args.capital > 0:
        capital = args.capital
    else:
        capital = INITIAL_CAPITAL

    # Build AccountState from the loaded account data (None if simulation mode)
    account_state: qf.AccountState | None = None
    if account is not None:
        raw_peak = account.get("peak_equity", 0)
        peak_equity = float(raw_peak) if raw_peak is not None and raw_peak > 0 else float(account.get("_total_equity", capital))
        account_state = qf.AccountState(
            cash=float(account["cash"]),
            position_value=float(account.get("_position_value", 0)),
            total_equity=float(account.get("_total_equity", capital)),
            peak_equity=peak_equity,
            positions=dict(account.get("positions", {})),
            risk_state=dict(account.get("risk_state", {})),
        )

    # ── Load previous run's risk state for continuity ──
    prev_risk = _load_prev_risk_state(args.output_dir, end_date)

    # ── P0: account risk_state takes priority over file risk_state ──
    # Account risk state is the ground truth — it comes from the real brokerage
    # account and does not need symbols_hash validation. File risk state is
    # auxiliary and must pass identity checks (applied after tradable is built).
    account_risk_used = False
    if account and isinstance(account.get("risk_state"), dict):
        account_risk: dict[str, Any] = account["risk_state"]
        # Account risk state is valid if it contains any risk key — even
        # all-false means "account explicitly has no lock", which is
        # different from "no information". We check key presence, not truthiness.
        has_any_key = any(
            k in account_risk
            for k in ("terminal_risk_lock", "sector_guard_active", "cycle_lock_count")
        )
        if has_any_key:
            if prev_risk:
                # Merge: account fields override file fields (account is truth)
                merged = dict(prev_risk)
                for key in ("terminal_risk_lock", "sector_guard_active", "cycle_lock_count"):
                    if key in account_risk:
                        merged[key] = account_risk[key]
                prev_risk = merged
                print(f"  ℹ 账户风险状态已合并 (账户优先级高于文件)")
            else:
                # No file state — use account state directly (no hash needed)
                prev_risk = dict(account_risk)
                print(f"  ℹ 使用账户风险状态 (无文件状态)")
            account_risk_used = True
            if prev_risk.get("terminal_risk_lock"):
                print(f"  ⚠ 账户终态锁定已激活")
            if prev_risk.get("sector_guard_active"):
                print(f"  ⚠ 账户板块风控已激活")
        else:
            print("  ℹ 账户 risk_state 无风险字段，将使用文件/默认状态")

    mode_label = "实盘账户模式" if account else "模拟模式 (无账户文件)"
    print("=" * 72)
    print("  AI 板块 26 标的每日信号扫描")
    print("=" * 72)
    print(f"  运行模式:   {mode_label}")
    print(f"  标的数量:   {len(SYMBOLS)}")
    print(f"  回测区间:   {start_date} → {end_date}")
    if account is not None:
        total_equity = account.get("_total_equity", capital)
        position_value = account.get("_position_value", 0)
        cash = float(account["cash"])
        print(f"  初始资金:   ¥{capital:,.0f} (总权益 ¥{total_equity:,.0f} = 现金 ¥{cash:,.0f} + 持仓 ¥{position_value:,.0f})")
    else:
        print(f"  初始资金:   ¥{capital:,.0f}")
    if account is not None:
        real_positions = account.get("positions", {})
        print(f"  实盘持仓:   {len(real_positions)} 个标的")
        peak_equity = account.get("peak_equity")
        if peak_equity is not None and float(peak_equity) > 0:
            print(f"  峰值权益:   ¥{float(peak_equity):,.0f}")
    if prev_risk:
        print(f"  上次扫描:   {prev_risk.get('scan_date', '?')}")
        if prev_risk.get("terminal_risk_lock"):
            print(f"  ⚠ 上次终态锁定已激活 — 请检查是否需要人工干预")
        if prev_risk.get("sector_guard_active"):
            print(f"  ⚠ 上次板块风控已激活")
    print(f"  指标状态:   warm")
    print(f"  数据源:     AKShare 在线前复权 (增量缓存)")
    print(f"  缓存目录:   {args.cache_dir}")
    if account is None:
        print("-" * 72)
        print("  ⚠ 模拟模式: 未提供 --account 参数，信号基于从零开始的回测。")
        print("    实盘持仓、风险状态可能与模拟结果不一致。")
        print("    建议使用 --account account.json 提供真实账户状态。")
    print("-" * 72)

    # Configure incremental cache for efficient daily updates
    qf.DataFetcher._cache_dir = args.cache_dir

    # ── Pre-screen: skip symbols with no data (e.g. not yet listed) ──
    # The engine loads all symbols at once and raises on any failure, so we
    # probe each symbol individually and build a tradable universe.
    tradable: dict[str, str] = {}
    skipped: list[tuple[str, str, str]] = []  # (code, name, reason)
    # Use the warmup start date (1 year before start_date) for the probe so
    # the engine has enough history for indicator calculation.
    probe_start = (
        pd.Timestamp(start_date) - pd.Timedelta(days=400)
    ).strftime("%Y-%m-%d")

    print("  正在检查标的可交易性...")
    print("-" * 72)
    stale_symbols: list[tuple[str, str, str]] = []  # (code, name, last_cache_date)
    for code, name in SYMBOLS.items():
        try:
            df = qf.DataFetcher.load_stock_data(
                code, probe_start, end_date, data_dir=None
            )
            if df is not None and not df.empty:
                tradable[code] = name
                stale = df.attrs.get("_stale", False)
                if stale:
                    last_date = df.attrs.get("_cache_last_date", "?")
                    stale_symbols.append((code, name, str(last_date)))
                    print(f"  ⚠ {code} {name}: {len(df)} 条数据 (缓存过期，截止 {last_date})")
                else:
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

    # ── Fail-closed: refuse to produce signals on stale data ──
    if stale_symbols and not args.allow_stale:
        print("=" * 72)
        print("  ✗ 数据过期 — 拒绝生成信号 (fail-closed)")
        print("=" * 72)
        for code, name, last_date in stale_symbols:
            print(f"    {code} {name}: 缓存截止 {last_date}（网络获取失败）")
        print()
        print("  信号可能不反映最新交易日，已中止扫描。")
        print("  如需强制使用缓存数据，请添加 --allow-stale 参数。")
        print("  ⚠ 使用过期数据生成的信号不可用于实盘决策。")
        return 1
    elif stale_symbols and args.allow_stale:
        print("─" * 72)
        print("  ⚠ 数据过期警告 (--allow-stale 已启用)")
        print("─" * 72)
        for code, name, last_date in stale_symbols:
            print(f"  {code} {name}: 缓存截止 {last_date}（网络获取失败）")
        print("  信号可能不反映最新交易日，请勿直接用于实盘决策。")
        print()

    if not tradable:
        print("  错误: 没有可交易的标的，退出。")
        return 1

    # ── Data freshness cross-check: verify all stocks have data ending on same date ──
    # P1 fix: ensure no stock is silently lagging behind the others
    data_end_dates: dict[str, list[str]] = {}
    for code in tradable:
        try:
            df = qf.DataFetcher.load_stock_data(
                code, probe_start, end_date, data_dir=None
            )
            if df is not None and not df.empty:
                end = str(df.index[-1].date())
                data_end_dates.setdefault(end, []).append(code)
        except Exception:
            pass
    if len(data_end_dates) > 1 and not args.allow_stale:
        latest_common = max(data_end_dates.keys())
        lagging = sorted(d for d in data_end_dates if d < latest_common)
        if lagging:
            print("  ⚠ 数据截止日期不一致:")
            for d in sorted(data_end_dates):
                count = len(data_end_dates[d])
                marker = " ← 滞后" if d in lagging else ""
                print(f"    {d}: {count} 只标的{marker}")
            print("  ⚠ 标的间数据截止日不一致，信号可能基于不完整信息。")
            print("  建议检查数据源或等待数据更新后重试。")
            if not args.allow_stale:
                print("  ✗ 数据不一致 — 拒绝生成信号 (fail-closed)")
                print("  如需强制运行，请添加 --allow-stale 参数。")
                return 1

    # ── Validate FILE risk state identity to prevent cross-contamination ──
    # Only applies when using pure file state (no account override).
    # Account-provided risk state is the ground truth and skips this check.
    if prev_risk and not account_risk_used:
        config_fingerprint = f"capital={capital:.0f}|start={start_date}"
        identity_parts = [
            "trade",
            str(len(tradable)),
            ",".join(sorted(tradable.keys())),
            config_fingerprint,
        ]
        if args.account:
            identity_parts.append(args.account)
        current_hash = hashlib.sha256(
            "|".join(identity_parts).encode("utf-8")
        ).hexdigest()[:16]
        prev_hash = prev_risk.get("symbols_hash", "")
        prev_total = prev_risk.get("total_symbols", 0)
        if not prev_hash:
            print("  ⚠ 前次风险状态缺少 symbols_hash (旧格式)，拒绝加载以防止交叉污染。")
            print("    删除 risk_state.json 可清除此警告。")
            prev_risk = None
        elif prev_hash != current_hash:
            print("  ⚠ 前次风险状态的标的池不匹配，跳过加载以防止交叉污染。")
            if prev_total != len(tradable):
                print(f"    标的数: 上次={prev_total} 本次={len(tradable)} (不一致)")
            prev_risk = None

    print("  正在运行回测，请稍候...")
    print("-" * 72)

    engine = qf.BacktestEngine(capital)
    result = engine.run(
        tradable,
        start_date,
        end_date,
        data_dir=None,  # online AKShare with cache
        indicator_state="warm",
        warmup_calendar_days=365,
        risk_state=prev_risk,
        account_state=account_state,
    )

    # ── Extract latest pending signals ───────────────────────────────
    pending = result.get("pending_signals", [])
    # Group by symbol: collect all pending signals per symbol
    symbol_signals: dict[str, list[Any]] = defaultdict(list)
    for sig in pending:
        symbol_signals[sig.symbol].append(sig)

    # Reconstruct current positions from trade ledger
    sim_positions = _extract_positions(result.get("trades", []))

    # Real account positions (if provided)
    real_positions: dict[str, dict[str, Any]] = (
        account.get("positions", {}) if account else {}
    )

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
                "industry": qf._CoreBacktestEngine.get_symbol_group(code, "N/A"),
                "profile": qf._CoreBacktestEngine.get_symbol_profile(code, "N/A"),
                "real_shares": 0,
            })
            continue

        sigs = symbol_signals.get(code, [])
        held_shares = sim_positions.get(code, 0)

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

        # In account mode, use real holdings as the authoritative source
        if real_positions and code in real_positions:
            held_shares = int(real_positions[code].get("shares", 0))

        rows.append({
            "code": code,
            "name": name,
            "signal": signal_label,
            "held_shares": held_shares,
            "strategies": strategies,
            "industry": qf._CoreBacktestEngine.get_symbol_group(code, "default"),
            "profile": qf._CoreBacktestEngine.get_symbol_profile(code, "default"),
            "real_shares": int(real_positions[code].get("shares", 0)) if real_positions and code in real_positions else 0,
        })

    # ── Print summary table ──────────────────────────────────────────
    if account:
        print(f"{'代码':<8} {'名称':<8} {'信号':<12} {'行业':<18} {'模拟持仓':>10} {'实盘持仓':>10}  {'策略'}")
    else:
        print(f"{'代码':<10} {'名称':<10} {'信号':<12} {'行业':<20} {'Profile':<12} {'持仓股数':>12}  {'策略'}")
    print("─" * 100)

    buy_count = sell_count = hold_count = wait_count = untradeable_count = 0
    for row in rows:
        if account:
            sim_shares = sim_positions.get(row["code"], 0)
            real_shares = row["real_shares"]
            mismatch = " ⚠" if sim_shares != real_shares and (sim_shares > 0 or real_shares > 0) else ""
            print(
                f"{row['code']:<8} {row['name']:<8} {row['signal']:<12} "
                f"{row['industry']:<18} {sim_shares:>10,} {real_shares:>10,}  "
                f"{row['strategies']}{mismatch}"
            )
        else:
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
    safe_mode = result.get("safe_mode_active", False)
    if safe_mode:
        print(f"  ⚠ 保守参数模式: 已激活 (市场状态为 CHOPPY)")
    print()

    # ── Account-mode: position discrepancy warnings ─────────────────
    if account and real_positions:
        discrepancies = []
        for code in sorted(set(list(sim_positions.keys()) + list(real_positions.keys()))):
            sim_shares = sim_positions.get(code, 0)
            real_shares = int(real_positions.get(code, {}).get("shares", 0))
            if sim_shares != real_shares:
                name = SYMBOLS.get(code, code)
                discrepancies.append((code, name, sim_shares, real_shares))
        if discrepancies:
            print("─" * 72)
            print("  ⚠ 持仓差异 (模拟 vs 实盘)")
            print("─" * 72)
            for code, name, sim, real in discrepancies:
                print(f"  {code} {name}: 模拟 {sim:,} 股  |  实盘 {real:,} 股")
            print("  请核实实盘持仓是否与策略预期一致。")
            print()

    # ── Risk state continuity check ─────────────────────────────────
    if prev_risk:
        print("─" * 72)
        print("  风险状态连续性检查")
        print("─" * 72)
        prev_lock = prev_risk.get("terminal_risk_lock", False)
        curr_lock = bool(result.get("terminal_risk_lock", False))
        prev_guard = prev_risk.get("sector_guard_active", False)
        curr_guard = bool(guard)
        if prev_lock and not curr_lock:
            print("  ⚠ 上次终态锁定已激活，但本次回测未检测到 — 可能因回测区间不同")
            print("    如实盘仍有终态锁定，请勿根据本次信号加仓。")
        elif prev_lock and curr_lock:
            print("  终态锁定: 上次 ✓  本次 ✓ (一致)")
        elif not prev_lock and curr_lock:
            print("  ⚠ 本次检测到终态锁定 — 上次未激活")
        else:
            print("  终态锁定: 上次 ✗  本次 ✗ (正常)")
        if prev_guard and not curr_guard:
            print("  ⚠ 上次板块风控已激活，本次已解除 — 确认市场是否真的恢复")
        elif prev_guard and curr_guard:
            print("  板块风控: 上次 ✓  本次 ✓ (一致)")
        elif not prev_guard and curr_guard:
            print("  ⚠ 本次板块风控已激活 — 上次未激活")
        else:
            print("  板块风控: 上次 ✗  本次 ✗ (正常)")
        print(f"  上次最大回撤: {prev_risk.get('max_drawdown', 0):.2%}")
        print(f"  本次最大回撤: {result.get('max_drawdown', 0):.2%}")
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
        "mode": "account" if account else "simulation",
        "symbols": SYMBOLS,
        "start_date": start_date,
        "initial_capital": capital,
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
            "safe_mode_active": bool(result.get("safe_mode_active", False)),
            "terminal_risk_lock": bool(result.get("terminal_risk_lock", False)),
        },
        "pending_signals": pending_serializable,
    }
    if account:
        artifact["account"] = {
            "cash": float(account["cash"]),
            "position_value": float(account.get("_position_value", 0)),
            "total_equity": float(account.get("_total_equity", capital)),
            "peak_equity": float(account["peak_equity"]) if account.get("peak_equity") else None,
            "position_count": len(real_positions),
        }
    if prev_risk:
        artifact["previous_risk_state"] = prev_risk
    output_file.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    # Persist risk state for the next daily run (with enhanced identity fields)
    config_fingerprint = f"capital={capital:.0f}|start={start_date}"
    _save_risk_state(
        args.output_dir, end_date, result, tradable,
        config_hash=config_fingerprint, account_path=args.account,
    )
    print(f"  结果已保存: {output_file}")
    print(f"  风险状态已保存: {Path(args.output_dir) / 'risk_state.json'}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
