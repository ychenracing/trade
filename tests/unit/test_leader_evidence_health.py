from __future__ import annotations

import pandas as pd
import pytest

from quantfusion.config.regime import MAX_EVIDENCE_STALENESS_DAYS
from quantfusion.regime.evidence import select_positive_momentum_leaders


def _frame(days: int, *, end: str, start_price: float = 10.0) -> pd.DataFrame:
    index = pd.bdate_range(end=pd.Timestamp(end), periods=days)
    closes = pd.Series(
        [start_price + i * 0.1 for i in range(days)],
        index=index,
        dtype="float64",
    )
    return pd.DataFrame(
        {
            "date": index,
            "open": closes.to_numpy(),
            "high": (closes * 1.01).to_numpy(),
            "low": (closes * 0.99).to_numpy(),
            "close": closes.to_numpy(),
            "volume": 1_000_000.0,
        }
    )


def _indexed_frame(days: int, *, end: str, start_price: float = 10.0) -> pd.DataFrame:
    frame = _frame(days, end=end, start_price=start_price)
    return frame.set_index(pd.DatetimeIndex(frame.pop("date")))


def _loader_for(*, short_end: str):
    reference_symbols = {"300308", "300502", "300394", "688008", "603986"}

    def load(code: str, boundary: str) -> pd.DataFrame:
        if code in reference_symbols:
            return _indexed_frame(260, end=boundary)
        if code == "mature":
            return _indexed_frame(260, end=boundary, start_price=20.0)
        if code == "short":
            return _indexed_frame(30, end=short_end, start_price=30.0)
        raise FileNotFoundError(code)

    return load


def test_fresh_short_history_is_ineligible_not_degraded() -> None:
    """A newly listed fresh symbol is observable even before leader warmup completes."""
    selection = select_positive_momentum_leaders(
        ("mature", "short"),
        data_dir="unused",
        as_of="2026-09-15",
        frame_loader=_loader_for(short_end="2026-09-15"),
    )

    assert selection.status == "READY"
    assert selection.observed_symbols == 2
    assert selection.unavailable_symbols == ()
    assert "short" not in selection.selected_symbols
    selection.require_ready("production route")


def test_stale_short_history_still_fails_closed() -> None:
    """Insufficient warmup must not hide genuinely stale market evidence."""
    stale_end = (
        pd.Timestamp("2026-09-15")
        - pd.Timedelta(days=MAX_EVIDENCE_STALENESS_DAYS + 1)
    ).strftime("%Y-%m-%d")
    selection = select_positive_momentum_leaders(
        ("mature", "short"),
        data_dir="unused",
        as_of="2026-09-15",
        frame_loader=_loader_for(short_end=stale_end),
    )

    assert selection.status == "DEGRADED"
    assert selection.unavailable_symbols == ("short",)
    with pytest.raises(RuntimeError, match="leader evidence is DEGRADED"):
        selection.require_ready("production route")


def test_verified_pre_listing_source_is_not_unavailable(tmp_path) -> None:
    """A real local series whose first bar is later than the boundary is N/A, not missing."""
    _frame(80, end="2026-09-15", start_price=40.0).to_csv(
        tmp_path / "688361.csv", index=False
    )

    selection = select_positive_momentum_leaders(
        ("688361",),
        data_dir=tmp_path,
        as_of="2026-01-30",
    )

    assert selection.status == "READY"
    assert selection.observed_symbols == 0
    assert selection.unavailable_symbols == ()
    assert selection.selected_symbols == ()
    selection.require_ready("production route")
