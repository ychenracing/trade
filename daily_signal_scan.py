#!/usr/bin/env python3
"""Daily signal scan for the 26 AI-sector universe.

Fetches the latest forward-adjusted closing data via AKShare (with incremental
cache), runs the Quant Fusion backtest, and extracts the latest pending
signal (buy / sell / hold) for each symbol.

Simulation mode (default): runs a fresh backtest from --start-date.
Signals reflect what the strategy *would have* done, not your real portfolio.

Account mode (--account) is currently DISABLED due to multiple architecture
defects. It will be re-enabled as a separate account signal engine.

Risk state (terminal_risk_lock, sector_guard_active, drawdown) is persisted to
risk_state.json with enhanced identity fields (symbols_hash, run_id) after each
run. Identity uses stable fields (symbol set + count + config fingerprint
including start date, indicator state, capital, and warmup days). Capital is
included because different capital means different position sizing and risk
exposure. Old risk state files without symbols_hash are rejected (fail-closed)
to prevent cross-contamination.

**Risk state is NOT injected into the engine.** The daily scan replays the
full history from --start-date to --end-date each time. Injecting the
previous run's end-state (e.g. terminal_risk_lock=True from July 30) into a
fresh replay starting from July 1 would create a time-direction error:
future末端状态改变了过去的历史路径. Instead, the engine independently
rebuilds all risk states (terminal locks, sector guards, cycle locks) from
the actual historical data. The saved risk_state.json is loaded for
**display and continuity checking only** — it shows the user what the
previous run detected, but does NOT influence the current backtest.

Risk state date validation: ``scan_date`` must be <= the requested
``end_date``. This prevents forward contamination — e.g. loading an
August 1 risk state into a July 20 replay. Violations are rejected
(fail-closed, exit code 1).

Risk state writes are atomic (temp file + os.replace) to prevent corruption
from disk full, process kill, or power loss. Corrupted risk state files cause
the scan to exit with code 1 rather than silently discarding terminal lock
state. Same-day reruns preserve the previous state so terminal lock and sector
guard continuity is maintained.

When the risk state identity hash does not match (different symbol set, count,
or configuration), buy signals are suppressed (fail-closed) to prevent entering
new positions without verified risk-state continuity. Sell and hold signals
are still shown, including in mixed buy/sell signals (only the buy part is
suppressed). In the JSON artifact, blocked buy signals are placed in a
separate ``blocked_signals`` list — ``pending_signals`` only contains
executable signals, so downstream consumers that check ``direction`` on
``pending_signals`` will never see blocked buys.

When the identity does not match, the old risk state is NOT overwritten —
the previous terminal lock and sector guard are preserved. The user must use
``--reset-risk-state`` to intentionally establish a new identity. This ensures
that a mismatch continues to suppress buys until the user explicitly resolves
the configuration change.

Risk state includes ``schema_version`` and type-validated fields
(``schema_version``, ``scan_date``, ``terminal_risk_lock``,
``sector_guard_active``, ``cycle_lock_count``, ``max_drawdown``,
``total_return``, ``final_assets``). Unknown schema versions are rejected
(fail-closed) to enforce forward compatibility. Numeric fields are validated
for finiteness (no NaN/Inf) and non-negativity where applicable.
``scan_date`` is validated as a valid ``YYYY-MM-DD`` date,
``total_return`` must be >= -1.0, and ``symbols_hash`` (if present) must be
a 16-char hex string. Files that fail schema validation are treated as
corrupt (exit code 1). Values are also validated before saving — NaN/Inf and
negative ``cycle_lock_count`` are rejected at write time to prevent creating
an invalid state file.

Both risk state and JSON artifact are serialized with ``allow_nan=False`` to
guarantee strict JSON (ECMA-404) output. The backtest result is validated
IMMEDIATELY after ``engine.run()`` returns — before any printing or
formatting — for type, finiteness, and presence of ``final_assets``,
``total_return``, ``max_drawdown``, ``sharpe``, and ``total_trades``.
Strict type checking rejects strings (even if float-convertible like "1.23"),
bool (which is a subclass of int in Python), None, and non-dict results.
When any field is invalid, an error artifact is written to a SEPARATE file
(``signals_<date>.error.json``) and the scan exits with code 1. The last
successful artifact (``signals_<date>.json``) is never overwritten by an
error artifact.

**Transaction ordering (artifact-first):** The artifact is written to disk
BEFORE risk state is committed. This ensures that if the artifact write
fails, no risk state has been saved — preventing state/artifact
inconsistency. If the risk state save subsequently fails, the artifact
already exists on disk with ``risk_state_saved: false``, which correctly
reflects the state. The artifact is then updated (best-effort) with the
actual ``risk_state_saved`` status. Both artifact and risk state share the
same ``run_id`` for traceability.

A ``latest_success.json`` pointer file is updated only on successful
artifact write, providing a stable reference for downstream consumers to
find the last good signals.

Stale data fail-closed: if any symbol's cached data is stale (network fetch
failed) or data end dates are inconsistent across symbols, the scan refuses to
produce signals and exits with code 1. Override with --allow-stale only when
you understand the risk; stale-data signals must not be used for live trading.

Usage:
    python daily_signal_scan.py [--end-date YYYY-MM-DD] [--cache-dir DIR] [--capital N]
    python daily_signal_scan.py --reset-risk-state  # clear old identity

If --end-date is omitted, today's date is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime
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


def _validate_result_fields(result: Any) -> list[str]:
    """Validate backtest result fields for type and finiteness.

    Returns a list of error strings for invalid fields. An empty list
    means the result is valid. This function must be called BEFORE any
    formatting or printing of result fields, because ``None``, strings,
    or missing keys would cause ``TypeError`` / ``KeyError`` in f-string
    format specifiers like ``{:,.0f}``.

    Strict type checking is enforced:
    - ``result`` must be a dict (not None, list, or other type).
    - Numeric fields (``final_assets``, ``total_return``, ``max_drawdown``,
      ``sharpe``) must be ``int`` or ``float`` — strings like ``"1.23"``
      are REJECTED even though ``float("1.23")`` would succeed, because
      the original string would still cause ``TypeError`` when used in
      f-string format specifiers (e.g. ``f"{value:.2%}"``).
    - ``bool`` is REJECTED for numeric fields (bool is a subclass of int
      in Python, but ``True / False`` is semantically wrong for financial
      metrics).
    - ``total_trades`` must be a non-negative ``int`` (not bool, not
      float, not string).
    - All numeric values must be finite (no NaN/Inf).
    """
    # Guard against non-dict results (None, list, etc.)
    if not isinstance(result, dict):
        return [f"result is {type(result).__name__}, expected dict"]

    invalid: list[str] = []

    # Validate float fields with strict type checking.
    # Strings (even float-convertible like "1.23") are rejected because
    # they would cause TypeError in f-string format specifiers.
    # bool is rejected because it's semantically wrong for financial metrics.
    for field in ("final_assets", "total_return", "max_drawdown", "sharpe"):
        val = result.get(field)
        if val is None:
            invalid.append(f"{field}=None (missing or null)")
            continue
        # Reject bool — it's a subclass of int but semantically wrong
        if isinstance(val, bool):
            invalid.append(f"{field}={val!r} (bool, expected number)")
            continue
        # Reject strings — even if float-convertible, the original string
        # would cause TypeError in f-string format specifiers
        if isinstance(val, str):
            invalid.append(f"{field}={val!r} (str, expected number)")
            continue
        # Must be int or float
        if not isinstance(val, (int, float)):
            invalid.append(f"{field}={val!r} (type {type(val).__name__}, expected number)")
            continue
        # Must be finite
        fval = float(val)
        if not math.isfinite(fval):
            invalid.append(f"{field}={fval} (non-finite)")

    # Validate total_trades — must be a non-negative int (not bool, not
    # float, not string). int() would silently convert or truncate
    # floats and strings, masking data corruption.
    tt = result.get("total_trades")
    if tt is None:
        invalid.append("total_trades=None (missing or null)")
    elif isinstance(tt, bool):
        invalid.append(f"total_trades={tt!r} (bool, expected int)")
    elif isinstance(tt, str):
        invalid.append(f"total_trades={tt!r} (str, expected int)")
    elif not isinstance(tt, int):
        invalid.append(f"total_trades={tt!r} (type {type(tt).__name__}, expected int)")
    elif tt < 0:
        invalid.append(f"total_trades={tt} (negative)")

    return invalid


def _compute_identity_hash(
    tradable: dict[str, str], config_fingerprint: str,
) -> str:
    """Compute the risk-state identity hash from stable fields.

    The hash includes the symbol set, count, and config fingerprint
    (start date, indicator state, capital, warmup days). Cash/capital
    is included because different capital means different position
    sizing and risk exposure.
    """
    identity_parts = [
        "trade",
        str(len(tradable)),
        ",".join(sorted(tradable.keys())),
        config_fingerprint,
    ]
    return hashlib.sha256(
        "|".join(identity_parts).encode("utf-8")
    ).hexdigest()[:16]


def _generate_run_id(end_date: str) -> str:
    """Generate a unique run ID for traceability."""
    import uuid
    return f"trade_{end_date}_{uuid.uuid4().hex[:8]}"


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

    .. deprecated::
        The ``--account`` CLI flag is disabled. This function is retained for
        unit tests and future use by the separate account signal engine. It
        is not called in the production ``main()`` path.

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


_RISK_STATE_SCHEMA_VERSION = 1

# Set of known schema versions. Unknown versions are rejected (fail-closed)
# to prevent misinterpreting fields with changed semantics in future versions.
_KNOWN_SCHEMA_VERSIONS: set[int] = {1}

# Required fields and their expected types for risk state validation.
# ``bool`` fields use ``bool`` explicitly; numeric fields use ``(int, float)``
# but ``bool`` is always rejected for numeric fields (bool is a subclass of
# int in Python).
_RISK_STATE_REQUIRED_FIELDS: dict[str, type | tuple[type, ...]] = {
    "schema_version": int,
    "scan_date": str,
    "terminal_risk_lock": bool,
    "sector_guard_active": bool,
    "cycle_lock_count": int,
    "max_drawdown": (int, float),
    "total_return": (int, float),
    "final_assets": (int, float),
}


def _validate_risk_state(data: Any) -> str | None:
    """Validate risk state schema, fields, and types.

    Returns ``None`` if valid, or an error message string if invalid.

    Checks performed (in order):
    1. Data is a dict.
    2. ``schema_version`` exists, is an int (not bool), and is a known version.
    3. All required fields exist with correct types.
    4. ``bool`` is never accepted where ``int`` or ``float`` is expected.
    5. Numeric fields (``max_drawdown``, ``total_return``, ``final_assets``)
       are finite (no NaN/Inf).
    6. ``cycle_lock_count`` and ``final_assets`` are non-negative.
    7. ``total_return`` >= -1.0 (cannot lose more than 100%).
    8. ``scan_date`` is a valid ``YYYY-MM-DD`` date.
    9. ``symbols_hash`` (if present) is a 16-char hex string.
    10. ``total_symbols`` (if present) is a non-negative int.

    Unknown ``schema_version`` values are rejected to enforce forward
    compatibility — a future version with changed field semantics must not
    be silently loaded by this code.
    """
    if not isinstance(data, dict):
        return "risk_state.json 内容不是有效 JSON 对象"

    # Validate schema_version first — unknown versions are rejected
    sv = data.get("schema_version")
    if sv is None:
        return "risk_state.json 缺少必需字段 'schema_version'"
    if isinstance(sv, bool) or not isinstance(sv, int):
        return (
            f"risk_state.json 字段 'schema_version' 应为 int，"
            f"实际为 {type(sv).__name__}"
        )
    if sv not in _KNOWN_SCHEMA_VERSIONS:
        return (
            f"risk_state.json schema_version={sv} 不是已知版本 "
            f"(支持: {sorted(_KNOWN_SCHEMA_VERSIONS)})"
        )

    # Validate remaining required fields
    for field, expected_type in _RISK_STATE_REQUIRED_FIELDS.items():
        if field == "schema_version":
            continue  # already validated above
        if field not in data:
            return f"risk_state.json 缺少必需字段 '{field}'"
        val = data[field]
        if expected_type is bool:
            if not isinstance(val, bool):
                return (
                    f"risk_state.json 字段 '{field}' 应为 bool，"
                    f"实际为 {type(val).__name__}"
                )
        elif expected_type is int:
            if isinstance(val, bool) or not isinstance(val, int):
                return (
                    f"risk_state.json 字段 '{field}' 应为 int，"
                    f"实际为 {type(val).__name__}"
                )
        elif isinstance(expected_type, tuple):
            # Numeric type: accept int or float but reject bool
            if isinstance(val, bool) or not isinstance(val, expected_type):
                return (
                    f"risk_state.json 字段 '{field}' 应为 number，"
                    f"实际为 {type(val).__name__}"
                )
            # Reject NaN and Inf — they break comparisons and formatting
            if isinstance(val, float) and not math.isfinite(val):
                return (
                    f"risk_state.json 字段 '{field}' 包含非有限值 "
                    f"(NaN/Inf)"
                )
        elif not isinstance(val, expected_type):
            return (
                f"risk_state.json 字段 '{field}' 应为 "
                f"{expected_type.__name__}，实际为 {type(val).__name__}"
            )

    # Range validation for specific fields
    if data.get("cycle_lock_count", 0) < 0:
        return "risk_state.json 字段 'cycle_lock_count' 不能为负数"
    if data.get("final_assets", 0) < 0:
        return "risk_state.json 字段 'final_assets' 不能为负数"

    # Semantic range validation
    total_ret = data.get("total_return")
    if isinstance(total_ret, (int, float)) and total_ret < -1.0:
        return f"risk_state.json 字段 'total_return' 不能小于 -1 ({total_ret})"

    # Validate scan_date is a valid YYYY-MM-DD date
    scan_date = data.get("scan_date")
    if isinstance(scan_date, str):
        try:
            datetime.strptime(scan_date, "%Y-%m-%d")
        except ValueError:
            return (
                f"risk_state.json 字段 'scan_date' 不是有效的 YYYY-MM-DD 日期: "
                f"{scan_date!r}"
            )

    # Validate symbols_hash format if present (16-char hex)
    symbols_hash = data.get("symbols_hash")
    if symbols_hash is not None:
        if not isinstance(symbols_hash, str) or len(symbols_hash) != 16:
            return (
                f"risk_state.json 字段 'symbols_hash' 应为 16 字符十六进制，"
                f"实际为 {type(symbols_hash).__name__} 长度 {len(symbols_hash) if isinstance(symbols_hash, str) else 'N/A'}"
            )
        try:
            int(symbols_hash, 16)
        except ValueError:
            return f"risk_state.json 字段 'symbols_hash' 不是有效的十六进制: {symbols_hash!r}"

    # Validate total_symbols if present
    total_symbols = data.get("total_symbols")
    if total_symbols is not None:
        if isinstance(total_symbols, bool) or not isinstance(total_symbols, int):
            return f"risk_state.json 字段 'total_symbols' 应为 int，实际为 {type(total_symbols).__name__}"
        if total_symbols < 0:
            return f"risk_state.json 字段 'total_symbols' 不能为负数 ({total_symbols})"

    return None


def _load_prev_risk_state(
    output_dir: str, end_date: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Load the risk state saved by a previous daily scan run.

    Returns a tuple of ``(state, error)``:

    - File missing: ``(None, None)`` — first run, not an error.
    - Same-day rerun: ``(state, None)`` — the state is returned so the
      caller can preserve terminal lock and sector guard continuity.
    - Corrupt or unreadable file: ``(None, error_msg)`` — the caller
      should treat this as a fail-closed condition and exit with code 1.
    - Schema validation failure: ``(None, error_msg)`` — same as corrupt.
    - Date validation failure: ``(None, error_msg)`` — the saved
      ``scan_date`` is later than the requested ``end_date``, which
      would be forward contamination (e.g. loading an August 1 state
      into a July 20 replay). Rejected (fail-closed).
    """
    state_file = Path(output_dir) / "risk_state.json"
    if not state_file.exists():
        return None, None
    try:
        raw = state_file.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"risk_state.json 损坏或无法读取: {exc}"
    validation_error = _validate_risk_state(data)
    if validation_error:
        return None, validation_error

    # Date validation: prevent forward contamination.
    # The saved scan_date must be <= the requested end_date. If a user
    # first runs with end_date=2026-08-01 and then runs with
    # --end-date 2026-07-20, the August 1 risk state must NOT be loaded
    # into the July 20 replay — that would be direct look-ahead bias.
    scan_date_str = data.get("scan_date", "")
    if scan_date_str and end_date:
        try:
            scan_d = datetime.strptime(scan_date_str, "%Y-%m-%d").date()
            end_d = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            # Date format errors are already caught by _validate_risk_state,
            # but guard against edge cases.
            return None, (
                f"risk_state.json scan_date 或 end_date 日期解析失败: "
                f"scan_date={scan_date_str!r}, end_date={end_date!r}"
            )
        if scan_d > end_d:
            return None, (
                f"风险状态日期前视污染: risk_state.json 的 scan_date="
                f"{scan_date_str} 晚于本次 end_date={end_date}。"
                f"拒绝加载以防止未来状态污染历史回放。"
                f"请使用 --reset-risk-state 清除旧状态后重试。"
            )

    return data, None


