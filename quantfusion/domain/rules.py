"""Shared validation and A-share market rules."""

from __future__ import annotations

import re
from typing import Any

import numpy as np
import pandas as pd


A_SHARE_LOT_SIZE = 100
SYMBOL_RE = re.compile("^\\d{6}$")
_SYMBOL_RE = SYMBOL_RE

def _is_finite_number(value: Any) -> bool:
    """Return whether value is a finite real number."""
    try:
        return bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


def _require_finite(
    name: str,
    value: Any,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
    inclusive_max: bool = True,
) -> float:
    """Validate and normalize one bounded finite value."""
    if not _is_finite_number(value):
        raise ValueError(
            f"Configuration {name} must be finite; current value is {value!r}"
        )
    value = float(value)
    if min_value is not None and value < min_value:
        raise ValueError(
            f"Configuration {name} must be >= {min_value}; current value is {value}"
        )
    if max_value is not None:
        if inclusive_max and value > max_value:
            raise ValueError(
                f"Configuration {name} must be <= {max_value}; current value is {value}"
            )
        if not inclusive_max and value >= max_value:
            raise ValueError(
                f"Configuration {name} must be < {max_value}; current value is {value}"
            )
    return value


def _require_positive(
    name: str, value: Any, *, max_value: float | None = None, inclusive_max: bool = True
) -> float:
    """Validate a positive value with an optional upper bound."""
    value = _require_finite(
        name, value, max_value=max_value, inclusive_max=inclusive_max
    )
    if value <= 0:
        raise ValueError(f"Configuration {name} must be > 0; current value is {value}")
    return value


def _require_bool(name: str, value: Any) -> bool:
    """Reject truthy substitutes and return an actual Boolean."""
    if not isinstance(value, bool):
        raise ValueError(
            f"Configuration {name} must be bool; current value is {value!r}"
        )
    return value


def _require_int(name: str, value: Any, *, min_value: int = 0) -> int:
    """Validate an integer without accepting booleans or fractions."""
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise ValueError(
            f"Configuration {name} must be an integer; current value is {value!r}"
        )
    value = int(value)
    if value < min_value:
        raise ValueError(
            f"Configuration {name} must be >= {min_value}; current value is {value}"
        )
    return value


def _floor_to_lot(shares: float, lot_size: int = A_SHARE_LOT_SIZE) -> int:
    """Round a finite positive share count down to a board lot."""
    if (
        isinstance(lot_size, bool)
        or not isinstance(lot_size, (int, np.integer))
        or lot_size <= 0
    ):
        raise ValueError(
            f"lot_size must be a positive integer; current value is {lot_size!r}"
        )
    if not _is_finite_number(shares) or float(shares) <= 0:
        return 0
    return int(float(shares) // lot_size) * lot_size


def _limit_pct_for_code(code: str, cfg: dict | None = None, name: str = "") -> float:
    """Resolve the estimated daily board limit for a symbol."""
    code = str(code)
    if not _SYMBOL_RE.match(code):
        raise ValueError(
            f"Stock code must contain six digits; current value is {code!r}"
        )
    cfg = cfg or {}
    overrides = cfg.get("per_symbol_limit_pct", {}) or {}
    if code in overrides:
        return float(overrides[code])
    st_symbols = set(cfg.get("st_symbols", set()) or set())
    upper_name = str(name or "").upper()
    if code in st_symbols or "ST" in upper_name:
        return 0.05
    if code.startswith(("3", "68", "69")):
        return 0.2
    if code.startswith(("8", "4", "9")):
        return 0.3
    return 0.1


def _parse_dates(values: pd.Series | pd.Index) -> pd.Series:
    """Parse exchange dates without interpreting YYYYMMDD as nanoseconds."""
    ser = pd.Series(values)
    as_str = ser.astype("string").str.strip()
    parsed = pd.Series(pd.NaT, index=ser.index, dtype="datetime64[ns]")
    yyyymmdd = as_str.str.fullmatch("\\d{8}", na=False)
    if yyyymmdd.any():
        parsed.loc[yyyymmdd] = pd.to_datetime(
            as_str.loc[yyyymmdd], format="%Y%m%d", errors="coerce"
        )
    rest = ~yyyymmdd
    if rest.any():
        try:
            parsed.loc[rest] = pd.to_datetime(
                as_str.loc[rest], errors="coerce", format="mixed"
            )
        except TypeError:
            parsed.loc[rest] = pd.to_datetime(as_str.loc[rest], errors="coerce")
    return parsed

# Public canonical names; legacy facades retain the historical underscored aliases.
is_finite_number = _is_finite_number
require_finite = _require_finite
require_positive = _require_positive
require_bool = _require_bool
require_int = _require_int
floor_to_lot = _floor_to_lot
limit_pct_for_code = _limit_pct_for_code
parse_dates = _parse_dates
