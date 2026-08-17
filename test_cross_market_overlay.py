"""cross_market_overlay 穿越牛熊叠加层单元测试。

覆盖灾变止损的两遍扫描语义：同一标的在多个袖套中的全部持仓必须在
触发当日一起退出，冷却期只抑制未来重新入场，绝不阻断当日的兄弟仓位退出。
"""

from __future__ import annotations

import unittest

import pandas as pd

from cross_market_overlay import CATASTROPHE_COOLDOWN_DAYS, CrossMarketOverlay
from quantfusion.risk.overlay.adapter import (
    apply_risk_actions,
    resolve_risk_actions,
)
from quantfusion.risk.overlay.models import RiskAction


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

    def test_evaluation_does_not_mutate_pending_before_adaptation(self) -> None:
        """风险政策先返回不可变动作，执行适配器再写入 T+1 队列。"""
        date = pd.Timestamp("2026-01-06")
        state = _State(
            _Sleeve({"AAA": {"fast": _Pos(100, 100.0, 90.0)}}),
            {"AAA": _frame(70.0)},
        )
        overlay = CrossMarketOverlay()
        actions = overlay.evaluate_actions(
            [state],
            date,
            date_pos=1,
            assets=2_000_000,
            peak=2_000_000,
            scoring_fn=None,
        )
        self.assertEqual(state.pending, [])
        self.assertEqual([action.reason for action in actions], ["catastrophe_stop"])
        apply_risk_actions(actions, [state])
        self.assertEqual(len(state.pending), 1)

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

    def test_dedicated_weak_route_is_monitor_only(self) -> None:
        """生产弱市路由持有执行权时，叠加层保留状态但不重复卖出。"""
        date = pd.Timestamp("2026-01-06")
        frame = _frame(70.0)
        state = _State(
            _Sleeve({"AAA": {"fast": _Pos(100, 100.0, 90.0)}}),
            {"AAA": frame},
        )
        overlay = CrossMarketOverlay()
        overlay.set_outer_route("weak", date)
        overlay.on_day(
            [state], date, date_pos=1,
            assets=1_700_000, peak=2_000_000, scoring_fn=None,
        )
        self.assertEqual(state.pending, [])
        snapshot = overlay.state_snapshot()
        self.assertEqual(snapshot["execution_owner"], "production_route")
        self.assertEqual(snapshot["outer_route"], "weak")

    def test_fail_closed_when_material_unmapped_holdings(self) -> None:
        """报告 P1-5：持仓含大量未映射子行业标的时，集中度风控失败关闭。"""
        # 300308 与 300502 同属 optical 簇；999999 未映射到任何子行业。
        frame1 = _frame(100.0)
        frame2 = _frame(50.0)
        frame3 = _frame(80.0)
        states = [
            _State(
                _Sleeve({
                    "300308": {"fast": _Pos(1000, 100.0, 90.0)},
                    "300502": {"fast": _Pos(1000, 50.0, 45.0)},
                    "999999": {"fast": _Pos(1000, 80.0, 70.0)},
                }),
                {"300308": frame1, "300502": frame2, "999999": frame3},
            )
        ]
        overlay = CrossMarketOverlay()
        overlay._assets_history = [155_000, 153_000, 151_000, 150_000]
        prices = {"300308": 100.0, "300502": 50.0, "999999": 80.0}
        # 未映射 8 万元 / 总资产 23 万元 = 34.8% >= 5% 阈值 -> 失败关闭。
        overlay._apply_concentration_guard(
            states, prices, "2026-01-06", scoring_fn=None,
            drawdown=0.12, assets=230_000,
        )
        self.assertEqual(states[0].pending, [])
        self.assertTrue(
            any(e["event"] == "concentration_guard_fail_closed" for e in overlay.events)
        )

    def test_ignores_negligible_unmapped_tail(self) -> None:
        """报告 P1-5：未映射持仓占比很小（< 5%）时仍正常裁剪集中簇。"""
        frame1 = _frame(100.0)
        frame2 = _frame(50.0)
        frame3 = _frame(80.0)
        states = [
            _State(
                _Sleeve({
                    "300308": {"fast": _Pos(1000, 100.0, 90.0)},
                    "300502": {"fast": _Pos(1000, 50.0, 45.0)},
                    "999999": {"fast": _Pos(100, 80.0, 70.0)},
                }),
                {"300308": frame1, "300502": frame2, "999999": frame3},
            )
        ]
        overlay = CrossMarketOverlay()
        overlay._assets_history = [175_000, 173_000, 171_000, 170_000]
        prices = {"300308": 100.0, "300502": 50.0, "999999": 80.0}
        # 未映射 8000 / 总资产 17 万 = 4.7% < 5% 阈值被忽略；optical 簇 15 万/17 万 = 88% > 80%。
        overlay._apply_concentration_guard(
            states, prices, "2026-01-06", scoring_fn=None,
            drawdown=0.12, assets=170_000,
        )
        # optical 市值 15 万 / 17 万 = 88% > 80% 上限，应裁剪簇内最弱股。
        sells = overlay.pending_actions
        self.assertTrue(any(
            action.reason == "concentration_trim" for action in sells
        ))

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


