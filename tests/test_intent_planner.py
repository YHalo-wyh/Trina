"""事实提案的范围、来源去重和预算边界。"""
from __future__ import annotations

import unittest

from trina.intent_planner import FactIntentPlanner
from trina.swarm import TaskSpec


class FactIntentPlannerTests(unittest.TestCase):
    def test_only_supported_verified_facts_generate_proposals(self):
        facts = [
            {"id": "asset", "kind": "asset", "payload": {"ports": {"80": {}}}},
            {"id": "finding", "kind": "finding", "payload": {"status": "verified"}},
            {"id": "exploited", "kind": "finding", "payload": {"status": "exploited"}},
            {"id": "credential", "kind": "credential", "payload": {"verified": True}},
            {"id": "unverified", "kind": "finding", "payload": {"status": "detected"}},
            {"id": "candidate", "kind": "credential", "payload": {"verified": False}},
            {"id": "flag", "kind": "flag", "payload": {"value": "flag{test}"}},
        ]
        proposals = FactIntentPlanner().propose(TaskSpec("seed", "host.test", "分析"), facts)
        self.assertEqual([item.fact_ids[0] for item in proposals], ["asset", "finding", "exploited", "credential"])

    def test_child_inherits_scope_and_never_increases_source_limits(self):
        source = TaskSpec("seed", "host.test", "分析", max_steps=1, max_seconds=2,
                          capabilities=("binary.debug",))
        proposal = FactIntentPlanner().propose(source, [
            {"id": "fact", "kind": "asset", "payload": {"ports": {"80": {}}}},
        ])[0]
        self.assertEqual(proposal.target, source.target)
        self.assertEqual(proposal.capabilities, source.capabilities)
        self.assertEqual((proposal.max_steps, proposal.max_seconds), (1, 2))
        self.assertEqual(proposal.depends_on, ("seed",))

    def test_updated_observation_in_same_lineage_reuses_planning_key(self):
        planner = FactIntentPlanner()
        seed = TaskSpec("seed", "host.test", "分析")
        first = planner.propose(seed, [
            {"id": "one", "kind": "asset", "payload": {"ports": {"80": {}}}},
        ])[0]
        followup = TaskSpec(first.id, seed.target, first.goal)
        second = planner.propose(followup, [
            {"id": "two", "kind": "asset", "payload": {"ports": {"80": {}, "443": {}}}},
        ], planning_scope=seed.id)[0]
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.depends_on, (followup.id,))


if __name__ == "__main__":
    unittest.main()
