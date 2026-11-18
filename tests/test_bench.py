"""TSecBench 答题 API 接入测试（本地假服务，不出公网）。"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from trina.bench import BenchOptions, BenchmarkClient, BenchmarkError, BenchmarkRunner, submission_candidates
from trina.bench.runner import choose_target
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
    def __init__(self, handler=_FakeAPI):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
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
    scope = ScopeConfig(trust_mode="restricted", control_plane_hosts=["127.0.0.1"])
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

    def test_logs_include_task_code_for_interleaved_workers(self):
        _FakeAPI.submitted = []
        _FakeAPI.started = []
        _FakeAPI.closed = []
        messages: list[str] = []
        with _Server() as server:
            def fake_runner(task, target, goal, **kwargs):
                del task, goal, kwargs
                state = RunState(run_id="log", target=target, status="success")
                state.flags = [{"value": "flag{one}"}]
                return state

            summary = BenchmarkRunner(
                TrinaConfig(), BenchmarkClient(server.url, "test-token", _guard()),
                options=BenchOptions(max_steps=1, per_task_seconds=30, total_seconds=120),
                runner=fake_runner, log=messages.append,
            ).run()
        self.assertEqual(summary.solved, 1)
        self.assertTrue(any("[challenge-1]" in message for message in messages))

    def test_v1_multi_flag_and_multi_address(self):
        """V1 题目可能返回多个容器地址与多个 flag；必须逐个提交并在 finally 关闭。"""

        class V1API(_FakeAPI):
            submitted: list[tuple[str, str]] = []
            started: list[str] = []
            closed: list[str] = []
            flags = {"v1-multi": ["flag{one}", "flag{two}"]}

            def do_GET(self):  # noqa: N802
                if not self._auth():
                    return
                if self.path == "/openapi/v1/challenges":
                    self._json(200, [{
                        "unique_code": "v1-multi",
                        "description": "V1 多 flag 测试题",
                        "difficulty": "medium",
                        "level": 2,
                        "total_score": 200,
                        "flag_count": 2,
                        "correct_flag_count": 0,
                        "is_completed": False,
                        "container_status": "stopped",
                        "container_addr": [],
                    }])
                    return
                self._json(404, {"code": "challenge_not_found", "message": "not found", "detail": {}})

            def do_POST(self):  # noqa: N802
                if not self._auth():
                    return
                parts = urlsplit(self.path)
                query = parse_qs(parts.query)
                code = (query.get("unique_code") or [""])[0]
                if parts.path.endswith("/start"):
                    type(self).started.append(code)
                    self._json(200, {"unique_code": code,
                                     "container_addr": ["10.9.0.7:1337", "10.9.0.7:8080"]})
                    return
                if parts.path.endswith("/close"):
                    type(self).closed.append(code)
                    self._json(200, {"unique_code": code, "closed": True})
                    return
                if parts.path.endswith("/submit"):
                    payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                    code = str(payload.get("unique_code") or code)
                    flag = str(payload.get("flag") or "")
                    correct = flag in self.flags.get(code, [])
                    type(self).submitted.append((code, flag))
                    count = len({f for c, f in type(self).submitted if c == code and f in self.flags.get(code, [])})
                    self._json(200, {
                        "correct": correct,
                        "awarded": 100 if correct else 0,
                        "cumulative_score": 100 * count,
                        "correct_flag_count": count,
                        "total_flag_count": 2,
                        "matched_flag_index": 0 if correct else None,
                    })
                    return
                self._json(404, {"code": "challenge_not_found", "message": "not found", "detail": {}})

        V1API.submitted, V1API.started, V1API.closed = [], [], []
        targets: list[str] = []

        def fake_runner(task, target, goal, **kwargs):
            del task, goal, kwargs
            targets.append(target)
            state = RunState(run_id="v1", target=target, status="success")
            state.flags = [{"value": "flag{one}"}, {"value": "flag{two}"}]
            return state

        with _Server(V1API) as server:
            summary = BenchmarkRunner(
                TrinaConfig(), BenchmarkClient(server.url, "test-token", _guard()),
                options=BenchOptions(max_steps=2, per_task_seconds=30, total_seconds=120),
                runner=fake_runner, log=lambda message: None,
            ).run()

        self.assertEqual(targets, ["http://10.9.0.7:8080"])
        self.assertEqual(summary.solved, 1)
        self.assertEqual(summary.tasks[0].correct_flags, 2)
        self.assertEqual(V1API.started, ["v1-multi"])
        self.assertEqual(sorted(V1API.submitted), [("v1-multi", "flag{one}"), ("v1-multi", "flag{two}")])
        self.assertEqual(V1API.closed, ["v1-multi"])
        self.assertEqual(choose_target(["10.0.0.1:1337", "10.0.0.1:8080"]), "http://10.0.0.1:8080")
        self.assertEqual(choose_target(["https://a.example:443/x"]), "https://a.example:443/x")

    def test_start_retries_after_timeout_and_reclaims_orphan(self):
        """start 超时可能留下平台容器；必须关闭孤儿并重试，避免 max active 卡死整场。"""

        class FlakyClient:
            def __init__(self):
                self.starts = 0
                self.closed: list[str] = []

            def fork(self):
                return self

            def list_challenges(self):
                return [
                    {"unique_code": "challenge-1", "description": "target", "difficulty": "easy",
                     "level": 1, "total_score": 100, "flag_count": 1, "correct_flag_count": 0,
                     "is_completed": False, "container_status": "stopped", "container_addr": []},
                    {"unique_code": "orphan-1", "description": "stale", "difficulty": "easy",
                     "level": 1, "total_score": 100, "flag_count": 1, "correct_flag_count": 0,
                     "is_completed": True, "container_status": "available", "container_addr": ["10.0.0.9:80"]},
                ]

            def start(self, code):
                self.starts += 1
                if self.starts == 1:
                    raise BenchmarkError("start read timeout", status=0)
                return {"unique_code": code, "container_addr": ["127.0.0.1:8080"]}

            def close(self, code):
                self.closed.append(code)
                return {"closed": True}

            def submit(self, code, flag):
                del code, flag
                return {"correct": True, "awarded": 100, "cumulative_score": 100,
                        "correct_flag_count": 1, "total_flag_count": 1, "matched_flag_index": 0}

        client = FlakyClient()

        def fake_runner(task, target, goal, **kwargs):
            del task, goal, kwargs
            state = RunState(run_id="flaky", target=target, status="success")
            state.flags = [{"value": "flag{one}"}]
            return state

        summary = BenchmarkRunner(
            TrinaConfig(), client,
            options=BenchOptions(max_steps=1, per_task_seconds=30, total_seconds=120, workers=1),
            runner=fake_runner, log=lambda message: None,
        ).run()

        self.assertEqual(summary.solved, 1)
        self.assertGreaterEqual(client.starts, 2)
        self.assertIn("challenge-1", client.closed)
        self.assertIn("orphan-1", client.closed)

    def test_commander_extends_partial_task(self):
        """部分 Flag 且单题预算耗尽时，Commander 应从储备池加时并继续。"""

        class PartialClient:
            def __init__(self):
                self.correct = 0
                self.closed: list[str] = []

            def fork(self):
                return self

            def list_challenges(self):
                return [{
                    "unique_code": "partial-1", "description": "two flags", "difficulty": "easy",
                    "level": 1, "total_score": 200, "flag_count": 2, "correct_flag_count": 0,
                    "is_completed": False, "container_status": "stopped", "container_addr": [],
                }]

            def start(self, code):
                return {"unique_code": code, "container_addr": ["127.0.0.1:8080"]}

            def close(self, code):
                self.closed.append(code)
                return {"closed": True}

            def submit(self, code, flag):
                del code, flag
                self.correct += 1
                return {"correct": True, "awarded": 100, "cumulative_score": 100 * self.correct,
                        "correct_flag_count": self.correct, "total_flag_count": 2,
                        "matched_flag_index": self.correct - 1}

        client = PartialClient()

        def fake_runner(task, target, goal, *, attempt, **kwargs):
            del task, goal, kwargs
            state = RunState(run_id=f"partial-{attempt}", target=target, status="partial")
            state.flags = [{"value": f"flag{{part{attempt}}}"}]
            return state

        summary = BenchmarkRunner(
            TrinaConfig(), client,
            options=BenchOptions(
                per_task_seconds=1, total_seconds=600, attempts=1,
                commander=True, commander_extension_seconds=1, commander_max_extensions=1,
                commander_min_remaining_seconds=10, commander_reserve_ratio=0.10,
            ),
            runner=fake_runner, log=lambda message: None,
        ).run()

        self.assertEqual(summary.solved, 1)
        self.assertEqual(summary.tasks[0].attempts, 2)
        self.assertEqual(summary.tasks[0].attempt_metrics[0]["commander_action"], "extend")
        self.assertEqual(summary.tasks[0].metrics["commander_extensions"], 1)

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

    def test_retry_inherits_state_and_reports_efficiency_metrics(self):
        _FakeAPI.submitted = []
        calls: list[tuple[int, int]] = []

        def continuing(task, target, goal, *, attempt, resume_state=None, **kwargs):
            del task, goal, kwargs
            if attempt == 1:
                state = RunState(run_id="first", target=target, status="partial")
                state.host(target).ports[80] = {"service": "http"}
                state.counters.update({"decision_rounds": 2, "llm_calls": 2, "tool_calls": 1})
            else:
                self.assertIsNotNone(resume_state)
                self.assertIn(80, resume_state.hosts[target].ports)
                state = RunState.from_dict(resume_state.to_dict())
                state.run_id, state.status = "second", "success"
                state.flags.append({"value": "flag{one}", "source": "continued"})
                state.counters.update({"decision_rounds": 3, "llm_calls": 3, "tool_calls": 2})
            calls.append((attempt, int(state.counters["decision_rounds"])))
            return state

        with _Server() as server:
            runner = BenchmarkRunner(
                TrinaConfig(), BenchmarkClient(server.url, "test-token", _guard()),
                options=BenchOptions(max_steps=4, per_task_seconds=30, total_seconds=120, attempts=2),
                runner=continuing, log=lambda message: None,
            )
            summary = runner.run()
        self.assertEqual(calls, [(1, 2), (2, 3)])
        self.assertEqual(summary.solved, 1)
        outcome = summary.tasks[0]
        self.assertEqual(outcome.attempts, 2)
        self.assertEqual(outcome.metrics["decision_rounds"], 3)
        self.assertEqual(outcome.metrics["tool_calls"], 2)
        self.assertLess(outcome.attempt_metrics[0]["budget_seconds"], 30, "首轮必须给续做保留时间")
        self.assertLessEqual(outcome.duration, 30)

    def test_no_new_fact_stops_retry_early(self):
        calls = 0

        def stalled(task, target, goal, **kwargs):
            nonlocal calls
            del task, goal, kwargs
            calls += 1
            return RunState(run_id=f"stalled-{calls}", target=target, status="failed")

        with _Server() as server:
            summary = BenchmarkRunner(
                TrinaConfig(), BenchmarkClient(server.url, "test-token", _guard()),
                options=BenchOptions(max_steps=1, per_task_seconds=30, total_seconds=120, attempts=3),
                runner=stalled, log=lambda message: None,
            ).run()
        self.assertEqual(calls, 1)
        self.assertEqual(summary.tasks[0].attempts, 1)

    def test_partial_score_does_not_override_stop_or_block(self):
        from trina.bench.runner import TaskOutcome

        outcome = TaskOutcome("partial", correct_flags=1, total_flags=2)
        for status in ("blocked", "stopped"):
            state = RunState("run", "host.test", status=status)
            self.assertFalse(BenchmarkRunner._retry_worthwhile(state, outcome, True))

    def test_multiple_tasks_share_swarm_scheduler_and_run_concurrently(self):
        class ParallelAPI(_FakeAPI):
            submitted: list[tuple[str, str]] = []
            started: list[str] = []
            closed: list[str] = []
            flags = {"challenge-1": ["flag{one}"], "challenge-2": ["flag{two}"]}

            def do_GET(self):  # noqa: N802
                if not self._auth():
                    return
                if self.path == "/openapi/v1/challenges":
                    rows = [{
                        "unique_code": code, "description": code, "difficulty": "easy", "level": index,
                        "total_score": 100, "flag_count": 1, "correct_flag_count": 0,
                        "is_completed": False, "container_status": "stopped", "container_addr": [],
                    } for index, code in enumerate(("challenge-1", "challenge-2"), 1)]
                    self._json(200, rows)
                    return
                self._json(404, {"code": "challenge_not_found", "message": "not found", "detail": {}})

        active = maximum = 0
        lock = threading.Lock()
        barrier = threading.Barrier(2, timeout=5)

        def fake_runner(task, target, goal, **kwargs):
            nonlocal active, maximum
            del goal, kwargs
            with lock:
                active += 1
                maximum = max(maximum, active)
            barrier.wait()
            with lock:
                active -= 1
            state = RunState(run_id=task.unique_code, target=target, status="success")
            value = "flag{one}" if task.unique_code == "challenge-1" else "flag{two}"
            state.flags = [{"value": value, "source": "fake"}]
            return state

        with _Server(ParallelAPI) as server:
            runner = BenchmarkRunner(
                TrinaConfig(), BenchmarkClient(server.url, "test-token", _guard()),
                options=BenchOptions(max_steps=1, per_task_seconds=30, total_seconds=120, workers=2),
                runner=fake_runner, log=lambda message: None,
            )
            summary = runner.run()
        self.assertEqual(maximum, 2)
        self.assertEqual(summary.solved, 2)
        self.assertEqual(set(ParallelAPI.closed), {"challenge-1", "challenge-2"})
        self.assertEqual({item[0] for item in ParallelAPI.submitted}, {"challenge-1", "challenge-2"})


if __name__ == "__main__":
    unittest.main()
