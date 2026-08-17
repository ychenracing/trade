"""Pure validation and serialization support for the daily scan."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

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


today_str = _today_str
validate_result_fields = _validate_result_fields
classify_signal = _classify_signal
extract_positions = _extract_positions
load_account = _load_account
