"""CTFY v3 合同与实际回环 HTTP 协议夹具；不冒充线上平台测评。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from trina.audit import AuditLog
from trina.config import TrinaConfig
from trina.ctfy import CTFYTask, run_ctfy
from trina.scope import ScopeGuard
from trina.state import RunState
from trina.tools import build_registry
from trina.tools.registry import ToolContext


class CTFYTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.requests = []
        self.gateway_calls = 0
        self.pending = False
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                owner.requests.append(("GET", self.path, None))
                self.reply({"questions": [{"id": "flag"}, {"id": "next"}]})
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path.endswith("chat/completions"):
                    owner.gateway_calls += 1
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    if owner.gateway_calls == 1:
                        text = json.dumps({"action": "ctfy_task", "params": {"action": "submit", "question_id": "flag", "answer": "right"}})
                    else:
                        text = "本地协议夹具结束"
                    for frame in ({"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
                                  {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 50, "completion_tokens": 10}}):
                        self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                owner.requests.append(("POST", self.path, body))
                if owner.pending:
                    self.reply({"status": "pending"})
                else:
                    self.reply({"id": "submission-local", "question_id": body["question_id"],
                                "correct": body["answer"] == "right", "challenge_fully_solved": body["answer"] == "right"})
            def reply(self, body):
                encoded = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.task = {"schema_version": 3, "run_mode": "instance", "challenge": {"id": "local-fixture"},
                     "competition_id": "local", "questions": [{"id": "flag"}], "gated_questions": 1,
                     "platform_url": self.base, "target_instance": "this-instance"}
        self.write_task()
        env = patch.dict(os.environ, {"CTFY_PLATFORM_URL": self.base, "CTFY_TOKEN": "fixture-token",
            "CTFY_EVAL_DRY_RUN": "0", "LLM_API_KEY": "fixture-model-key", "LLM_MODEL": "test-gateway",
            "LLM_FLAVOR": "openai", "LLM_BASE_URL": self.base + "/v1", "CTFY_EVAL_MAX_TURNS": "3"})
        env.start()
        self.addCleanup(env.stop)
        cfg = TrinaConfig().with_overrides(scope__control_plane_hosts=["127.0.0.1"], run__workspace=str(self.root / "runs"), audit__dir=str(self.root / "logs"))
        self.ctx = ToolContext(config=cfg, guard=ScopeGuard(cfg.scope), audit=AuditLog.open(cfg.audit_dir, "ctfy-fixture"),
            state=RunState("ctfy-fixture", "", "protocol"), http=None, workspace=cfg.workspace_dir / "ctfy-fixture")

    def write_task(self):
        (self.root / "task.json").write_text(json.dumps(self.task), encoding="utf-8")

    def test_actual_http_instance_and_qa_verdicts_and_gated_refresh(self):
        task = CTFYTask.load(self.root)
        data, _ = task.submit(self.ctx, "flag", "wrong")
        self.assertFalse(data["correct"])
        self.assertEqual(task.solved, [])
        task.submit(self.ctx, "flag", "right")
        self.assertEqual(task.solved, ["flag"])
        self.assertIn("next", {row["id"] for row in task.task["questions"]})
        self.assertEqual(self.requests[0][2]["instance_id"], "this-instance")
        task.task["run_mode"] = "qa"
        task.submit(self.ctx, "next", ["right", "backup"])
        post = [row for row in self.requests if row[0] == "POST"][-1]
        self.assertEqual(post[1], "/api/v1/qa/submissions")
        self.assertNotIn("instance_id", post[2])
        self.assertEqual(post[2]["answer"], ["right", "backup"])
        self.assertEqual(task.answers, {}, "已在线提交的答案不得在 result.json 自动重复提交")

    def test_pending_is_not_verified_and_cross_question_is_refused(self):
        task = CTFYTask.load(self.root)
        self.pending = True
        with self.assertRaisesRegex(ValueError, "correct"):
            task.submit(self.ctx, "flag", "right")
        self.assertFalse(task.fully_solved)
        self.assertEqual(task.solved, [])
        before = len(self.requests)
        with self.assertRaises(ValueError):
            task.submit(self.ctx, "other-question", "anything")
        self.assertEqual(len(self.requests), before)

    def test_dry_run_no_credentials_no_gateway_and_missing_key_no_fallback(self):
        current = Path.cwd()
        try:
            with patch.dict(os.environ, {"CTFY_EVAL_DRY_RUN": "1", "LLM_API_KEY": ""}):
                self.assertEqual(run_ctfy(self.root), 0)
            self.assertEqual(self.gateway_calls, 0)
            self.assertEqual(json.loads((self.root / "result.json").read_text())["stop_reason"], "dry_run")
            with patch.dict(os.environ, {"LLM_API_KEY": ""}):
                with self.assertRaisesRegex(ValueError, "不回退"):
                    run_ctfy(self.root)
        finally:
            os.chdir(current)

    def test_actual_gateway_chat_registry_submit_and_result_file(self):
        current = Path.cwd()
        try:
            self.assertEqual(run_ctfy(self.root), 0)
            result = json.loads((self.root / "result.json").read_text())
            self.assertEqual(result["solved"], ["flag"])
            self.assertTrue(result["fully_solved"])
            self.assertGreater(self.gateway_calls, 0)
            self.assertGreater(result["input_tokens"], 0)
            saved = (self.root / "result.json").read_text()
            self.assertNotIn("fixture-model-key", saved)
            self.assertNotIn("fixture-token", saved)
        finally:
            os.chdir(current)