class ConcentrationGuardTests(unittest.TestCase):
    """报告 4.8 行业集中度 / 相关性簇风控的 bull-silent 行为。"""

    def test_trims_overconcentrated_cluster_during_drawdown(self) -> None:
        """单一子行业簇超过 80% 且组合回撤+下跌时，裁剪簇内最弱股。"""
        # 300308 与 300502 同属 optical 簇。
        frame1 = _frame(100.0)
        frame2 = _frame(50.0)
        states = [
            _State(
                _Sleeve({
                    "300308": {"fast": _Pos(1000, 100.0, 90.0)},
                    "300502": {"fast": _Pos(1000, 50.0, 45.0)},
                }),
                {"300308": frame1, "300502": frame2},
            )
        ]
        overlay = CrossMarketOverlay()
        # 组合正处回撤（12%）且近期净值连续下跌（bull-silent 前提）。
        overlay._assets_history = [155_000, 153_000, 151_000, 150_000]
        prices = {"300308": 100.0, "300502": 50.0}
        overlay._apply_concentration_guard(
            states, prices, "2026-01-06", scoring_fn=None,
            drawdown=0.12, assets=150_000,
        )
        sells = overlay.pending_actions
        # 无评分函数时按代码字典序取簇内最弱股（300308），裁剪其超额敞口。
        self.assertTrue(any(action.symbol == "300308" for action in sells))
        self.assertTrue(any(action.reason == "concentration_trim" for action in sells))
        self.assertTrue(overlay.events)

    def test_bull_silent_no_trim_when_no_drawdown(self) -> None:
        """组合未回撤（牛市）时，即使集中也不裁剪。"""
        frame1 = _frame(100.0)
        frame2 = _frame(50.0)
        states = [
            _State(
                _Sleeve({
                    "300308": {"fast": _Pos(1000, 100.0, 90.0)},
                    "300502": {"fast": _Pos(1000, 50.0, 45.0)},
                }),
                {"300308": frame1, "300502": frame2},
            )
        ]
        overlay = CrossMarketOverlay()
        overlay._assets_history = [150_000, 150_000, 150_000, 150_000]
        prices = {"300308": 100.0, "300502": 50.0}
        overlay._apply_concentration_guard(
            states, prices, "2026-01-06", scoring_fn=None,
            drawdown=0.0, assets=150_000,
        )
        self.assertEqual(states[0].pending, [])
        self.assertEqual(overlay.events, [])


def _volatile_frame(closes: list[float]) -> pd.DataFrame:
    """Build an OHLC frame whose close series follows ``closes``.

    The high/low bands are widened so the Wilder ATR is realistic (non-zero),
    which is what the layered ATR chandelier needs to be meaningful.
    """
    idx = pd.date_range("2025-11-01", periods=len(closes), freq="D")
    return pd.DataFrame(
        {
            "open": [c * 0.99 for c in closes],
            "high": [c * 1.05 for c in closes],
            "low": [c * 0.95 for c in closes],
            "close": closes,
            "volume": [1_000_000] * len(closes),
        },
        index=idx,
    )


