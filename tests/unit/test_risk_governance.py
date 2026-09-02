"""风险治理层单元测试（P0-1/P0-2/P0-3/P1-1/P1-2）。

覆盖预热健康契约分级、风险事件校准指标、独立风险意见语义、
风险篮覆盖置信度与袖套共识证据，以及 overlay 新增的覆盖度审计接口。
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from quantfusion.risk import governance as rg
from quantfusion.risk.overlay.policy import CrossMarketOverlay


def _frame(
    dates: pd.DatetimeIndex,
    closes: list[float] | None = None,
) -> pd.DataFrame:
    values = closes if closes is not None else [10.0] * len(dates)
    return pd.DataFrame(
        {
            "open": values,
            "high": values,
            "low": values,
            "close": values,
            "volume": [1_000_000] * len(dates),
        },
        index=pd.DatetimeIndex(dates),
    )


def _calendar(
    start: str = "2024-04-01", end: str = "2026-07-20"
) -> pd.DatetimeIndex:
    return pd.bdate_range(start, end)


class WarmupHealthTests(unittest.TestCase):
    """P0-1: READY / DEGRADED / NOT_READY 三级契约。"""

    def test_full_history_fresh_reference_is_ready(self) -> None:
        calendar = _calendar()
        data_map = {
            "300308": _frame(calendar),
            "300502": _frame(calendar),
        }
        report = rg.assess_warmup_health(
            data_map,
            "2025-04-01",
            "2026-07-20",
            reference_symbols=("300308", "300502"),
            reference_frames={
                "300308": _frame(calendar),
                "300502": _frame(calendar),
            },
            regime_index_frames={"300308": _frame(calendar)},
            required_days=240,
        )
        self.assertEqual(report.warmup_status, "READY")
        self.assertEqual(report.indicator_ready_ratio, 1.0)
        self.assertEqual(report.reference_basket_ready_ratio, 1.0)
        self.assertTrue(report.regime_index_ready)
        self.assertTrue(report.sleeve_state_ready)
        self.assertEqual(report.reasons, ())
        self.assertEqual(report.new_symbol_count, 0)

    def test_new_listing_degrades_without_silent_confidence(self) -> None:
        calendar = _calendar()
        late_listing = _frame(pd.bdate_range("2025-12-31", "2026-07-20"))
        report = rg.assess_warmup_health(
            {"300308": _frame(calendar), "920045": late_listing},
            "2025-04-01",
            "2026-07-20",
            reference_symbols=("300308",),
            reference_frames={"300308": _frame(calendar)},
            regime_index_frames={"300308": _frame(calendar)},
            required_days=240,
        )
        self.assertEqual(report.warmup_status, "DEGRADED")
        self.assertIn("920045", report.new_symbols)
        self.assertGreater(report.new_symbol_count, 0)
        self.assertTrue(
            any("new_symbols" in reason for reason in report.reasons)
        )

    def test_missing_reference_member_degrades_reference_ratio(self) -> None:
        calendar = _calendar()
        report = rg.assess_warmup_health(
            {"300308": _frame(calendar)},
            "2025-04-01",
            "2026-07-20",
            reference_symbols=("300308", "688256"),
            reference_frames={"300308": _frame(calendar)},
            regime_index_frames={"300308": _frame(calendar)},
            required_days=240,
        )
        self.assertEqual(report.reference_basket_ready_ratio, 0.5)
        self.assertIn("reference_basket_incomplete", report.reasons)
        self.assertEqual(report.warmup_status, "DEGRADED")

    def test_cold_start_without_history_is_not_ready(self) -> None:
        calendar = pd.bdate_range("2025-04-01", "2026-07-20")
        report = rg.assess_warmup_health(
            {"300308": _frame(calendar)},
            "2025-04-01",
            "2026-07-20",
            reference_symbols=("300308",),
            reference_frames={"300308": _frame(calendar)},
            regime_index_frames={"300308": _frame(calendar)},
            required_days=240,
        )
        self.assertEqual(report.warmup_status, "NOT_READY")
        self.assertEqual(report.indicator_ready_ratio, 0.0)
        self.assertFalse(report.sleeve_state_ready)

    def test_missing_regime_evidence_is_not_ready(self) -> None:
        calendar = _calendar()
        report = rg.assess_warmup_health(
            {"300308": _frame(calendar)},
            "2025-04-01",
            "2026-07-20",
            reference_symbols=("300308",),
            reference_frames={"300308": _frame(calendar)},
            regime_index_frames={},
            required_days=240,
        )
        self.assertEqual(report.warmup_status, "NOT_READY")
        self.assertIn("regime_index_missing_or_stale", report.reasons)

    def test_stale_data_is_reported(self) -> None:
        calendar = pd.bdate_range("2024-04-01", "2026-06-01")
        report = rg.assess_warmup_health(
            {"300308": _frame(calendar)},
            "2025-04-01",
            "2026-07-20",
            reference_symbols=("300308",),
            reference_frames={"300308": _frame(calendar)},
            regime_index_frames={"300308": _frame(calendar)},
            required_days=240,
        )
        self.assertGreater(report.stale_symbol_count, 0)
        self.assertEqual(report.warmup_status, "DEGRADED")


class RiskEventCalibrationTests(unittest.TestCase):
    """P0-2: 风险事件分类器的 precision / recall / lead time / 机会成本。"""

    def test_perfect_alert_detects_shock_with_lead_time(self) -> None:
        # 60 日平稳 -> 警报(等级1，5日) -> 10 日内下跌 20% -> 恢复走平
        assets = [100.0] * 60 + [100.0] * 5 + [80.0] * 10 + [80.0] * 20
        levels = [0] * 60 + [1] * 5 + [0] * 30
        dates = [
            d.strftime("%Y-%m-%d")
            for d in pd.bdate_range("2025-01-01", periods=len(assets))
        ]
        report = rg.calibrate_risk_events(dates, assets, levels)
        metrics = report["metrics"]
        self.assertEqual(report["status"], "ok")
        self.assertEqual(metrics["alert_episode_count"], 1)
        self.assertEqual(metrics["shock_precision"], 1.0)
        self.assertEqual(metrics["shock_recall"], 1.0)
        self.assertGreaterEqual(metrics["median_lead_time_days"], 0)
        self.assertEqual(metrics["missed_crash_count"], 0)
        self.assertFalse(report["events"][0]["realized_shock"] is False)

    def test_unalerted_crash_counts_as_missed(self) -> None:
        assets = [100.0] * 30 + [70.0] * 30
        levels = [0] * 60
        dates = [
            d.strftime("%Y-%m-%d")
            for d in pd.bdate_range("2025-01-01", periods=len(assets))
        ]
        report = rg.calibrate_risk_events(dates, assets, levels)
        metrics = report["metrics"]
        self.assertEqual(metrics["shock_episode_count"], 1)
        self.assertEqual(metrics["shock_recall"], 0.0)
        self.assertEqual(metrics["missed_crash_count"], 1)
        self.assertLess(metrics["missed_crash_loss_median"], -0.20)

    def test_false_alert_without_shock_reports_cost_and_silence(self) -> None:
        # 警报（第10-14日，冻结加仓）结束后价格继续上涨 10% -> 误报机会成本。
        assets = [100.0] * 15 + [110.0] * 35
        levels = [0] * 10 + [1] * 5 + [0] * 35
        dates = [
            d.strftime("%Y-%m-%d")
            for d in pd.bdate_range("2025-01-01", periods=len(assets))
        ]
        report = rg.calibrate_risk_events(dates, assets, levels)
        metrics = report["metrics"]
        self.assertEqual(metrics["shock_precision"], 0.0)
        self.assertEqual(metrics["false_positive_count"], 1)
        self.assertGreater(metrics["false_positive_cost_median"], 0.0)
        self.assertLess(metrics["bull_silence_ratio"], 1.0)

    def test_l1_only_freeze_opportunity_metrics_present(self) -> None:
        # L1 冻结 5 日，随后 20 日组合上涨 10%（牛市正常回踩场景）。
        assets = [100.0] * 20 + [90.0] * 5 + [90.0] * 5 + [99.0] * 20
        levels = [0] * 20 + [1] * 5 + [0] * 25
        dates = [
            d.strftime("%Y-%m-%d")
            for d in pd.bdate_range("2025-01-01", periods=len(assets))
        ]
        report = rg.calibrate_risk_events(dates, assets, levels)
        metrics = report["metrics"]
        self.assertEqual(metrics["l1_only_episode_count"], 1)
        self.assertIsNotNone(metrics["l1_only_median_post_return_20d"])
        self.assertAlmostEqual(
            metrics["l1_only_median_post_return_20d"], 0.10, places=2
        )
        self.assertEqual(metrics["l1_escalation_precision"], 0.0)

    def test_event_outcome_windows_record_basket_min_return(self) -> None:
        # 警报（第55-59日）先于下跌（第60日 100->80）触发。
        assets = [100.0] * 60 + [80.0] * 20
        levels = [0] * 55 + [1] * 5 + [0] * 20
        basket = [0.0] * 60 + [-0.02] * 20
        dates = [
            d.strftime("%Y-%m-%d")
            for d in pd.bdate_range("2025-01-01", periods=len(assets))
        ]
        report = rg.calibrate_risk_events(
            dates, assets, levels, basket_daily_returns=basket
        )
        outcomes = report["events"][0]["outcomes"]
        self.assertIn("20d", outcomes)
        self.assertLess(outcomes["20d"]["portfolio_min_return"], -0.15)
        self.assertLess(outcomes["20d"]["max_drawdown"], -0.15)
        self.assertIsNotNone(outcomes["20d"]["basket_min_return"])

    def test_length_mismatch_inputs_return_insufficient_data(self) -> None:
        report = rg.calibrate_risk_events(["2025-01-01"], [100.0], [0, 1])
        self.assertEqual(report["status"], "insufficient_data")


class RiskOpinionTests(unittest.TestCase):
    """P0-3: 独立风险意见对象语义。"""

    def _coverage(self, confidence: float = 0.9) -> rg.BasketCoverage:
        return rg.BasketCoverage(
            observed=22,
            total_basket=23,
            observed_industries=4,
            total_industries=5,
            held_symbols=("300308",),
            held_mapped_ratio=1.0,
            confidence=confidence,
        )

    def test_bull_silent_level_zero_opinion(self) -> None:
        opinion = rg.build_risk_opinion(
            "2026-07-20", 0, self._coverage(), regime="trend"
        )
        self.assertTrue(opinion.bull_silent)
        self.assertFalse(opinion.block_new_entries)
        self.assertFalse(opinion.block_pyramids)
        self.assertEqual(opinion.recommended_gross_cap, 1.0)
        self.assertEqual(opinion.reason_codes, ())
        self.assertEqual(opinion.risk_confidence, 0.9)

    def test_level_semantics_match_overlay_execution(self) -> None:
        l1 = rg.build_risk_opinion("2026-07-20", 1, self._coverage())
        self.assertFalse(l1.block_new_entries)
        self.assertTrue(l1.block_pyramids)
        self.assertIn("sector_warning_armed", l1.reason_codes)
        l2 = rg.build_risk_opinion("2026-07-20", 2, self._coverage())
        self.assertTrue(l2.block_new_entries)
        self.assertTrue(l2.block_pyramids)
        self.assertEqual(l2.recommended_gross_cap, 0.70)
        self.assertIn("sector_risk_confirmed", l2.reason_codes)
        l3 = rg.build_risk_opinion("2026-07-20", 3, self._coverage())
        self.assertEqual(l3.recommended_gross_cap, 0.50)
        self.assertIn("sustained_risk_failure", l3.reason_codes)

    def test_risk_level_is_clamped(self) -> None:
        opinion = rg.build_risk_opinion("2026-07-20", 9, self._coverage())
        self.assertEqual(opinion.risk_level, 3)
        negative = rg.build_risk_opinion("2026-07-20", -2, self._coverage())
        self.assertEqual(negative.risk_level, 0)

    def test_low_coverage_and_stress_reason_codes(self) -> None:
        opinion = rg.build_risk_opinion(
            "2026-07-20",
            2,
            self._coverage(confidence=0.4),
            stressed_sub_industry="optical",
            catastrophe_cooldown_active=True,
            outer_route="weak",
            sleeve_consensus_decline_streak=3,
        )
        self.assertIn("low_basket_coverage", opinion.reason_codes)
        self.assertIn("subindustry_stress:optical", opinion.reason_codes)
        self.assertIn("catastrophe_cooldown_active", opinion.reason_codes)
        self.assertIn("outer_route_defensive:weak", opinion.reason_codes)
        self.assertIn("sleeve_consensus_declining", opinion.reason_codes)
        self.assertFalse(opinion.bull_silent)
        self.assertEqual(opinion.weakest_clusters, ("optical",))

    def test_as_dict_is_json_ready(self) -> None:
        import json

        opinion = rg.build_risk_opinion(
            "2026-07-20", 1, self._coverage(), sleeve_consensus=0.6667
        )
        payload = json.loads(json.dumps(opinion.as_dict(), allow_nan=False))
        self.assertEqual(payload["date"], "2026-07-20")
        self.assertEqual(payload["sleeve_consensus"], 0.6667)
        self.assertIsInstance(payload["coverage"], dict)


class BasketCoverageTests(unittest.TestCase):
    """P1-2: 风险篮覆盖置信度。"""

    def test_full_coverage_confidence_is_one(self) -> None:
        coverage = rg.basket_coverage_confidence(
            23, 23, 5, 5, ("300308",), {"300308": "optical"}
        )
        self.assertEqual(coverage.confidence, 1.0)
        self.assertEqual(coverage.as_dict()["observed_ratio"], 1.0)

    def test_confidence_weights_blend_three_factors(self) -> None:
        coverage = rg.basket_coverage_confidence(
            12, 23, 3, 5, (), {}
        )
        expected = (
            rg.COVERAGE_WEIGHT_OBSERVED * (12 / 23)
            + rg.COVERAGE_WEIGHT_INDUSTRY * (3 / 5)
            + rg.COVERAGE_WEIGHT_HELD * 1.0
        )
        self.assertAlmostEqual(coverage.confidence, expected, places=6)

    def test_unmapped_held_book_lowers_confidence(self) -> None:
        mapped = rg.basket_coverage_confidence(
            23, 23, 5, 5, ("300308",), {"300308": "optical"}
        )
        unmapped = rg.basket_coverage_confidence(
            23, 23, 5, 5, ("300308", "600000"), {"300308": "optical"}
        )
        self.assertLess(
            unmapped.confidence,
            mapped.confidence,
        )
        self.assertLess(unmapped.held_mapped_ratio, 1.0)


class SleeveAgreementTests(unittest.TestCase):
    """P1-1: 三袖套分歧证据（纯观测，不新增状态机）。"""

    def test_consensus_counts_and_deployment(self) -> None:
        snap = rg.compute_sleeve_agreement(
            "2026-07-20",
            ("fast", "base", "slow"),
            [{"AAA", "BBB"}, {"AAA"}, {"AAA", "BBB", "CCC"}],
            [100.0, 100.0, 100.0],
            [0.0, 100.0, 30.0],
        )
        # AAA=3, BBB=2, CCC=1 -> mean = (3+2+1)/(3*3) = 0.667
        self.assertAlmostEqual(snap.mean_consensus, 2 / 3, places=6)
        self.assertEqual(snap.symbols_by_three, 1)
        self.assertEqual(snap.symbols_by_two, 1)
        self.assertEqual(snap.symbols_by_one, 1)
        self.assertAlmostEqual(
            snap.sleeve_deployment["fast"], 1.0, places=6
        )
        self.assertAlmostEqual(snap.sleeve_deployment["base"], 0.0, places=6)
        self.assertAlmostEqual(snap.sleeve_deployment["slow"], 0.7, places=6)
        self.assertEqual(snap.weakest_sleeve, "base")

    def test_decline_streak_accumulates_and_resets(self) -> None:
        first = rg.compute_sleeve_agreement(
            "2026-07-18",
            ("fast", "base", "slow"),
            [{"A"}, {"A"}, {"A"}],
            [100.0, 100.0, 100.0],
            [0.0, 0.0, 0.0],
        )
        self.assertEqual(first.decline_streak, 0)
        second = rg.compute_sleeve_agreement(
            "2026-07-19",
            ("fast", "base", "slow"),
            [{"A"}, {"A"}, set()],
            [100.0, 100.0, 100.0],
            [0.0, 0.0, 0.0],
            previous_consensus=first.mean_consensus,
            previous_streak=first.decline_streak,
        )
        self.assertEqual(second.decline_streak, 1)
        third = rg.compute_sleeve_agreement(
            "2026-07-20",
            ("fast", "base", "slow"),
            [{"A", "B"}, {"A", "B"}, {"A", "B"}],
            [100.0, 100.0, 100.0],
            [0.0, 0.0, 0.0],
            previous_consensus=second.mean_consensus,
            previous_streak=second.decline_streak,
        )
        self.assertEqual(third.decline_streak, 0)

    def test_empty_book_defaults_to_full_consensus(self) -> None:
        snap = rg.compute_sleeve_agreement(
            "2026-07-20",
            ("fast", "base", "slow"),
            [set(), set(), set()],
            [100.0, 100.0, 100.0],
            [100.0, 100.0, 100.0],
        )
        self.assertEqual(snap.mean_consensus, 1.0)
        self.assertIsNone(snap.weakest_sleeve)


class OverlayCoverageAccessorTests(unittest.TestCase):
    """P1-2: overlay 覆盖度审计接口（纯读取，不影响决策）。"""

    def test_initial_coverage_metrics_are_empty(self) -> None:
        overlay = CrossMarketOverlay()
        metrics = overlay.coverage_metrics()
        self.assertEqual(metrics["observed"], 0)
        self.assertEqual(metrics["total_basket"], 23)
        self.assertEqual(metrics["total_industries"], 5)
        self.assertIsNone(metrics["date"])

    def test_catastrophe_cooldown_detection(self) -> None:
        overlay = CrossMarketOverlay()
        self.assertFalse(overlay.has_active_catastrophe_cooldown(100))
        overlay._catastrophe_cooldown["AAA"] = 110
        self.assertTrue(overlay.has_active_catastrophe_cooldown(100))
        self.assertFalse(overlay.has_active_catastrophe_cooldown(110))


class ForwardDrawdownTests(unittest.TestCase):
    """内部前视回撤窗口计算（供事件校准）。"""

    def test_forward_dd_uses_window_start_as_reference(self) -> None:
        assets = np.array([100.0, 102.0, 90.0, 91.0, 92.0, 93.0, 94.0])
        fwd = rg._forward_max_drawdown(assets)
        # 第2日起 20 日窗口内最低 90，参考价 102 -> -11.8%
        self.assertAlmostEqual(fwd[1], 90.0 / 102.0 - 1.0, places=9)
        # 首日窗口最低 90，参考价 100 -> -10%
        self.assertAlmostEqual(fwd[0], -0.10, places=9)


if __name__ == "__main__":
    unittest.main()
