"""成果链路的真实回环 HTTP、持久化、证据约束、复测版本与超时测试。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from trina.audit import AuditLog
from trina.config import TrinaConfig
from trina.scope import ScopeGuard
from trina.state import RunState
from trina.tools import build_registry
from trina.tools.http import HttpClient
from trina.tools.registry import Tool, ToolContext, ToolResult, ToolSpec, ParamSpec
from trina.traffic import TrafficStore


class Fixture(BaseHTTPRequestHandler):
    calls: list = []

    def log_message(self, *_):
        pass

    def do_GET(self):
        type(self).calls.append(self.path)
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/api/users?id=1")
            self.end_headers()
            return
        body = {
            "/": '<html><script src="/assets/main.js"></script><a href="/page.html">page</a><a href="/logout">logout</a><script src="https://outside.invalid/x.js"></script></html>',
            "/assets/main.js": 'const r={path:"/admin",permission:"admin:view"}; const chunk="./lazy.js"; fetch("/api/users?id=1");',
            "/assets/lazy.js": 'const route={path:"/settings/users"}; fetch("/api/settings");',
            "/page.html": '<script src="/assets/mpa.js"></script>',
            "/assets/mpa.js": 'fetch("/api/mpa")',
        }.get(self.path, json.dumps({"marker": "confirmed", "path": self.path}))
        if self.path == "/large":
            body = "x" * 200100
        self.send_response(200)
        self.send_header("Content-Type", "text/html" if self.path in {"/", "/page.html"} else "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        type(self).calls.append(payload)
        method = payload["method"]
        if method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return
        result = ({"protocolVersion": "2025-03-26"} if method == "initialize" else
                  {"tools": [{"name": "list_assets", "inputSchema": {"type": "object", "required": ["project"]}},
                             {"name": "create_scan_task", "inputSchema": {"type": "object"}}]} if method == "tools/list" else
                  {"content": [{"type": "text", "text": "fixture-assets"}]})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Mcp-Session-Id", "fixture-session")
        self.end_headers()
        self.wfile.write(json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": result}).encode())

    def do_DELETE(self):
        self.send_response(200)
        self.end_headers()


class Fetch(Tool):
    spec = ToolSpec(name="fixture_fetch", summary="夹具", category="utility", action_level="detect", params={"url": ParamSpec("str", "URL")})

    def run(self, ctx, url):
        res = ctx.http.get(url, headers={"Authorization": "secret", "X-API-Key": "secret"})
        return ToolResult(ok=res.ok, data=res.to_dict(), evidence=[res.as_evidence()])


class CasebookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config = TrinaConfig().with_overrides(run__workspace=str(root / "runs"), audit__dir=str(root / "logs"))
        self.config.scope.trust_mode = "full"
        self.config.scope.max_qps = 10000
        self.registry = build_registry()
        self.registry.register(Fetch())
        guard = ScopeGuard(self.config.scope)
        self.ctx = ToolContext(config=self.config, guard=guard, audit=AuditLog.open(root / "logs", "case"),
            state=RunState("case", self.url, "确认信息泄露"), http=HttpClient(guard), workspace=root / "run", tools=self.registry)
        Fixture.calls = []

    def execute(self, name, **params):
        return self.registry.execute(name, params, self.ctx)

    def fetch(self, path="/api/users?id=1"):
        result = self.execute("fixture_fetch", url=self.url + path)
        self.assertTrue(result.ok, result.error)
        return result.data["traffic_id"]

    def finding(self, status="detected"):
        params = {"title": "信息泄露", "vuln_class": "information_disclosure", "target": self.url + "/api/users?id=1",
                  "evidence_refs": [self.fetch()], "status": status}
        if status == "verified":
            params.update(verification_refs=[self.fetch()], verification="独立请求复核响应")
        result = self.execute("report_finding", **params)
        self.assertTrue(result.ok, result.error)
        return result.data["id"]

    def test_http_capture_paging_redirects_and_redaction(self):
        tid = self.fetch("/redirect")
        hits = self.execute("traffic_search", method="GET").data["traffic"]
        self.assertEqual(len(hits), 2)
        meta = self.execute("traffic_get", traffic_id=tid)
        self.assertNotIn("secret", json.dumps(meta.data))
        self.assertNotIn("secret", json.dumps(self.ctx.state.evidence))
        body = self.execute("traffic_blob", traffic_id=tid, limit=8).data
        self.assertTrue(body["has_more"])
        self.assertEqual(body["next_offset"], 8)
        self.assertIn("confirmed", self.execute("traffic_blob", traffic_id=tid).data["text"])
        self.assertEqual(TrafficStore(self.ctx.workspace).get(tid)["id"], tid)

    def test_http_capture_marks_truncation(self):
        tid = self.fetch("/large")
        body = self.execute("traffic_blob", traffic_id=tid).data
        self.assertTrue(body["capture_truncated"])
        self.assertEqual(body["total"], 200000)

    def test_unknown_traffic_invalid_offsets_and_fake_evidence_rejected(self):
        for name, params in (("traffic_get", {"traffic_id": "fake"}),
            ("traffic_blob", {"traffic_id": self.fetch(), "offset": -1}),
            ("record_fact", {"text": "假的", "evidence_refs": ["fake"]}),
            ("report_finding", {"title": "假的", "vuln_class": "sqli", "target": self.url, "evidence_refs": ["fake"]})):
            self.assertFalse(self.execute(name, **params).ok)
        self.assertEqual(self.ctx.state.findings, [])

    def test_command_evidence_can_register_and_detect_modification(self):
        result = self.execute("terminal_exec", argv=[sys.executable, "-c", "print('real evidence')"], cwd="run")
        self.assertTrue(result.ok, result.error)
        ref = result.data["evidence_id"]
        self.assertTrue(self.execute("record_fact", text="观察到输出", evidence_refs=[ref]).ok)
        ev = next(e for e in self.ctx.state.evidence if e.get("id") == ref)
        Path(ev["path"]).write_text("changed", encoding="utf-8")
        self.assertFalse(self.execute("record_fact", text="旧证据", evidence_refs=[ref]).ok)

    def test_failed_command_cannot_prove_success(self):
        result = self.execute("terminal_exec", argv=[sys.executable, "-c", "raise SystemExit(2)"], cwd="run")
        self.assertFalse(result.ok)
        self.assertFalse(self.execute("record_fact", text="声称成功", evidence_refs=[result.data["evidence_id"]]).ok)

    def test_authority_verifier_ok_becomes_structured_goal_fact(self):
        verifier = Path(self.ctx.workspace) / "verifier.py"
        verifier.write_text("print('OK')\n", encoding="utf-8")
        result = self.execute("terminal_exec", argv=[sys.executable, "verifier.py", "flag{fixture_ok}"], cwd="run")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.metadata["authority_verifier"]["status"], "passed")
        self.assertEqual(self.ctx.state.flags[0]["value"], "flag{fixture_ok}")
        self.assertTrue(any(item.get("status") == "passed" for item in self.ctx.state.casebook["goal_verifications"]))
        goals = self.execute("list_goals").data["goals"]
        self.assertTrue(any(item["id"] == "flag" and item["satisfied"] for item in goals))
        proof = self.execute("prove_goal", goal_id="flag", evidence_refs=[result.data["evidence_id"]])
        self.assertTrue(proof.ok, proof.error)

    def test_plain_ok_output_is_not_authority_verifier(self):
        result = self.execute("terminal_exec", argv=[sys.executable, "-c", "print('OK')"], cwd="run")
        self.assertTrue(result.ok, result.error)
        self.assertNotIn("authority_verifier", result.metadata)
        self.assertEqual(self.ctx.state.flags, [])

    def test_embedded_verifier_process_is_authoritative_when_it_returns_ok(self):
        verifier = Path(self.ctx.workspace) / "verifier.py"
        verifier.write_text("print('OK')\n", encoding="utf-8")
        code = (
            "import subprocess,sys; "
            "r=subprocess.run([sys.executable,'verifier.py','flag{embedded_ok}'], capture_output=True, text=True); "
            "print(r.stdout.strip())"
        )
        result = self.execute("terminal_exec", argv=[sys.executable, "-c", code], cwd="run")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.metadata["authority_verifier"]["status"], "passed")
        self.assertEqual(self.ctx.state.flags[0]["value"], "flag{embedded_ok}")

    def test_finding_dedup_and_real_traffic_bindings(self):
        fid = self.finding()
        other = self.finding()
        self.assertEqual(fid, other)
        self.assertEqual(len(self.ctx.state.findings), 1)
        tid = self.fetch()
        self.assertTrue(self.execute("bind_finding_traffic", finding_id=fid, traffic_ids=[tid]).ok)
        self.assertIn(tid, self.execute("get_finding_traffic", finding_id=fid).data["traffic_ids"])
        self.assertFalse(self.execute("bind_finding_traffic", finding_id=fid, traffic_ids=["fake"]).ok)

    def test_verified_requires_distinct_proof_and_synthetic_is_rejected(self):
        tid = self.fetch()
        self.assertFalse(self.execute("report_finding", title="x", vuln_class="sqli", target=self.url,
                                     evidence_refs=[tid], status="verified", verification_refs=[tid], verification="重复").ok)
        self.ctx.state.add_evidence({"id": "mock", "synthetic": True})
        self.assertFalse(self.execute("report_finding", title="x", vuln_class="sqli", target=self.url, evidence_refs=["mock"]).ok)
        self.assertTrue(self.execute("record_fact", text="模拟前端路由", kind="inference", evidence_refs=["mock"]).ok)

    def test_assets_remain_untested_until_explicit_coverage(self):
        tid = self.fetch()
        assets = [{"target": self.url + "/api/users", "method": "GET", "evidence_refs": [tid]}]
        result = self.execute("insert_assets", assets=assets)
        self.assertTrue(result.ok, result.error)
        aid = result.data["assets"][0]["id"]
        self.assertEqual(self.execute("list_untested_assets").data["total"], 1)
        self.assertTrue(self.execute("mark_asset_tested", asset_id=aid, evidence_refs=[tid], checks="正常对照检查").ok)
        self.assertEqual(self.execute("list_untested_assets").data["total"], 0)
        self.execute("insert_assets", assets=assets)
        self.assertEqual(self.execute("list_untested_assets").data["total"], 0)
        restored = RunState.from_dict(self.ctx.state.to_dict())
        self.assertEqual(restored.casebook, self.ctx.state.casebook)

    def test_bookkeeping_tokens_do_not_count_as_new_knowledge(self):
        fid = self.finding()
        before = self.ctx.state.knowledge_digest()
        self.execute("get_finding_retest_context", finding_id=fid, environment="v1")
        self.assertEqual(before, self.ctx.state.knowledge_digest())

    def test_reports_include_assets_traffic_and_retest_history(self):
        from trina.report import build_report
        fid = self.finding("verified")
        snapshot = self.execute("get_finding_retest_context", finding_id=fid, environment="v1").data
        result = self.execute("record_finding_retest_result", finding_id=fid, context_token=snapshot["token"],
            result="fixed", evidence_refs=[self.fetch("/fixed")], control_refs=[self.fetch("/normal")],
            comparable=True, environment="v1", explanation="测试正常对照可达，旧行为已消失")
        self.assertTrue(result.ok, result.error)
        report = build_report(self.ctx.state, self.config)
        self.assertIn("fixed", report.markdown)
        self.assertIn("fixed", report.html)
        self.assertIn("关联流量", report.markdown)
        self.assertIn("200", report.markdown)

    def test_prompt_requires_distinct_routes_independent_proof_and_evidence_version(self):
        from trina.brain.prompts import render_system_prompt
        prompt = render_system_prompt()
        for text in ("机理不同", "改变其前提的新证据", "上游产物", "独立命令", "inconclusive", "版本变化"):
            self.assertIn(text, prompt)
        self.assertNotIn("ScopeSentry", prompt)

    def test_prove_goal_uses_state_instead_of_text(self):
        tid = self.fetch()
        gid = self.execute("list_goals").data["goals"][0]["id"]
        self.assertFalse(self.execute("prove_goal", goal_id=gid, evidence_refs=[tid]).ok)
        self.finding("verified")
        self.assertTrue(self.execute("prove_goal", goal_id=gid, evidence_refs=[tid]).ok)
        self.ctx.state.goal = "完成自定义工作"
        self.assertFalse(self.execute("list_goals").data["evaluable"])
        self.assertFalse(self.execute("prove_goal", goal_id="fake", evidence_refs=[tid]).ok)

    def test_retest_requires_fresh_evidence_comparable_environment_and_control(self):
        fid = self.finding("verified")
        old = self.fetch()
        context = self.execute("get_finding_retest_context", finding_id=fid, environment="fixture v1 reader").data
        new = self.fetch()
        control = self.fetch("/normal")
        base = dict(finding_id=fid, context_token=context["token"], result="reproduced", evidence_refs=[new],
                    control_refs=[control], comparable=True, environment="fixture v1 reader", explanation="新请求与正常对照")
        self.assertFalse(self.execute("record_finding_retest_result", **{**base, "evidence_refs": [old]}).ok)
        self.assertFalse(self.execute("record_finding_retest_result", **{**base, "comparable": False}).ok)
        self.assertFalse(self.execute("record_finding_retest_result", **{**base, "environment": "different"}).ok)
        self.assertTrue(self.execute("record_finding_retest_result", **base).ok)
        self.assertFalse(self.execute("record_finding_retest_result", **base).ok)

    def test_retest_rejects_stale_finding_version(self):
        fid = self.finding()
        context = self.execute("get_finding_retest_context", finding_id=fid, environment="v1").data
        self.ctx.state.findings[0].description = "evidence changed"
        result = self.execute("record_finding_retest_result", finding_id=fid, context_token=context["token"],
            result="inconclusive", evidence_refs=[self.fetch()], control_refs=[], environment="v2", explanation="环境差异")
        self.assertFalse(result.ok)
        self.assertIn("版本", result.error)

    def test_static_recon_follows_chunks_and_mpa_but_not_write_links_or_external(self):
        result = self.execute("api_recon_static", url=self.url, max_depth=3)
        self.assertTrue(result.ok, result.error)
        self.assertIn("/assets/lazy.js", Fixture.calls)
        self.assertIn("/assets/mpa.js", Fixture.calls)
        self.assertNotIn("/logout", Fixture.calls)
        self.assertNotIn("/api/users?id=1", Fixture.calls)
        self.assertIn("/settings/users", result.data["routes"])
        self.assertEqual(result.data["route_tree"]["settings"], {"users": {}})
        self.assertTrue(self.execute("list_untested_assets").data["total"])

    def test_static_recon_obeys_request_budget(self):
        result = self.execute("api_recon_static", url=self.url, max_requests=1)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(Fixture.calls), 1)
        self.assertTrue(result.data["budget_exhausted"])

    def test_timed_out_tool_cannot_late_write_state_or_traffic(self):
        class Late(Fetch):
            spec = ToolSpec(name="fixture_late", summary="迟到", category="utility", action_level="detect", params={})
            def run(inner, ctx):
                time.sleep(.06)
                ctx.state.casebook["late"] = True
                return super(Late, inner).run(ctx, CasebookTests.url)
        self.registry.register(Late())
        result = self.registry.execute("fixture_late", {}, self.ctx, timeout=.01)
        self.assertFalse(result.ok)
        time.sleep(.1)
        self.assertNotIn("late", self.ctx.state.casebook)
        self.assertEqual(Fixture.calls, [])
        self.assertEqual(self.execute("traffic_search").data["traffic"], [])

    def test_mcp_http_live_schema_and_read_write_separation(self):
        from trina.capabilities.catalog import CapabilityCatalog
        from trina.capabilities.runtime import CapabilityRuntime
        catalog = CapabilityCatalog()
        catalog.discover([Path(__file__).resolve().parents[1] / "capabilities" / "scopesentry"])
        cap = catalog.get("scopesentry.query")
        runtime = CapabilityRuntime(self.registry)
        with patch.dict("os.environ", {"TRINA_SCOPESENTRY_URL": self.url + "/mcp", "TRINA_SCOPESENTRY_KEY": "fixture-secret"}):
            registry, _ = runtime.bind([cap], check_lease=lambda: None, publish=lambda *_: None)
            listed = registry.execute("scopesentry_query", {"action": "list_tools"}, self.ctx)
            self.assertTrue(listed.ok, listed.error)
            self.assertEqual([t["name"] for t in listed.data["tools"]], ["list_assets"])
            self.assertNotIn("fixture-secret", json.dumps(listed.evidence))
            rejected = registry.execute("scopesentry_query", {"action": "call", "tool_name": "create_scan_task"}, self.ctx)
            self.assertFalse(rejected.ok)
            invalid = registry.execute("scopesentry_query", {"action": "call", "tool_name": "list_assets"}, self.ctx)
            self.assertFalse(invalid.ok)
            called = registry.execute("scopesentry_query", {"action": "call", "tool_name": "list_assets", "arguments": {"project": "fixture"}}, self.ctx)
            self.assertTrue(called.ok, called.error)
            self.assertIn("fixture-assets", json.dumps(called.data))
            self.assertNotIn("ScopeSentry", __import__("trina.brain.prompts", fromlist=["render_system_prompt"]).render_system_prompt())

    def test_external_mcp_missing_configuration_and_restricted_writes_do_not_connect(self):
        from trina.capabilities.catalog import CapabilityCatalog
        from trina.capabilities.runtime import CapabilityRuntime
        catalog = CapabilityCatalog()
        catalog.discover([Path(__file__).resolve().parents[1] / "capabilities" / "scopesentry"])
        runtime = CapabilityRuntime(self.registry)
        cap = catalog.get("scopesentry.manage")
        with patch.dict("os.environ", {"TRINA_SCOPESENTRY_URL": "", "TRINA_SCOPESENTRY_KEY": ""}):
            self.assertFalse(runtime.available(cap)[0])
        with patch.dict("os.environ", {"TRINA_SCOPESENTRY_URL": self.url + "/mcp", "TRINA_SCOPESENTRY_KEY": "fixture"}):
            self.config.scope.trust_mode = "restricted"
            result = runtime.invoke(cap, self.ctx, {"action": "call", "tool_name": "create_scan_task", "arguments": {}})
            self.assertTrue(result.denied)
            self.assertEqual(Fixture.calls, [])


if __name__ == "__main__":
    unittest.main()
