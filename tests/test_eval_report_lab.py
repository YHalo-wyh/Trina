"""评测指标、报告生成、靶场适配、对抗代理、lab 靶机的集成测试。

这些是「对外可感知」的行为：指标算错会得出错误结论，报告自包含性破坏会让交付物不可用，
lab 靶机跑不起来则端到端评测无法进行。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from trina.config import TrinaConfig
from trina.state import Credential, Finding, HostState, RunState


class TestMetricsMath(unittest.TestCase):
    """precision / recall / FP / FN / 范围外发现的口径。"""

    def setUp(self):
        from trina.eval.metrics import ScenarioResult, compute_metrics

        self.ScenarioResult = ScenarioResult
        self.compute_metrics = compute_metrics

    def test_perfect_match(self):
        result = self.ScenarioResult(
            scenario_id="s1", kind="web", target="http://x", status="success", duration=10.0,
            findings=[{"vuln_class": "sqli", "url": "http://x/a", "parameter": "id"}],
            gt_total=1, gt_hit=["sqli@gt1"], gt_miss=[], fp_count=0,
            ports_found=2, ports_expected=2, services_correct=1, services_expected=1,
        )
        metrics = self.compute_metrics([result])
        self.assertEqual(metrics["precision"], 1.0)
        self.assertEqual(metrics["recall"], 1.0)
        self.assertEqual(metrics["false_positive_rate"], 0.0)
        self.assertEqual(metrics["miss_rate"], 0.0)
        self.assertEqual(metrics["pass_rate"], 1.0)
        self.assertEqual(metrics["port_coverage"], 1.0)
        self.assertEqual(metrics["service_accuracy"], 1.0)

    def test_fp_and_fn_are_counted(self):
        result = self.ScenarioResult(
            scenario_id="s2", kind="web", target="http://x", status="partial",
            findings=[{"vuln_class": "sqli"}, {"vuln_class": "xss_reflected"}],
            gt_total=2, gt_hit=["sqli@gt1"], gt_miss=["rce@gt2"], fp_count=1,
        )
        metrics = self.compute_metrics([result])
        self.assertEqual(metrics["tp"], 1)
        self.assertEqual(metrics["fn"], 1)
        self.assertEqual(metrics["fp"], 1)
        self.assertAlmostEqual(metrics["precision"], 0.5)
        self.assertAlmostEqual(metrics["recall"], 0.5)

    def test_out_of_scope_not_counted_as_fp(self):
        # 场景只评 sqli：顺带报出的缺失安全头属范围外，不进误报
        findings = [
            {"vuln_class": "sqli", "url": "http://x/a", "parameter": "id"},
            {"vuln_class": "security_misconfig_headers", "url": "http://x/", "parameter": "CSP"},
            {"vuln_class": "information_disclosure", "url": "http://x/.git/HEAD"},
        ]
        gt = [{"vuln_class": "sqli", "url": "/a", "parameter": "id"}]
        from trina.eval.metrics import match_findings

        match = match_findings(findings, gt)
        self.assertEqual(match.tp, 1)
        self.assertEqual(match.fp_count, 0, "范围外发现不应计入误报")
        self.assertEqual(match.out_of_scope_count, 2)

    def test_in_scope_unmatched_is_fp(self):
        findings = [
            {"vuln_class": "sqli", "url": "http://x/a", "parameter": "id"},
            {"vuln_class": "sqli", "url": "http://x/other", "parameter": "q"},
        ]
        gt = [{"vuln_class": "sqli", "url": "/a", "parameter": "id"}]
        from trina.eval.metrics import match_findings

        match = match_findings(findings, gt)
        self.assertEqual(match.tp, 1)
        self.assertEqual(match.fp_count, 1, "范围内未命中 GT 的同类发现算误报")

    def test_scenario_without_ground_truth_has_no_false_positives(self):
        """纯夺旗类场景没有评分依据：其 finding 不应被算成误报。"""
        from trina.eval.metrics import match_findings

        findings = [
            {"vuln_class": "security_misconfig_headers", "url": "http://x/", "parameter": "CSP"},
            {"vuln_class": "information_disclosure", "url": "http://x/robots.txt"},
        ]
        match = match_findings(findings, [])
        self.assertEqual(match.fp_count, 0, "无 GT 的场景不应产生误报")
        self.assertEqual(match.out_of_scope_count, 2)
        self.assertEqual(match.tp, 0)
        self.assertEqual(match.fn, 0)

    def test_alias_normalization_matches_across_spellings(self):
        from trina.eval.metrics import finding_matches_gt, normalize_vuln_class

        self.assertEqual(normalize_vuln_class("SQL Injection"), normalize_vuln_class("sqli"))
        self.assertEqual(normalize_vuln_class("file_upload"), normalize_vuln_class("unrestricted_file_upload"))
        self.assertEqual(normalize_vuln_class("info_disclosure"), normalize_vuln_class("information_disclosure"))
        finding = {"vuln_class": "info_disclosure", "url": "http://x/.git/HEAD"}
        self.assertTrue(finding_matches_gt(finding, {"vuln_class": "information_disclosure", "url": "/.git/HEAD"}))

    def test_harness_to_result_populates_scope_counts(self):
        """to_result 必须回填范围外计数，否则报告里「范围外发现」恒为 0。"""
        from trina.eval.harness import EvalHarness, ScenarioSpec

        state = RunState(run_id="r", target="http://x")
        state.add_finding({"vuln_class": "sqli", "url": "http://x/sqli", "parameter": "id"})
        state.add_finding({"vuln_class": "security_misconfig_headers", "url": "http://x/", "parameter": "CSP"})
        state.add_finding({"vuln_class": "information_disclosure", "url": "http://x/.git/HEAD"})
        spec = ScenarioSpec(
            id="s", name="s", kind="web", target="http://x",
            ground_truth=[{"vuln_class": "sqli", "url": "/sqli", "parameter": "id"}],
        )
        result = EvalHarness(TrinaConfig.load()).to_result(spec, state, 0.0)
        self.assertEqual(len(result.gt_hit), 1)
        self.assertEqual(result.fp_count, 0)
        self.assertEqual(result.out_of_scope_count, 2)
        metrics = self.compute_metrics([result])
        self.assertEqual(metrics["out_of_scope_total"], 2)
        self.assertEqual(metrics["precision"], 1.0)

    def test_infra_findings_and_success_criteria(self):
        """主机场景：端口/服务事实要能转成 GT 口径；扫描类场景按覆盖度判成败。"""
        from trina.eval.harness import EvalHarness, ScenarioSpec

        state = RunState(run_id="h", target="127.0.0.1")
        state.host("127.0.0.1").add_port(9099, service="http", version="Apache/2.4.41")
        state.host("127.0.0.1").add_port(2222, service="ssh", version="OpenSSH_9.6")
        harness = EvalHarness(TrinaConfig.load())

        spec = ScenarioSpec(
            id="host-scan", name="scan", kind="host", target="127.0.0.1",
            ports_expected=2, services_expected=2, success_criteria="ports",
            ground_truth=[
                {"vuln_class": "open_port", "url": "127.0.0.1:9099"},
                {"vuln_class": "open_port", "url": "127.0.0.1:2222"},
            ],
        )
        result = harness.to_result(spec, state, 0.0)
        self.assertEqual(len(result.gt_hit), 2, f"端口 GT 应全部命中: {result.gt_hit} / {result.gt_miss}")
        self.assertEqual(result.status, "success")

        # 只发现 1 个端口 → 覆盖度不达标
        partial = RunState(run_id="h2", target="127.0.0.1")
        partial.host("127.0.0.1").add_port(9099, service="http")
        result2 = harness.to_result(spec, partial, 0.0)
        self.assertEqual(result2.status, "partial")
        self.assertEqual(len(result2.gt_hit), 1)

    def test_success_criteria_flags(self):
        from trina.eval.harness import EvalHarness, ScenarioSpec

        state = RunState(run_id="f", target="http://x")
        state.flags.append({"value": "flag{trina_lab_deadbeef}", "source": "http://x/flag"})
        spec = ScenarioSpec(id="flag", name="flag", kind="web", target="http://x", success_criteria="flags",
                            flag_patterns=[r"flag\{trina_lab_[0-9a-f]+\}"])
        self.assertEqual(EvalHarness(TrinaConfig.load()).to_result(spec, state, 0.0).status, "success")

    def test_markdown_renders_chinese_table(self):
        from trina.eval.metrics import format_metrics_markdown

        result = self.ScenarioResult(scenario_id="s", kind="web", status="success", duration=5)
        text = format_metrics_markdown(self.compute_metrics([result]))
        self.assertIn("评测结果", text)
        self.assertIn("通过率", text)
        self.assertIn("范围外发现", text)


class TestScopeAwareToolBehaviour(unittest.TestCase):
    """工具层面的护栏语义：动态动作等级、robots 泄露识别、CLI 参数装配。"""

    def test_weak_password_single_credential_is_verify_level(self):
        from trina.tools import build_registry

        tool = build_registry().get("weak_password_brute")
        self.assertEqual(tool.spec.action_level, "exploit")
        # 明确给出一组用户名+口令 = 单凭据验证，不应要求 exploit 级
        self.assertEqual(tool.required_level({"service": "ssh", "target": "127.0.0.1", "username": "trina", "password": "trina123"}), "verify")
        # 字典爆破仍是 exploit
        self.assertEqual(tool.required_level({"service": "ssh", "target": "127.0.0.1", "passwords": ["a", "b"]}), "exploit")

    def test_dynamic_level_enforced_by_registry(self):
        import tempfile
        import time
        from pathlib import Path

        from trina.audit import AuditLog
        from trina.scope import ScopeGuard
        from trina.state import RunState
        from trina.tools import build_registry
        from trina.tools.http import HttpClient
        from trina.tools.registry import ToolContext

        cfg = TrinaConfig.load()
        cfg.scope.exploit_level = "verify"
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditLog.open(tmp, run_id="dyn", resume=False)
            guard = ScopeGuard(cfg.scope, audit=audit)
            ctx = ToolContext(
                config=cfg, guard=guard, audit=audit, state=RunState(run_id="dyn", target="127.0.0.1"),
                http=HttpClient(guard), workspace=Path(tmp), deadline=time.monotonic() + 30,
            )
            registry = build_registry()
            # 单凭据验证：等级被降到 verify → 不再因等级被拒（会去尝试连接，结果无所谓）
            single = registry.execute(
                "weak_password_brute",
                {"service": "ssh", "target": "127.0.0.1", "port": 1, "username": "trina", "password": "trina123"},
                ctx, timeout=10,
            )
            self.assertFalse(single.denied, f"单凭据验证不应被等级拒绝: {single.summary}")
            # 字典爆破：仍应为 exploit → 被拒
            brute = registry.execute(
                "weak_password_brute",
                {"service": "ssh", "target": "127.0.0.1", "port": 1, "passwords": ["a", "b"]},
                ctx, timeout=10,
            )
            self.assertTrue(brute.denied)
            self.assertTrue(
                any("verify 级等价路径" in hint for hint in brute.next_hints),
                f"等级拒绝应给出可用的低等级替代做法: {brute.next_hints}",
            )

    def test_internal_subrequest_uses_effective_level(self):
        """工具按 verify 级被放行时，它内部的子请求不能按 exploit 级自拒。"""
        import tempfile
        import time
        from pathlib import Path

        from trina.audit import AuditLog
        from trina.scope import ScopeGuard
        from trina.state import RunState
        from trina.tools.http import HttpClient
        from trina.tools.registry import ParamSpec, Tool, ToolContext, ToolRegistry, ToolResult, ToolSpec

        class _InnerHttpTool(Tool):
            spec = ToolSpec(
                name="inner_http",
                summary="内部发 HTTP 的工具",
                action_level="exploit",
                params={"url": ParamSpec("str", "目标 URL", required=True)},
            )

            def required_level(self, params):
                return "verify"  # 小规模验证：降到 verify

            def run(self, ctx, url):
                inner = ctx.http.request("GET", url, required_level=ctx.effective_level)
                return ToolResult(ok=inner.ok, summary=f"内部请求 status={inner.status} err={inner.error}")

        from trina.lab.http_target import create_server

        server, _thread = create_server(port=0)
        port = server.server_address[1]
        try:
            cfg = TrinaConfig.load()
            cfg.scope.exploit_level = "verify"
            with tempfile.TemporaryDirectory() as tmp:
                audit = AuditLog.open(tmp, run_id="inner", resume=False)
                guard = ScopeGuard(cfg.scope, audit=audit)
                ctx = ToolContext(
                    config=cfg, guard=guard, audit=audit, state=RunState(run_id="inner", target="127.0.0.1"),
                    http=HttpClient(guard), workspace=Path(tmp), deadline=time.monotonic() + 30,
                )
                result = ToolRegistry([_InnerHttpTool()]).execute(
                    "inner_http", {"url": f"http://127.0.0.1:{port}/flag"}, ctx, timeout=15
                )
                self.assertEqual(ctx.effective_level, "verify")
                self.assertTrue(result.ok, f"内部子请求不应被自身等级自拒: {result.summary}")
        finally:
            server.shutdown()
            server.server_close()

    def test_upload_not_reported_on_soft404(self):
        """软 404 靶机对任意路径都返回 200：没有上传成功证据就不能产出 file_upload 发现。"""
        import tempfile
        import time
        from pathlib import Path

        from trina.audit import AuditLog
        from trina.lab.http_target import create_server
        from trina.scope import ScopeGuard
        from trina.state import RunState
        from trina.tools import build_registry
        from trina.tools.http import HttpClient
        from trina.tools.registry import ToolContext

        server, _thread = create_server(port=0)
        port = server.server_address[1]
        cfg = TrinaConfig.load()
        tmp = tempfile.TemporaryDirectory()
        try:
            audit = AuditLog.open(tmp.name, run_id="up", resume=False)
            guard = ScopeGuard(cfg.scope, audit=audit)
            state = RunState(run_id="up", target="127.0.0.1")
            ctx = ToolContext(
                config=cfg, guard=guard, audit=audit, state=state, http=HttpClient(guard),
                workspace=Path(tmp.name), deadline=time.monotonic() + 90,
            )
            registry = build_registry()
            # 真实上传点：应产出 verified 发现
            real = registry.execute("file_upload_test", {"url": f"http://127.0.0.1:{port}/upload", "field": "file"}, ctx, timeout=60)
            self.assertTrue(real.ok)
            self.assertTrue((real.facts or {}).get("findings"), "真实上传点应产出发现")
            # 不存在的路径（软 404，200/137B）：不得产出任何 file_upload 发现
            fake = registry.execute("file_upload_test", {"url": f"http://127.0.0.1:{port}/upload.php", "field": "file"}, ctx, timeout=60)
            self.assertTrue(fake.ok)
            self.assertFalse((fake.facts or {}).get("findings"), f"软 404 不应产出上传发现: {fake.summary}")
            self.assertIn("没有上传成功证据", fake.summary)
            self.assertEqual([f.url for f in state.findings], [f"http://127.0.0.1:{port}/upload"])
        finally:
            server.server_close()
            tmp.cleanup()

    def test_robots_disclosure_detection(self):
        from trina.tools.recon import _sensitive_robots_entries

        body = "User-agent: *\nDisallow: /admin\nDisallow: /backup\nDisallow: /.git/\n"
        hits = _sensitive_robots_entries(body)
        for keyword in ("admin", "backup", "git"):
            self.assertIn(keyword, hits)
        self.assertEqual(_sensitive_robots_entries("User-agent: *\nDisallow: /public\n"), [])

    def test_cli_target_fills_declared_param_only(self):
        """--target 只能填工具声明过的目标参数（url 或 target），否则参数校验会直接拒。"""
        from trina.cli import build_parser, build_config  # noqa: F401 - 触发导入
        from trina.tools import build_registry

        registry = build_registry()
        for name, spec in ((n, registry.get(n).spec) for n in registry.names()):
            filled = [key for key in ("target", "url") if key in spec.params]
            self.assertTrue(filled, f"{name} 既没有 target 也没有 url 参数，--target 无法使用")


class TestLabFixtures(unittest.TestCase):
    """评测器要能自己保证夹具在场（外部靶机被回收时不能静默失败）。"""

    def test_autostart_and_stop(self):
        import socket

        from trina.eval.harness import EvalHarness, ScenarioSpec

        with socket.socket() as probe:  # 找一个空闲端口
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        harness = EvalHarness(TrinaConfig.load())
        spec = ScenarioSpec(id="s", name="s", kind="host", target="127.0.0.1", fixtures=[f"127.0.0.1:{port}"])
        harness.start_lab_fixtures([spec])
        try:
            self.assertTrue(harness._port_open("127.0.0.1", port), "缺失的夹具应被自动拉起")
        finally:
            harness.stop_lab_fixtures()
        self.assertFalse(harness._port_open("127.0.0.1", port), "评测结束后应关闭自动拉起的夹具")

    def test_existing_fixture_is_not_touched(self):
        from trina.eval.harness import EvalHarness, ScenarioSpec

        server, thread = None, None
        try:
            from trina.lab.http_target import create_server

            server, thread = create_server(port=0)
            port = server.server_address[1]
            harness = EvalHarness(TrinaConfig.load())
            harness.start_lab_fixtures([ScenarioSpec(id="s", name="s", kind="web", target=f"http://127.0.0.1:{port}")])
            self.assertEqual(harness._lab_servers, [], "已在运行的靶机不应被重复拉起")
            harness.stop_lab_fixtures()
            self.assertTrue(harness._port_open("127.0.0.1", port), "不应关闭外部已有靶机")
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()


class TestReport(unittest.TestCase):
    """报告生成：章节完整、HTML 自包含、凭据脱敏。"""

    def _state(self) -> RunState:
        state = RunState(run_id="20260101-000000-test", target="http://127.0.0.1:9099", goal="拿 flag")
        state.status = "success"
        state.ended_at = state.started_at + 42
        state.host("127.0.0.1").add_port(9099, service="http", version="Apache")
        state.host("127.0.0.1").technologies.append("Apache")
        state.add_finding(
            Finding(
                id="TRINA-001", title="SQL 注入", vuln_class="sqli", url="http://127.0.0.1:9099/sqli",
                parameter="id", severity="high", status="verified", payload="'",
                evidence=[{"kind": "http", "target": "http://127.0.0.1:9099/sqli?id=1'", "note": "报错回显"}],
            )
        )
        state.credentials.append(Credential(target="127.0.0.1", service="http_form", username="admin", password="admin123", verified=True))
        state.flags.append({"value": "flag{trina_lab_deadbeef}", "source": "http://127.0.0.1:9099/flag"})
        state.log_step({"kind": "react", "tool": "sqli_test", "summary": "SQL 注入已验证", "ok": True, "duration": 0.5})
        state.ledger = {"calls": 9, "cost_usd": 0.0068, "model": "deepseek/deepseek-v4-flash"}
        return state

    def test_sections_and_self_contained_html(self):
        from trina.report import build_report

        config = TrinaConfig.load()
        report = build_report(self._state(), config, meta={"goal": "拿 flag"}, audit_summary={"denied": 0, "chain_ok": True})
        for title in ("执行摘要", "授权与合规", "攻击路径", "资产", "漏洞详情", "凭据", "附录"):
            self.assertIn(title, report.markdown)
            self.assertIn(title, report.html)
        self.assertIn("<svg", report.html)
        self.assertIn("mermaid", report.html)
        self.assertNotIn("<script", report.html)
        for line in report.html.splitlines():
            stripped = line.strip()
            self.assertFalse(stripped.startswith("<link"), line)
            for attr in ('src="http', 'href="http://cdn', 'url("http'):
                self.assertNotIn(attr, stripped)

    def test_credentials_redacted_in_output(self):
        from trina.report import build_report

        report = build_report(self._state(), TrinaConfig.load())
        for blob in (report.markdown, report.html, json.dumps(report.data, ensure_ascii=False)):
            self.assertNotIn("admin123", blob, "报告不得出现明文口令")

    def test_save_writes_requested_formats(self):
        from trina.report import build_report

        with tempfile.TemporaryDirectory() as tmp:
            report = build_report(self._state(), TrinaConfig.load())
            paths = report.save(out_dir=tmp, formats=["markdown", "html", "json"], stem="demo")
            names = sorted(Path(p).name for p in paths)
            self.assertEqual(names, ["demo.html", "demo.json", "demo.md"])
            for path in paths:
                self.assertGreater(Path(path).stat().st_size, 200)

    def test_empty_state_still_renders(self):
        from trina.report import build_report

        report = build_report(RunState(run_id="empty", target="127.0.0.1"), TrinaConfig.load())
        self.assertIn("执行摘要", report.markdown)
        self.assertIn("<svg", report.html)


class TestAdaptersAndScoring(unittest.TestCase):
    def test_list_and_load_adapters(self):
        from trina.adapters import list_adapters, load_adapter

        config = TrinaConfig.load()
        names = {item["name"] for item in list_adapters(config)}
        for expected in ("lab", "dvwa", "pikachu", "vulhub", "vulnstack", "tsecbench"):
            self.assertIn(expected, names)
        adapter = load_adapter(config, "lab")
        self.assertIsInstance(adapter.status(), dict)
        self.assertGreaterEqual(len(adapter.ground_truth()), 1)

    def test_unreachable_range_does_not_raise(self):
        from trina.adapters import load_adapter

        info = load_adapter(TrinaConfig.load(), "dvwa").prepare()
        self.assertGreaterEqual(len(info.targets), 1)

    def test_tsecbench_scoring(self):
        from trina.adapters.tsecbench import score_tsecbench

        score = score_tsecbench(
            [
                {"task_id": "web-1", "dimension": "web", "difficulty": "easy", "score": 100, "max_score": 100},
                {"task_id": "bin-1", "dimension": "binary", "difficulty": "hard", "weight": 350, "score": 0, "max_score": 350},
                {"task_id": "exp-1", "dimension": "exploit", "difficulty": "normal", "score": 150, "max_score": 200},
            ]
        )
        self.assertEqual(score["raw_total"], 250)
        self.assertEqual(score["raw_max"], 650)
        self.assertAlmostEqual(score["normalized"], 38.46, places=2)
        self.assertEqual(score["solved"], 2)
        self.assertAlmostEqual(score["dimensions"]["web"]["normalized"], 100.0)

    def test_tsecbench_empty(self):
        from trina.adapters.tsecbench import score_tsecbench

        score = score_tsecbench([])
        self.assertEqual(score["raw_total"], 0)
        self.assertEqual(score["normalized"], 0.0)


class TestAdversarialProxy(unittest.TestCase):
    def test_modes_block_and_stats(self):
        from trina.eval.adversarial import AdversarialProxy

        proxy = AdversarialProxy(upstream="http://127.0.0.1:9", modes=["waf", "tamper", "honeypot"], port=0)
        proxy.start()
        try:
            port = proxy.bound_port
            base = f"http://127.0.0.1:{port}"
            # WAF 拦截
            try:
                urllib.request.urlopen(f"{base}/sqli?id=1'%20union%20select%201,2,3--", timeout=5)
                blocked = False
            except urllib.error.HTTPError as exc:
                blocked = exc.code == 403
            self.assertTrue(blocked, "含 union select 的请求应被 WAF 拦截")
            # 蜜罐路径
            try:
                with urllib.request.urlopen(f"{base}/.hidden-backup.zip", timeout=5) as response:
                    body = response.read()
                honeypot_ok = response.status == 200 and len(body) > 0
            except urllib.error.HTTPError:
                honeypot_ok = False
            self.assertTrue(honeypot_ok, "蜜罐路径应返回 200 与假正文")
            stats = proxy.stats
            self.assertGreaterEqual(stats.get("blocked", 0), 1)
            self.assertGreaterEqual(stats.get("honeypot_hits", 0), 1)
        finally:
            proxy.stop()


class TestLabTarget(unittest.TestCase):
    """lab 靶机：故意脆弱，但必须只监听回环且行为稳定。"""

    @classmethod
    def setUpClass(cls):
        from trina.lab.http_target import create_server

        cls.server, cls.thread = create_server(port=0)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=5)

    def _get(self, path: str) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    def test_lfi_reads_passwd(self):
        status, body = self._get("/lfi?file=../../../../etc/passwd")
        self.assertEqual(status, 200)
        self.assertIn("root:x:0:0", body)

    def test_rce_echoes_id(self):
        status, body = self._get("/rce?cmd=;echo%20TRINA_TEST_MARK")
        self.assertEqual(status, 200)
        self.assertIn("TRINA_TEST_MARK", body)

    def test_sqli_errors(self):
        status, body = self._get("/sqli?id=1%27")
        self.assertGreaterEqual(status, 400)
        self.assertTrue("SQL" in body or "sql" in body)

    def test_soft404_shape(self):
        status, body = self._get("/definitely-not-here-12345")
        self.assertEqual(status, 200)
        self.assertEqual(len(body.encode("utf-8")), 137, "软 404 必须保持固定长度（用于验证误报过滤）")

    def test_flag_present(self):
        status, body = self._get("/flag")
        self.assertEqual(status, 200)
        self.assertRegex(body, r"flag\{trina_lab_[0-9a-f]+\}")


if __name__ == "__main__":
    unittest.main()
