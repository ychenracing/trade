"""Reading copies cannot weaken either daily publication transaction."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from quantfusion.application import account_scan
from . import test_daily_artifact_transactions as transaction_tests
from ._daily_scan_support import dss


class ReadingReportIntegrationTests(unittest.TestCase):
    def test_simulation_publishes_reading_copy_after_json_and_state(self):
        helper = transaction_tests.ArtifactFirstTransactionTests()
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()) as out:
            result = helper._make_mock_result()
            self.assertEqual(helper._run_main_with_mock(root, result), 0)
            reports = list(Path(root).glob("signals_*.md"))
            self.assertEqual(len(reports), 1)
            data = json.loads((Path(root) / "signals_2026-07-30.json").read_text())
            state = json.loads((Path(root) / "risk_state.json").read_text())
            self.assertEqual(data["run_id"], state["run_id"])
            self.assertIn("模拟信号，不是真实持仓", reports[0].read_text())
            self.assertIn("逐股结论与依据", out.getvalue())
            self.assertNotIn("建议总仓位不超过50%", out.getvalue())

    def test_risk_state_failure_does_not_publish_reading_copy(self):
        helper = transaction_tests.ArtifactFirstTransactionTests()
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()), \
             patch.object(dss, "_save_risk_state", side_effect=OSError("disk full")):
            self.assertEqual(helper._run_main_with_mock(root, helper._make_mock_result()), 1)
            self.assertEqual(list(Path(root).glob("*.md")), [])
            self.assertFalse((Path(root) / "latest_success.json").exists())

    def test_invalid_engine_result_does_not_publish_reading_copy(self):
        helper = transaction_tests.ArtifactFirstTransactionTests()
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()):
            self.assertEqual(helper._run_main_with_mock(root, helper._make_mock_result(final_assets=None)), 1)
            self.assertEqual(list(Path(root).glob("*.md")), [])

    def test_account_report_uses_published_advice_and_does_not_create_simulation_state(self):
        result = {"mode": "account_decision_support", "as_of": "2026-07-30", "snapshot_date": "2026-07-30",
                  "data_complete": True, "valuation_complete": True, "estimated_equity": 10000.,
                  "cash": 10000., "buys_suppressed": False, "actions": [],
                  "deployment_decision": {"name": "cash_preservation"}}
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()) as out, \
             patch.object(account_scan, "load_account_snapshot_with_sha256", return_value=(object(), "known-hash")), \
             patch.object(account_scan.AccountSignalEngine, "run", return_value=result):
            status = account_scan.run_account_scan(account_path="not-real.json", symbols={"300308": "示例股票"},
                       end_date="2026-07-30", cache_dir=root, regime_data_dir=root, output_dir=root)
            self.assertEqual(status, 0)
            reports = list(Path(root).glob("account_signals_*.md"))
            self.assertEqual(len(reports), 1)
            self.assertIn("未输出个股建议", out.getvalue())
            self.assertFalse((Path(root) / "risk_state.json").exists())
            self.assertFalse((Path(root) / "latest_success.json").exists())
            data = json.loads((Path(root) / "account_signals_2026-07-30.json").read_text())
            self.assertEqual(data, result)

    def test_corrupt_risk_state_is_retained_without_delete_to_trade_advice(self):
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()) as out:
            state = Path(root) / 'risk_state.json'
            original = b'{"broken":'
            state.write_bytes(original)
            with patch('sys.argv', ['daily_scan', '--output-dir', root, '--end-date', '2026-07-30']):
                self.assertEqual(dss.main(), 1)
            self.assertEqual(state.read_bytes(), original)
            self.assertEqual(list(Path(root).glob('*.md')), [])
            self.assertIn('不要删除风险状态来绕过锁定', out.getvalue())
            self.assertNotIn('请删除 risk_state.json 后重试', out.getvalue())
