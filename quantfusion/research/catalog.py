"""Frozen local-data catalog and expanding walk-forward folds."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from quantfusion.research.candidates import (
    DateWindow,
    WalkForwardFold,
    date_string,
    timestamp,
)

_date_string = date_string
_timestamp = timestamp

class LocalDataCatalog:
    """Index deterministic CSV coverage and choose only symbols available per window."""

    def __init__(
        self,
        data_dir: str | Path,
        symbols: dict[str, str],
        regime_symbols: Iterable[str],
        *,
        min_window_rows: int = 5,
    ) -> None:
        self.data_dir = Path(data_dir).expanduser()
        if not self.data_dir.is_dir():
            raise ValueError(f"Data directory does not exist: {self.data_dir}")
        if min_window_rows < 1:
            raise ValueError("min_window_rows must be positive")
        self.symbols = dict(symbols)
        self.min_window_rows = int(min_window_rows)
        self._dates: dict[str, pd.DatetimeIndex] = {}
        fingerprint = hashlib.sha256()
        required = sorted(set(symbols) | set(regime_symbols))
        for code in required:
            path = self.data_dir / f"{code}.csv"
            if not path.is_file():
                raise ValueError(f"Missing local market data for {code}: {path}")
            fingerprint.update(code.encode("ascii"))
            fingerprint.update(hashlib.sha256(path.read_bytes()).digest())
            frame = pd.read_csv(path)
            if "date" not in frame.columns:
                raise ValueError(f"Market data for {code} is missing the date column")
            dates = pd.DatetimeIndex(pd.to_datetime(frame["date"], errors="coerce"))
            dates = dates[~dates.isna()].sort_values().unique()
            if dates.empty:
                raise ValueError(f"Market data for {code} contains no valid dates")
            self._dates[code] = pd.DatetimeIndex(dates)
        combined = sorted({date for dates in self._dates.values() for date in dates})
        self.calendar = pd.DatetimeIndex(combined)
        self.fingerprint = fingerprint.hexdigest()

    def available_symbols(self, window: DateWindow) -> dict[str, str]:
        """Return tradable inputs with enough rows in this historical window."""
        start = _timestamp(window.start)
        end = _timestamp(window.end)
        available = {
            code: name
            for code, name in self.symbols.items()
            if int(((self._dates[code] >= start) & (self._dates[code] <= end)).sum())
            >= self.min_window_rows
        }
        if not available:
            raise ValueError(f"No supplied symbol has enough data in {window.name}")
        return available

    def coverage(self) -> dict[str, dict[str, Any]]:
        """Return date ranges recorded in the optimization artifact."""
        return {
            code: {
                "first_date": _date_string(dates[0]),
                "last_date": _date_string(dates[-1]),
                "rows": len(dates),
            }
            for code, dates in sorted(self._dates.items())
            if code in self.symbols
        }


def build_walk_forward_folds(
    calendar: pd.DatetimeIndex,
    *,
    start: str,
    test_start: str,
    end: str,
    train_months: int = 12,
    validation_months: int = 6,
    step_months: int = 6,
    minimum_folds: int = 2,
) -> tuple[list[WalkForwardFold], DateWindow]:
    """Build expanding training folds without touching the final holdout."""
    for name, value in (
        ("train_months", train_months),
        ("validation_months", validation_months),
        ("step_months", step_months),
        ("minimum_folds", minimum_folds),
    ):
        if value < 1:
            raise ValueError(f"{name} must be positive")
    if step_months < validation_months:
        raise ValueError(
            "step_months must be at least validation_months so validation folds "
            "do not overlap"
        )
    bounded = calendar[(calendar >= _timestamp(start)) & (calendar <= _timestamp(end))]
    if bounded.empty:
        raise ValueError("No trading dates exist inside the requested range")
    holdout_dates = bounded[bounded >= _timestamp(test_start)]
    if holdout_dates.empty:
        raise ValueError("test_start leaves no final holdout dates")
    actual_test_start = holdout_dates[0]
    pretest = bounded[bounded < actual_test_start]
    if pretest.empty:
        raise ValueError("No pre-test data is available for parameter selection")
    first_validation_target = bounded[0] + pd.DateOffset(months=train_months)
    validation_start_candidates = pretest[pretest >= first_validation_target]
    if validation_start_candidates.empty:
        raise ValueError("Training span leaves no validation window")
    cursor = validation_start_candidates[0]
    folds: list[WalkForwardFold] = []
    while cursor < actual_test_start:
        validation_end_target = (
            cursor + pd.DateOffset(months=validation_months) - pd.Timedelta(days=1)
        )
        validation_dates = pretest[
            (pretest >= cursor) & (pretest <= validation_end_target)
        ]
        training_dates = pretest[pretest < cursor]
        if validation_dates.empty or training_dates.empty:
            break
        fold_number = len(folds) + 1
        folds.append(
            WalkForwardFold(
                name=f"fold_{fold_number}",
                train=DateWindow(
                    f"fold_{fold_number}_train",
                    _date_string(training_dates[0]),
                    _date_string(training_dates[-1]),
                    "train",
                ),
                validation=DateWindow(
                    f"fold_{fold_number}_validation",
                    _date_string(validation_dates[0]),
                    _date_string(validation_dates[-1]),
                    "validation",
                ),
            )
        )
        next_target = cursor + pd.DateOffset(months=step_months)
        next_dates = pretest[pretest >= next_target]
        if next_dates.empty or next_dates[0] <= cursor:
            break
        cursor = next_dates[0]
    if len(folds) < minimum_folds:
        raise ValueError(
            f"Only {len(folds)} walk-forward folds are available; "
            f"at least {minimum_folds} are required"
        )
    test_window = DateWindow(
        "final_holdout",
        _date_string(actual_test_start),
        _date_string(bounded[-1]),
        "test",
    )
    return folds, test_window
