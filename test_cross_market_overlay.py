"""cross_market_overlay 穿越牛熊叠加层单元测试。

覆盖灾变止损的两遍扫描语义：同一标的在多个袖套中的全部持仓必须在
触发当日一起退出，冷却期只抑制未来重新入场，绝不阻断当日的兄弟仓位退出。
"""

from __future__ import annotations

import unittest

import pandas as pd

from cross_market_overlay import CATASTROPHE_COOLDOWN_DAYS, CrossMarketOverlay


class _Pos:
    """最小持仓：仅暴露叠加层读取的字段。"""

    def __init__(self, shares: int, peak: float, entry: float) -> None:
        self.shares = shares
        self.highest_close_since_entry = peak
        self.entry_price = entry


class _Sleeve:
    def __init__(self, positions: dict) -> None:
        self.positions = positions


class _State:
    def __init__(self, sleeve: _Sleeve, data_map: dict) -> None:
        self.sleeve = sleeve
        self.data_map = data_map
        self.pending: list = []


def _frame(close: float) -> pd.DataFrame:
    idx = pd.to_datetime(["2026-01-05", "2026-01-06"])
    return pd.DataFrame(
        {
            "open": [close, close],
            "high": [close, close],
            "low": [close, close],
            "close": [close, close],
            "volume": [1_000_000, 1_000_000],
        },
        index=idx,
    )


class CatastropheStopTests(unittest.TestCase):
    """灾变止损机制行为。"""

    def _trigger(self, state_peaks: list[tuple[float, float]]) -> tuple[list[_State], CrossMarketOverlay, list]:
        date = pd.Timestamp("2026-01-06")
        frame = _frame(70.0)  # 收盘 70，峰值 100 -> -30% >= 28%
        data_map = {"AAA": frame}
        states = [
            _State(_Sleeve({"AAA": {name: _Pos(100, peak, entry)}}), dict(data_map))
            for name, (peak, entry) in zip(("fast", "base", "slow"), state_peaks)
        ]
        overlay = CrossMarketOverlay()
        overlay.on_day(
            states, date, date_pos=1,
            assets=2_000_000, peak=2_000_000, scoring_fn=None,
        )
        sells = [sig for st in states for sig, _ in st.pending]
        return states, overlay, sells

    def test_sibling_positions_exit_same_day(self) -> None:
        """同一标的三袖套持仓必须在触发当日全部退出。"""
        _, overlay, sells = self._trigger([(100.0, 90.0)] * 3)
        self.assertEqual(len(sells), 3)
        for sig in sells:
            self.assertEqual(sig.symbol, "AAA")
            self.assertEqual(sig.direction, "sell")
            self.assertEqual(sig.reason.split(":")[0], "catastrophe_stop")
        # 每个标的只记录一条事件，避免重复记账。
        self.assertEqual(len(overlay.events), 1)

    def test_normal_pullback_does_not_trigger(self) -> None:
        """健康回调（-20%）不触发，保持 bull-silent。"""
        date = pd.Timestamp("2026-01-06")
        frame = _frame(80.0)  # 峰值 100 -> -20% < 28%
        states = [
            _State(_Sleeve({"AAA": {"fast": _Pos(100, 100.0, 90.0)}}), {"AAA": frame})
        ]
        overlay = CrossMarketOverlay()
        overlay.on_day(states, date, date_pos=1, assets=2_000_000, peak=2_000_000, scoring_fn=None)
        self.assertEqual(states[0].pending, [])
        self.assertEqual(overlay.events, [])

    def test_cooldown_suppresses_future_reentry_only(self) -> None:
        """冷却期抑制后续日期的重复退出，但当日兄弟仓位仍全部退出。"""
        states, overlay, _ = self._trigger([(100.0, 90.0)] * 2)
        self.assertEqual(len(states[0].pending), 1)
        self.assertEqual(len(states[1].pending), 1)
        # 冷却到期日前再次检查：同一标的处于冷却期，不再生成卖出。
        future = pd.Timestamp("2026-01-06") + pd.Timedelta(days=1)
        frame = _frame(60.0)
        for st in states:
            st.pending = []
            st.data_map["AAA"] = frame
        overlay.on_day(
            list(states), future, date_pos=2,
            assets=2_000_000, peak=2_000_000, scoring_fn=None,
        )
        self.assertEqual(states[0].pending, [])
        self.assertEqual(states[1].pending, [])
        # 冷却期数值正确。
        self.assertEqual(overlay._catastrophe_cooldown["AAA"], 1 + CATASTROPHE_COOLDOWN_DAYS)


if __name__ == "__main__":
    unittest.main()
