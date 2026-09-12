"""Readable reports must explain actual decisions, not invent new ones."""
from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


def simulation() -> dict:
    return {
        "mode": "simulation", "status": "ok", "scan_date": "2026-07-30",
        "run_id": "example-run", "start_date": "2025-04-01",
        "symbols": {"300308": "示例股票"},
        "signals": [{"code": "300308", "name": "示例股票", "signal": "持有", "held_shares": 300}],
        "summary": {"buys_suppressed": False, "risk_state_identity_mismatch": False,
                    "current_route_mismatch": False, "warmup_not_ready": False},
        "deployment": {"current_decision": {"name": "frozen_trend_engine", "boundary": "2026-07-30"}},
        "warmup_health": {"warmup_status": "READY", "reasons": []},
        "risk_opinion": {"date": "2026-07-30", "risk_level": 0, "risk_confidence": .8,
                         "reason_codes": [], "block_new_entries": False, "block_pyramids": False},
        "account_risk_budget": {"enabled": True, "mechanism": "AB5", "status": "APPLIED", "latest": {"date": "2026-07-30", "buy_scale": .5}},
        "pending_signals": [], "blocked_signals": [], "risk_state_saved": True,
        "portfolio": {"final_assets": 2100000., "total_return": .05, "max_drawdown": -.25},
    }


def account() -> dict:
    return {
        "mode": "account_decision_support", "as_of": "2026-07-30", "snapshot_date": "2026-07-30",
        "evidence_date": "2026-07-30", "valuation_complete": True, "data_complete": True,
        "estimated_equity": 200000., "cash": 10000., "buys_suppressed": False,
        "buy_suppression_reasons": [], "deployment_decision": {"name": "frozen_trend_engine"},
        "account_risk_budget": {"enabled": True, "mechanism": "AB5", "status": "APPLIED", "buy_scale": 1.},
        "actions": [],
    }


def signal(direction: str, reason: str, **fields) -> dict:
    return {"symbol": "300308", "strategy_name": "turtle_breakout", "direction": direction,
            "target_shares": 100, "price": 100., "reason": reason, "signal_date": "2026-07-30",
            "executable": True, "blocked": False, **fields}


class DailyReportTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec("quantfusion.application.daily_report"),
                             "Plain-language report renderer is not implemented")
        return importlib.import_module("quantfusion.application.daily_report")

    def render(self, data, **kwargs):
        return self.module().render_daily_report(data, **kwargs)

    def test_opposite_signals_remain_separate(self):
        data = simulation()
        data["pending_signals"] = [signal("buy", "Donchian breakout(ADX=25.0)"),
                                   signal("sell", "ATR trailing stop@99.00", strategy_name="dual_ma")]
        text = self.render(data)
        for phrase in ("支持买入", "支持卖出", "买卖信号并存", "不相互抵消", "99.00", "下一可交易日"):
            self.assertIn(phrase, text)
        self.assertNotIn("净买入", text)

    def test_block_reason_uses_actual_summary_not_generic_legacy_label(self):
        data = simulation()
        data["summary"].update(buys_suppressed=True, warmup_not_ready=True)
        data["blocked_signals"] = [signal("buy", "MA golden cross(RSI=60)", blocked=True,
                                        executable=False, blocked_reason="risk_state_identity_mismatch")]
        text = self.render(data)
        self.assertIn("历史数据不足", text)
        self.assertIn("暂不买入", text)
        self.assertNotIn("股票池或配置与上次不一致", text)
        self.assertIn("MA golden cross", text)  # original trace is retained

    def test_risk_opinion_is_not_a_new_execution_gate(self):
        data = simulation()
        data["pending_signals"] = [signal("buy", "Donchian breakout(ADX=25)")]
        data["risk_opinion"].update(risk_level=2, block_new_entries=True, reason_codes=["sector_risk_confirmed"])
        text = self.render(data)
        self.assertIn("买入候选", text)
        self.assertIn("独立风险意见", text)
        self.assertIn("不等于实际拦截", text)
        self.assertIn("不是涨跌概率", text)

    def test_historical_drawdown_is_not_current_position_advice(self):
        text = self.render(simulation())
        self.assertIn("历史回放", text)
        self.assertIn("-25.00%", text)
        self.assertNotIn("建议总仓位不超过50%", text)
        self.assertNotIn("当前回撤 25", text)

    def test_old_events_cannot_explain_today_block(self):
        replay = {"order_events": [{"date": "2026-07-29", "symbol": "300308", "direction": "buy",
                                    "event": "rejected_insufficient_cash"}]}
        text = self.render(simulation(), replay=replay)
        self.assertNotIn("现金不足", text)

    def test_today_budget_and_sell_capacity_events_are_visible(self):
        replay = {"order_events": [
            {"date": "2026-07-30", "symbol": "300308", "direction": "buy",
             "event": "account_budget_buy_reduced", "authorized_shares": 0},
            {"date": "2026-07-30", "symbol": "300308", "direction": "sell",
             "event": "deferred_sell_no_prior_adv_capacity"}]}
        text = self.render(simulation(), replay=replay)
        for phrase in ("买入数量被风险预算缩减", "成交量容量不足", "当日回放记录"):
            self.assertIn(phrase, text)

    def test_t1_partial_sell_is_not_full_executable_sell(self):
        data = account()
        data["actions"] = [{"symbol": "300308", "name": "示例股票", "action": "SELL", "shares": 1000,
                            "sellable_shares": 600, "recommended_shares": 600, "blocked_shares": 400,
                            "execution_status": "PARTIALLY_T1_BLOCKED", "close": 90., "protective_stop": 95.,
                            "reason": "close 90.00 <= protective stop 95.00"}]
        text = self.render(data)
        for phrase in ("600 股", "400 股", "次日才能卖", "90.00", "95.00"):
            self.assertIn(phrase, text)
        self.assertNotIn("已卖出", text)

    def test_zero_size_candidate_is_not_actionable(self):
        data = account()
        data["actions"] = [{"symbol": "300308", "name": "示例股票", "action": "BUY_CANDIDATE", "shares": 0,
                            "strategies": ["dual_ma"], "indicative_target_shares": 0,
                            "execution_status": "INDICATIVE_REVIEW_ONLY", "reason": "dual_ma"}]
        text = self.render(data)
        self.assertIn("数量为零", text)
        self.assertIn("不能据此买入", text)
        self.assertNotIn("资金不足导致", text)  # zero size alone cannot identify cause

    def test_account_unavailable_does_not_become_zero_or_safe(self):
        data = account()
        data.update(estimated_equity=None, valuation_complete=False, data_complete=False, buys_suppressed=True)
        data["actions"] = [{"symbol": "300308", "name": "示例股票", "action": "DATA_ERROR",
                            "shares": 1000, "recommended_shares": None, "blocked_shares": None,
                            "reason": "no market data"}]
        text = self.render(data)
        self.assertIn("无法判断", text)
        self.assertIn("未提供", text)
        self.assertNotIn("总资产：0", text)
        self.assertNotIn("风险等级：正常", text)

    def test_account_no_action_is_not_silently_explained_as_no_signal(self):
        text = self.render(account(), symbols={"300308": "示例股票"})
        self.assertIn("示例股票", text)
        self.assertIn("未输出个股建议", text)
        self.assertIn("不能判断", text)

    def test_peak_evidence_gap_and_budget_clip_are_explained(self):
        data = account()
        data["actions"] = [{"symbol": "300308", "action": "REDUCE_REVIEW", "shares": 1000,
                            "recommended_shares": 300, "blocked_shares": 0, "sellable_shares": 1000,
                            "peak_evidence_status": "PEAK_EVIDENCE_INCOMPLETE",
                            "reason": "account_budget_trim (close-known plan, not a fill)"}]
        text = self.render(data)
        self.assertIn("持仓最高价记录不完整", text)
        self.assertIn("账户整体风险预算", text)
        self.assertIn("不代表已成交", text)

    def test_unknown_evidence_is_visible_without_fabrication(self):
        data = simulation()
        data["pending_signals"] = [signal("sell", "UNRECOGNIZED_RISK_TOKEN")]
        text = self.render(data)
        self.assertIn("未翻译", text)
        self.assertIn("UNRECOGNIZED_RISK_TOKEN", text)
        self.assertNotIn("没有风险", text)

    def test_renderer_is_deterministic_and_does_not_mutate_inputs(self):
        data = simulation()
        data["pending_signals"] = [signal("sell", "ATR trailing stop@90")]
        replay = {"order_events": [], "risk_events": []}
        before = copy.deepcopy((data, replay))
        self.assertEqual(self.render(data, replay=replay), self.render(data, replay=replay))
        self.assertEqual((data, replay), before)

    def test_report_escapes_untrusted_markdown_and_html(self):
        data = account()
        text = self.render(data, symbols={"300308": "<script>alert(1)</script>\n# 假标题"})
        self.assertNotIn("<script>", text)
        self.assertNotIn("\n# 假标题", text)

    def test_sidecar_is_bound_to_source_bytes_and_never_changes_them(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()):
            path = Path(root) / "signals_2026-07-30.json"
            raw = json.dumps(simulation(), ensure_ascii=False).encode()
            path.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            report = module.publish_daily_report(path)
            self.assertIsNotNone(report)
            self.assertIn(digest[:12], report.name)
            self.assertIn(digest, report.read_text(encoding="utf-8"))
            self.assertEqual(path.read_bytes(), raw)
            self.assertFalse((Path(root) / "latest_success.json").exists())

    def test_sidecar_failure_does_not_change_authoritative_output(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()) as out:
            path = Path(root) / "account_signals_2026-07-30.json"
            path.write_text(json.dumps(account()), encoding="utf-8")
            before = path.read_bytes()
            with patch.object(module.os, "replace", side_effect=OSError("disk full")):
                self.assertIsNone(module.publish_daily_report(path))
            self.assertIn("阅读版未保存", out.getvalue())
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(root).glob("*.md")), [])
            self.assertEqual(list(Path(root).glob("*.tmp")), [])

    def test_failure_artifact_never_becomes_a_success_report(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()):
            path = Path(root) / "signals_2026-07-30.error.json"
            path.write_text(json.dumps({"mode": "simulation", "status": "error"}))
            self.assertIsNone(module.publish_daily_report(path))
            self.assertEqual(list(Path(root).glob("*.md")), [])


    def test_source_replacement_is_rejected_without_report(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()):
            path = Path(root) / "signals_2026-07-30.json"
            path.write_text(json.dumps(simulation()))
            self.assertIsNone(module.publish_daily_report(path, expected_identity=("run_id", "other-run")))
            self.assertEqual(list(Path(root).glob("*.md")), [])

    def test_nonfinite_source_cannot_become_a_normal_report(self):
        module = self.module()
        data = simulation()
        data["portfolio"]["final_assets"] = float("nan")
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()):
            path = Path(root) / "signals_2026-07-30.json"
            path.write_text(json.dumps(data))
            self.assertIsNone(module.publish_daily_report(path))
            self.assertEqual(list(Path(root).glob("*.md")), [])

    def test_zero_or_unexecutable_pending_buy_is_not_displayed_as_available(self):
        for fields in ({"target_shares": 0}, {"executable": False}, {"blocked": True}):
            with self.subTest(fields=fields):
                data = simulation()
                data["pending_signals"] = [signal("buy", "Donchian breakout", **fields)]
                text = self.render(data)
                self.assertIn("不能据此买入", text)

    def test_missing_account_buy_quantity_is_not_an_available_candidate(self):
        data = account()
        data["actions"] = [{"symbol": "300308", "action": "BUY_CANDIDATE", "shares": 0, "reason": "dual_ma"}]
        self.assertIn("不能据此买入", self.render(data))

    def test_quick_view_reuses_each_stocks_actual_conclusion(self):
        data = account()
        data['actions'] = [{'symbol': '300308', 'name': '示例股票', 'action': 'BUY_CANDIDATE',
                            'indicative_target_shares': 0, 'shares': 0, 'reason': 'dual_ma'}]
        text = self.render(data, symbols={'300308': '示例股票', '300502': '未出建议股票'})
        self.assertIn('## 逐股速览', text)
        self.assertIn('| 示例股票 | 300308 | 候选数量为零，不能据此买入 |', text)
        self.assertIn('| 未出建议股票 | 300502 | 本次未输出个股建议 |', text)
        self.assertLess(text.index('## 逐股速览'), text.index('## 逐股结论与依据'))

    def test_markdown_keeps_explanations_in_separate_paragraphs(self):
        data = simulation()
        text = self.render(data)
        self.assertIn('模拟持有 300 股；这不是真实账户持仓。\n\n支持买入', text)
        self.assertIn('| 股票 | 代码 | 本次结论 |\n| --- | --- | --- |', text)
