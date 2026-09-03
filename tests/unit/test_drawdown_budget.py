"""Contracts for ex-ante drawdown-budgeted portfolio risk."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

import pandas as pd

from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.domain.models import Position, Signal
from quantfusion.engine.universe import BacktestEngine
from quantfusion.risk.budget import (
    DrawdownBudgetController,
    RiskBook,
    portfolio_adverse_loss,
)
from quantfusion.risk.managers import RecoverableDrawdownRiskManager


class DrawdownBudgetFormulaTests(unittest.TestCase):
    def test_policy_snapshot_exposes_preregistered_parameters(self) -> None:
        snapshot = PortfolioPolicy().as_dict()

        self.assertTrue(snapshot["drawdown_budget_enabled"])
        self.assertEqual(snapshot["drawdown_budget_peak_fraction"], 0.175)
        self.assertEqual(snapshot["drawdown_budget_execution_buffer"], 0.005)
        self.assertEqual(snapshot["drawdown_budget_adverse_atr_multiple"], 1.0)
        self.assertEqual(snapshot["drawdown_budget_other_group_weight"], 0.5)
        self.assertEqual(snapshot["drawdown_budget_release_ratio"], 0.8)
        self.assertEqual(snapshot["drawdown_budget_reentry_days"], 5)
        self.assertEqual(snapshot["drawdown_budget_recovery_confirmations"], 3)
        self.assertEqual(snapshot["drawdown_budget_recovery_delta"], 0.02)
        self.assertEqual(snapshot["drawdown_budget_normal_exit"], 0.05)

    def test_lifetime_peak_survives_cycle_rearm(self) -> None:
        policy = PortfolioPolicy(
            drawdown_alert=0.10,
            confirmed_drawdown=0.12,
            emergency_drawdown=0.15,
            terminal_drawdown=0.30,
            drawdown_confirmations=1,
            rearm_trading_days=2,
            concentration_drawdown_adjustment=0.0,
        )
        manager = RecoverableDrawdownRiskManager({"max_drawdown": 0.12}, policy)
        dates = pd.date_range("2026-01-01", periods=4, freq="D").tolist()
        positions = {date: index for index, date in enumerate(dates)}

        self.assertIsNone(
            manager.check_portfolio_risk(1_000.0, "2026-01-01", dates, positions)
        )
        self.assertIsNotNone(
            manager.check_portfolio_risk(870.0, "2026-01-02", dates, positions)
        )
        self.assertIsNotNone(
            manager.check_portfolio_risk(870.0, "2026-01-03", dates, positions)
        )
        self.assertIsNone(
            manager.check_portfolio_risk(880.0, "2026-01-04", dates, positions)
        )

        self.assertEqual(manager.peak_assets, 880.0)
        self.assertEqual(manager.lifetime_peak_assets, 1_000.0)

    def test_remaining_cushion_uses_lifetime_peak_floor(self) -> None:
        controller = DrawdownBudgetController(execution_buffer_peak_fraction=0.005)

        snapshot = controller.snapshot(
            current_assets=850.0,
            lifetime_peak_assets=1_000.0,
            books=[],
        )

        self.assertAlmostEqual(snapshot.drawdown_floor, 820.0)
        self.assertAlmostEqual(snapshot.remaining_cushion, 30.0)
        self.assertAlmostEqual(snapshot.execution_buffer, 5.0)
        self.assertAlmostEqual(snapshot.available_budget, 25.0)

    def test_same_group_has_no_diversification_credit(self) -> None:
        books = [
            RiskBook("a", "optical", 10, 20.0, 20.0, 10.0, 1.0),
            RiskBook("b", "optical", 10, 16.0, 16.0, 10.0, 1.0),
            RiskBook("c", "memory", 10, 18.0, 18.0, 10.0, 1.0),
        ]

        estimate = portfolio_adverse_loss(
            books, adverse_atr_multiple=1.0, other_group_loss_weight=0.5
        )

        self.assertEqual(estimate.group_losses, {"memory": 80.0, "optical": 160.0})
        self.assertEqual(estimate.projected_loss, 200.0)
        self.assertTrue(estimate.complete)

    def test_mark_appreciation_does_not_increase_invested_risk(self) -> None:
        before = RiskBook("x", "optical", 10, 100.0, 100.0, 90.0, 1.0)
        after = RiskBook("x", "optical", 10, 200.0, 100.0, 90.0, 1.0)

        self.assertEqual(
            portfolio_adverse_loss([before]).projected_loss,
            portfolio_adverse_loss([after]).projected_loss,
        )

    def test_single_group_and_missing_evidence_fail_closed(self) -> None:
        estimate = portfolio_adverse_loss(
            [RiskBook("x", "unmapped", 10, 20.0, 10.0, 0.0, 0.0)]
        )

        self.assertEqual(estimate.projected_loss, 200.0)
        self.assertFalse(estimate.complete)
        self.assertEqual(estimate.missing_symbols, ("x",))

    def test_symbol_code_does_not_change_the_formula(self) -> None:
        ordinary = RiskBook("300308", "optical", 100, 20.0, 10.0, 15.0, 1.0)
        retained = RiskBook("688205", "optical", 100, 20.0, 10.0, 15.0, 1.0)

        self.assertEqual(
            portfolio_adverse_loss([ordinary]).projected_loss,
            portfolio_adverse_loss([retained]).projected_loss,
        )


class DrawdownBudgetStateTests(unittest.TestCase):
    @staticmethod
    def _book(*, mark: float, stop: float = 80.0, atr: float = 1.0) -> RiskBook:
        return RiskBook("x", "optical", 5, mark, 100.0, stop, atr)

    def test_warning_never_allows_new_risk(self) -> None:
        controller = DrawdownBudgetController(execution_buffer_peak_fraction=0.005)
        snapshot = controller.snapshot(1_000.0, 1_000.0, [])

        decision = controller.decide(snapshot, position=0, warning_active=True)

        self.assertEqual(decision.state, "constrained")
        self.assertEqual(decision.new_risk_capacity, 0.0)
        self.assertFalse(decision.allow_new_risk)

    def test_winner_appreciation_alone_does_not_queue_reduction(self) -> None:
        controller = DrawdownBudgetController(execution_buffer_peak_fraction=0.005)
        first = controller.snapshot(1_000.0, 1_000.0, [self._book(mark=100.0)])
        controller.decide(first, position=0, warning_active=False)
        appreciated = controller.snapshot(
            1_050.0, 1_050.0, [self._book(mark=150.0)]
        )

        decision = controller.decide(
            appreciated, position=1, warning_active=False
        )

        self.assertEqual(decision.reduction_fraction, 0.0)
        self.assertFalse(decision.risk_driver_worsened)
        self.assertFalse(decision.cushion_worsened)

    def test_worsening_atr_queues_budget_reduction(self) -> None:
        controller = DrawdownBudgetController(execution_buffer_peak_fraction=0.005)
        first = controller.snapshot(1_000.0, 1_000.0, [self._book(mark=100.0)])
        controller.decide(first, position=0, warning_active=False)
        stressed = controller.snapshot(
            900.0, 1_000.0, [self._book(mark=100.0, atr=25.0)]
        )

        decision = controller.decide(stressed, position=1, warning_active=False)

        self.assertGreater(decision.reduction_fraction, 0.0)
        self.assertTrue(decision.risk_driver_worsened)
        self.assertTrue(decision.cushion_worsened)

    def test_constrained_state_does_not_repeat_small_reductions(self) -> None:
        controller = DrawdownBudgetController(execution_buffer_peak_fraction=0.005)
        first = controller.snapshot(
            900.0, 1_000.0, [self._book(mark=100.0, atr=25.0)]
        )
        initial = controller.decide(first, position=0, warning_active=False)
        self.assertGreater(initial.reduction_fraction, 0.0)

        slightly_worse = controller.snapshot(
            895.0, 1_000.0, [self._book(mark=100.0, atr=25.2)]
        )
        repeated = controller.decide(
            slightly_worse, position=1, warning_active=False
        )

        self.assertEqual(repeated.reduction_fraction, 0.0)

    def test_underwater_reentry_requires_cooldown_and_hysteresis(self) -> None:
        controller = DrawdownBudgetController(execution_buffer_peak_fraction=0.005)
        breached = controller.snapshot(
            850.0, 1_000.0, [self._book(mark=90.0, stop=80.0, atr=6.0)]
        )
        entered = controller.decide(breached, position=0, warning_active=False)
        self.assertEqual(entered.state, "constrained")
        self.assertFalse(entered.allow_new_risk)

        safe = controller.snapshot(
            880.0, 1_000.0, [self._book(mark=100.0, stop=96.0, atr=1.0)]
        )
        for position in range(1, 7):
            decision = controller.decide(
                safe, position=position, warning_active=False
            )
            self.assertEqual(decision.state, "constrained")
            self.assertFalse(decision.allow_new_risk)

        recovered = controller.decide(safe, position=7, warning_active=False)
        self.assertEqual(recovered.state, "recovering")
        self.assertTrue(recovered.allow_new_risk)
        first_capacity = recovered.new_risk_capacity

        improved = controller.snapshot(
            900.0, 1_000.0, [self._book(mark=100.0, stop=96.0, atr=1.0)]
        )
        next_decision = controller.decide(
            improved, position=8, warning_active=False
        )
        self.assertGreater(next_decision.new_risk_capacity, first_capacity)

        warned = controller.decide(improved, position=9, warning_active=True)
        self.assertEqual(warned.state, "constrained")
        self.assertFalse(warned.allow_new_risk)

    def test_cash_book_can_recover_without_an_impossible_equity_rally(self) -> None:
        controller = DrawdownBudgetController(execution_buffer_peak_fraction=0.005)
        breached = controller.snapshot(
            950.0, 1_000.0, [self._book(mark=150.0, atr=30.0)]
        )
        controller.decide(breached, position=0, warning_active=False)
        cash = controller.snapshot(950.0, 1_000.0, [])

        for position in range(1, 7):
            decision = controller.decide(cash, position=position, warning_active=False)
            self.assertEqual(decision.state, "constrained")
        recovered = controller.decide(cash, position=7, warning_active=False)

        self.assertEqual(recovered.state, "recovering")
        self.assertGreater(recovered.new_risk_capacity, 0.0)


class DrawdownBudgetEngineBoundaryTests(unittest.TestCase):
    @staticmethod
    def _state(
        *,
        positions: dict | None = None,
        pending: list | None = None,
        close: float = 100.0,
        atr: float = 1.0,
    ) -> SimpleNamespace:
        date = pd.Timestamp("2026-01-05")
        order_events: list[dict] = []

        def record_order_event(**event: object) -> None:
            order_events.append(dict(event))

        sleeve = SimpleNamespace(
            positions=positions or {},
            _record_order_event=record_order_event,
            order_events=order_events,
        )
        return SimpleNamespace(
            sleeve=sleeve,
            data_map={
                "300308": pd.DataFrame({"close": [close]}, index=[date]),
                "688205": pd.DataFrame({"close": [close]}, index=[date]),
            },
            indicator_map={
                "300308": {"atr": pd.Series([atr], index=[date])},
                "688205": {"atr": pd.Series([atr], index=[date])},
            },
            pending=list(pending or []),
        )

    def test_warning_removes_buy_but_keeps_sell(self) -> None:
        engine = BacktestEngine()
        strategy = SimpleNamespace(name="turtle_breakout")
        buy = Signal(
            "688205", "turtle_breakout", "buy", 100, 100.0, 90.0,
            "entry", "2026-01-05", 1.0,
        )
        sell = Signal(
            "300308", "turtle_breakout", "sell", 100, 100.0,
            reason="strategy exit", signal_date="2026-01-05",
        )
        state = self._state(pending=[(buy, strategy), (sell, strategy)])
        events: list[dict] = []

        engine._apply_drawdown_budget(
            [state], pd.Timestamp("2026-01-05"), 0, 100_000.0, 100_000.0,
            warning_active=True, events=events,
        )

        self.assertEqual([item[0].direction for item in state.pending], ["sell"])
        self.assertEqual(state.sleeve.order_events[0]["event"], "blocked_drawdown_budget")
        self.assertEqual(events[-1]["state"], "constrained")
        self.assertEqual(engine._drawdown_budget_curve[-1]["date"], "2026-01-05")
        self.assertEqual(
            engine._drawdown_budget_curve[-1]["state"], "constrained"
        )
        self.assertIn(
            "projected_adverse_loss", engine._drawdown_budget_curve[-1]
        )
        self.assertIn(
            "group_adverse_losses", engine._drawdown_budget_curve[-1]
        )

    def test_buy_is_lot_clipped_to_remaining_adverse_loss_capacity(self) -> None:
        engine = BacktestEngine()
        strategy = SimpleNamespace(name="turtle_breakout")
        buy = Signal(
            "300308", "turtle_breakout", "buy", 2_000, 100.0, 90.0,
            "entry", "2026-01-05", 1.0,
        )
        state = self._state(pending=[(buy, strategy)])

        engine._apply_drawdown_budget(
            [state], pd.Timestamp("2026-01-05"), 0, 100_000.0, 100_000.0,
            warning_active=False, events=[],
        )

        self.assertEqual(state.pending[0][0].target_shares, 1_700)
        self.assertEqual(
            state.sleeve.order_events[0]["event"], "clipped_to_drawdown_budget"
        )

    def test_worsening_existing_risk_queues_next_open_reduction(self) -> None:
        engine = BacktestEngine()
        position = Position(
            "300308", "turtle_breakout", 2_000, 100.0, "2025-12-01",
            stop_loss=80.0,
        )
        state = self._state(
            positions={"300308": {"turtle_breakout": position}}, atr=10.0
        )
        events: list[dict] = []

        engine._apply_drawdown_budget(
            [state], pd.Timestamp("2026-01-05"), 0, 90_000.0, 100_000.0,
            warning_active=False, events=events,
        )

        signal, strategy = state.pending[0]
        self.assertEqual(signal.direction, "sell")
        self.assertEqual(signal.signal_date, "2026-01-05")
        self.assertEqual(signal.reason.split(":")[0], "drawdown_budget_reduction")
        self.assertIsNone(strategy)
        self.assertGreaterEqual(signal.target_shares, 100)
        self.assertEqual(events[-1]["event"], "drawdown_budget_state")

    def test_zero_budget_ratio_is_json_safe_in_evidence_curve(self) -> None:
        engine = BacktestEngine()
        position = Position(
            "300308", "turtle_breakout", 100, 100.0, "2025-12-01",
            stop_loss=80.0,
        )
        state = self._state(
            positions={"300308": {"turtle_breakout": position}}
        )

        engine._apply_drawdown_budget(
            [state], pd.Timestamp("2026-01-05"), 0, 82_000.0, 100_000.0,
            warning_active=False, events=[],
        )

        self.assertIsNone(
            engine._drawdown_budget_curve[-1]["projected_loss_ratio"]
        )


if __name__ == "__main__":
    unittest.main()
