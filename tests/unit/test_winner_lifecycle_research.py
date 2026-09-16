"""Contracts for causal Trade-native winner lifecycle research helpers."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from quantfusion.research.winner_lifecycle import (
    classify_winner_lifecycle,
    should_defer_atr_trailing_exit,
)


def _position(*, entry: float = 100.0, peak: float = 150.0) -> SimpleNamespace:
    return SimpleNamespace(entry_price=entry, highest_close_since_entry=peak)


def test_existing_profit_lock_defines_strategic_winner_without_new_threshold() -> None:
    state = classify_winner_lifecycle(
        _position(),
        close=130.0,
        cfg={"profit_lock_activation": 0.30, "profit_lock_giveback": 0.18},
    )

    assert state.strategic_winner is True
    assert state.profit_lock_active is True
    assert state.profit_lock_intact is True
    assert state.peak_gain == pytest.approx(0.5)
    assert state.current_gain == pytest.approx(0.3)


def test_atr_exit_can_only_be_deferred_while_existing_profit_lock_is_intact() -> None:
    cfg = {"profit_lock_activation": 0.30, "profit_lock_giveback": 0.18}
    position = _position()

    assert should_defer_atr_trailing_exit(
        position, close=130.0, reason="ATR trailing stop@132.00", cfg=cfg
    )
    assert not should_defer_atr_trailing_exit(
        position, close=122.0, reason="ATR trailing stop@132.00", cfg=cfg
    )


def test_non_atr_and_non_winner_exits_remain_authoritative() -> None:
    cfg = {"profit_lock_activation": 0.30, "profit_lock_giveback": 0.18}

    assert not should_defer_atr_trailing_exit(
        _position(), close=130.0, reason="hard stop15%", cfg=cfg
    )
    assert not should_defer_atr_trailing_exit(
        _position(peak=125.0),
        close=120.0,
        reason="ATR trailing stop@121.00",
        cfg=cfg,
    )
