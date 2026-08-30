"""T+1 sellability semantics for real-account holding recommendations."""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest.mock import patch

import pandas as pd

from quantfusion.account.models import AccountPosition, AccountSnapshot
from quantfusion.application import account_scan
from quantfusion.regime.models import (
    DeploymentDecision,
    LeaderSelection,
    RegimeEvidence,
)


class AccountSignalSellabilityTests(unittest.TestCase):
    """Holding actions never recommend more than the known T+1 quantity."""

    HELD = "300308"
    OTHER = "300502"
    AS_OF = "2026-02-01"
    UNKNOWN_REASON = (
        "T+1 可卖数量未知，必须人工核验，不得把当前总持仓当作可执行卖出数量。"
    )

    @staticmethod
    def _frame(*, close: float) -> pd.DataFrame:
        dates = pd.bdate_range("2025-09-01", periods=100)
        return pd.DataFrame(
            {
                "open": close,
                "close": close,
                "high": close,
                "low": close,
                "volume": 1_000_000.0,
            },
            index=dates,
        )

    @staticmethod
    def _indicators(frame: pd.DataFrame) -> dict[str, pd.Series]:
        return {
            "atr": pd.Series(1.0, index=frame.index),
            "ma_short": pd.Series(101.0, index=frame.index),
            "ma_long": pd.Series(99.0, index=frame.index),
        }

    @classmethod
    def _decision(
        cls,
        name: str,
        *,
        selected: tuple[str, ...] = (),
    ) -> DeploymentDecision:
        leaders = (
            LeaderSelection(
                as_of=cls.AS_OF,
                requested_symbols=(cls.HELD, cls.OTHER),
                observed_symbols=len(selected),
                selected_symbols=selected,
                selected_returns=tuple(0.1 for _ in selected),
            )
            if name == "positive_momentum_hold"
            else None
        )
        return DeploymentDecision(
            name=name,
            boundary="2026-01-31",
            reason="unit test",
            regime=RegimeEvidence(
                as_of="2026-01-31",
                regime="weak",
                observations=(),
            ),
            leaders=leaders,
        )

    @classmethod
    def _snapshot(
        cls,
        sellable_shares: int | None,
        *,
        cash: float = 0.0,
    ) -> AccountSnapshot:
        return AccountSnapshot(
            cash=cash,
            peak_equity=1_000.0,
            positions=(
                AccountPosition(
                    symbol=cls.HELD,
                    shares=100,
                    sellable_shares=sellable_shares,
                    avg_cost=100.0,
                    entry_date="",
                ),
            ),
        )

    def _run_priced(
        self,
        *,
        sellable_shares: int | None,
        decision: DeploymentDecision,
        close: float,
    ) -> dict:
        engine = account_scan.AccountSignalEngine(
            cache_dir="unused",
            regime_data_dir="unused",
        )
        frame = self._frame(close=close)
        with (
            patch.object(
                account_scan.data_contracts,
                "refresh_regime_indices",
                return_value={},
            ),
            patch.object(
                account_scan.RegimeAdaptiveBacktestEngine,
                "decide_current",
                return_value=decision,
            ),
            patch.object(engine, "_frame", return_value=frame),
            patch.object(
                account_scan.BacktestEngine,
                "config_for_symbol",
                return_value={
                    "hard_stop": 0.15,
                    "profit_lock_activation": 0.20,
                    "trail_atr_mult": 4.0,
                },
            ),
            patch.object(
                account_scan.Indicators,
                "compute_all",
                return_value=self._indicators(frame),
            ),
        ):
            return engine.run(
                self._snapshot(sellable_shares),
                {self.HELD: "持仓", self.OTHER: "候选"},
                as_of=self.AS_OF,
            )

    @staticmethod
    def _held_action(result: dict) -> dict:
        return next(
            item for item in result["actions"] if item["symbol"] == "300308"
        )

    def test_sell_and_reduce_apply_known_t1_quantities(self) -> None:
        scenarios = (
            ("SELL", self._decision("frozen_trend_engine"), 80.0),
            ("REDUCE_REVIEW", self._decision("cash_preservation"), 100.0),
        )
        quantities = (
            (100, 100, 0, "EXECUTABLE"),
            (40, 40, 60, "PARTIALLY_T1_BLOCKED"),
            (0, 0, 100, "T1_BLOCKED"),
        )

        for expected_action, decision, close in scenarios:
            for sellable, recommended, blocked, status in quantities:
                with self.subTest(action=expected_action, sellable=sellable):
                    action = self._held_action(
                        self._run_priced(
                            sellable_shares=sellable,
                            decision=decision,
                            close=close,
                        )
                    )
                    self.assertEqual(action["action"], expected_action)
                    self.assertEqual(action["shares"], 100)
                    self.assertEqual(action["sellable_shares"], sellable)
                    self.assertEqual(action["recommended_shares"], recommended)
                    self.assertEqual(action["blocked_shares"], blocked)
                    self.assertEqual(action["execution_status"], status)
                    self.assertLessEqual(action["recommended_shares"], sellable)

    def test_sell_and_reduce_require_manual_check_when_sellable_is_unknown(self) -> None:
        scenarios = (
            ("SELL", self._decision("frozen_trend_engine"), 80.0),
            ("REDUCE_REVIEW", self._decision("cash_preservation"), 100.0),
        )

        for expected_action, decision, close in scenarios:
            with self.subTest(action=expected_action):
                action = self._held_action(
                    self._run_priced(
                        sellable_shares=None,
                        decision=decision,
                        close=close,
                    )
                )
                self.assertEqual(action["action"], expected_action)
                self.assertEqual(action["shares"], 100)
                self.assertIsNone(action["sellable_shares"])
                self.assertIsNone(action["recommended_shares"])
                self.assertIsNone(action["blocked_shares"])
                self.assertEqual(action["execution_status"], "SELLABLE_UNKNOWN")
                self.assertTrue(action["reason"].endswith(self.UNKNOWN_REASON))

    def test_hold_has_no_recommended_or_blocked_quantity(self) -> None:
        action = self._held_action(
            self._run_priced(
                sellable_shares=None,
                decision=self._decision("frozen_trend_engine"),
                close=100.0,
            )
        )
        self.assertEqual(action["action"], "HOLD")
        self.assertEqual(action["shares"], 100)
        self.assertIsNone(action["sellable_shares"])
        self.assertEqual(action["recommended_shares"], 0)
        self.assertEqual(action["blocked_shares"], 0)
        self.assertEqual(action["execution_status"], "NO_ACTION")

    def test_data_error_never_recommends_an_executable_quantity(self) -> None:
        engine = account_scan.AccountSignalEngine(
            cache_dir="unused",
            regime_data_dir="unused",
        )
        with (
            patch.object(
                account_scan.data_contracts,
                "refresh_regime_indices",
                return_value={},
            ),
            patch.object(
                account_scan.RegimeAdaptiveBacktestEngine,
                "decide_current",
                return_value=self._decision("cash_preservation"),
            ),
            patch.object(
                engine,
                "_frame",
                side_effect=ValueError("provider unavailable"),
            ),
        ):
            result = engine.run(
                self._snapshot(40),
                {self.HELD: "持仓"},
                as_of=self.AS_OF,
            )

        action = self._held_action(result)
        self.assertEqual(action["action"], "DATA_ERROR")
        self.assertEqual(action["shares"], 100)
        self.assertEqual(action["sellable_shares"], 40)
        self.assertIsNone(action["recommended_shares"])
        self.assertIsNone(action["blocked_shares"])
        self.assertEqual(action["execution_status"], "DATA_UNAVAILABLE")

    def test_weak_buy_candidate_payload_is_unchanged(self) -> None:
        engine = account_scan.AccountSignalEngine(
            cache_dir="unused",
            regime_data_dir="unused",
        )
        snapshot = AccountSnapshot(
            cash=100_000.0,
            peak_equity=100_000.0,
            positions=(),
        )
        with (
            patch.object(
                account_scan.data_contracts,
                "refresh_regime_indices",
                return_value={},
            ),
            patch.object(
                account_scan.RegimeAdaptiveBacktestEngine,
                "decide_current",
                return_value=self._decision(
                    "positive_momentum_hold",
                    selected=(self.OTHER,),
                ),
            ),
        ):
            result = engine.run(
                snapshot,
                {self.OTHER: "候选"},
                as_of=self.AS_OF,
            )

        self.assertEqual(
            result["actions"],
            [
                {
                    "symbol": self.OTHER,
                    "name": "候选",
                    "action": "BUY_CANDIDATE",
                    "shares": 0,
                    "reason": "current weak-regime leader",
                }
            ],
        )

    def test_console_prefers_recommended_quantity_and_status(self) -> None:
        result = {
            "estimated_equity": 1_000.0,
            "unpriced_symbols": [],
            "deployment_decision": {"name": "cash_preservation"},
            "actions": [
                {
                    "symbol": self.HELD,
                    "name": "持仓",
                    "action": "REDUCE_REVIEW",
                    "shares": 100,
                    "recommended_shares": 40,
                    "execution_status": "PARTIALLY_T1_BLOCKED",
                    "reason": "test",
                },
                {
                    "symbol": self.OTHER,
                    "name": "候选",
                    "action": "SELL",
                    "shares": 100,
                    "recommended_shares": None,
                    "execution_status": "SELLABLE_UNKNOWN",
                    "reason": "test",
                },
            ],
        }
        output = io.StringIO()
        with (
            patch.object(account_scan, "load_account_snapshot"),
            patch.object(account_scan.AccountSignalEngine, "run", return_value=result),
            patch.object(account_scan, "atomic_json"),
            contextlib.redirect_stdout(output),
        ):
            exit_code = account_scan.run_account_scan(
                account_path="unused.json",
                symbols={},
                end_date=self.AS_OF,
                cache_dir="unused",
                regime_data_dir="unused",
                output_dir="unused",
            )

        self.assertEqual(exit_code, 0)
        self.assertIn(
            "recommended_shares=40 | execution_status=PARTIALLY_T1_BLOCKED",
            output.getvalue(),
        )
        self.assertIn(
            "recommended_shares=UNKNOWN | execution_status=SELLABLE_UNKNOWN",
            output.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()