class CooldownBlocksBuysTests(unittest.TestCase):
    """报告 P0-4：灾变冷却必须阻断所有买入路径，而非仅抑制重复退出。"""

    def _make_state(self, symbol: str, direction: str, shares: int = 100) -> _State:
        frame = _frame(70.0)
        state = _State(_Sleeve({}), {"AAA": frame})
        signal = _Signal(symbol, direction, shares)
        state.pending = [(signal, None)]
        return state

    def test_buy_blocked_during_cooldown(self) -> None:
        """冷却期内同一标的新买单必须被阻断，且事件可审计。"""
        overlay = CrossMarketOverlay()
        overlay._catastrophe_cooldown["AAA"] = 10  # 冷却到第10个交易位置
        states = self._make_state("AAA", "buy")
        overlay.block_cooldown_buys([states], pd.Timestamp("2026-01-06"), date_pos=5)
        self.assertEqual(states.pending, [])
        self.assertTrue(
            any(e["event"] == "cooldown_blocked_buy" for e in overlay.events)
        )

    def test_buy_allowed_after_cooldown_expiry(self) -> None:
        """冷却到期后，新买单不再被阻断。"""
        overlay = CrossMarketOverlay()
        overlay._catastrophe_cooldown["AAA"] = 10
        states = self._make_state("AAA", "buy")
        overlay.block_cooldown_buys([states], pd.Timestamp("2026-01-06"), date_pos=12)
        self.assertEqual(len(states.pending), 1)

    def test_sell_never_blocked_by_cooldown(self) -> None:
        """冷却只阻断买入，同一标的的卖出（例如兄弟袖套退出）不受影响。"""
        overlay = CrossMarketOverlay()
        overlay._catastrophe_cooldown["AAA"] = 10
        states = self._make_state("AAA", "sell")
        overlay.block_cooldown_buys([states], pd.Timestamp("2026-01-06"), date_pos=2)
        self.assertEqual(len(states.pending), 1)


class _Signal:
    def __init__(self, symbol: str, direction: str, shares: int) -> None:
        self.symbol = symbol
        self.direction = direction
        self.shares = shares


class LayeredStopIndependentTriggerTests(unittest.TestCase):
    """报告 P0-1：分层止损各保护线独立触发，不再被 28% 峰值回撤统一门槛抵消。"""

    def _run(
        self, pos_peak: float, pos_entry: float, price: float, risk_level: int,
        assets: float = 90_000, peak: float = 100_000,
    ) -> list:
        """Run overlay on a single declining position and return sell signals.

        ``assets`` defaults to 90k against a 100k peak (10% account drawdown) so
        the tighter layered lines can arm when ``risk_level`` >= 1; a clean-bull
        caller passes assets == peak to keep the drawdown gate closed.
        """
        closes = [price] * 40
        frame = _volatile_frame(closes)
        states = [
            _State(
                _Sleeve({"AAA": {"fast": _Pos(100, pos_peak, pos_entry)}}),
                {"AAA": frame},
            )
        ]
        overlay = CrossMarketOverlay(enable_early_sector_risk=False)
        overlay._risk_level = risk_level
        overlay.on_day(
            states, frame.index[-1], date_pos=40,
            assets=assets, peak=peak, scoring_fn=None,
        )
        return [sig for st in states for sig, _ in st.pending]

    def test_cost_stop_triggers_below_28pct_peak_drawdown(self) -> None:
        """成本止损：亏损18%、峰值回撤不足28%时也应退出（旧逻辑不会退出）。"""
        # entry=peak=100, price=80 -> -20% from entry AND peak (peak_drop 20% < 28%).
        # 风险等级=1 且账户回撤10% -> 成本止损(82)武装；价格80跌破82 -> 以 cost_stop 退出。
        sells = self._run(pos_peak=100.0, pos_entry=100.0, price=80.0, risk_level=1)
        self.assertTrue(sells)
        self.assertEqual(sells[0].symbol, "AAA")
        self.assertEqual(sells[0].reason.split(":")[0], "cost_stop")

    def test_bull_silent_20pct_pullback_at_risk0_no_exit(self) -> None:
        """风险等级0时，20%健康回调不退出（只保留28%灾变地板）。"""
        # entry=peak=100, price=80 -> peak_drop 20% < 28%，且风险等级0未启用分层线。
        sells = self._run(pos_peak=100.0, pos_entry=100.0, price=80.0, risk_level=0)
        self.assertEqual(sells, [])

    def test_bull_silent_no_arm_when_account_at_peak_even_with_risk_warning(self) -> None:
        """账户仍在峰值（未回撤）时，即使风险等级>=1也不武装紧致线（bull-silent）。"""
        # assets==peak -> drawdown 0 < LAYERED_ARM_PORTFOLIO_DRAWDOWN。
        sells = self._run(
            pos_peak=100.0, pos_entry=100.0, price=80.0, risk_level=1,
            assets=100_000, peak=100_000,
        )
        self.assertEqual(sells, [])

    def test_catastrophe_still_exits_above_28pct(self) -> None:
        """28%灾变回撤始终启用，风险等级0也会全部退出。"""
        # entry=peak=100, price=70 -> peak_drop 30% >= 28% -> catastrophe_stop。
        sells = self._run(pos_peak=100.0, pos_entry=100.0, price=70.0, risk_level=0)
        self.assertTrue(sells)
        self.assertEqual(sells[0].reason.split(":")[0], "catastrophe_stop")


