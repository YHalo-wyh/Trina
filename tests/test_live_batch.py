"""真模型跑批的离线部分：任务装载与验收判定（不联网、不调用模型）。"""

from __future__ import annotations

import unittest

from trina.eval.live_batch import CONFIGS, LiveTask, judge, load_tasks


class LiveSuiteTest(unittest.TestCase):
    def test_suite_has_at_least_thirty_tasks(self) -> None:
        tasks = load_tasks()
        self.assertGreaterEqual(len(tasks), 30)
        ids = [task.id for task in tasks]
        self.assertEqual(len(ids), len(set(ids)), "任务 id 必须唯一")

    def test_every_task_declares_expectation_and_target(self) -> None:
        for task in load_tasks():
            with self.subTest(task=task.id):
                self.assertTrue(task.expect_tool, "每条任务必须声明期望工具")
                self.assertTrue(task.target.startswith(("http://127.0.0.1", "127.0.0.1")), "只允许本地授权目标")
                self.assertLessEqual(task.max_steps, 4)

    def test_four_configs_are_declared(self) -> None:
        self.assertEqual(
            set(CONFIGS),
            {"legacy-json", "layered-json", "legacy-native", "layered-native"},
        )


class JudgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.task = LiveTask(id="t", goal="g", target="http://127.0.0.1:9099/", expect_tool="http_fingerprint")

    def test_expected_tool_ok_accepts(self) -> None:
        steps = [{"action": "http_fingerprint", "ok": True}, {"action": "final"}]
        verdict = judge(self.task, steps, [])
        self.assertTrue(verdict["chosen"])
        self.assertTrue(verdict["tool_ok"])
        self.assertTrue(verdict["accepted"])

    def test_wrong_tool_is_rejected(self) -> None:
        steps = [{"action": "port_scan", "ok": True}, {"action": "final"}]
        self.assertFalse(judge(self.task, steps, [])["accepted"])

    def test_tool_failure_not_accepted(self) -> None:
        steps = [{"action": "http_fingerprint", "ok": False, "error": "timeout"}]
        verdict = judge(self.task, steps, [])
        self.assertTrue(verdict["chosen"])
        self.assertFalse(verdict["tool_ok"])
        self.assertFalse(verdict["accepted"])

    def test_flag_task_requires_flag(self) -> None:
        task = LiveTask(id="f", goal="g", target="http://127.0.0.1:9099", expect_tool="flag_hunt", expect_flag=True)
        steps = [{"action": "flag_hunt", "ok": True}]
        self.assertFalse(judge(task, steps, [])["accepted"])
        self.assertTrue(judge(task, steps, ["flag{trina_lab_x}"])["accepted"])

    def test_expect_flag_ignored_for_non_flag_tasks(self) -> None:
        steps = [{"action": "http_fingerprint", "ok": True}]
        self.assertTrue(judge(self.task, steps, [])["accepted"])


if __name__ == "__main__":
    unittest.main()
