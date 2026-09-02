"""Artifact-first daily signal-scan application service."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pandas as pd

from quantfusion.application import account_scan, engine_api as qf, regime_api as ra
from quantfusion.application.daily_signals import (
    apply_buy_suppression,
    serialize_pending_signals,
)
from quantfusion.application.daily_support import (
    classify_signal,
    extract_positions,
    today_str,
    validate_result_fields,
)
from quantfusion.config.daily import (
    DEFAULT_CACHE_DIR,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_REGIME_DATA_DIR,
    INITIAL_CAPITAL,
    START_DATE,
    SYMBOLS,
)
from quantfusion.data import contracts as market_data_contracts
from quantfusion.data.snapshot import (
    materialize_frozen_snapshot,
    sha256_file,
    verify_frozen_snapshot,
)
from quantfusion.io.state_store import (
    compute_identity_hash,
    generate_run_id,
    load_prev_risk_state,
    save_risk_state,
    validate_risk_state,
)

_apply_buy_suppression = apply_buy_suppression
_classify_signal = classify_signal
_extract_positions = extract_positions
_today_str = today_str
_validate_result_fields = validate_result_fields
_serialize_pending_signals = serialize_pending_signals
_materialize_frozen_snapshot = materialize_frozen_snapshot
_sha256_file = sha256_file
_verify_frozen_snapshot = verify_frozen_snapshot
_compute_identity_hash = compute_identity_hash
_generate_run_id = generate_run_id
_load_prev_risk_state = load_prev_risk_state
_save_risk_state = save_risk_state
_validate_risk_state = validate_risk_state

def _run_main() -> int:
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
        help="Simulation-only override for stale cached data. Account decision "
        "support always rejects provider-marked stale data.",
    )
    parser.add_argument(
        "--account",
        default="",
        help="Real-account JSON snapshot for separate point-in-time decision support.",
    )
    parser.add_argument(
        "--account-id",
        default="main",
        help="Expected account identity for --account snapshots (default: main).",
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
        help=f"Initial capital (default: {INITIAL_CAPITAL:,.0f}).",
    )
    parser.add_argument(
        "--deployment-mode",
        choices=("auto", "trend", "weak"),
        default="auto",
        help="Causal strategy route (default: auto). trend/weak are diagnostic overrides.",
    )
    parser.add_argument(
        "--regime-data-dir",
        default=DEFAULT_REGIME_DATA_DIR,
        help="Local fixed-index evidence directory used by the causal router.",
    )
    parser.add_argument(
        "--reset-risk-state",
        action="store_true",
        help="Delete risk_state.json before running. Use this when you "
        "intentionally change symbol set, start date, or configuration to "
        "establish a new risk-state identity without buy suppression.",
    )
    args = parser.parse_args()

    end_date = args.end_date or _today_str()
    start_date = args.start_date or START_DATE

    # Real holdings use a separate point-in-time engine. They are never
    # passed to the replay engine, so account advice cannot create
    # a hybrid or look-ahead-contaminated equity curve.
    if args.account:
        return account_scan.run_account_scan(
            account_path=args.account,
            symbols=SYMBOLS,
            end_date=end_date,
            cache_dir=args.cache_dir,
            regime_data_dir=args.regime_data_dir,
            output_dir=args.output_dir,
            expected_account_id=args.account_id,
        )

    # --reset-risk-state: delete the old risk state file before running.
    # This runs only after the --account check passes, so the user cannot
    # accidentally lose state while trying to use a disabled mode.
    if args.reset_risk_state:
        state_file = Path(args.output_dir) / "risk_state.json"
        if state_file.exists():
            try:
                state_file.unlink()
                print(f"  ℹ 已删除旧风险状态: {state_file}")
            except OSError as exc:
                print(f"  ✗ 删除风险状态文件失败: {exc}")
                return 1
        else:
            print(f"  ℹ 无旧风险状态可删除: {state_file}")

    # ── Resolve capital ──
    if args.capital > 0:
        capital = args.capital
    else:
        capital = INITIAL_CAPITAL

    # ── Load previous run's risk state for continuity ──
    prev_risk, risk_error = _load_prev_risk_state(args.output_dir, end_date)
    if risk_error:
        print(f"  ✗ {risk_error}")
        print("  风险状态文件损坏 — 拒绝继续运行以防止丢失终态锁定状态。")
        print("  请删除 risk_state.json 后重试，或检查文件权限。")
        return 1

    mode_label = "模拟模式"
    print("=" * 72)
    print("  AI 板块 26 标的每日信号扫描")
    print("=" * 72)
    print(f"  运行模式:   {mode_label}")
    print(f"  标的数量:   {len(SYMBOLS)}")
    print(f"  回测区间:   {start_date} → {end_date}")
    print(f"  初始资金:   ¥{capital:,.0f}")
    if prev_risk:
        print(f"  上次扫描:   {prev_risk.get('scan_date', '?')}")
        if prev_risk.get("terminal_risk_lock"):
            print("  ⚠ 上次终态锁定已激活 — 请检查是否需要人工干预")
        if prev_risk.get("sector_guard_active"):
            print("  ⚠ 上次板块风控已激活")
    print("  指标状态:   warm")
    print("  数据源:     AKShare 在线前复权 (增量缓存)")
    print(f"  缓存目录:   {args.cache_dir}")
    print("-" * 72)
    print("  ℹ 模拟模式: 信号基于从零开始的回测，不代表真实账户持仓。")
    print("-" * 72)

    # Configure incremental cache for efficient daily updates. Caches without
    # an explicit share-volume contract are rebuilt.
    index_refresh = market_data_contracts.refresh_regime_indices(
        args.regime_data_dir, end_date=end_date, strict=False
    )

    # ── Pre-screen: skip symbols with no data (e.g. not yet listed) ──
    # The engine loads all symbols at once and raises on any failure, so we
    # probe each symbol individually and build a tradable universe.
    tradable: dict[str, str] = {}
    snapshot_frames: dict[str, pd.DataFrame] = {}
    skipped: list[tuple[str, str, str]] = []  # (code, name, reason)
    # Use a start date ~400 days before start_date for the probe so the
    # engine has enough warmup history for indicator calculation.
    # (400 calendar days ≈ 13 months, slightly more than the 365-day
    # warmup_calendar_days used by the engine, to ensure coverage.)
    probe_start_ts = cast(
        pd.Timestamp, pd.Timestamp(start_date) - pd.Timedelta(days=400)
    )
    probe_start = probe_start_ts.strftime("%Y-%m-%d")

    print("  正在检查标的可交易性...")
    print("-" * 72)
    stale_symbols: list[tuple[str, str, str]] = []  # (code, name, last_cache_date)
    fatal_data_errors: list[tuple[str, str, str]] = []
    known_listing_dates = {"688825": "2026-07-27"}
    for code, name in SYMBOLS.items():
        try:
            df = qf.DataFetcher.load_stock_data(
                code, probe_start, end_date, data_dir=None, cache_dir=args.cache_dir
            )
            if df is not None and not df.empty:
                tradable[code] = name
                snapshot_frames[code] = df.copy()
                stale = df.attrs.get("_stale", False)
                if stale:
                    last_date = df.attrs.get("_cache_last_date", "?")
                    stale_symbols.append((code, name, str(last_date)))
                    print(f"  ⚠ {code} {name}: {len(df)} 条数据 (缓存过期，截止 {last_date})")
                else:
                    print(f"  ✓ {code} {name}: {len(df)} 条数据")
            else:
                listing = known_listing_dates.get(code)
                if listing and cast(pd.Timestamp, pd.Timestamp(listing)) > cast(
                    pd.Timestamp, pd.Timestamp(end_date)
                ):
                    skipped.append((code, name, f"尚未上市 ({listing})"))
                    print(f"  ✗ {code} {name}: 尚未上市 ({listing})")
                else:
                    fatal_data_errors.append((code, name, "返回空数据"))
                    print(f"  ✗ {code} {name}: 预期可交易但返回空数据")
        except Exception as exc:
            listing = known_listing_dates.get(code)
            if listing and cast(pd.Timestamp, pd.Timestamp(listing)) > cast(
                pd.Timestamp, pd.Timestamp(end_date)
            ):
                skipped.append((code, name, f"尚未上市 ({listing})"))
                print(f"  ✗ {code} {name}: 尚未上市 ({listing})")
            else:
                fatal_data_errors.append((code, name, str(exc)[:160]))
                print(f"  ✗ {code} {name}: 数据获取失败 — {str(exc)[:60]}")

    if fatal_data_errors and not args.allow_stale:
        print("  ✗ 预期可交易标的数据失败，拒绝缩小股票池后继续运行。")
        for code, name, reason in fatal_data_errors:
            print(f"    {code} {name}: {reason}")
        return 1

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
                code, probe_start, end_date, data_dir=None, cache_dir=args.cache_dir
            )
            if df is not None and not df.empty:
                end = str(pd.Timestamp(cast(Any, df.index[-1])).date())
                data_end_dates.setdefault(end, []).append(code)
        except Exception:
            # Best-effort probe: a single symbol failing to load here only
            # omits it from the freshness comparison, never aborts the scan.
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
            print("  ✗ 数据不一致 — 拒绝生成信号 (fail-closed)")
            print("  如需强制运行，请添加 --allow-stale 参数。")
            return 1

    # Freeze the exact stock and index bytes before either the current-route
    # decision or the requested replay. Same-day reruns reuse this directory
    # only after checking the hashed manifest, every CSV hash, and the absence
    # of extra evidence files.
    run_id = _generate_run_id(end_date)
    snapshot_dir = Path(args.output_dir) / "snapshots" / end_date
    try:
        snapshot_manifest = _materialize_frozen_snapshot(
            snapshot_dir=snapshot_dir,
            cache_dir=args.cache_dir,
            regime_data_dir=args.regime_data_dir,
            frames=snapshot_frames,
            end_date=end_date,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"  ✗ 冻结数据快照失败: {exc}")
        print("  数据证据不可追溯 — 拒绝生成信号 (fail-closed)")
        return 1
    snapshot_market_dir = snapshot_dir / "market_data"
    snapshot_regime_dir = snapshot_dir / "regime_data"
    print(f"  冻结数据快照: {snapshot_dir}")

    # ── Validate FILE risk state identity to prevent cross-contamination ──
    # Identity uses stable fields only (symbol set + count + config
    # fingerprint including start date, indicator state, capital, and
    # warmup days). Cash/capital is included because different capital
    # means different position sizing and risk exposure.
    # When the identity does not match, buy signals are suppressed (fail-closed)
    # to prevent entering new positions without verified risk-state continuity.
    config_fingerprint = (
        f"start={start_date}|indicator=warm"
        f"|capital={capital}|warmup=365|deployment={args.deployment_mode}"
    )
    suppress_buys = False
    if prev_risk:
        current_hash = _compute_identity_hash(tradable, config_fingerprint)
        prev_hash = prev_risk.get("symbols_hash", "")
        prev_total = prev_risk.get("total_symbols", 0)
        if not prev_hash:
            print("  ⚠ 前次风险状态缺少 symbols_hash (旧格式)，拒绝加载以防止交叉污染。")
            print("    删除 risk_state.json 可清除此警告。")
            prev_risk = None
            suppress_buys = True
        elif prev_hash != current_hash:
            print("  ⚠ 前次风险状态的标的池或配置不匹配，跳过加载以防止交叉污染。")
            if prev_total != len(tradable):
                print(f"    标的数: 上次={prev_total} 本次={len(tradable)} (不一致)")
            prev_risk = None
            suppress_buys = True
            print("  ⚠ 买入信号将被抑制 (风险状态不匹配 — fail-closed)。")
            print("    删除 risk_state.json 并重新运行可恢复完整信号。")

    # Risk-state identity and current-route safety are independent. A route
    # change blocks new buys but must not disable the state/artifact transaction.
    risk_identity_mismatch = suppress_buys

    current_decision = ra.RegimeAdaptiveBacktestEngine(capital).decide_current(
        tradable,
        as_of=max(data_end_dates) if data_end_dates else end_date,
        data_dir=snapshot_regime_dir,
        leader_data_dir=snapshot_market_dir,
    )
    print(f"  当前点位路由: {current_decision.name} (边界 {current_decision.boundary})")

    print("  正在运行回测，请稍候...")
    print("-" * 72)

    # risk_state is not passed to the engine. Each requested replay rebuilds
    # risk state from its own dated inputs. The saved risk_state.json is used
    # only for display and continuity checks, never as replay input.
    engine = ra.RegimeAdaptiveBacktestEngine(capital)
    result = engine.run(
        tradable,
        start_date,
        end_date,
        data_dir=str(snapshot_market_dir),
        indicator_state="warm",
        warmup_calendar_days=365,
        deployment_mode=args.deployment_mode,
        regime_data_dir=str(snapshot_regime_dir),
        leader_data_dir=str(snapshot_market_dir),
    )

    # ── Validate result IMMEDIATELY after engine.run() ──────────────
    # This must happen BEFORE any printing or formatting, because None,
    # string, or missing fields would cause TypeError/KeyError in f-string
    # format specifiers (e.g. {:,.0f}). If invalid, we write an error
    # artifact to a SEPARATE file (signals_<date>.error.json) so the last
    # successful artifact (signals_<date>.json) is never overwritten.
    result_invalid_fields = _validate_result_fields(result)
    result_is_valid = len(result_invalid_fields) == 0

    if not result_is_valid:
        # Result is invalid — do NOT save risk state, do NOT overwrite
        # the last successful artifact. Write an error artifact to a
        # separate .error.json file so downstream consumers can detect
        # the failure without losing the last good signals.
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        error_file = output_dir / f"signals_{end_date}.error.json"
        error_artifact: dict[str, Any] = {
            "scan_date": end_date,
            "mode": "simulation",
            "status": "error",
            "error": "回测结果包含非有限值或类型错误 — 信号不可用",
            "invalid_fields": result_invalid_fields,
            "risk_state_saved": False,
            "run_id": run_id,
            "created_at": datetime.now().isoformat(),
        }
        try:
            error_content = json.dumps(
                error_artifact, ensure_ascii=False, indent=2,
                allow_nan=False,
            ) + "\n"
            efd, etmp = tempfile.mkstemp(
                dir=str(output_dir), prefix=".error_", suffix=".tmp"
            )
            try:
                with os.fdopen(efd, "w", encoding="utf-8") as f:
                    f.write(error_content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(etmp, str(error_file))
            except OSError:
                try:
                    os.unlink(etmp)
                except OSError:
                    pass
        except (OSError, ValueError):
            pass  # Best-effort error artifact write
        print(f"  ✗ 回测结果无效: {', '.join(result_invalid_fields)}")
        print(f"  ✗ 错误信号文件已保存: {error_file}")
        print("  信号不可用 — 请检查数据完整性后重试。")
        print("  (上次成功的信号文件未被覆盖)")
        return 1

    # ── Warmup health contract (2026-08-16 报告 P0-1) ─────────────────
    # NOT_READY: 输出不得作为正式交易信号 → 抑制买入（fail-closed）。
    # DEGRADED: 风险判断保留，仅显著提示（不抑制信号）。
    warmup_health = result.get("warmup_health") or {}
    warmup_status = str(warmup_health.get("warmup_status", "UNKNOWN"))
    risk_opinion = result.get("risk_opinion")
    warmup_not_ready = warmup_status == "NOT_READY"
    if warmup_not_ready:
        suppress_buys = True
        print("  ✗ 预热健康契约: NOT_READY — 输出不可作为正式交易信号。")
        print(
            "    原因: "
            + "; ".join(warmup_health.get("reasons", []) or ["unknown"])
        )
        print("    所有新增买入已失败关闭；请补齐预热数据后重跑。")
    elif warmup_status == "DEGRADED":
        print(
            "  ⚠ 预热健康契约: DEGRADED ("
            + "; ".join(warmup_health.get("reasons", []) or ["unknown"])
            + ") — 风险判断保留，新增风险动作建议人工确认。")

    # ── Extract latest pending signals ───────────────────────────────
    pending = result.get("pending_signals", [])
    replay_decision = result.get("deployment_decision", {})
    current_selected = (
        set(current_decision.leaders.selected_symbols)
        if current_decision.leaders is not None
        else set()
    )
    replay_selected = set(result.get("selected_symbols", []))
    live_route_mismatch = (
        replay_decision.get("name") != current_decision.name
        or (
            current_decision.name == "positive_momentum_hold"
            and replay_selected != current_selected
        )
    )
    if live_route_mismatch:
        suppress_buys = True
        print("  ⚠ 当前路由与历史回放起点路由不同，所有新增买入已失败关闭。")
        print("    卖出信号仍保留；真实持仓请使用 --account 点位引擎。")
    # Group by symbol: collect all pending signals per symbol
    symbol_signals: dict[str, list[Any]] = defaultdict(list)
    for sig in pending:
        symbol_signals[sig.symbol].append(sig)

    # Reconstruct current positions from trade ledger
    sim_positions = _extract_positions(result.get("trades", []))

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
                "industry": qf.get_symbol_group(code, "N/A"),
                "profile": qf.get_symbol_profile(code, "N/A"),
            })
            continue

        sigs = symbol_signals.get(code, [])
        held_shares = sim_positions.get(code, 0)

        if sigs:
            # Use the extracted suppression function for testability and
            # consistent behavior between display and artifact.
            signal_label, strategies, _ = _apply_buy_suppression(
                sigs, suppress_buys
            )
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
            "industry": qf.get_symbol_group(code, "default"),
            "profile": qf.get_symbol_profile(code, "default"),
        })

    # ── Print summary table ──────────────────────────────────────────
    print(f"{'代码':<10} {'名称':<10} {'信号':<12} {'行业':<20} {'Profile':<12} {'持仓股数':>12}  {'策略'}")
    print("─" * 100)

    buy_count = sell_count = hold_count = wait_count = untradeable_count = 0
    suppressed_buy_count = 0
    for row in rows:
        print(
            f"{row['code']:<10} {row['name']:<10} {row['signal']:<12} "
            f"{row['industry']:<20} {row['profile']:<12} "
            f"{row['held_shares']:>12,}  {row['strategies']}"
        )
        if "买入已抑制" in row["signal"]:
            # Mixed signal with buys suppressed — only the sell part
            # remains visible (buy suppression removes buy labels and
            # appends "[买入已抑制]" to the remaining sell signals).
            suppressed_buy_count += 1
            if "卖出" in row["signal"]:
                sell_count += 1
            else:
                wait_count += 1
        elif "风险状态不匹配" in row["signal"]:
            # Pure buy suppressed — count as wait
            suppressed_buy_count += 1
            wait_count += 1
        elif "买入" in row["signal"]:
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
    if suppressed_buy_count:
        print(f"  ⚠ {suppressed_buy_count} 个买入信号已被抑制 (风险状态不匹配)")
    print()

    # ── Portfolio metrics ────────────────────────────────────────────
    print("─" * 72)
    print("  组合绩效指标")
    print("─" * 72)
    print(f"  最终资产:     ¥{result['final_assets']:>15,.0f}")
    print(f"  总收益率:       {result['total_return']:>14.2%}")
    print(f"  最大回撤:       {result['max_drawdown']:>14.2%}")
    print(f"  Sharpe:         {result['sharpe']:>14.2f}")
    print(f"  成交记录数:     {result['total_trades']:>14}")
    print(
        "  日期/股票/方向桶:"
        f"{result.get('date_symbol_side_count', 0):>12}"
    )
    print(f"  自动策略路由:   {result.get('deployment_policy', 'unknown'):>14}")
    allocation_mode = result.get("allocation_mode", "ensemble")
    print(f"  分配模式:       {allocation_mode:>14}")
    print(
        f"  风险锁定:       "
        f"{'是' if result.get('terminal_risk_lock') else '否'}"
    )
    guard = result.get("sector_guard_active", False)
    print(f"  板块风控激活:   {'是' if guard else '否'}")
    safe_mode = result.get("safe_mode_active", False)
    if safe_mode:
        print("  ⚠ 市场状态: CHOPPY (震荡市)")
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
    # Check current backtest drawdown to advise on position sizing
    if result.get("max_drawdown", 0) and abs(result["max_drawdown"]) > 0.15:
        dd = abs(result["max_drawdown"])
        advisory = ""
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

    # ── Independent risk opinion (2026-08-16 报告 P0-3) ──────────────
    if isinstance(risk_opinion, dict):
        print("─" * 72)
        print("  独立风险意见 (与交易动作分离)")
        print("─" * 72)
        level = int(risk_opinion.get("risk_level", 0))
        print(f"  日期:           {risk_opinion.get('date', '?')}")
        print(f"  风险等级:       L{level}")
        print(
            f"  风险置信度:     {float(risk_opinion.get('risk_confidence', 0.0)):.2f}"
            "  (风险篮覆盖度加权)"
        )
        print(f"  市场状态:       {risk_opinion.get('regime', '?')}")
        print(
            f"  健康牛市沉默:   {'是' if risk_opinion.get('bull_silent') else '否'}"
        )
        print(
            f"  禁止新开仓:     {'是' if risk_opinion.get('block_new_entries') else '否'}"
            f"   冻结加仓: {'是' if risk_opinion.get('block_pyramids') else '否'}"
        )
        print(
            f"  建议总敞口上限: {float(risk_opinion.get('recommended_gross_cap', 1.0)):.0%}"
        )
        clusters = risk_opinion.get("weakest_clusters") or []
        if clusters:
            print(f"  最弱集群:       {', '.join(map(str, clusters))}")
        consensus = risk_opinion.get("sleeve_consensus")
        if consensus is not None:
            print(
                f"  袖套共识:       {float(consensus):.2f}"
                f" (连续退化 {int(risk_opinion.get('sleeve_consensus_decline_streak', 0))} 日)"
            )
        reasons = risk_opinion.get("reason_codes") or []
        if reasons:
            print(f"  原因代码:       {', '.join(map(str, reasons))}")
        print()

    # ── Build artifact and pre-serialize to detect nested NaN ───────
    # The artifact is pre-serialized (allow_nan=False) to detect NaN/Inf
    # in nested structures (pending_signals, risk_events) BEFORE writing
    # to disk. If nested NaN is found, an error artifact is written to
    # .error.json and no risk state is saved.
    # The actual artifact file is written to disk FIRST, then risk state
    # is saved — this is the artifact-first transaction ordering.
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"signals_{end_date}.json"

    # Serialize pending signals — blocked buys are separated into a
    # dedicated ``blocked_signals`` list so ``pending_signals`` only
    # contains executable signals.
    pending_serializable, blocked_serializable = _serialize_pending_signals(
        pending, suppress_buys
    )

    # Build artifact with risk_state_saved placeholder (updated after save)
    artifact: dict[str, Any] = {
        "scan_date": end_date,
        "mode": "simulation",
        "status": "ok",
        "run_id": run_id,
        "created_at": datetime.now().isoformat(),
        "symbols": SYMBOLS,
        "start_date": start_date,
        "initial_capital": capital,
        "signals": rows,
        "summary": {
            "buy": buy_count,
            "sell": sell_count,
            "hold": hold_count,
            "wait": wait_count,
            "untradeable": untradeable_count,
            "suppressed_buys": suppressed_buy_count,
            "buys_suppressed": suppress_buys,
            "risk_state_identity_mismatch": risk_identity_mismatch,
            "current_route_mismatch": live_route_mismatch,
            "warmup_not_ready": warmup_not_ready,
        },
        "warmup_health": warmup_health,
        "risk_opinion": risk_opinion,
        "portfolio": {
            "final_assets": float(result["final_assets"]),
            "total_return": float(result["total_return"]),
            "max_drawdown": float(result["max_drawdown"]),
            "sharpe": float(result["sharpe"]),
            "total_trades": int(result["total_trades"]),
            "sell_trades": int(result.get("sell_trades", 0)),
            "date_symbol_side_count": int(
                result.get("date_symbol_side_count", 0)
            ),
            "date_symbol_sell_side_count": int(
                result.get("date_symbol_sell_side_count", 0)
            ),
            "sector_guard_active": bool(guard),
            "safe_mode_active": bool(result.get("safe_mode_active", False)),
            "terminal_risk_lock": bool(result.get("terminal_risk_lock", False)),
        },
        "deployment": {
            "mode": args.deployment_mode,
            "policy": result.get("deployment_policy", "unknown"),
            "decision": result.get("deployment_decision", {}),
            "current_decision": asdict(current_decision),
            "current_route_buy_suppression": live_route_mismatch,
            "index_refresh": index_refresh,
            "requested_symbols": result.get("requested_symbols", sorted(tradable)),
            "selected_symbols": result.get("selected_symbols", sorted(tradable)),
            "unavailable_symbols": result.get("unavailable_symbols", []),
            "snapshot_directory": str(snapshot_dir),
            "snapshot_manifest_sha256": _sha256_file(
                snapshot_dir / "manifest.json"
            ),
            "snapshot_schema_version": snapshot_manifest["schema_version"],
        },
        "pending_signals": pending_serializable,
        "blocked_signals": blocked_serializable,
        "risk_state_saved": False,  # updated after state save
    }
    if prev_risk:
        artifact["previous_risk_state"] = prev_risk

    # Pre-serialize artifact to detect nested NaN BEFORE writing to disk.
    # allow_nan=False ensures strict JSON (ECMA-404) — NaN/Infinity
    # tokens are rejected at serialization time. If this succeeds, the
    # artifact is safe to write to disk.
    try:
        artifact_content = json.dumps(
            artifact, ensure_ascii=False, indent=2, default=str,
            allow_nan=False,
        ) + "\n"
    except ValueError as exc:
        # Nested NaN detected — do NOT save risk state. Write error
        # artifact to .error.json so the last success file is preserved.
        error_file = output_dir / f"signals_{end_date}.error.json"
        error_artifact = {
            "scan_date": end_date,
            "mode": "simulation",
            "status": "error",
            "error": f"信号文件序列化失败 (嵌套非有限值): {exc}",
            "invalid_fields": result_invalid_fields,
            "risk_state_saved": False,
            "run_id": run_id,
            "created_at": datetime.now().isoformat(),
        }
        try:
            ec = json.dumps(error_artifact, ensure_ascii=False, indent=2,
                            allow_nan=False) + "\n"
            efd, etmp = tempfile.mkstemp(
                dir=str(output_dir), prefix=".error_", suffix=".tmp")
            try:
                with os.fdopen(efd, "w", encoding="utf-8") as f:
                    f.write(ec)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(etmp, str(error_file))
            except OSError:
                try:
                    os.unlink(etmp)
                except OSError:
                    pass
        except (OSError, ValueError):
            pass
        print(f"  ✗ 信号文件序列化失败 (嵌套非有限值): {exc}")
        print(f"  ✗ 错误信号文件已保存: {error_file}")
        print("  风险状态未保存 — 上次成功的信号文件未被覆盖。")
        return 1

    # ── Write artifact to disk FIRST (artifact-first transaction) ──
    # The artifact is written BEFORE risk state is saved. This ensures
    # that if the artifact write fails, no risk state has been committed
    # — preventing state/artifact inconsistency. If the risk state save
    # subsequently fails, the artifact already exists on disk with
    # ``risk_state_saved: false``, which correctly reflects the state.
    # The artifact is then updated (best-effort) with the actual
    # ``risk_state_saved`` status.
    artifact["risk_state_saved"] = False
    artifact_content = json.dumps(
        artifact, ensure_ascii=False, indent=2, default=str,
        allow_nan=False,
    ) + "\n"

    # ── Write artifact file (atomic) ─────────────────────────────────
    artifact_fd, artifact_tmp = tempfile.mkstemp(
        dir=str(output_dir), prefix=".signals_", suffix=".tmp"
    )
    try:
        with os.fdopen(artifact_fd, "w", encoding="utf-8") as f:
            f.write(artifact_content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(artifact_tmp, str(output_file))
    except OSError as exc:
        try:
            os.unlink(artifact_tmp)
        except OSError:
            pass
        print(f"  ✗ 信号文件保存失败: {exc}")
        print("  信号文件未保存属于运行失败 — 请检查磁盘空间和权限后重试。")
        print("  风险状态未保存 — 无状态/产物不一致。")
        return 1

    # ── Save risk state AFTER successful artifact write ─────────────
    # Now that the artifact is safely on disk, save the risk state.
    # Both artifact and risk state share the same run_id for traceability.
    risk_state_saved = False
    risk_state_save_error = ""
    if not risk_identity_mismatch:
        try:
            _save_risk_state(
                args.output_dir,
                end_date,
                result,
                run_id=run_id,
                tradable=tradable,
                config_hash=config_fingerprint,
            )
            risk_state_saved = True
        except (OSError, ValueError, TypeError) as exc:
            risk_state_save_error = str(exc)

    # ── Update artifact with actual risk_state_saved status ─────────
    # Best-effort re-write of the artifact with the final
    # risk_state_saved status. This re-serialization is guaranteed not
    # to fail because we only changed a bool and optionally added a
    # string error message — no new NaN sources.
    if risk_state_saved or risk_state_save_error:
        artifact["risk_state_saved"] = risk_state_saved
        if risk_state_save_error:
            artifact["risk_state_save_error"] = risk_state_save_error
        try:
            updated_content = json.dumps(
                artifact, ensure_ascii=False, indent=2, default=str,
                allow_nan=False,
            ) + "\n"
            ufd, utmp = tempfile.mkstemp(
                dir=str(output_dir), prefix=".signals_", suffix=".tmp"
            )
            try:
                with os.fdopen(ufd, "w", encoding="utf-8") as f:
                    f.write(updated_content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(utmp, str(output_file))
            except OSError:
                try:
                    os.unlink(utmp)
                except OSError:
                    pass
        except (OSError, ValueError):
            pass  # Best-effort update — artifact already has the signals

    # ── Update latest_success.json pointer ───────────────────────────
    # Publish only after the artifact and continuity-state transaction has
    # succeeded. Identity mismatch intentionally preserves the prior state
    # and remains a successful, sell-capable scan, so it may publish.
    if not risk_state_save_error:
        try:
            pointer = {"file": output_file.name, "run_id": run_id,
                       "scan_date": end_date}
            pfd, ptmp = tempfile.mkstemp(
                dir=str(output_dir), prefix=".latest_", suffix=".tmp")
            try:
                with os.fdopen(pfd, "w", encoding="utf-8") as f:
                    json.dump(pointer, f, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(ptmp, str(output_dir / "latest_success.json"))
            except OSError:
                try:
                    os.unlink(ptmp)
                except OSError:
                    pass
        except OSError:
            pass  # Best-effort pointer file

    # Print final save status
    if risk_state_saved:
        print(f"  结果已保存: {output_file}")
        print(f"  风险状态已保存: {Path(args.output_dir) / 'risk_state.json'}")
    elif risk_identity_mismatch:
        print(f"  结果已保存: {output_file}")
        print("  ⚠ 风险状态未保存 (身份不匹配 — 保留旧状态以维持终态锁连续性)")
        print("  使用 --reset-risk-state 可建立新身份并清除买入抑制。")
    else:
        print(f"  结果已保存: {output_file}")
        if risk_state_save_error:
            print(f"  ✗ 风险状态保存失败: {risk_state_save_error}")
            print("  跨日终态锁未保存属于运行失败 — 请检查磁盘空间和权限后重试。")
    print()

    # Risk state save failure is a runtime error.
    if risk_state_save_error:
        return 1
    return 0


def main() -> int:
    """Run one scan with request-scoped market-data cache configuration."""
    return _run_main()


if __name__ == "__main__":
    raise SystemExit(main())
