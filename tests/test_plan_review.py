"""工程分工预览的结构校验。"""
from __future__ import annotations

import json
import unittest
from unittest.mock import Mock

from trina.llm.base import LLMResponse
from trina.plan_review import PlanReview


def branch(branch_id: str, *dependencies: str) -> dict:
    return {
        "id": branch_id, "title": branch_id, "depends_on": list(dependencies),
        "owner": "worker", "deliverable": "独立交付", "reason": "验收清晰",
    }


class PlanReviewTests(unittest.TestCase):
    def request(self, payload: dict) -> PlanReview:
        backend = Mock()
        backend.complete_messages.return_value = LLMResponse(text=json.dumps(payload))
        review = PlanReview("整理文档", split_preview=True).request(backend)
        self.assertNotIn("tools", backend.complete_messages.call_args.kwargs)
        return review

    def test_groups_branches_with_shared_prerequisites_and_preserves_actual_edges(self):
        review = self.request({"branches": [
            branch("review", "docs", "tests"), branch("docs"), branch("tests"), branch("examples", "docs"),
        ]})
        self.assertEqual([[b.id for b in layer] for layer in review.layers], [
            ["docs", "tests"], ["review", "examples"],
        ])
        self.assertEqual(review.layers[1][1].depends_on, ("docs",))
        self.assertIn("前置 docs", review.split_sections()[2][1])

    def test_rejects_cycle_unknown_duplicate_and_unexpected_fields(self):
        for branches, error in (
            ([branch("a", "b"), branch("b", "a")], "循环依赖"),
            ([branch("a", "missing"), branch("b")], "不存在"),
            ([branch("a"), branch("a")], "重复"),
            ([{**branch("docs"), "command": "unexpected"}], "字段"),
        ):
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                self.request({"branches": branches})

    def test_followup_keeps_read_only_mode(self):
        review = self.request({"question": "覆盖范围？", "options": ["用户手册", "开发手册"]})
        next_review = review.followup("用户手册")
        self.assertTrue(next_review.split_preview)
        self.assertEqual(next_review.answers, (("覆盖范围？", "用户手册"),))

    def test_unsupported_preview_returns_a_clear_error(self):
        with self.assertRaisesRegex(ValueError, "离线测试规划"):
            self.request({"unsupported": True})


if __name__ == "__main__":
    unittest.main()
