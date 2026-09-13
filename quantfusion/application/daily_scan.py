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
from quantfusion.application.daily_report import publish_daily_report
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
from quantfusion.config.regime import MAX_EVIDENCE_STALENESS_DAYS
from quantfusion.data import contracts as market_data_contracts
from quantfusion.data.sessions import (
    DEFAULT_CALENDAR_FILE,
    index_coverage,
    require_frame_coverage,
    resolve_scan_dates,
)
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


def _scan_title() -> str:
    return f"AI 板块 {len(SYMBOLS)} 标的每日信号扫描"


def _run_main() -> int:
    parser = argparse.ArgumentParser(description="Daily AI-sector signal scan")
    parser.add_argument(
        "--end-date",
        default="",
        help="Backtest end date YYYY-MM-DD (default: today in Asia/Shanghai)",
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
        help="Does not override mandatory trading-session coverage or provider-stale rejection.",
    )
    parser.add_argument(
        "--calendar-file",
        default=str(DEFAULT_CALENDAR_FILE),
        help="Reviewed, finite SSE/SZSE session-calendar JSON input.",
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
        help="Establish a new simulation risk-state identity after a successful "
        "validated run. The previous file is retained on failure; this is not "
        "a release of an existing real-account risk lock.",
    )
    args = parser.parse_args()

    end_date = args.end_date or _today_str()
    start_date = args.start_date or START_DATE
    try:
        scan_dates = resolve_scan_dates(end_date, calendar_file=args.calendar_file)
    except (OSError, ValueError, TypeError) as exc:
        print(f"  ✗ 交易日证据未就绪: {exc}")
        return 1

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
            calendar_file=args.calendar_file,
        )

    # A requested new simulation identity is only an in-memory choice here.
    # Never delete the last risk state before data, replay and publication
    # succeed. The existing atomic state writer replaces it after the artifact.
    if args.reset_risk_state:
        prev_risk, risk_error = None, None
        print("  ℹ 新研究身份的风险状态仅在验证和结果写入成功后原子替换。")
    else:
        prev_risk, risk_error = _load_prev_risk_state(args.output_dir, end_date)
    if risk_error:
        print(f"  ✗ {risk_error}")
        print("  风险状态文件损坏 — 拒绝继续运行以防止丢失终态锁定状态。")
        print("  请先保留原文件，核对内容、权限和账户身份；不要删除风险状态来绕过锁定。")
        return 1

    capital = args.capital if args.capital > 0 else INITIAL_CAPITAL
    mode_label = "模拟模式"
    print("=" * 72)
    print(f"  {_scan_title()}")
    print("=" * 72)
    print(f"  运行模式:   {mode_label}")
    print(f"  标的数量:   {len(SYMBOLS)}")
    print(f"  回测区间:   {start_date} → {end_date}")
    print(f"  应覆盖交易日: {scan_dates['required_evidence_date']}")
    print(f"  下一可交易日: {scan_dates['next_trading_date']}")
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
    try:
        index_coverage(args.regime_data_dir, scan_dates)
    except (OSError, ValueError, TypeError) as exc:
        print(f"  ✗ 指数证据不完整，拒绝生成新信号: {exc}")
        return 1

    # Preserve the explicit pre-listing exception, not silent universe shrinkage.
    tradable: dict[str, str] = {}
    snapshot_frames: dict[str, pd.DataFrame] = {}
    actual_evidence_dates: dict[str, str] = {}
    skipped: list[tuple[str, str, str]] = []
    probe_start_ts = cast(
        pd.Timestamp, pd.Timestamp(start_date) - pd.Timedelta(days=400)
    )
    probe_start = probe_start_ts.strftime("%Y-%m-%d")

    print("  正在检查标的可交易性...")
    print("-" * 72)
    stale_symbols: list[tuple[str, str, str]] = []
    fatal_data_errors: list[tuple[str, str, str]] = []
    known_listing_dates = {"688825": "2026-07-27"}
    # Signal-only regime references share the frozen market-data directory,
    # but must never enter the trade pool, report rows or risk-state identity.
    regime_symbols = qf.PortfolioPolicy().regime_symbols
    data_symbols = dict(SYMBOLS)
    for code in regime_symbols:
        data_symbols.setdefault(code, "市场状态参考")
    for code, name in data_symbols.items():
        try:
            df = qf.DataFetcher.load_stock_data(
                code, probe_start, end_date, data_dir=None, cache_dir=args.cache_dir
            )
            if df is not None and not df.empty:
                observed = pd.Timestamp(cast(Any, df.index[-1])).normalize()
                data_age = (pd.Timestamp(end_date).normalize() - observed).days
                if pd.isna(observed) or data_age < 0:
                    raise ValueError("market data has an invalid or future observation")
                actual_evidence_dates[code] = require_frame_coverage(df, scan_dates, code)
                if code in SYMBOLS:
                    tradable[code] = name
                snapshot_frames[code] = df.copy()
                stale = df.attrs.get("_stale", False) or data_age > MAX_EVIDENCE_STALENESS_DAYS
                if stale:
                    last_date = df.attrs.get("_cache_last_date", str(observed.date()))
                    stale_symbols.append((code, name, str(last_date)))
                    print(f"  ⚠ {code} {name}: {len(df)} 条数据 (缓存过期，截止 {last_date})")
                else:
                    print(f"  ✓ {code} {name}: {len(df)} 条数据")
            else:
                listing = known_listing_dates.get(code)
                if code not in regime_symbols and listing and cast(pd.Timestamp, pd.Timestamp(listing)) > cast(
                    pd.Timestamp, pd.Timestamp(end_date)
                ):
                    skipped.append((code, name, f"尚未上市 ({listing})"))
                    print(f"  ✗ {code} {name}: 尚未上市 ({listing})")
                else:
                    fatal_data_errors.append((code, name, "返回空数据"))
                    print(f"  ✗ {code} {name}: 必需行情返回空数据")
        except Exception as exc:
            listing = known_listing_dates.get(code)
            if code not in regime_symbols and listing and cast(pd.Timestamp, pd.Timestamp(listing)) > cast(
                pd.Timestamp, pd.Timestamp(end_date)
            ):
                skipped.append((code, name, f"尚未上市 ({listing})"))
                print(f"  ✗ {code} {name}: 尚未上市 ({listing})")
            else:
                fatal_data_errors.append((code, name, str(exc)[:160]))
                print(f"  ✗ {code} {name}: 数据获取失败 — {str(exc)[:60]}")

    missing_references = set(regime_symbols) - snapshot_frames.keys()
    if missing_references or fatal_data_errors:
        print("  ✗ 必需交易或参考标的数据失败，拒绝缺失参考篮子或缩小股票池后继续运行。")
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

    # Session coverage and the original natural-day/provider checks are
    # independent. An override cannot turn stale inputs into trade advice.
    if stale_symbols:
        print("=" * 72)
        print("  ✗ PROVIDER_STALE_OR_AGE: 数据过期 — 拒绝生成信号 (fail-closed)")
        print("=" * 72)
        for code, name, last_date in stale_symbols:
            print(f"    {code} {name}: 缓存截止 {last_date}（网络获取失败或超过日期容忍）")
        if args.allow_stale:
            print("  --allow-stale 不覆盖提供方 stale 标记或交易日完整性要求。")
        return 1

    if not tradable:
        print("  错误: 没有可交易的标的，退出。")
        return 1

    # Freeze the exact stock and index bytes before either the current-route
    # decision or replay. The calendar identity is part of this new input;
    # an old manifest is never retroactively certified.
    run_id = _generate_run_id(end_date)
    snapshot_dir = Path(args.output_dir) / "snapshots" / end_date
    try:
        snapshot_manifest = _materialize_frozen_snapshot(
            snapshot_dir=snapshot_dir,
            cache_dir=args.cache_dir,
            regime_data_dir=args.regime_data_dir,
            frames=snapshot_frames,
            end_date=end_date,
            scan_dates=scan_dates,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"  ✗ 冻结数据快照失败: {exc}")
        print("  数据证据不可追溯 — 拒绝生成信号 (fail-closed)")
        if snapshot_dir.exists():
            print("  请保留原快照和风险状态；独立复现可指定新的 --output-dir，勿改写冻结证据。")
        return 1
    snapshot_market_dir = snapshot_dir / "market_data"
    snapshot_regime_dir = snapshot_dir / "regime_data"
    print(f"  冻结数据快照: {snapshot_dir}")

    # Risk-state continuity identifies the account/replay, independently of
    # the dated immutable market-evidence identity in the snapshot manifest.
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
            print("    请保留原文件并核对来源，不要通过删除风险状态消除警告。")
            prev_risk = None
            suppress_buys = True
        elif prev_hash != current_hash:
            print("  ⚠ 前次风险状态的标的池或配置不匹配，跳过加载以防止交叉污染。")
            if prev_total != len(tradable):
                print(f"    标的数: 上次={prev_total} 本次={len(tradable)} (不一致)")
            prev_risk = None
            suppress_buys = True
            print("  ⚠ 买入信号将被抑制 (风险状态不匹配 — fail-closed)。")
            print("    请核对股票池、配置和原风险状态；删除状态不代表原账户风险已解除。")

    # Risk-state identity and current-route safety are independent. A route
    # change blocks new buys but must not disable the state/artifact transaction.
    risk_identity_mismatch = suppress_buys

    current_decision = ra.RegimeAdaptiveBacktestEngine(capital).decide_current(
        tradable,
        as_of=end_date,
        data_dir=snapshot_regime_dir,
        leader_data_dir=snapshot_market_dir,
    )
    print(f"  当前点位路由: {current_decision.name} (边界 {current_decision.boundary})")

    print("  正在运行回测...")
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

    # Validate before formatting, publishing, or replacing the old risk state.
    result_invalid_fields = _validate_result_fields(result)
    budget_status = result.get("account_risk_budget") if isinstance(result, dict) else None
    if (not isinstance(budget_status, dict) or budget_status.get("enabled") is not True
            or budget_status.get("mechanism") != "AB5" or budget_status.get("status") != "APPLIED"):
        result_invalid_fields.append("account_risk_budget: default AB5 was not evaluated")
    result_is_valid = len(result_invalid_fields) == 0

    if not result_is_valid:
        # Keep the last successful artifact and risk state intact.
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        error_file = output_dir / f"signals_{end_date}.error.json"
        error_artifact: dict[str, Any] = {
            "scan_date": end_date,
            "mode": "simulation",
            "status": "error",
            "error": "回测结果或默认账户风险预算未通过校验 — 信号不可用",
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
            pass
        print(f"  ✗ 回测结果无效: {', '.join(result_invalid_fields)}")
        print(f"  ✗ 错误信号文件已保存: {error_file}")
        print("  信号不可用 — 请检查数据完整性后重试。")
        print("  (上次成功的信号文件未被覆盖)")
        return 1

    # NOT_READY suppresses buys. DEGRADED preserves valid risk opinions.
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
    symbol_signals: dict[str, list[Any]] = defaultdict(list)
    for sig in pending:
        symbol_signals[sig.symbol].append(sig)

    sim_positions = _extract_positions(result.get("trades", []))
    symbol_names = {code: name for code, name in SYMBOLS.items()}
    rows: list[dict[str, Any]] = []
    skipped_codes = {code for code, _, _ in skipped}

    for code in sorted(SYMBOLS.keys()):
        name = symbol_names[code]
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
            signal_label, strategies, _ = _apply_buy_suppression(sigs, suppress_buys)
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

    buy_count = sell_count = hold_count = wait_count = untradeable_count = 0
    suppressed_buy_count = 0
    for row in rows:
        if "买入已抑制" in row["signal"]:
            suppressed_buy_count += 1
            if "卖出" in row["signal"]:
                sell_count += 1
            else:
                wait_count += 1
        elif "风险状态不匹配" in row["signal"]:
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

    guard = result.get("sector_guard_active", False)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"signals_{end_date}.json"
    pending_serializable, blocked_serializable = _serialize_pending_signals(
        pending, suppress_buys
    )

    artifact: dict[str, Any] = {
        "scan_date": end_date,
        "scan_dates": scan_dates,
        "actual_evidence_dates": actual_evidence_dates,
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
            "date_symbol_side_count": int(result.get("date_symbol_side_count", 0)),
            "date_symbol_sell_side_count": int(result.get("date_symbol_sell_side_count", 0)),
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
            "snapshot_manifest_sha256": _sha256_file(snapshot_dir / "manifest.json"),
            "snapshot_schema_version": snapshot_manifest["schema_version"],
            "index_evidence": snapshot_manifest.get("index_evidence", {}),
        },
        "account_risk_budget": budget_status,
        "pending_signals": pending_serializable,
        "blocked_signals": blocked_serializable,
        "risk_state_saved": False,
    }
    if prev_risk:
        artifact["previous_risk_state"] = prev_risk

    # Strict serialization precedes all successful publication/state writes.
    try:
        artifact_content = json.dumps(
            artifact, ensure_ascii=False, indent=2, default=str,
            allow_nan=False,
        ) + "\n"
    except ValueError as exc:
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

    # Artifact-first transaction: failed artifact writes cannot replace state.
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

    # Best-effort update: false remains truthful if the update itself fails.
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
            pass

    # Publish the success pointer only after the continuity transaction.
    # Identity mismatch deliberately retains the old state and valid sells.
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
            pass

    if risk_state_saved:
        print(f"  结果已保存: {output_file}")
        print(f"  风险状态已保存: {Path(args.output_dir) / 'risk_state.json'}")
    elif risk_identity_mismatch:
        print(f"  结果已保存: {output_file}")
        print("  ⚠ 风险状态未保存 (身份不匹配 — 保留旧状态以维持终态锁连续性)")
        print("  只有明确建立新研究身份时才使用 --reset-risk-state；先保全原状态，这不解除原账户风险锁。")
    else:
        print(f"  结果已保存: {output_file}")
        if risk_state_save_error:
            print(f"  ✗ 风险状态保存失败: {risk_state_save_error}")
            print("  跨日终态锁未保存属于运行失败 — 请检查磁盘空间和权限后重试。")
    print()

    if risk_state_save_error:
        return 1
    publish_daily_report(output_file, replay=result, expected_identity=("run_id", run_id))
    return 0


def main() -> int:
    """Run one scan with request-scoped market-data cache configuration."""
    return _run_main()


if __name__ == "__main__":
    raise SystemExit(main())
