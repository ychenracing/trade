"""Non-economic controls for the frozen C6 S concentration action."""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from quantfusion.risk.overlay.policy import CrossMarketOverlay


def _frame(last_open: float = 9.8) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=5)
    return pd.DataFrame(
        {
            "open": [12.0, 11.5, 11.0, 10.0, last_open],
            "high": [12.0, 11.5, 11.0, 10.0, last_open],
            "low": [12.0, 11.5, 11.0, 10.0, last_open],
            "close": [12.0, 11.5, 11.0, 10.0, last_open],
            "volume": [1_000_000] * 5,
        },
        index=index,
    )


def _fixture(*, include_cross_cluster: bool = True):
    frame = _frame()
    positions = {
        symbol: {
            "trend": SimpleNamespace(
                shares=9_000,
                entry_price=11.0,
                highest_close_since_entry=12.0,
                entry_date="2025-12-01",
            )
        }
        for symbol in ("601869", "002384")
    }
    sleeve = SimpleNamespace(
        sleeve_name="fast",
        positions=positions,
        cash=20_000.0,
        _regime_state="TREND",
        _regime_transition_days=0,
        _remaining_adv_capacity=lambda *args: 100_000,
        _opening_limit_state=lambda *args: None,
    )
    state = SimpleNamespace(
        sleeve=sleeve,
        data_map={symbol: frame for symbol in positions},
        all_dates=list(frame.index),
        pending=[],
    )
    risk_symbols = ["300308", "300502", "601869", "002384"]
    if include_cross_cluster:
        risk_symbols += ["688008", "688072", "002409", "688256"]
    overlay = CrossMarketOverlay(
        risk_frames={symbol: frame for symbol in risk_symbols}
    )
    overlay._risk_level = 1
    overlay._risk_level_day = 3
    overlay._assets_history = [220_000.0, 215_000.0, 210_000.0]
    overlay._c6_s_enabled = True
    return overlay, state


def test_s_trims_at_the_first_qualified_prelegacy_close() -> None:
    overlay, state = _fixture()
    actions = overlay.evaluate(
        [state],
        pd.Timestamp("2026-01-04"),
        3,
        200_000.0,
        210_526.31578947368,
        lambda symbol: 0.0 if symbol == "601869" else 1.0,
    )
    assert len(actions) == 1
    assert actions[0].symbol == "601869"
    assert actions[0].shares == 2_000
    assert actions[0].reason == "concentration_trim"
    assert state.pending == []


def test_s_fails_closed_when_independent_coverage_is_incomplete() -> None:
    overlay, state = _fixture(include_cross_cluster=False)
    actions = overlay.evaluate(
        [state],
        pd.Timestamp("2026-01-04"),
        3,
        200_000.0,
        210_526.31578947368,
        None,
    )
    assert actions == ()
    assert overlay.c6_s_evidence["coverage"]["coverage_passed"] is False
    assert overlay.c6_s_evidence["early_sell_required"] is False


def test_dominant_cluster_tie_break_is_label_order_and_input_invariant() -> None:
    overlay, state = _fixture()
    state.sleeve.positions.update(
        {
            "688072": state.sleeve.positions["601869"],
            "688082": state.sleeve.positions["002384"],
        }
    )
    state.data_map.update({"688072": _frame(), "688082": _frame()})
    first = overlay.observe_c6_s_evidence(
        [state], pd.Timestamp("2026-01-04"), 3,
        {symbol: 10.0 for symbol in state.sleeve.positions},
        400_000.0, 0.05, None,
    )
    state.sleeve.positions = dict(reversed(state.sleeve.positions.items()))
    second = overlay.observe_c6_s_evidence(
        [state], pd.Timestamp("2026-01-04"), 3,
        {symbol: 10.0 for symbol in state.sleeve.positions},
        400_000.0, 0.05, None,
    )
    assert first["worst_cluster"] == "equipment"
    assert second["worst_cluster"] == first["worst_cluster"]
