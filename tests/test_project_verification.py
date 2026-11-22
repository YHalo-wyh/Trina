"""离线工作单的绑定、交接、预算和恢复回归。"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.project_verification import ProjectVerification, write_workspace_reports
from scripts.verify_contract import execute_check


class ProjectVerificationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "input.toml").write_bytes(b"test profile")
        (self.root / "comparison.txt").write_bytes(b"different report totals")
        self.work = ProjectVerification(self.root, "docs-review")
        self.work.initialize(["input.toml"])

    def plan(self, **overrides):
        value = {"premise": "stale-doc-links", "budget_s": 12,
                 "expected_signal": "课程检查可能发现一个已移动的引用",
                 "failure_criterion": "静态检查返回非零", "alternative": "检查源文档的索引规则",
                 "checks": [{"id": "V-CONTRACT", "estimated_s": 3}, {"id": "V-COURSE", "estimated_s": 4}]}
        path = self.root / "plan.json"
        path.write_text(json.dumps(value | overrides), encoding="utf-8")
        return "plan.json"

    @patch("scripts.project_verification.execute_check")
    def test_rejects_drift_and_invalid_plan_before_execution(self, execute):
        for change in ({"budget_s": 4}, {"failure_criterion": " "}, {"checks": [{"id": "V-TESTS"}]},
                       {"checks": [{"id": "V-COURSE", "estimated_s": 1}] * 2}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.work.run(self.plan(**change))
        (self.root / "input.toml").write_text("different profile", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "绑定文件已变化"):
            self.work.run(self.plan())
        with self.assertRaises(ValueError):
            self.work.record("stale-doc-links", "refuted", "comparison.txt")
        execute.assert_not_called()
        self.assertEqual(self.work.show()["rounds"], {})

    @patch("scripts.project_verification.execute_check")
    def test_reserves_once_and_keeps_full_result_after_reopen(self, execute):
        path = self.plan()

        def run_check(root, identifier, *, timeout):
            state = self.work.show()["rounds"]["stale-doc-links"]
            self.assertEqual(state["status"], "started")
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 12)
            return {"exit_code": 0, "timed_out": False, "output": "x" * 6000}

        execute.side_effect = run_check
        result = self.work.run(path)
        self.assertEqual(result["status"], "passed")
        reopened = ProjectVerification(self.root, "docs-review")
        evidence = json.loads((self.root / result["evidence"]).read_text(encoding="utf-8"))
        self.assertEqual(len(evidence["results"][1]["output"]), 6000)
        self.assertTrue(reopened.show()["rounds"]["stale-doc-links"]["evidence_sha256"])
        with self.assertRaisesRegex(ValueError, "已登记过一轮"):
            reopened.run(path)
        self.assertEqual(execute.call_count, 2)

    @patch("scripts.project_verification.execute_check")
    def test_refuted_premise_cannot_be_overwritten_or_silently_retried(self, execute):
        self.work.record("stale-doc-links", "refuted", "comparison.txt")
        with self.assertRaisesRegex(ValueError, "已被证否"):
            self.work.run(self.plan())
        with self.assertRaisesRegex(ValueError, "已有带证据的结论"):
            self.work.record("stale-doc-links", "verified", "comparison.txt")
        self.assertEqual(self.work.handoff(), {"state": "runs/project-verification/docs-review/STATE.json",
                                              "refuted": ["stale-doc-links"], "refuted_count": 1})
        execute.assert_not_called()
        execute.return_value = {"exit_code": 1, "timed_out": False, "output": "different result"}
        entry = self.work.run(self.plan(premise="wrong-index", replaces="stale-doc-links"))
        self.assertEqual(entry["status"], "failed")
        execute.assert_called_once()
        (self.root / "comparison.txt").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "证据文件已变化"):
            self.work.run(self.plan(premise="bad-links", replaces="stale-doc-links"))

    @patch("scripts.project_verification.execute_check")
    def test_timeout_consumes_round_without_refuting_premise(self, execute):
        execute.return_value = {"exit_code": None, "timed_out": True, "output": "partial"}
        entry = self.work.run(self.plan())
        self.assertEqual(entry["status"], "timed_out")
        self.assertEqual(self.work.show()["verdicts"], {})
        execute.assert_called_once()

    @patch("scripts.project_verification.execute_check")
    def test_changes_during_run_invalidate_a_passing_result(self, execute):
        def run_check(*args, **kwargs):
            (self.root / "input.toml").write_bytes(b"changed during checking")
            return {"exit_code": 0, "timed_out": False, "output": "passing result"}

        execute.side_effect = run_check
        entry = self.work.run(self.plan())
        self.assertEqual(entry["status"], "input_changed")
        evidence = json.loads((self.root / entry["evidence"]).read_text(encoding="utf-8"))
        self.assertIn("binding_error", evidence["results"][-1])

    def test_reports_preserve_legacy_verdicts_and_flag_changed_evidence(self):
        self.work.record("older-record", "refuted", "comparison.txt")
        self.work.record("invalid-index", "refuted", "comparison.txt", conclusion="引用失效 | <旧入口>\n另见明细",
                         recheck="复跑 V-COURSE")
        result = write_workspace_reports(self.root)
        index = self.root / result["status_report"]
        findings = self.work.directory / "REJECTED.md"
        self.assertEqual(result["unreadable"], [])
        self.assertIn("[2 条](docs-review/REJECTED.md)", index.read_text(encoding="utf-8"))
        text = findings.read_text(encoding="utf-8")
        self.assertIn("older-record | 已证否", text)
        self.assertIn("引用失效 &#124; &lt;旧入口&gt; 另见明细", text)
        self.assertIn("适用上列输入", text)
        self.assertIn("复跑 V-COURSE", text)
        self.assertIn(self.work.show()["binding"]["inputs"][0]["sha256"], text)
        before = self.work.path.read_bytes()

        (self.root / "comparison.txt").write_bytes(b"changed evidence")
        write_workspace_reports(self.root)
        self.assertIn("证据变更或缺失，需复验", findings.read_text(encoding="utf-8"))
        self.assertEqual(self.work.path.read_bytes(), before)

        (self.root / "input.toml").write_bytes(b"changed input")
        write_workspace_reports(self.root)
        self.assertIn("输入变更，需复验", findings.read_text(encoding="utf-8"))
        self.assertIn("输入变更，需复验", index.read_text(encoding="utf-8"))

    @patch("scripts.project_verification.execute_check")
    def test_index_distinguishes_unwritten_result_and_stale_passing_result(self, execute):
        def run_check(*args, **kwargs):
            snapshot = write_workspace_reports(self.root)
            index = (self.root / snapshot["status_report"]).read_text(encoding="utf-8")
            self.assertIn("结果未落盘", index)
            return {"exit_code": 0, "timed_out": False, "output": "pass"}

        execute.side_effect = run_check
        entry = self.work.run(self.plan())
        snapshot = write_workspace_reports(self.root)
        index = self.root / snapshot["status_report"]
        self.assertIn("| 通过 | stale-doc-links |", index.read_text(encoding="utf-8"))
        (self.root / entry["evidence"]).unlink()
        write_workspace_reports(self.root)
        self.assertIn("结果变更或缺失，需复验", index.read_text(encoding="utf-8"))
        self.assertEqual(execute.call_count, 2)

    def test_manual_report_is_preserved_and_other_work_orders_are_listed(self):
        manual = self.work.directory / "REJECTED.md"
        manual.write_text("手记", encoding="utf-8")
        other = ProjectVerification(self.root, "other-check")
        other.initialize(["input.toml"])
        snapshot = write_workspace_reports(self.root)
        self.assertEqual(snapshot["unreadable"], ["docs-review"])
        self.assertEqual(manual.read_text(encoding="utf-8"), "手记")
        index = (self.root / snapshot["status_report"]).read_text(encoding="utf-8")
        self.assertIn("docs-review | 无法生成交接记录", index)
        self.assertIn("[other-check](other-check/STATE.json) | 待检查", index)

    def test_rejects_a_work_order_copied_under_another_id_and_outside_paths(self):
        other = ProjectVerification(self.root, "different-review")
        other.directory.mkdir()
        other.path.write_bytes(self.work.path.read_bytes())
        with self.assertRaisesRegex(ValueError, "工作区或 ID 不匹配"):
            other.show()
        with self.assertRaisesRegex(ValueError, "路径超出工作区"):
            self.work.record("foreign-input", "verified", "../comparison.txt")

    def test_concurrent_writer_cannot_mutate_work_order(self):
        with self.work._lock(), self.assertRaisesRegex(ValueError, "另一个进程更新"):
            self.work.record("stale-doc-links", "refuted", "comparison.txt")
        self.assertEqual(self.work.show()["verdicts"], {})

    @patch("scripts.verify_contract.subprocess.run")
    def test_check_runner_passes_timeout_and_preserves_partial_output(self, run):
        run.side_effect = subprocess.TimeoutExpired(["python"], .25, output=b"partial", stderr=b" error")
        result = execute_check(self.root, "V-COURSE", timeout=.25)
        self.assertEqual(run.call_args.kwargs["timeout"], .25)
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["output"], "partial error")


if __name__ == "__main__":
    unittest.main()
