"""漏洞验证必须区分执行效果、参数反射和固定错误页。"""

from __future__ import annotations

import html
import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from trina.lab.http_target import LabState, run_command
from trina.tools.exploit import CmdInjectTest, LfiTest, SqliTest
from trina.tools.http import HttpResult


class _Responses:
    """按参数生成响应，完整保存实际探针供断言。"""

    def __init__(self, respond):
        self.respond = respond
        self.calls = []

    def request(self, method, url, **kwargs):
        values = kwargs.get("params") or kwargs.get("data") or {}
        value = str(next(iter(values.values()), ""))
        self.calls.append(value)
        status, body = self.respond(value)
        return HttpResult(url=url, method=method, status=status, body=body, request_body=value)


class TestProbeIntegrity(unittest.TestCase):
    def _run(self, tool, respond, **params):
        transport = _Responses(respond)
        with patch("trina.tools.exploit.have", return_value=False):
            result = tool.run(SimpleNamespace(http=transport), url="http://127.0.0.1/probe", param="q", **params)
        return result, transport

    def test_reflected_parameter_is_not_command_execution(self):
        for render in (lambda value: value, html.escape):
            with self.subTest(render=render):
                result, _ = self._run(CmdInjectTest(), lambda value: (200, f"<pre>{render(value)}</pre>"))
                self.assertTrue(result.ok)
                self.assertFalse(result.data["vulnerable"])
                self.assertFalse(result.facts)

    def test_split_command_marker_reproduces_with_fresh_challenge(self):
        state = LabState()
        result, transport = self._run(CmdInjectTest(), lambda value: (200, run_command(state, value)))
        self.assertTrue(result.data["vulnerable"])
        self.assertNotIn(result.data["mark"], result.facts["findings"][0]["payload"])
        self.assertGreaterEqual(len(result.evidence), 2)
        self.assertGreater(len(set(re.findall(r"TRINA_MARK_[A-Z0-9]+", " ".join(
            str(ev["response"]["body_excerpt"]) for ev in result.evidence
        )))), 1)
        self.assertGreaterEqual(len(transport.calls), 2)

    def test_stale_command_output_is_not_confirmed(self):
        first_marker = []

        def respond(value):
            rendered = run_command(LabState(), value)
            if not first_marker:
                first_marker.append(rendered)
            return 200, first_marker[0]

        result, _ = self._run(CmdInjectTest(), respond)
        self.assertFalse(result.data["vulnerable"])

    def test_fixed_sql_error_page_is_not_injection(self):
        result, _ = self._run(SqliTest(), lambda value: (500, "You have an error in your SQL syntax"), technique="error")
        self.assertFalse(result.data["vulnerable"])
        self.assertFalse(result.facts)

    def test_sql_error_must_reproduce_and_disappear_in_control(self):
        result, _ = self._run(SqliTest(), lambda value: (
            (200, "normal") if value == "1" else (500, "You have an error in your SQL syntax")
        ), technique="error")
        self.assertTrue(result.data["vulnerable"])
        self.assertGreaterEqual(len(result.evidence), 4)

    def test_transient_sql_error_is_not_confirmed(self):
        seen = set()

        def respond(value):
            if value != "1" and value not in seen:
                seen.add(value)
                return 500, "You have an error in your SQL syntax"
            return 200, "normal"

        result, _ = self._run(SqliTest(), respond, technique="error")
        self.assertFalse(result.data["vulnerable"])

    def test_static_file_signature_is_not_lfi(self):
        result, _ = self._run(LfiTest(), lambda value: (200, "<pre>root:x:0:0:root:/root:/bin/bash</pre>"))
        self.assertFalse(result.data["vulnerable"])
        self.assertFalse(result.facts)

    def test_lfi_has_missing_file_control_and_repeated_evidence(self):
        result, _ = self._run(LfiTest(), lambda value: (
            (200, "root:x:0:0:root:/root:/bin/bash") if "etc/passwd" in value else (200, "file missing")
        ))
        self.assertTrue(result.data["vulnerable"])
        self.assertGreaterEqual(len(result.evidence), 4)

    def test_error_page_with_file_signature_is_not_lfi(self):
        result, _ = self._run(LfiTest(), lambda value: (
            (403, "root:x:0:0:root:/root:/bin/bash") if "etc/passwd" in value else (200, "file missing")
        ))
        self.assertFalse(result.data["vulnerable"])


if __name__ == "__main__":
    unittest.main()
