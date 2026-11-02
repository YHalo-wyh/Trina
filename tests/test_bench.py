"""TSecBench 答题 API 接入测试（本地假服务，不出公网）。"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from trina.bench import BenchOptions, BenchmarkClient, BenchmarkRunner, submission_candidates
from trina.config import ScopeConfig, TrinaConfig
from trina.scope import ScopeGuard
from trina.state import RunState


class _FakeAPI(BaseHTTPRequestHandler):
    """最小答题 API：只实现测试需要的 4 个端点。"""

    token = "test-token"
    submitted: list[tuple[str, str]] = []
    started: list[str] = []
    closed: list[str] = []
    listed = 0
    flags = {"challenge-1": ["flag{one}"]}

    def log_message(self, *args):  # noqa: D401 - 静默
        pass

    def _json(self, status: int, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self) -> bool:
        if self.headers.get("BENCHMARK_TOKEN") != self.token:
            self._json(404, {"code": "task_not_found", "message": "bad token", "detail": {}})
            return False
        return True

    def do_GET(self):  # noqa: N802
        if not self._auth():
            return
        if self.path == "/openapi/v1/challenges":
            type(self).listed += 1
            self._json(200, [
                {
                    "unique_code": "challenge-1",
                    "description": "测试题",
                    "difficulty": "easy",
                    "level": 1,
                    "total_score": 100,
                    "flag_count": 1,
                    "correct_flag_count": 0,
                    "is_completed": False,
                    "container_status": "stopped",
                    "container_addr": [],
                }
            ])
            return
        self._json(404, {"code": "challenge_not_found", "message": "not found", "detail": {}})

    def do_POST(self):  # noqa: N802
        if not self._auth():
            return
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        code = (query.get("unique_code") or [""])[0]
        if parts.path == "/openapi/v1/challenges/start":
            type(self).started.append(code)
            self._json(200, {"unique_code": code, "container_addr": ["127.0.0.1:8080"]})
            return
        if parts.path == "/openapi/v1/challenges/close":
            type(self).closed.append(code)
            self._json(200, {"unique_code": code, "closed": True})
            return
        if parts.path == "/openapi/v1/challenges/submit":
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            code = str(payload.get("unique_code") or code)
            flag = str(payload.get("flag") or "")
            type(self).submitted.append((code, flag))
            correct = flag in self.flags.get(code, [])
            self._json(
                200,
                {
                    "correct": correct,
                    "awarded": 100 if correct else 0,
                    "cumulative_score": 100 if correct else 0,
                    "correct_flag_count": 1 if correct else 0,
                    "total_flag_count": 1,
                    "matched_flag_index": 0 if correct else None,
                },
            )
            return
        self._json(404, {"code": "challenge_not_found", "message": "not found", "detail": {}})


class _Server:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAPI)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def _guard() -> ScopeGuard:
    scope = ScopeConfig(control_plane_hosts=["127.0.0.1"])
    scope.validate()
    return ScopeGuard(scope)


class TestControlPlaneGuard(unittest.TestCase):
    def test_only_whitelisted_host(self):
        g = _guard()
        self.assertTrue(g.check_control_plane("http://127.0.0.1:8080/openapi/v1/challenges"))
        self.assertFalse(g.check_control_plane("http://evil.example/openapi/v1/challenges"))
        self.assertFalse(g.check_control_plane("http://127.0.0.1:8080/x", method="DELETE"))

    def test_control_plane_does_not_authorize_target_scan(self):
        g = _guard()
        # 控制面白名单只影响答题 API，不把公网主机变成可扫描目标
        self.assertFalse(g.check_target("8.8.8.8"))


class TestClient(unittest.TestCase):
    def test_flow_and_auth(self):
        _FakeAPI.submitted = []
        _FakeAPI.started = []
        _FakeAPI.closed = []
        with _Server() as server:
            client = BenchmarkClient(server.url, "test-token", _guard())
            tasks = client.list_challenges()
            self.assertEqual(tasks[0]["unique_code"], "challenge-1")
            started = client.start("challenge-1")
            self.assertEqual(started["container_addr"], ["127.0.0.1:8080"])
            result = client.submit("challenge-1", "flag{one}")
            self.assertTrue(result["correct"])
            closed = client.close("challenge-1")
            self.assertTrue(closed["closed"])
        self.assertEqual(_FakeAPI.started, ["challenge-1"])
        self.assertEqual(_FakeAPI.submitted, [("challenge-1", "flag{one}")])
        self.assertEqual(_FakeAPI.closed, ["challenge-1"])


class TestRunner(unittest.TestCase):
    def test_submission_candidates_include_state_and_evidence(self):
        state = RunState(run_id="r", target="http://127.0.0.1:8080")
        state.flags = [{"value": "flag{from_state}", "source": "flag_hunt"}]
        state.evidence = [{"response": {"body_excerpt": "noise flag{from_body} tail"}}]
        values = submission_candidates(state)
        self.assertEqual(values[0], "flag{from_state}")
        self.assertIn("flag{from_body}", values)

    def test_submission_candidates_skip_placeholders(self):
        state = RunState(run_id="r", target="http://127.0.0.1:8080", goal="flag 形如 flag{...}，也可能是 flag{example_flag_value}")
        state.flags = [{"value": "flag{...}"}]
        values = submission_candidates(state)
        self.assertEqual(values, [])

    def test_runner_solves_and_closes(self):
        _FakeAPI.submitted = []
        _FakeAPI.started = []
        _FakeAPI.closed = []
        with _Server() as server:
            client = BenchmarkClient(server.url, "test-token", _guard())

            def fake_runner(task, target, goal, **kwargs):
                state = RunState(run_id="r", target=target)
                state.flags = [{"value": "flag{one}", "source": "fake"}]
                state.status = "success"
                return state

            runner = BenchmarkRunner(
                TrinaConfig(),
                client,
                options=BenchOptions(max_steps=1, per_task_seconds=30, total_seconds=120),
                runner=fake_runner,
                log=lambda message: None,
            )
            summary = runner.run()
        self.assertEqual(summary.solved, 1)
        self.assertEqual(summary.score, 100)
        self.assertEqual(_FakeAPI.submitted, [("challenge-1", "flag{one}")])
        self.assertEqual(_FakeAPI.closed, ["challenge-1"])

    def test_container_closed_when_runner_raises(self):
        _FakeAPI.closed = []
        with _Server() as server:
            client = BenchmarkClient(server.url, "test-token", _guard())

            def broken(*args, **kwargs):
                raise RuntimeError("boom")

            runner = BenchmarkRunner(
                TrinaConfig(),
                client,
                options=BenchOptions(max_steps=1, per_task_seconds=30, total_seconds=120),
                runner=broken,
                log=lambda message: None,
            )
            summary = runner.run()
        self.assertEqual(summary.solved, 0)
        self.assertIn("challenge-1", _FakeAPI.closed)


if __name__ == "__main__":
    unittest.main()
