"""审计哈希链测试：篡改必须被检出。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from trina.audit import AuditLog, redact, sha256_text


class TestAuditChain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = AuditLog.open(self.tmp.name, run_id="t1", resume=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_chain_valid_after_writes(self):
        for index in range(5):
            self.log.append("tool.start", tool=f"t{index}", decision="allow")
        ok, detail = self.log.verify()
        self.assertTrue(ok, detail)
        self.assertIn("5 条记录", detail)

    def test_prev_hash_links(self):
        first = self.log.append("a")
        second = self.log.append("b")
        self.assertEqual(second["prev_hash"], first["hash"])
        self.assertEqual(first["prev_hash"], "0" * 64)

    def test_tamper_detected(self):
        self.log.append("tool.start", tool="x", decision="allow")
        self.log.append("tool.end", tool="x", decision="allow")
        self.log.append("run.end", status="success")
        path = Path(self.log.path)
        lines = path.read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[0])
        record["decision"] = "deny"
        lines[0] = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ok, detail = self.log.verify()
        self.assertFalse(ok)
        self.assertIn("篡改", detail)

    def test_deleted_record_detected(self):
        for index in range(3):
            self.log.append("e", index=index)
        path = Path(self.log.path)
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
        path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")
        ok, detail = self.log.verify()
        self.assertFalse(ok)

    def test_resume_continues_chain(self):
        self.log.append("e1")
        resumed = AuditLog.open(self.tmp.name, run_id="t1")
        self.assertEqual(resumed._prev_hash, self.log._prev_hash)
        resumed.append("e2")
        ok, _ = resumed.verify()
        self.assertTrue(ok)
        self.assertEqual(len(resumed.read_all()), 2)

    def test_summary_counts(self):
        self.log.append("tool.start", tool="port_scan", decision="allow")
        self.log.append("tool.denied", tool="rce_exec", decision="deny", reason="越界")
        summary = self.log.summary()
        self.assertEqual(summary["tools"]["port_scan"], 1)
        self.assertEqual(summary["denied"], 1)

    def test_secrets_redacted(self):
        self.log.append("cred", username="admin", password="admin123", cookie="session=abc")
        raw = Path(self.log.path).read_text(encoding="utf-8")
        self.assertNotIn("admin123", raw)
        self.assertNotIn("session=abc", raw)
        self.assertIn("<redacted:", raw)

    def test_redact_helper(self):
        out = redact({"password": "p", "nested": {"api_key": "k", "keep": 1}})
        self.assertTrue(out["password"].startswith("<redacted:"))
        self.assertTrue(out["nested"]["api_key"].startswith("<redacted:"))
        self.assertEqual(out["nested"]["keep"], 1)

    def test_sha256_text_stable(self):
        self.assertEqual(sha256_text("abc"), sha256_text("abc"))


if __name__ == "__main__":
    unittest.main()