class AtrReuseTests(unittest.TestCase):
    """报告 P0-2：叠加层 ATR 复用统一 Indicators.atr（逐日前收盘价），与手工一致。"""

    def test_atr_matches_manual_wilder_shift(self) -> None:
        """``_atr_at`` 与统一 ``Indicators.atr``（逐日前收盘价）样本一致。"""
        closes = [100.0] + [
            100.0 + 8.0 * ((i * 7) % 5 - 2) for i in range(1, 60)
        ]
        frame = _volatile_frame(closes)
        overlay = CrossMarketOverlay()
        loc = len(closes) - 1
        atr_overlay = overlay._atr_at(frame, loc)
        # 统一口径：Indicators.atr 使用逐日 close.shift(1)，是权威实现。
        from quant_fusion import Indicators
        atr_core = float(Indicators.atr(frame, period=20, method="wilder").iloc[loc])
        self.assertTrue(atr_overlay > 0)
        self.assertAlmostEqual(atr_overlay, atr_core, places=5)

    def test_atr_differs_from_fixed_prev_close_bug(self) -> None:
        """证明旧实现（固定前收盘价）会被修复：结果与正确 ATR 不同。"""
        # 构造一个前收盘价法会产生偏差的序列：后段价格大幅上移，
        # 用一个固定前收盘价与逐日 shift(1) 会得到不同真实波幅。
        closes = [100.0] * 30 + [150.0] * 30
        frame = _volatile_frame(closes)
        overlay = CrossMarketOverlay()
        loc = len(closes) - 1
        atr_overlay = overlay._atr_at(frame, loc)
        # 旧 bug 实现：固定取 close.iloc[loc-1] 作为每个 TR 的前收盘。
        high = frame["high"].astype(float)
        low = frame["low"].astype(float)
        close = frame["close"].astype(float)
        fixed_prev = float(close.iloc[loc - 1])
        tr_fixed = pd.concat(
            [
                high.iloc[: loc + 1] - low.iloc[: loc + 1],
                (high.iloc[: loc + 1] - fixed_prev).abs(),
                (low.iloc[: loc + 1] - fixed_prev).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr_bug = float(tr_fixed.ewm(alpha=1.0 / 20.0, adjust=False).mean().iloc[-1])
        self.assertNotAlmostEqual(atr_overlay, atr_bug, places=5)


class IndependentRiskBasketTests(unittest.TestCase):
    """The master basket must work even when the user trades a tiny pool."""

    @staticmethod
    def _declining_frame() -> pd.DataFrame:
        dates = pd.bdate_range("2026-01-02", periods=25)
        closes = [100.0] * 20 + [100.0, 94.0, 88.0, 82.0, 76.0]
        return pd.DataFrame(
            {
                "open": closes,
                "high": [value * 1.01 for value in closes],
                "low": [value * 0.99 for value in closes],
                "close": closes,
                "volume": [1_000_000] * len(closes),
            },
            index=dates,
        )

    def test_continuous_deterioration_escalates_without_recovery_reshock(self) -> None:
        frame = self._declining_frame()
        risk_frames = {
            symbol: frame.copy()
            for symbol in (
                "300308", "300502", "688008", "603986", "688072", "688082"
            )
        }
        state = _State(
            _Sleeve(
                {
                    "300308": {"fast": _Pos(100, 100.0, 90.0)},
                    "300502": {"fast": _Pos(100, 100.0, 90.0)},
                }
            ),
            {},
        )
        overlay = CrossMarketOverlay(risk_frames=risk_frames)
        overlay._assets_history = [100_000.0]
        for offset, loc in enumerate((22, 23, 24)):
            overlay.on_day(
                [state],
                frame.index[loc],
                date_pos=loc,
                assets=95_000.0 - offset * 2_500.0,
                peak=100_000.0,
                scoring_fn=None,
            )
        self.assertGreaterEqual(overlay.risk_level, 2)
        self.assertTrue(overlay.blocks_new_positions)
        loaded_events = [
            event for event in overlay.events if event.get("event") == "sector_risk_level"
        ]
        self.assertTrue(any(event["continuous_stress_days"] >= 3 for event in loaded_events))

    def test_confirmed_risk_blocks_new_entries_and_pyramids_but_not_sells(self) -> None:
        state = _State(_Sleeve({}), {})
        state.pending = [
            (_RiskSignal("NEW", "fast", "buy", 100, "entry"), None),
            (_RiskSignal("HELD", "fast", "buy", 100, "pyramid"), None),
            (_RiskSignal("HELD", "fast", "sell", 100, "exit"), None),
        ]
        overlay = CrossMarketOverlay()
        overlay._risk_level = 2
        overlay.block_risk_buys(
            [state], pd.Timestamp("2026-01-06"), {"HELD"}
        )
        self.assertEqual(len(state.pending), 1)
        self.assertEqual(state.pending[0][0].direction, "sell")


class _RiskSignal:
    """Overlay-style sell signal (strategy is None) for P1-1 consolidation tests."""

    def __init__(self, symbol: str, strategy_name: str, direction: str,
                 target_shares: int, reason: str) -> None:
        self.symbol = symbol
        self.strategy_name = strategy_name
        self.direction = direction
        self.target_shares = target_shares
        self.reason = reason


class UnifiedRiskPriorityTests(unittest.TestCase):
    """报告 P1-1：多个风险机制同一天对同一标的下单时，只保留最高优先级动作。"""

    @staticmethod
    def _action(
        symbol: str,
        strategy_name: str,
        shares: int,
        reason: str,
        priority: int,
    ) -> RiskAction:
        return RiskAction(
            symbol=symbol,
            strategy_name=strategy_name,
            shares=shares,
            price=10.0,
            signal_date="2026-01-06",
            reason=reason,
            priority=priority,
        )

    def test_full_exit_wins_over_concentration_trim(self) -> None:
        """同一标的同一策略：灾变全退（高优先级）应覆盖集中度减仓。"""
        winners, suppressed = resolve_risk_actions(
            (
                self._action("AAA", "fast", 100, "concentration_trim", 40),
                self._action("AAA", "fast", 100, "cost_stop", 90),
            )
        )
        self.assertEqual([action.reason for action in winners], ["cost_stop"])
        self.assertEqual(
            [action.reason for action in suppressed], ["concentration_trim"]
        )

    def test_sibling_sleeve_exits_all_survive(self) -> None:
        """同一标的三个袖套的同日全退（兄弟退出）互不冲突，全部保留。"""
        actions = tuple(
            self._action("AAA", strategy, 100, "catastrophe_stop", 100)
            for strategy in ("fast", "base", "slow")
        )
        winners, suppressed = resolve_risk_actions(actions)
        self.assertEqual(winners, actions)
        self.assertEqual(suppressed, ())

    def test_higher_priority_and_larger_target_wins_on_tie(self) -> None:
        """同优先级时，目标股数更大（更保守）的动作胜出。"""
        winners, _ = resolve_risk_actions(
            (
                self._action("BBB", "base", 30, "sector_risk_trim", 60),
                self._action("BBB", "base", 80, "sector_risk_trim", 60),
            )
        )
        self.assertEqual(winners[0].shares, 80)

    def test_strategy_level_sells_untouched(self) -> None:
        """策略普通卖出（strategy 非 None）不受叠加层优先级合并影响。"""
        class _DummyStrat:
            pass

        state = _State(_Sleeve({}), {})
        ordinary = (
            _RiskSignal("CCC", "fast", "sell", 50, "exit_signal"),
            _DummyStrat(),
        )
        state.pending = [ordinary]
        apply_risk_actions(
            (self._action("AAA", "fast", 100, "catastrophe_stop", 100),),
            state,
        )
        self.assertIs(state.pending[0], ordinary)
        self.assertEqual(len(state.pending), 2)


if __name__ == "__main__":
    unittest.main()
