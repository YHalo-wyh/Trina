"""HTTP 出口的逐跳校验、请求语义与证据一致性回归。"""

from __future__ import annotations

import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import requests

from trina.audit import AuditLog
from trina.config import TrinaConfig
from trina.scope import ScopeGuard
from trina.tools.http import HttpClient


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8")
        self.server.visits.append((self.command, self.path, body, self.headers.get("Cookie", "")))
        status, headers = self.server.routes.get(self.path.split("?", 1)[0], (200, {}))
        content = "|".join(self.server.visits[-1]).encode("utf-8")
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    do_POST = do_GET
    do_HEAD = do_GET


class TestHttpRedirects(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.servers = []
        cls.threads = []
        for _ in range(2):
            server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            server.routes = {}
            server.visits = []
            thread.start()
            cls.servers.append(server)
            cls.threads.append(thread)
        cls.base, cls.other = [f"http://127.0.0.1:{server.server_port}" for server in cls.servers]

    @classmethod
    def tearDownClass(cls):
        for server, thread in zip(cls.servers, cls.threads):
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audit = AuditLog.open(self.tmp.name, run_id="redirect", resume=False)
        self.guard = ScopeGuard(TrinaConfig.load().scope, audit=self.audit)
        self.client = HttpClient(self.guard, timeout=2, retries=1)
        self.client.session.trust_env = False
        self.addCleanup(self.client.session.close)
        for server in self.servers:
            server.routes.clear()
            server.visits.clear()

    def test_cross_origin_redirect_never_contacts_destination(self):
        self.servers[0].routes["/start"] = (302, {"Location": self.other + "/final"})
        result = self.client.get(self.base + "/start", headers={"Authorization": "Bearer fixture"})
        self.assertFalse(result.ok)
        self.assertIn("护栏拒绝", result.error)
        self.assertEqual(self.servers[1].visits, [])
        self.assertEqual(len(self.client.denials), 1)
        self.assertTrue(self.audit.verify()[0])

    def test_redirect_query_payload_is_rechecked(self):
        self.servers[0].routes["/start"] = (302, {"Location": "/final?q=DROP%20TABLE%20users"})
        result = self.client.get(self.base + "/start")
        self.assertFalse(result.ok)
        self.assertIn("护栏拒绝", result.error)
        self.assertEqual(len(self.servers[0].visits), 1)

    def test_relative_redirects_keep_actual_final_url_and_history(self):
        self.servers[0].routes.update({
            "/start": (302, {"Location": "middle"}),
            "/middle": (301, {"Location": "/final?ok=1"}),
        })
        result = self.client.get(self.base + "/start")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.url, self.base + "/final?ok=1")
        self.assertEqual(result.history, [self.base + "/start", self.base + "/middle"])
        self.assertEqual(result.as_evidence()["request"]["url"], result.url)
        self.assertEqual(self.client.requests_made, 3)

    def test_303_changes_post_to_get_without_replaying_body(self):
        self.servers[0].routes["/start"] = (303, {"Location": "/final"})
        result = self.client.post(self.base + "/start", data={"q": "a&b"})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.servers[0].visits[-1][:3], ("GET", "/final", ""))
        self.assertEqual(result.method, "GET")
        self.assertEqual(result.request_body, "")

    def test_307_preserves_form_and_redirect_cookie(self):
        self.servers[0].routes["/start"] = (307, {"Location": "/final", "Set-Cookie": "lab=fixture; Path=/"})
        result = self.client.post(self.base + "/start", data={"q": "a&b"})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.servers[0].visits[-1][:3], ("POST", "/final", "q=a%26b"))
        self.assertIn("lab=fixture", self.servers[0].visits[-1][3])
        self.assertEqual(result.request_body, "q=a%26b")

    def test_redirects_can_be_disabled(self):
        self.servers[0].routes["/start"] = (302, {"Location": "/final"})
        result = self.client.get(self.base + "/start", allow_redirects=False)
        self.assertEqual(result.status, 302)
        self.assertEqual(result.history, [])
        self.assertEqual(len(self.servers[0].visits), 1)

    def test_redirect_loop_is_bounded(self):
        self.servers[0].routes["/loop"] = (302, {"Location": "/loop"})
        result = self.client.get(self.base + "/loop")
        self.assertFalse(result.ok)
        self.assertIn("重定向", result.error)
        self.assertEqual(len(self.servers[0].visits), self.client.session.max_redirects + 1)

    def test_post_transport_error_is_not_retried(self):
        with patch.object(self.client.session, "send", side_effect=requests.ConnectionError("fixture disconnect")) as send:
            result = self.client.post(self.base + "/write", data={"q": "once"})
        self.assertFalse(result.ok)
        self.assertEqual(send.call_count, 1)

    def test_read_transport_error_can_retry(self):
        send = self.client.session.send
        outcomes = [requests.ConnectionError("temporary"), None]

        def flaky(*args, **kwargs):
            outcome = outcomes.pop(0)
            if outcome:
                raise outcome
            return send(*args, **kwargs)

        with patch.object(self.client.session, "send", side_effect=flaky):
            result = self.client.get(self.base + "/final")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.client.requests_made, 2)

    def test_http_rejects_non_http_schemes_before_transport(self):
        with patch.object(self.client.session, "send") as send:
            result = self.client.get("ftp://127.0.0.1/file")
        self.assertFalse(result.ok)
        self.assertIn("护栏拒绝", result.error)
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
