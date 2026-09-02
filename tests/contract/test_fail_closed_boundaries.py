"""账户和指数数据失败关闭边界的回归测试。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from quantfusion.account.models import AccountPosition, AccountSnapshot
from quantfusion.application.account_scan import AccountSignalEngine
from quantfusion.data import contracts
from quantfusion.engine.universe import BacktestEngine
from quantfusion.engine.replay import RegimeAdaptiveBacktestEngine
from quantfusion.indicators.technical import Indicators
from quantfusion.regime.models import DeploymentDecision, LeaderSelection, RegimeEvidence


def _decision(*, leaders: tuple[str, ...]) -> DeploymentDecision:
    """构造不依赖外部行情的弱市路由证据。"""
    return DeploymentDecision(
        name="positive_momentum_hold",
        boundary="2026-01-31",
        reason="test",
        regime=RegimeEvidence(
            as_of="2026-01-31",
            regime="weak",
            observations=(),
        ),
        leaders=LeaderSelection(
            as_of="2026-01-31",
            requested_symbols=("300308", "300502"),
            observed_symbols=len(leaders),
            selected_symbols=leaders,
            selected_returns=tuple(0.1 for _ in leaders),
        ),
    )


class AccountFailClosedTests(unittest.TestCase):
    """验证账户不完整时不会产生新增风险敞口。"""

    @staticmethod
    def _snapshot(
        *, entry_date: str = "2025-09-01"
    ) -> AccountSnapshot:
        return AccountSnapshot(
            schema_version=3,
            account_id="main",
            snapshot_date="2026-02-01",
            cash=100_000.0,
            peak_equity=1_000_000.0,
            positions=(
                AccountPosition(
                    symbol="300308",
                    shares=100,
                    sellable_shares=100,
                    avg_cost=100.0,
                    entry_date=entry_date,
                ),
            ),
        )

    def test_unprocessed_holding_suppresses_new_buy_candidates(self) -> None:
        engine = AccountSignalEngine(
            cache_dir="cache",
            regime_data_dir="regime",
        )
        with (
            patch.object(
                contracts,
                "refresh_regime_indices",
                return_value={},
            ),
            patch.object(
                RegimeAdaptiveBacktestEngine,
                "decide_current",
                return_value=_decision(leaders=("300502",)),
            ),
            patch.object(
                engine,
                "_frame",
                side_effect=ValueError("provider unavailable"),
            ),
        ):
            result = engine.run(
                self._snapshot(),
                {"300308": "中际旭创", "300502": "新易盛"},
                as_of="2026-02-01",
            )

        self.assertTrue(result["buys_suppressed"])
        self.assertFalse(result["valuation_complete"])
        self.assertFalse(
            any(item["action"] == "BUY_CANDIDATE" for item in result["actions"])
        )

    def test_future_entry_date_is_rejected_and_suppresses_buys(self) -> None:
        dates = pd.bdate_range("2025-09-01", periods=100)
        frame = pd.DataFrame(
            {
                "open": 100.0,
                "close": 100.0,
                "high": 101.0,
                "low": 99.0,
                "volume": 1_000_000.0,
            },
            index=dates,
        )
        indicators = {
            "atr": pd.Series(2.0, index=dates),
            "ma_short": pd.Series(100.0, index=dates),
            "ma_long": pd.Series(99.0, index=dates),
        }
        engine = AccountSignalEngine(
            cache_dir="cache",
            regime_data_dir="regime",
        )
        with (
            patch.object(
                contracts,
                "refresh_regime_indices",
                return_value={},
            ),
            patch.object(
                RegimeAdaptiveBacktestEngine,
                "decide_current",
                return_value=_decision(leaders=("300502",)),
            ),
            patch.object(engine, "_frame", return_value=frame),
            patch.object(
                BacktestEngine,
                "config_for_symbol",
                return_value={
                    "hard_stop": 0.15,
                    "profit_lock_activation": 0.2,
                    "trail_atr_mult": 4.0,
                },
            ),
            patch.object(Indicators, "compute_all", return_value=indicators),
        ):
            result = engine.run(
                self._snapshot(entry_date="2026-03-01"),
                {"300308": "中际旭创", "300502": "新易盛"},
                as_of="2026-02-01",
            )

        self.assertTrue(result["buys_suppressed"])
        self.assertEqual(result["actions"][0]["action"], "DATA_ERROR")
        self.assertIn("later than as_of", result["actions"][0]["reason"])
        self.assertFalse(
            any(item["action"] == "BUY_CANDIDATE" for item in result["actions"])
        )


class IndexParsingFailClosedTests(unittest.TestCase):
    """验证提供方坏行不会被静默删除或补零。"""

    @staticmethod
    def _frame() -> pd.DataFrame:
        dates = pd.bdate_range("2025-01-02", periods=61)
        return pd.DataFrame(
            {
                "date": dates,
                "open": 10.0,
                "close": 10.1,
                "high": 10.5,
                "low": 9.5,
                "volume": 1_000.0,
            }
        )

    def test_unparseable_price_is_rejected(self) -> None:
        frame = self._frame()
        frame["open"] = frame["open"].astype(object)
        frame.loc[1, "open"] = "bad"
        with self.assertRaisesRegex(ValueError, "unparseable date or OHLC"):
            contracts._normalize_index_frame(frame, end_date="2025-12-31")

    def test_unparseable_volume_is_rejected(self) -> None:
        frame = self._frame()
        frame["volume"] = frame["volume"].astype(object)
        frame.loc[1, "volume"] = "bad"
        with self.assertRaisesRegex(ValueError, "unparseable volume"):
            contracts._normalize_index_frame(frame, end_date="2025-12-31")


if __name__ == "__main__":
    unittest.main()
