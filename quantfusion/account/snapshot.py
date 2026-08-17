"""Strict account JSON loading and schema validation."""

from __future__ import annotations

import json
import math
import re
from numbers import Real
from pathlib import Path
from typing import Any, cast

import pandas as pd

from quantfusion.account.models import AccountPosition, AccountSnapshot

SYMBOL_RE = re.compile(r"^\d{6}$")
EXPECTED_DATA_ERRORS = (
    ImportError,
    IndexError,
    KeyError,
    OSError,
    RuntimeError,
    ValueError,
)

_SYMBOL_RE = SYMBOL_RE

def _require_real(name: str, value: object, *, minimum: float = 0.0) -> float:
    """拒绝布尔值和字符串，并返回满足下界的有限实数。"""
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a real number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < minimum:
        raise ValueError(f"{name} must be finite and >= {minimum}")
    return normalized


def _require_positive_int(name: str, value: object) -> int:
    """只接受严格正整数，避免 JSON 布尔值或小数被静默转换。"""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _require_non_negative_int(name: str, value: object) -> int:
    """只接受非负整数（T+1 可卖股数允许为 0）。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _optional_positive_real(name: str, value: object) -> float | None:
    """校验可选的正有限实数。"""
    if value is None:
        return None
    normalized = _require_real(name, value, minimum=0.0)
    if normalized <= 0:
        raise ValueError(f"{name} must be > 0 when provided")
    return normalized


def _validate_entry_date(value: object, *, symbol: str) -> str:
    """校验可选建仓日期并规范为 YYYY-MM-DD。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        timestamp = pd.Timestamp(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"position {symbol} has invalid entry_date") from exc
    if timestamp is pd.NaT:
        raise ValueError(f"position {symbol} has invalid entry_date")
    return cast(pd.Timestamp, timestamp).strftime("%Y-%m-%d")


def _optional_text(value: object) -> str | None:
    """把可选标量规范为去空白字符串；缺失或空串返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_mapping(value: object, *, name: str = "mapping") -> dict[str, Any]:
    """校验可选对象字段必须为字典，缺失时返回空字典。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object when provided")
    return dict(value)


def _optional_int(value: object, *, default: int) -> int:
    """校验可选正整数；缺失或非正整数时返回默认值。"""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("optional integer must be a non-negative integer")
    return value


def load_account_snapshot(path: str | Path) -> AccountSnapshot:
    """从严格 JSON 文件加载并验证真实账户快照。"""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read account snapshot: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("account snapshot root must be an object")

    cash = _require_real("account cash", payload.get("cash", 0.0))
    peak = _require_real("peak_equity", payload.get("peak_equity", cash))

    raw_positions = payload.get("positions", {})
    if not isinstance(raw_positions, dict):
        raise ValueError("positions must be an object keyed by stock code")

    positions: list[AccountPosition] = []
    for raw_code, raw in raw_positions.items():
        code = str(raw_code)
        if _SYMBOL_RE.fullmatch(code) is None:
            raise ValueError(f"invalid stock code in account snapshot: {code!r}")
        if not isinstance(raw, dict):
            raise ValueError(f"position {code} must be an object")

        shares = _require_positive_int(f"position {code} shares", raw.get("shares", 0))
        avg_cost = _require_real(
            f"position {code} avg_cost",
            raw.get("avg_cost", raw.get("price", 0.0)),
        )
        if avg_cost <= 0:
            raise ValueError(f"position {code} avg_cost must be > 0")
        highest_close = _optional_positive_real(
            f"position {code} highest_close",
            raw.get("highest_close"),
        )
        sellable = raw.get("sellable_shares")
        if sellable is not None:
            sellable = _require_non_negative_int(
                f"position {code} sellable_shares", sellable
            )
            if sellable > shares:
                raise ValueError(
                    f"position {code} sellable_shares must not exceed shares"
                )
        last_add = _validate_entry_date(raw.get("last_add_date", ""), symbol=code)
        positions.append(
            AccountPosition(
                symbol=code,
                shares=shares,
                avg_cost=avg_cost,
                entry_date=_validate_entry_date(raw.get("entry_date", ""), symbol=code),
                highest_close=highest_close,
                sellable_shares=sellable,
                position_source=_optional_text(raw.get("position_source")),
                last_add_date=last_add or None,
            )
        )

    positions.sort(key=lambda item: item.symbol)
    return AccountSnapshot(
        cash=cash,
        peak_equity=peak,
        positions=tuple(positions),
        cooldowns=_optional_mapping(payload.get("cooldowns")),
        route_state=_optional_mapping(payload.get("route_state")),
        risk_state=_optional_mapping(payload.get("risk_state")),
        pending_orders=tuple(payload.get("pending_orders", ())),
        last_execution_report=_optional_mapping(
            payload.get("last_execution_report")
        ),
        equity_history=tuple(payload.get("equity_history", ())),
        account_id=_optional_text(payload.get("account_id")) or "main",
        schema_version=_optional_int(payload.get("schema_version"), default=2),
    )
