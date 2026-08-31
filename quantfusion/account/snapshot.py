"""Strict account JSON loading and schema validation."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date
from numbers import Real
from pathlib import Path

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

_ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "account_id",
        "snapshot_date",
        "cash",
        "peak_equity",
        "positions",
    }
)
_POSITION_REQUIRED_FIELDS = frozenset(
    {"shares", "sellable_shares", "avg_cost", "entry_date"}
)
_POSITION_FIELDS = _POSITION_REQUIRED_FIELDS | {"highest_close"}
_MISSING = object()


def _required(payload: dict[str, object], field: str, *, where: str) -> object:
    if field not in payload:
        raise ValueError(f"{where} missing required field {field!r}")
    return payload[field]


def _reject_unknown_fields(
    payload: dict[str, object],
    allowed: frozenset[str],
    *,
    where: str,
) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        label = "root" if where == "account snapshot" else "position"
        raise ValueError(f"unknown {label} field(s): {', '.join(unknown)}")


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
    if value is _MISSING:
        return None
    normalized = _require_real(name, value, minimum=0.0)
    if normalized <= 0:
        raise ValueError(f"{name} must be > 0 when provided")
    return normalized


def _require_iso_date(name: str, value: object) -> str:
    """只接受精确 YYYY-MM-DD 字符串，不做宽松日期归一化。"""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a YYYY-MM-DD string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid YYYY-MM-DD date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must use exact YYYY-MM-DD format")
    return value


def _invalid_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _decode_snapshot(raw_bytes: bytes) -> dict[str, object]:
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("account snapshot must be UTF-8 JSON") from exc
    try:
        payload = json.loads(text, parse_constant=_invalid_json_constant)
    except json.JSONDecodeError as exc:
        raise ValueError(f"unable to parse account snapshot JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("account snapshot root must be an object")
    return payload


def load_account_snapshot_with_sha256(
    path: str | Path,
) -> tuple[AccountSnapshot, str]:
    """一次读取、严格验证 v3 快照，并返回精确输入字节哈希。"""
    try:
        raw_bytes = Path(path).read_bytes()
    except OSError as exc:
        raise ValueError(f"unable to read account snapshot: {exc}") from exc
    digest = hashlib.sha256(raw_bytes).hexdigest()
    payload = _decode_snapshot(raw_bytes)
    _reject_unknown_fields(payload, _ROOT_FIELDS, where="account snapshot")

    schema_version = _required(
        payload, "schema_version", where="account snapshot"
    )
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 3
    ):
        raise ValueError("account snapshot schema_version must equal 3")

    account_id = _required(payload, "account_id", where="account snapshot")
    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError("account snapshot account_id must be a non-empty string")
    snapshot_date = _require_iso_date(
        "account snapshot snapshot_date",
        _required(payload, "snapshot_date", where="account snapshot"),
    )
    cash = _require_real(
        "account cash",
        _required(payload, "cash", where="account snapshot"),
    )
    peak = _require_real(
        "peak_equity",
        _required(payload, "peak_equity", where="account snapshot"),
    )
    if peak <= 0:
        raise ValueError("peak_equity must be > 0")

    raw_positions = _required(payload, "positions", where="account snapshot")
    if not isinstance(raw_positions, dict):
        raise ValueError("positions must be an object keyed by stock code")

    positions: list[AccountPosition] = []
    for raw_code, raw in raw_positions.items():
        code = str(raw_code)
        if _SYMBOL_RE.fullmatch(code) is None:
            raise ValueError(f"invalid stock code in account snapshot: {code!r}")
        if not isinstance(raw, dict):
            raise ValueError(f"position {code} must be an object")
        _reject_unknown_fields(raw, _POSITION_FIELDS, where=f"position {code}")
        missing = sorted(_POSITION_REQUIRED_FIELDS - set(raw))
        if missing:
            raise ValueError(
                f"position {code} missing required field(s): {', '.join(missing)}"
            )

        shares = _require_positive_int(f"position {code} shares", raw["shares"])
        avg_cost = _require_real(
            f"position {code} avg_cost",
            raw["avg_cost"],
        )
        if avg_cost <= 0:
            raise ValueError(f"position {code} avg_cost must be > 0")
        highest_close = _optional_positive_real(
            f"position {code} highest_close",
            raw.get("highest_close", _MISSING),
        )
        sellable = _require_non_negative_int(
            f"position {code} sellable_shares", raw["sellable_shares"]
        )
        if sellable > shares:
            raise ValueError(
                f"position {code} sellable_shares must not exceed shares"
            )
        entry_date = _require_iso_date(
            f"position {code} entry_date", raw["entry_date"]
        )
        if entry_date > snapshot_date:
            raise ValueError(
                f"position {code} entry_date must not be later than snapshot_date"
            )
        positions.append(
            AccountPosition(
                symbol=code,
                shares=shares,
                sellable_shares=sellable,
                avg_cost=avg_cost,
                entry_date=entry_date,
                highest_close=highest_close,
            )
        )

    positions.sort(key=lambda item: item.symbol)
    return AccountSnapshot(
        schema_version=3,
        account_id=account_id,
        snapshot_date=snapshot_date,
        cash=cash,
        peak_equity=peak,
        positions=tuple(positions),
    ), digest


def load_account_snapshot(path: str | Path) -> AccountSnapshot:
    """从单一严格 v3 JSON schema 加载账户快照。"""
    snapshot, _ = load_account_snapshot_with_sha256(path)
    return snapshot
