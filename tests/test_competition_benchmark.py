"""本地工程对照必须由真实独立验收决定通过，缺失 Token 不冒充零用量。"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from benchmark_competition import (SOLUTIONS, config_for, digest, fixture, measure, prepare_memory,
                                   verified_receipt)
from trina.audit import AuditLog
from trina.scope import ScopeGuard
from trina.state import RunState
from trina.tools import build_registry
from trina.tools.registry import ToolContext
import trina.swarm as current


class CompetitionBenchmarkTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def check(self, source):
        item = fixture(self.root / "case", "prefix")
        item["solution"].write_text(source, encoding="utf-8")
        config = config_for(self.root, memory=False)
        audit = AuditLog.open(config.audit_dir, run_id="check")
        registry = build_registry(["workspace"])
        ctx = ToolContext(config=config, guard=ScopeGuard(config.scope, audit=audit), audit=audit,
            state=RunState("check", str(item["solution"])), http=None, workspace=self.root, tools=registry)
        self.addCleanup(ctx.extra["background_jobs"].close)
        before = time.time()
        result = registry.execute("terminal_exec", {"argv": [sys.executable, str(item["verifier"]),
            str(item["solution"])], "cwd": "run"}, ctx)
        return item, before, time.time(), result

    def test_checker_rejects_wrong_solution_despite_success_claim(self):
        item, begin, end, result = self.check("def solve(values, target): return 0\n")
        self.assertFalse(result.ok)
        self.assertEqual(verified_receipt(item, begin, end), {})

    def test_real_checker_passes_and_modified_or_stale_receipt_is_rejected(self):
        item, begin, end, result = self.check(SOLUTIONS["prefix"])
        self.assertTrue(result.ok, result.error)
        self.assertTrue(verified_receipt(item, begin, end))
        self.assertEqual(verified_receipt(item, end + 1, end + 2), {})
        item["verifier"].write_text("print('OK')", encoding="utf-8")
        self.assertEqual(verified_receipt(item, begin, end), {})

    def test_replay_matrix_runs_real_agents_with_frozen_memory(self):
        memory = prepare_memory(self.root / "training")
        frozen = digest(memory)
        row = measure(self.root / "D", "D_memory", current, memory)
        self.assertEqual(row["verified_fixture_rate"], 1)
        self.assertEqual(row["model_requests"], 24)
        self.assertEqual(row["context_budget_errors"], 0)
        self.assertEqual(row["provider_requests"], 0)
        self.assertIsNone(row["competition_success_rate"])
        self.assertIsNone(row["total_tokens"])
        self.assertGreater(row["memory"]["hits"], 0)
        self.assertLessEqual(row["peak_concurrency"], 4)
        self.assertEqual(row["duplicate_executions"], 0)
        self.assertEqual(row["latency"]["task_queue"]["samples"], 6)
        self.assertEqual(digest(memory), frozen)
        self.assertTrue(all(item["queue_seconds"] >= 0 for item in row["tasks"].values()))
