"""Strict, atomic daily risk-continuity state storage."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

_RISK_STATE_SCHEMA_VERSION = 1
_KNOWN_SCHEMA_VERSIONS: set[int] = {1}
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


compute_identity_hash = _compute_identity_hash
generate_run_id = _generate_run_id
validate_risk_state = _validate_risk_state
load_prev_risk_state = _load_prev_risk_state
save_risk_state = _save_risk_state