def _save_risk_state(
    output_dir: str, end_date: str, result: dict[str, Any],
    tradable: dict[str, str] | None = None,
    config_hash: str = "",
    run_id: str = "",
) -> None:
    """Persist risk state for restoration by the next daily scan run.

    The ``symbols_hash`` includes symbol set, count, and (when provided)
    config fingerprint so that the same symbol set with different
    configuration is treated as a different identity. A ``schema_version``
    is included for forward compatibility.

    The ``run_id`` is passed from the caller (the main scan function)
    so that the artifact, risk state, and latest_success pointer all
    share the same run_id for traceability. If not provided, a new one
    is generated (for backward compatibility with tests).

    Raises ``ValueError`` if any numeric field is NaN/Inf or if
    ``cycle_lock_count`` is negative. This prevents creating an invalid
    state file that would be rejected on the next load.
    """
    # Extract numeric values and validate they are finite before saving.
    # NaN/Inf would cause json.dumps(allow_nan=False) to raise ValueError
    # at serialization time (below), preventing the file from being written.
    # We validate here to give a clear error message and to prevent
    # reaching the serialization stage with invalid data.
    max_dd = float(result.get("max_drawdown", 0.0))
    total_ret = float(result.get("total_return", 0.0))
    final_assets = float(result.get("final_assets", 0.0))
    for name, val in [("max_drawdown", max_dd), ("total_return", total_ret),
                      ("final_assets", final_assets)]:
        if not math.isfinite(val):
            raise ValueError(
                f"拒绝保存非有限值: {name}={val} (NaN/Inf)"
            )
    cycle_lock = int(result.get("cycle_lock_count") or 0)
    if cycle_lock < 0:
        raise ValueError(
            f"拒绝保存负值: cycle_lock_count={cycle_lock}"
        )

    state: dict[str, Any] = {
        "schema_version": _RISK_STATE_SCHEMA_VERSION,
        "scan_date": end_date,
        "terminal_risk_lock": bool(result.get("terminal_risk_lock", False)),
        "sector_guard_active": bool(result.get("sector_guard_active", False)),
        "cycle_lock_count": cycle_lock,
        "max_drawdown": max_dd,
        "total_return": total_ret,
        "final_assets": final_assets,
    }
    if tradable:
        # Build identity hash from stable fields to prevent
        # cross-contamination between different configurations.
        state["symbols_hash"] = _compute_identity_hash(tradable, config_hash)
        state["total_symbols"] = len(tradable)
        # Use the caller-provided run_id so artifact, risk state, and
        # latest_success pointer all share the same run_id.
        state["run_id"] = run_id or _generate_run_id(end_date)
    state_file = Path(output_dir) / "risk_state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: write to a temp file in the same directory, then
    # os.replace() to the final path. This prevents partial writes from
    # corrupting the risk state file on disk full, process kill, or power loss.
    content = json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    fd, tmp_path = tempfile.mkstemp(
        dir=str(state_file.parent), prefix=".risk_state_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(state_file))
    except OSError:
        # Clean up the temp file if the replace failed
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _apply_buy_suppression(
    sigs: list[Any], suppress_buys: bool
) -> tuple[str, str, bool]:
    """Apply buy suppression to a list of pending signals for one symbol.

    Returns ``(signal_label, strategies, was_suppressed)``.

    When ``suppress_buys`` is ``True``:
    - Pure buy signals are replaced with "观望 (风险状态不匹配)".
    - Mixed buy/sell signals keep only the sell signals and append
      "[买入已抑制]" to the label.
    - Pure sell (or non-buy) signals are shown unchanged.

    Only ``sell`` directions are retained in mixed signals — other
    non-buy directions (e.g. ``hold``) are excluded to avoid mislabeling.
    """
    if not sigs:
        return ("", "", False)

    directions = sorted({s.direction for s in sigs})
    if len(directions) == 1:
        signal_label = _classify_signal(sigs[0])
        strategies = ", ".join(sorted({s.strategy_name for s in sigs}))
    else:
        parts = []
        for d in directions:
            d_sigs = [s for s in sigs if s.direction == d]
            parts.append(f"{_classify_signal(d_sigs[0])}({len(d_sigs)})")
        signal_label = " + ".join(parts)
        strategies = ", ".join(sorted({s.strategy_name for s in sigs}))

    was_suppressed = False
    if suppress_buys:
        buy_sigs = [s for s in sigs if s.direction == "buy"]
        sell_sigs = [s for s in sigs if s.direction == "sell"]
        if buy_sigs and sell_sigs:
            # Mixed buy/sell: keep only sell, suppress buy
            sell_dirs = sorted({s.direction for s in sell_sigs})
            parts = []
            for d in sell_dirs:
                d_sigs = [s for s in sell_sigs if s.direction == d]
                parts.append(f"{_classify_signal(d_sigs[0])}({len(d_sigs)})")
            signal_label = " + ".join(parts) + " [买入已抑制]"
            strategies = ", ".join(
                sorted({s.strategy_name for s in sell_sigs})
            )
            was_suppressed = True
        elif buy_sigs:
            # Pure buy (or buy + non-sell): suppress entirely
            signal_label = "观望 (风险状态不匹配)"
            strategies = "—"
            was_suppressed = True

    return (signal_label, strategies, was_suppressed)


def _serialize_pending_signals(
    pending: list[Any], suppress_buys: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Serialize pending signals into executable and blocked lists.

    Returns ``(executable_signals, blocked_signals)``.

    Blocked buy signals are moved to a separate ``blocked_signals`` list so
    that ``pending_signals`` only contains executable signals. This is the
    true fail-closed approach: downstream consumers that only check
    ``direction == "buy"`` on ``pending_signals`` will never see blocked
    buys, without needing to check the ``executable`` flag.

    Each entry in both lists includes ``blocked`` and ``executable`` flags
    for explicit machine consumption.
    """
    executable: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for sig in pending:
        try:
            entry = asdict(sig)
        except TypeError:
            entry = {
                "symbol": getattr(sig, "symbol", ""),
                "direction": getattr(sig, "direction", ""),
                "strategy_name": getattr(sig, "strategy_name", ""),
                "target_shares": getattr(sig, "target_shares", 0),
                "price": getattr(sig, "price", 0.0),
                "reason": getattr(sig, "reason", ""),
                "signal_date": getattr(sig, "signal_date", ""),
            }
        if suppress_buys and entry.get("direction") == "buy":
            entry["blocked"] = True
            entry["blocked_reason"] = "risk_state_identity_mismatch"
            entry["executable"] = False
            blocked.append(entry)
        else:
            entry["blocked"] = False
            entry["executable"] = True
            executable.append(entry)
    return executable, blocked


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
        help="DISABLED. Real-account JSON integration is under reconstruction. "
        "Use simulation mode (default) instead.",
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
        "--reset-risk-state",
        action="store_true",
        help="Delete risk_state.json before running. Use this when you "
        "intentionally change symbol set, start date, or configuration to "
        "establish a new risk-state identity without buy suppression.",
    )
    args = parser.parse_args()

    end_date = args.end_date or _today_str()
    start_date = args.start_date or START_DATE

    # --account mode is disabled — check this BEFORE --reset-risk-state so
    # that combining --reset-risk-state --account does not silently delete
    # the old risk state and then immediately exit with an account error.
    if args.account:
        print("=" * 72)
        print("  ⚠ --account 模式当前不可用")
        print("=" * 72)
        print()
        print("  真实账户模式存在多个架构缺陷，已暂时停用：")
        print("    - 单袖套账户快照被 reset 清空，不会实际注入")
        print("    - 三袖套混合真实和模拟账本")
        print("    - 外部持仓清仓执行有架构缺陷 (strategy=None 已加防护但未修复)")
        print("    - 峰值权益注入时序错误可能误触发终态锁")
        print("    - 满仓账户(现金为0)无法初始化")
        print("    - 账户模式绩效指标无经济意义")
        print()
        print("  请使用不带 --account 的模拟模式运行。")
        print("  真实账户信号引擎正在重构中。")
        return 1

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

    # Configure incremental cache for efficient daily updates
    qf.DataFetcher._cache_dir = args.cache_dir

    # ── Pre-screen: skip symbols with no data (e.g. not yet listed) ──
    # The engine loads all symbols at once and raises on any failure, so we
    # probe each symbol individually and build a tradable universe.
    tradable: dict[str, str] = {}
    skipped: list[tuple[str, str, str]] = []  # (code, name, reason)
    # Use a start date ~400 days before start_date for the probe so the
    # engine has enough warmup history for indicator calculation.
    # (400 calendar days ≈ 13 months, slightly more than the 365-day
    # warmup_calendar_days used by the engine, to ensure coverage.)
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
            print("  ✗ 数据不一致 — 拒绝生成信号 (fail-closed)")
            print("  如需强制运行，请添加 --allow-stale 参数。")
            return 1

    # ── Validate FILE risk state identity to prevent cross-contamination ──
    # Identity uses stable fields only (symbol set + count + config
    # fingerprint including start date, indicator state, capital, and
    # warmup days). Cash/capital is included because different capital
    # means different position sizing and risk exposure.
    # When the identity does not match, buy signals are suppressed (fail-closed)
    # to prevent entering new positions without verified risk-state continuity.
    config_fingerprint = (
        f"start={start_date}|indicator=warm"
        f"|capital={capital}|warmup=365"
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

    print("  正在运行回测，请稍候...")
    print("-" * 72)

    # NOTE: risk_state is NOT passed to the engine. The daily scan replays
    # the full history from start_date to end_date every time. Injecting the
    # previous run's end-state (e.g. terminal_risk_lock=True from July 30)
    # into a fresh replay starting from July 1 would create a time-direction
    # error: the future末端 state would change the past historical path.
    # Instead, the engine independently rebuilds all risk states from the
    # actual historical data. The saved risk_state.json is loaded for
    # display and continuity checking only — it does NOT influence the
    # current backtest.
    engine = qf.BacktestEngine(capital)
    result = engine.run(
        tradable,
        start_date,
        end_date,
        data_dir=None,  # online AKShare with cache
        indicator_state="warm",
        warmup_calendar_days=365,
    )

    # ── Validate result IMMEDIATELY after engine.run() ──────────────
    # This must happen BEFORE any printing or formatting, because None,
    # string, or missing fields would cause TypeError/KeyError in f-string
    # format specifiers (e.g. {:,.0f}). If invalid, we write an error
    # artifact to a SEPARATE file (signals_<date>.error.json) so the last
    # successful artifact (signals_<date>.json) is never overwritten.
    run_id = _generate_run_id(end_date)
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

    # ── Extract latest pending signals ───────────────────────────────
    pending = result.get("pending_signals", [])
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
                "industry": qf._CoreBacktestEngine.get_symbol_group(code, "N/A"),
                "profile": qf._CoreBacktestEngine.get_symbol_profile(code, "N/A"),
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
            "industry": qf._CoreBacktestEngine.get_symbol_group(code, "default"),
            "profile": qf._CoreBacktestEngine.get_symbol_profile(code, "default"),
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
    print(f"  总交易次数:     {result['total_trades']:>14}")
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
    if not suppress_buys:
        try:
            _save_risk_state(
                args.output_dir, end_date, result, tradable,
                config_hash=config_fingerprint,
                run_id=run_id,
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
    # Only updated on successful artifact write — provides a stable
    # pointer for downstream consumers to find the last good signals.
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
    elif suppress_buys:
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


if __name__ == "__main__":
    raise SystemExit(main())
