"""状态模型测试：facts 吸收、漏洞去重合并、证据上限、提示词摘要。"""

from __future__ import annotations

import unittest

from trina.state import Finding, RunState


class TestAbsorb(unittest.TestCase):
    def setUp(self):
        self.state = RunState(run_id="s", target="127.0.0.1")

    def test_ports_and_hosts(self):
        changed = self.state.absorb({"ports": [{"host": "127.0.0.1", "port": 80, "service": "http", "version": "nginx/1.24"}],
                                     "hosts": [{"host": "127.0.0.1", "alive": True}]})
        self.assertTrue(any("发现端口" in item for item in changed))
        host = self.state.hosts["127.0.0.1"]
        self.assertTrue(host.alive)
        self.assertEqual(host.ports[80]["service"], "http")
        self.assertEqual(host.ports[80]["version"], "nginx/1.24")
        # 重复吸收不产生新条目
        self.state.absorb({"ports": [{"host": "127.0.0.1", "port": 80, "service": "http"}]})
        self.assertEqual(len(self.state.hosts["127.0.0.1"].ports), 1)

    def test_services_paths_tech(self):
        self.state.absorb({"services": [{"host": "127.0.0.1", "technology": "Spring Boot"}]})
        self.state.absorb({"paths": [{"host": "127.0.0.1", "url": "http://127.0.0.1/admin", "status": 200}]})
        host = self.state.hosts["127.0.0.1"]
        self.assertIn("Spring Boot", host.technologies)
        self.assertIn("http://127.0.0.1/admin", host.web_paths)
        self.state.absorb({"paths": [{"host": "127.0.0.1", "url": "http://127.0.0.1/admin"}]})
        self.assertEqual(len(host.web_paths), 1)

    def test_credentials_and_flags_dedup(self):
        self.state.absorb({"credentials": [{"target": "127.0.0.1", "service": "ssh", "username": "root", "password": "toor", "verified": True}]})
        self.state.absorb({"credentials": [{"target": "127.0.0.1", "service": "ssh", "username": "root", "password": "toor"}]})
        self.assertEqual(len(self.state.credentials), 1)
        self.assertEqual(self.state.credentials[0].password, "toor")
        self.state.absorb({"flags": [{"value": "flag{a}"}, {"value": "flag{a}"}, {"value": "flag{b}"}]})
        self.assertEqual(len(self.state.flags), 2)

    def test_notes_do_not_crash(self):
        self.state.absorb({"notes": ["手工备注"]})
        self.assertIn("手工备注", self.state.hosts["127.0.0.1"].notes)
        self.state.absorb(None)
        self.state.absorb({})


class TestFindings(unittest.TestCase):
    def setUp(self):
        self.state = RunState(run_id="s", target="127.0.0.1")

    def test_ids_are_sequential(self):
        first = self.state.add_finding({"vuln_class": "sqli", "url": "http://x/a?id=1", "parameter": "id", "severity": "high"})
        second = self.state.add_finding({"vuln_class": "xss_reflected", "url": "http://x/b", "severity": "medium"})
        self.assertEqual(first.id, "TRINA-001")
        self.assertEqual(second.id, "TRINA-002")

    def test_dedup_merges_evidence_and_status(self):
        first = self.state.add_finding(
            {"vuln_class": "sqli", "url": "http://x/a?id=1", "parameter": "id", "severity": "medium",
             "status": "detected", "evidence": [{"kind": "http", "note": "布尔差异"}]}
        )
        second = self.state.add_finding(
            {"vuln_class": "sqli", "url": "http://x/a?id=1", "parameter": "id", "severity": "high",
             "status": "verified", "confidence": "high", "payload": "' AND 1=2--", "evidence": [{"kind": "http", "note": "时间盲注"}]}
        )
        self.assertIs(first, second)
        self.assertEqual(len(self.state.findings), 1)
        self.assertEqual(first.status, "verified")
        self.assertEqual(first.severity, "high")
        self.assertEqual(first.confidence, "high")
        self.assertEqual(first.payload, "' AND 1=2--")
        self.assertEqual(len(first.evidence), 2)

    def test_different_parameter_is_separate_finding(self):
        self.state.add_finding({"vuln_class": "sqli", "url": "http://x/a?id=1", "parameter": "id"})
        self.state.add_finding({"vuln_class": "sqli", "url": "http://x/a?id=1", "parameter": "name"})
        self.assertEqual(len(self.state.findings), 2)

    def test_severity_mapping_in_dict(self):
        finding = self.state.add_finding({"vuln_class": "rce", "severity": "critical", "title": "命令执行"})
        self.assertEqual(finding.to_dict()["severity_cn"], "严重")

    def test_missing_title_falls_back_to_class(self):
        finding = self.state.add_finding({"vuln_class": "lfi"})
        self.assertEqual(finding.title, "lfi")

    def test_status_only_upgrades(self):
        finding = self.state.add_finding({"vuln_class": "lfi", "status": "exploited"})
        self.state.add_finding({"vuln_class": "lfi", "status": "detected"})
        self.assertEqual(finding.status, "exploited")


class TestEvidenceAndPrompt(unittest.TestCase):
    def test_evidence_cap_enforced(self):
        state = RunState(run_id="s", target="127.0.0.1", max_evidence=3)
        for index in range(6):
            state.add_evidence({"kind": "http", "note": str(index)})
        self.assertEqual(len(state.evidence), 3)
        self.assertEqual(state.evidence[-1]["note"], "5")

    def test_summary_contains_key_facts(self):
        state = RunState(run_id="s", target="127.0.0.1")
        state.absorb({"ports": [{"host": "127.0.0.1", "port": 22, "service": "ssh"}]})
        state.absorb({"credentials": [{"target": "127.0.0.1", "service": "ssh", "username": "root", "password": "x"}]})
        state.absorb({"flags": [{"value": "flag{x}"}]})
        state.add_finding({"vuln_class": "sqli", "url": "http://127.0.0.1/a?id=1", "parameter": "id", "severity": "high"})
        summary = state.summary_for_prompt()
        for expected in ("127.0.0.1", "22/ssh", "flag{x}", "sqli", "TRINA-001", "凭据"):
            self.assertIn(expected, summary)

    def test_summary_truncated(self):
        state = RunState(run_id="s", target="127.0.0.1")
        for index in range(200):
            state.absorb({"paths": [{"host": "127.0.0.1", "url": f"http://127.0.0.1/p{index}"}]})
        self.assertLessEqual(len(state.summary_for_prompt(max_chars=500)), 560)

    def test_timeline_and_steps(self):
        state = RunState(run_id="s", target="127.0.0.1")
        state.set_phase("enum", "进入枚举")
        state.log_step({"kind": "react", "tool": "port_scan"})
        self.assertEqual(state.phase, "enum")
        self.assertEqual(state.counters["steps"], 1)
        self.assertTrue(any(item["kind"] == "phase" for item in state.timeline))

    def test_to_dict_is_json_serializable(self):
        import json

        state = RunState(run_id="s", target="127.0.0.1")
        state.absorb({"ports": [{"host": "127.0.0.1", "port": 80, "service": "http"}]})
        state.add_finding({"vuln_class": "xss_reflected", "url": "http://x/", "evidence": [{"kind": "http"}]})
        self.assertIsInstance(json.dumps(state.to_dict(), ensure_ascii=False), str)

    def test_finding_defaults(self):
        finding = Finding(id="X", title="t", vuln_class="c")
        self.assertEqual(finding.status, "detected")
        self.assertEqual(finding.confidence, "medium")


if __name__ == "__main__":
    unittest.main()
