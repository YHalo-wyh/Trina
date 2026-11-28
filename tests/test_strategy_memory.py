"""经验卡证据、条件、去重和真实 Commander 简报链路的离线测试。"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from trina.config import TrinaConfig
from trina.state import RunState
from trina.strategy_memory import StrategyMemory
from trina.swarm import SwarmRuntime, TaskSpec


class StrategyMemoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = StrategyMemory(self.root / "memory.sqlite3")
        self.task = TaskSpec("train", "puzzle.py", "实现 prefix sum 子数组统计代码", context_key="integers-negative-v1")

    def verified(self, run_id="train-run", stdout="OK\n", workspace=None):
        directory = workspace or self.root / run_id
        directory.mkdir(parents=True, exist_ok=True)
        proof = directory / "receipt.json"
        proof.write_text(json.dumps({"argv": ["python", "verifier.py"], "exit_code": 0,
                                    "stdout": stdout, "stderr": ""}), encoding="utf-8")
        state = RunState(run_id, self.task.target, self.task.goal, status="success")
        state.evidence = [{"id": "cmd-check", "path": str(proof), "sha256": hashlib.sha256(proof.read_bytes()).hexdigest(),
                           "successful": True, "kind": "command"}]
        state.casebook = {"goal_verifications": [{"status": "passed", "evidence_id": "cmd-check"}],
                         "facts": [{"text": "prefix sum 对负数输入可用", "evidence_refs": ["cmd-check"]}]}
        state.steps = [{"action": "terminal_exec", "ok": True, "summary": "通过输入断言"},
                       {"action": "terminal_exec", "ok": False, "summary": "滑窗未通过负数输入；不是环境错误"}]
        state.ledger = {"total_tokens": 120}
        state.ended_at = state.started_at + 1.5
        return state

    def save(self, state=None, task=None):
        state = state or self.verified()
        return self.memory.remember(task or self.task, state, workspace=self.root)

    def test_unverified_success_flag_or_forged_receipt_is_not_stored(self):
        state = RunState("unverified", "puzzle.py", status="success")
        state.flags = [{"value": "flag{claim}"}]
        self.assertIsNone(self.save(state))
        self.assertIsNone(self.save(self.verified(stdout="model says passed")))
        self.assertFalse(self.memory.path.exists())

    def test_round_trip_has_conditions_cost_failures_and_no_fact_injection(self):
        self.assertTrue(self.save())
        brief = self.memory.retrieve(replace(self.task, id="new"))[0]
        card = brief["experience"]
        self.assertEqual(card["conditions"]["context_key"], self.task.context_key)
        self.assertEqual(card["tokens"], 120)
        self.assertEqual(card["elapsed_seconds"], 1.5)
        self.assertEqual(len(card["failed_strategy"]), 1)
        self.assertEqual(brief["intelligence"], {})
        self.assertEqual(brief["destination_task"], "new")
        self.assertTrue(brief["evidence_refs"])

    def test_conflicting_conditions_excluded_and_missing_marked(self):
        self.save()
        self.assertEqual(self.memory.retrieve(replace(self.task, context_key="positive-only")), [])
        missing = self.memory.retrieve(replace(self.task, context_key=""))
        self.assertEqual(missing[0]["needs_revalidation"], ["context_key"])

    def test_unrelated_category_or_features_do_not_match(self):
        self.save()
        self.assertEqual(self.memory.retrieve(replace(self.task, goal="pcap 网络取证", target="traffic.pcap")), [])
        self.assertEqual(self.memory.retrieve(replace(self.task, goal="修复 markdown table 格式")), [])

    def test_duplicate_proof_is_idempotent_but_new_conditions_preserved(self):
        first = self.save()
        self.assertEqual(self.save(), first)
        second = self.save(task=replace(self.task, context_key="another-environment"))
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.memory.retrieve(replace(self.task, context_key=""))), 2)

    def test_modified_missing_synthetic_or_outside_proof_rejected(self):
        state = self.verified()
        state.evidence[0]["synthetic"] = True
        self.assertIsNone(self.save(state))
        state.evidence[0].pop("synthetic")
        self.assertIsNone(self.memory.remember(self.task, state, workspace=self.root / "other"))
        self.save(state)
        Path(state.evidence[0]["path"]).write_text("modified", encoding="utf-8")
        self.assertEqual(self.memory.retrieve(self.task), [])
        self.assertIsNone(self.save(state))

    def test_readonly_snapshot_and_context_size_limits(self):
        self.save()
        before = self.memory.path.read_bytes()
        frozen = StrategyMemory(self.memory.path, readonly=True)
        self.assertIsNone(frozen.remember(self.task, self.verified("new-run"), workspace=self.root))
        self.assertTrue(frozen.retrieve(self.task))
        self.assertEqual(frozen.retrieve(self.task, max_chars=50), [])
        self.assertEqual(frozen.retrieve(self.task, limit=0), [])
        self.assertEqual(before, self.memory.path.read_bytes())

    def test_retrieval_bounds_proof_io_and_checks_shared_proof_once_per_query(self):
        state = self.verified()
        self.save(state)
        self.save(state, task=replace(self.task, context_key="another-environment"))
        query = replace(self.task, context_key="")
        with patch.object(self.memory, "_proof", wraps=self.memory._proof) as proof:
            self.assertEqual(len(self.memory.retrieve(query)), 2)
            self.assertEqual(proof.call_count, 1)
            self.assertEqual(len(self.memory.retrieve(query)), 2)
            self.assertEqual(proof.call_count, 2, "跨查询必须重新核验，不能复用过期证明")
        with patch("trina.strategy_memory._PROOF_READ_BUDGET", 1), \
                patch.object(self.memory, "_proof", wraps=self.memory._proof) as proof:
            self.assertEqual(self.memory.retrieve(query), [])
            proof.assert_not_called()
        with patch.object(self.memory, "_proof", wraps=self.memory._proof) as proof:
            self.assertEqual(self.memory.retrieve(query, max_chars=10), [])
            proof.assert_not_called()

    def test_commander_uses_existing_briefs_and_caches_lookup_without_models(self):
        self.save()
        seen = []
        config = TrinaConfig.load().with_overrides(run__workspace=str(self.root / "runs"),
            audit__dir=str(self.root / "logs"), report__output_dir=str(self.root / "reports"))
        def execute(worker, task, run_id, hints, registry):
            seen.extend(json.loads(item) for item in hints)
            return RunState(run_id, task.target, task.goal, status="success")
        task = replace(self.task, id="new")
        runtime = SwarmRuntime(config, [task], executor=execute, strategy_memory=self.memory,
                               backend_factory=lambda config: object(), write_report=False)
        runtime._briefs(task)
        runtime._briefs(task)
        summary = runtime.run()
        self.assertEqual(len(seen), 1)
        self.assertEqual(summary["strategy_memory"]["queries"], 1)
        self.assertEqual(summary["resources"]["model_requests"], 0)
        self.assertEqual(runtime.coordination.facts, {})

    def test_commander_records_verified_completion_and_disabled_mode_is_empty(self):
        config = TrinaConfig.load().with_overrides(run__workspace=str(self.root / "runs"),
            audit__dir=str(self.root / "logs"), report__output_dir=str(self.root / "reports"))
        def execute(worker, task, run_id, hints, registry):
            return self.verified(run_id, workspace=config.workspace_dir / run_id)
        runtime = SwarmRuntime(config, [self.task], executor=execute,
            backend_factory=lambda config: object(), write_report=False)
        self.assertEqual(runtime.run()["strategy_memory"]["stored"], 1)
        self.assertTrue(runtime.strategy_memory.retrieve(self.task))
        disabled = SwarmRuntime(config.with_overrides(run__strategy_memory=False), [self.task],
            executor=execute, backend_factory=lambda config: object(), write_report=False)
        self.assertEqual(disabled._briefs(self.task), [])
        self.assertIsNone(disabled.strategy_memory)

    def test_memory_failure_does_not_fail_worker(self):
        self.memory.path.write_text("not sqlite", encoding="utf-8")
        config = TrinaConfig.load().with_overrides(run__workspace=str(self.root / "runs"),
            audit__dir=str(self.root / "logs"), report__output_dir=str(self.root / "reports"))
        runtime = SwarmRuntime(config, [self.task], strategy_memory=self.memory,
            executor=lambda worker, task, run_id, hints, registry: RunState(run_id, task.target, status="success"),
            backend_factory=lambda config: object(), write_report=False)
        summary = runtime.run()
        self.assertEqual(summary["tasks"][self.task.id]["status"], "completed")
        self.assertEqual(summary["strategy_memory"]["errors"], 1)
