"""项目目标与一次性定时任务的持久化、领取和执行状态测试。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from trina.cli import build_parser
from trina.config import TrinaConfig
from trina.tasks import ScheduledTask, TaskScheduler, TaskStore, execute_scheduled, parse_start


class TestTaskStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "tasks.json"
        self.project = Path(self.tmp.name) / "project-a"
        self.project.mkdir()
        self.store = TaskStore(self.path, project=self.project)
        self.config = TrinaConfig.load()

    def test_parse_relative_clock_and_reject_past(self):
        now = datetime(2026, 11, 6, 13, 0, tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual(parse_start("+10m", now=now), now + timedelta(minutes=10))
        self.assertEqual(parse_start("13:01", now=now), now.replace(minute=1))
        with self.assertRaisesRegex(ValueError, "未来"):
            parse_start("2026-11-06T12:00:00+08:00", now=now)

    def test_goal_is_project_scoped_and_can_be_cleared(self):
        other = TaskStore(self.path, project=Path(self.tmp.name) / "project-b")
        self.store.set_goal("验证登录流程")
        self.assertEqual(self.store.goal(), "验证登录流程")
        self.assertEqual(other.goal(), "")
        self.store.set_goal("")
        self.assertEqual(self.store.goal(), "")

    def test_claim_is_exclusive_and_crashed_task_is_not_replayed(self):
        now = datetime.now().astimezone()
        first = self.store.add("127.0.0.1", "+1s", config=self.config, now=now)
        second_store = TaskStore(self.path, project=self.project)
        due = now.timestamp() + 2
        claimed = self.store.claim("worker-a", now=due)
        self.assertEqual(claimed.id, first.id)
        self.assertIsNone(second_store.claim("worker-b", now=due))
        self.assertIsNone(second_store.claim("worker-b", now=due + 61))
        self.assertEqual(second_store.list()[0].status, "interrupted")

    def test_cancel_and_scheduler_completion(self):
        now = datetime.now().astimezone()
        cancelled = self.store.add("127.0.0.1", "+1s", config=self.config, now=now)
        self.store.cancel(cancelled.id)
        self.assertEqual(self.store.list()[0].status, "cancelled")

        task = self.store.add("127.0.0.1", "+1s", goal="收集服务", config=self.config, now=now)
        seen = []
        scheduler = TaskScheduler(
            self.store,
            execute=lambda item: seen.append(item) or SimpleNamespace(run_id="run-1", status="success"),
            log=lambda _message: None,
        )
        self.assertTrue(scheduler.tick(now=now.timestamp() + 2))
        self.assertEqual(seen[0].goal, "收集服务")
        stored = {item.id: item for item in self.store.list()}[task.id]
        self.assertEqual((stored.status, stored.run_id, stored.outcome), ("completed", "run-1", "success"))

    def test_scheduler_failure_is_recorded_once(self):
        now = datetime.now().astimezone()
        task = self.store.add("127.0.0.1", "+1s", config=self.config, now=now)

        def fail(_task):
            raise RuntimeError("fixture")

        scheduler = TaskScheduler(self.store, execute=fail, log=lambda _message: None)
        self.assertTrue(scheduler.tick(now=now.timestamp() + 2))
        stored = {item.id: item for item in self.store.list()}[task.id]
        self.assertEqual(stored.status, "failed")
        self.assertIn("RuntimeError: fixture", stored.error)

    def test_cli_supports_both_spelling_aliases(self):
        for name in ("schedule", "scheduled", "schedualed"):
            args = build_parser().parse_args([name, "list"])
            self.assertEqual(args.schedule_action, "list")
        self.assertIsNone(build_parser().parse_args(["run", "--target", "127.0.0.1"]).goal)
        self.assertIsNone(build_parser().parse_args(["play", "--target", "127.0.0.1"]).goal)

    def test_headless_approval_denies_levels_that_need_a_person(self):
        task = ScheduledTask("fixture", str(self.project), datetime.now().astimezone().isoformat(),
                             "127.0.0.1", approve="exploit")

        def run_plan(_config, _plan, **kwargs):
            self.assertTrue(kwargs["approval"]({"level": "detect"}))
            self.assertFalse(kwargs["approval"]({"level": "exploit"}))
            return SimpleNamespace(run_id="fixture", status="partial")

        with patch("trina.play.build_plan", return_value=SimpleNamespace()), patch("trina.play.run_plan", side_effect=run_plan):
            state = execute_scheduled(task, log=lambda _message: None)
        self.assertEqual(state.status, "partial")


if __name__ == "__main__":
    unittest.main()
