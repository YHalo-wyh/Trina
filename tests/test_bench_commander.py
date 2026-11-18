"""Bench Commander 动态决策测试。"""

from __future__ import annotations

import unittest

from trina.bench.commander import (
    CONTINUE,
    EXTEND,
    RELEASE,
    RETRY,
    BenchCommander,
    CommanderSettings,
)


class CommanderTests(unittest.TestCase):
    def setUp(self):
        self.commander = BenchCommander(CommanderSettings(
            extension_seconds=60,
            max_extensions=1,
            reserve_ratio=0.10,
            min_remaining_seconds=120,
            max_redundant_rate=0.80,
        ))

    def review(self, **overrides):
        values = {
            "status": "failed",
            "correct_flags": 0,
            "total_flags": 1,
            "progressed": False,
            "redundant_rate": 0.0,
            "attempt": 1,
            "extensions": 0,
            "task_remaining": 0,
            "global_remaining": 600,
            "total_seconds": 600,
        }
        values.update(overrides)
        return self.commander.review(**values)

    def test_partial_flag_uses_remaining_task_budget_first(self):
        decision = self.review(correct_flags=1, total_flags=2, progressed=True, task_remaining=30)
        self.assertEqual(decision.action, RETRY)

    def test_partial_flag_extends_from_reserve(self):
        decision = self.review(correct_flags=1, total_flags=2, progressed=True, task_remaining=0)
        self.assertEqual(decision.action, EXTEND)
        self.assertEqual(decision.seconds, 60)

    def test_new_facts_extend(self):
        decision = self.review(progressed=True, total_flags=1)
        self.assertEqual(decision.action, EXTEND)

    def test_no_progress_releases(self):
        self.assertEqual(self.review().action, RELEASE)

    def test_high_redundancy_releases(self):
        decision = self.review(progressed=True, redundant_rate=0.90)
        self.assertEqual(decision.action, RELEASE)

    def test_partial_flag_ignores_high_redundancy(self):
        decision = self.review(correct_flags=1, total_flags=2, progressed=False, redundant_rate=0.90)
        self.assertEqual(decision.action, EXTEND)

    def test_global_reserve_blocks_extension(self):
        # total=600 → reserve=120；剩余 170 − 60 = 110 < 120。
        decision = self.review(progressed=True, global_remaining=170)
        self.assertEqual(decision.action, RELEASE)

    def test_max_extensions_blocks_extension(self):
        decision = self.review(progressed=True, extensions=1)
        self.assertEqual(decision.action, RELEASE)

    def test_blocked_releases(self):
        decision = self.review(status="blocked", progressed=True)
        self.assertEqual(decision.action, RELEASE)

    def test_unlimited_total_allows_extension(self):
        decision = self.review(progressed=True, total_seconds=0, global_remaining=float("inf"))
        self.assertEqual(decision.action, EXTEND)

    def test_settings_validation(self):
        with self.assertRaises(ValueError):
            CommanderSettings(reserve_ratio=1.5).validate()
        with self.assertRaises(ValueError):
            CommanderSettings(max_extensions=-1).validate()

    def mid_review(self, **overrides):
        values = {
            "status": "running",
            "correct_flags": 0,
            "total_flags": 1,
            "progressed": False,
            "redundant_rate": 0.0,
            "extensions": 0,
            "task_remaining": 100,
            "global_remaining": 600,
            "total_seconds": 600,
        }
        values.update(overrides)
        return self.commander.review_mid_attempt(**values)

    def test_mid_continues_without_progress(self):
        self.assertEqual(self.mid_review().action, CONTINUE)

    def test_mid_stops_on_high_redundancy(self):
        decision = self.mid_review(redundant_rate=0.90)
        self.assertEqual(decision.action, RELEASE)

    def test_mid_extends_near_deadline_with_progress(self):
        decision = self.mid_review(progressed=True, task_remaining=0)
        self.assertEqual(decision.action, EXTEND)
        self.assertEqual(decision.seconds, 60)

    def test_mid_extends_partial_flag(self):
        decision = self.mid_review(correct_flags=1, total_flags=2, task_remaining=0)
        self.assertEqual(decision.action, EXTEND)

    def test_mid_max_extensions_continues(self):
        decision = self.mid_review(progressed=True, task_remaining=0, extensions=1)
        self.assertEqual(decision.action, CONTINUE)


if __name__ == "__main__":
    unittest.main()
