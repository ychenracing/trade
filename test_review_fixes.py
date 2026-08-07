"""生产审查修复的回归测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import account_signal_engine as account
import benchmark_validation as benchmarks
import market_data_contracts as contracts
import quant_fusion as qf
import regime_adaptive as ra


class ProviderVolumeContractTests(unittest.TestCase):
    """验证不同提供方的成交量统一为股。"""

    def test_eastmoney_lots_are_converted_to_shares(self) -> None:
        frame = pd.DataFrame(
            {
                "日期": ["2026-01-01"],
                "开盘": [10],
                "收盘": [10],
                "最高": [11],
                "最低": [9],
                "成交量": [123],
            }
        )
        normalized = qf.DataFetcher._normalize_provider_volume(
            frame,
            "Eastmoney",
        )
        self.assertEqual(float(normalized["成交量"].iloc[0]), 12_300.0)
        self.assertEqual(normalized.attrs["volume_unit"], "shares")

    def test_sina_share_volume_is_not_scaled(self) -> None:
        frame = pd.DataFrame(
            {
                "date": ["2026-01-01"],
                "open": [10],
                "close": [10],
                "high": [11],
                "low": [9],
                "volume": [12_300],
            }
        )
        normalized = qf.DataFetcher._normalize_provider_volume(frame, "Sina")
        self.assertEqual(float(normalized["volume"].iloc[0]), 12_300.0)

    def test_legacy_cache_without_unit_contract_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "300308.csv"
            path.write_text(
                "date,open,close,high,low,volume\n"
                "2026-01-01,1,1,1,1,10\n",
                encoding="utf-8",
            )
            self.assertFalse(
                qf.DataFetcher._cache_has_share_volume_contract(path)
            )
            qf.DataFetcher._write_cache_contract(path)
            self.assertTrue(
                qf.DataFetcher._cache_has_share_volume_contract(path)
            )


class RegimeFreshnessAndProtectionTests(unittest.TestCase):
    """验证弱市证据新鲜度和入场保护。"""

    def test_stale_stock_is_not_selected_as_a_leader(self) -> None:
        dates = pd.bdate_range("2022-01-03", periods=260)
        frame = pd.DataFrame(
            {
                "date": dates,
                "open": 10.0,
                "close": range(10, 270),
                "high": range(11, 271),
                "low": 9.0,
                "volume": 1_000_000,
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            frame.to_csv(Path(directory) / "300308.csv", index=False)
            selection = ra.select_positive_momentum_leaders(
                ("300308",),
                data_dir=directory,
                as_of="2024-12-31",
            )
        self.assertEqual(selection.selected_symbols, ())

    def test_weak_entry_has_nonzero_disaster_stop(self) -> None:
        dates = pd.bdate_range("2026-01-01", periods=30)
        frame = pd.DataFrame(
            {
                "open": 100.0,
                "close": 100.0,
                "high": 101.0,
                "low": 99.0,
                "volume": 1_000_000,
            },
            index=dates,
        )
        strategy = ra.PositiveMomentumHoldStrategy(
            {
                "strategy_weight": 0.5,
                "risk_pct": 0.03,
                "atr_multiplier": 2.0,
                "max_units": 1,
            }
        )
        context = qf.BarContext(
            i=29,
            df=frame,
            current_assets=2_000_000,
            indicators={"atr": pd.Series(2.0, index=dates)},
            symbol="300308",
            date=str(dates[-1].date()),
        )
        signal = strategy.on_bar(context)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertGreater(signal.stop_loss, 0.0)
        self.assertLess(signal.stop_loss, signal.price)


class AccountEngineTests(unittest.TestCase):
    """验证账户输入和不完整估值的失败关闭行为。"""

    def _write_snapshot(self, directory: str, payload: object) -> Path:
        path = Path(directory) / "account.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def test_account_parser_rejects_invalid_position(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_snapshot(
                directory,
                {
                    "cash": 1,
                    "positions": {
                        "300308": {"shares": 0, "avg_cost": 10}
                    },
                },
            )
            with self.assertRaises(ValueError):
                account.load_account_snapshot(path)

    def test_account_parser_rejects_coercive_numeric_values(self) -> None:
        invalid_positions = (
            {"shares": True, "avg_cost": 10},
            {"shares": 100.0, "avg_cost": 10},
            {"shares": 100, "avg_cost": "10"},
            {"shares": 100, "avg_cost": 10, "highest_close": float("inf")},
        )
        with tempfile.TemporaryDirectory() as directory:
            for position in invalid_positions:
                with self.subTest(position=position):
                    path = self._write_snapshot(
                        directory,
                        {
                            "cash": 0,
                            "positions": {"300308": position},
                        },
                    )
                    with self.assertRaises(ValueError):
                        account.load_account_snapshot(path)
                    path.unlink(missing_ok=True)

    def test_account_parser_rejects_invalid_symbol_and_date(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for code, entry_date in (
                ("30030", ""),
                ("300308", "not-a-date"),
            ):
                with self.subTest(code=code, entry_date=entry_date):
                    path = self._write_snapshot(
                        directory,
                        {
                            "cash": 0,
                            "positions": {
                                code: {
                                    "shares": 100,
                                    "avg_cost": 10,
                                    "entry_date": entry_date,
                                }
                            },
                        },
                    )
                    with self.assertRaises(ValueError):
                        account.load_account_snapshot(path)
                    path.unlink(missing_ok=True)

    def test_account_parser_accepts_zero_cash_full_investment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_snapshot(
                directory,
                {
                    "cash": 0,
                    "peak_equity": 2_000_000,
                    "positions": {
                        "300308": {"shares": 900, "avg_cost": 100}
                    },
                },
            )
            snapshot = account.load_account_snapshot(path)
            self.assertEqual(snapshot.cash, 0.0)
            self.assertEqual(snapshot.positions[0].shares, 900)

    def test_stale_position_frame_is_rejected(self) -> None:
        frame = pd.DataFrame(
            {
                "open": [10.0],
                "close": [10.0],
                "high": [10.5],
                "low": [9.5],
                "volume": [1_000_000.0],
            },
            index=[pd.Timestamp("2026-01-02")],
        )
        engine = account.AccountSignalEngine(
            cache_dir="cache",
            regime_data_dir="regime",
        )
        with patch.object(
            qf.DataFetcher,
            "load_stock_data",
            return_value=frame,
        ):
            with self.assertRaisesRegex(ValueError, "stale"):
                engine._frame("300308", "2026-02-01")

    def test_unpriced_holding_makes_total_equity_unavailable(self) -> None:
        snapshot = account.AccountSnapshot(
            cash=100.0,
            peak_equity=1_000.0,
            positions=(
                account.AccountPosition(
                    symbol="300308",
                    shares=100,
                    avg_cost=10.0,
                    entry_date="",
                ),
            ),
        )
        decision = ra.DeploymentDecision(
            name="cash_preservation",
            boundary="2026-01-31",
            reason="test",
            regime=ra.RegimeEvidence(
                as_of="2026-01-31",
                regime="unknown",
                observations=(),
            ),
            leaders=ra.LeaderSelection(
                as_of="2026-01-31",
                requested_symbols=("300308",),
                observed_symbols=0,
                selected_symbols=(),
                selected_returns=(),
            ),
        )
        engine = account.AccountSignalEngine(
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
                ra.RegimeAdaptiveBacktestEngine,
                "decide_current",
                return_value=decision,
            ),
            patch.object(
                engine,
                "_frame",
                side_effect=ValueError("provider unavailable"),
            ),
        ):
            result = engine.run(
                snapshot,
                {"300308": "中际旭创"},
                as_of="2026-02-01",
            )
        self.assertFalse(result["valuation_complete"])
        self.assertEqual(result["unpriced_symbols"], ["300308"])
        self.assertEqual(result["priced_market_value"], 0.0)
        self.assertIsNone(result["estimated_market_value"])
        self.assertIsNone(result["estimated_equity"])

    def test_target_shares_use_only_selected_candidate_subset(self) -> None:
        """现金分配只按被选中的候选子集计算，不被未选中候选稀释。"""
        snapshot = account.AccountSnapshot(
            cash=1_000_000.0,
            peak_equity=1_000_000.0,
            positions=(),
        )
        # 三个候选：两个被选中（target_weight 0.60 + 0.40），一个未选中。
        selected = [
            account.PointInTimeSignal(
                symbol="300308",
                strategy_name="s1",
                direction="buy",
                score=0.9,
                target_weight=0.60,
                target_shares=0,
                stop_price=None,
                reasons=("r",),
            ),
            account.PointInTimeSignal(
                symbol="300502",
                strategy_name="s2",
                direction="buy",
                score=0.8,
                target_weight=0.40,
                target_shares=0,
                stop_price=None,
                reasons=("r",),
            ),
        ]
        # 修复前传入完整 ranked（含 unselected），分母被放大、分配被稀释。
        unselected = [
            account.PointInTimeSignal(
                symbol="688256",
                strategy_name="s3",
                direction="buy",
                score=0.2,
                target_weight=0.60,
                target_shares=0,
                stop_price=None,
                reasons=("r",),
            )
        ]
        shares_selected, _ = account._compute_target_shares(
            "300308", 10.0, 0.60, snapshot, selected
        )
        shares_diluted, _ = account._compute_target_shares(
            "300308", 10.0, 0.60, snapshot, selected + unselected
        )
        # 选中子集归一化后应分配全部现金给该股整手；包含未选中候选后份额变小。
        self.assertGreater(shares_selected, shares_diluted)


class MarketDataContractTests(unittest.TestCase):
    """验证指数文件的排序、去重和 OHLC 契约。"""

    @staticmethod
    def _frame() -> pd.DataFrame:
        dates = pd.bdate_range("2025-01-02", periods=61)
        frame = pd.DataFrame(
            {
                "date": dates,
                "open": 10.0,
                "close": 10.1,
                "high": 10.5,
                "low": 9.5,
                "volume": 1_000.0,
            }
        )
        return pd.concat([frame.iloc[::-1], frame.tail(1)], ignore_index=True)

    def test_index_frame_is_sorted_and_deduplicated(self) -> None:
        normalized = contracts._normalize_index_frame(
            self._frame(),
            end_date="2025-12-31",
        )
        self.assertEqual(len(normalized), 61)
        dates = pd.to_datetime(normalized["date"])
        self.assertTrue(dates.is_monotonic_increasing)
        self.assertFalse(dates.duplicated().any())

    def test_invalid_ohlc_relationship_is_rejected(self) -> None:
        frame = self._frame()
        frame.loc[1, "high"] = 1.0
        with self.assertRaisesRegex(ValueError, "relationships"):
            contracts._normalize_index_frame(
                frame,
                end_date="2025-12-31",
            )

    def test_non_finite_price_is_rejected(self) -> None:
        frame = self._frame()
        frame.loc[1, "close"] = float("inf")
        with self.assertRaisesRegex(ValueError, "finite"):
            contracts._normalize_index_frame(
                frame,
                end_date="2025-12-31",
            )

    def test_non_finite_volume_is_rejected(self) -> None:
        frame = self._frame()
        frame.loc[1, "volume"] = float("inf")
        with self.assertRaisesRegex(ValueError, "volume must be finite"):
            contracts._normalize_index_frame(
                frame,
                end_date="2025-12-31",
            )

    def test_strict_historical_refresh_requires_frozen_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "missing frozen"):
                contracts.refresh_regime_indices(
                    directory,
                    end_date="2025-12-31",
                    strict=True,
                )


class BenchmarkValidationTests(unittest.TestCase):
    """验证基准工具对空输入、代码和日期的显式拒绝。"""

    def test_empty_symbols_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-empty"):
            benchmarks.run_benchmarks(
                {},
                data_dir="market_data",
                regime_data_dir="historical_data",
                start="2025-01-01",
                end="2025-12-31",
            )

    def test_invalid_symbol_is_rejected_before_data_loading(self) -> None:
        with self.assertRaisesRegex(ValueError, "six-digit"):
            benchmarks.run_benchmarks(
                {"bad": "bad"},
                data_dir="market_data",
                regime_data_dir="historical_data",
                start="2025-01-01",
                end="2025-12-31",
            )

    def test_reversed_period_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "later"):
            benchmarks._validate_period("2026-01-02", "2026-01-01")


if __name__ == "__main__":
    unittest.main()
