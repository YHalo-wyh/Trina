"""审计哈希链测试：篡改必须被检出。"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from trina.audit import AuditLog, redact, sha256_text
from trina.cli import main


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

    def test_cli_show_verifies_and_summarizes_in_one_pass(self):
        first = self.log.append("fixture.first", tool="reader", decision="deny")
        second = self.log.append("fixture.second", tool="reader")
        original = self.log.path.read_bytes()

        with patch.object(AuditLog, "iter_records", wraps=AuditLog.iter_records) as read:
            code, output = self._audit_command("show", "--limit", "1")

        read.assert_called_once_with(self.log.path)
        self.assertEqual(code, 0)
        summary, offset = json.JSONDecoder().raw_decode(output)
        self.assertEqual(summary, {**self.log.summary(), "chain_ok": True})
        self.assertIn(f"#{second['seq']} fixture.second", output[offset:])
        self.assertNotIn(f"#{first['seq']} fixture.first", output[offset:])
        self.assertEqual(self.log.path.read_bytes(), original)

    def test_corrupt_record_fails_verify_and_is_visible_in_show_and_tail(self):
        self.log.append("fixture.first")
        self.log.append("fixture.last")
        first, last = self.log.path.read_text(encoding="utf-8").splitlines()
        for malformed in ("{truncated", "[]", "null"):
            with self.subTest(malformed=malformed):
                self.log.path.write_text(f"{first}\n{malformed}\n{last}\n", encoding="utf-8")
                code, output = self._audit_command("verify")
                self.assertEqual(code, 2)
                self.assertIn("第 2 行不是合法 JSON", output)

                code, output = self._audit_command("show", "--limit", "0")
                self.assertEqual(code, 0)
                summary = json.loads(output)
                self.assertFalse(summary["chain_ok"])
                self.assertEqual(summary["records"], 3)
                self.assertEqual(summary["events"], {"fixture.first": 1, "corrupt": 1, "fixture.last": 1})

                code, output = self._audit_command("tail", "--limit", "2")
                self.assertEqual(code, 0)
                damaged, intact = (json.loads(line) for line in output.splitlines())
                self.assertEqual(damaged, {"event": "corrupt", "raw": malformed})
                self.assertEqual(intact["event"], "fixture.last")

    def test_cli_tail_retains_order_and_limit_semantics(self):
        for _ in range(22):
            self.log.append("fixture")
        with patch("trina.audit.json.loads", wraps=json.loads) as decode:
            self.assertEqual([record["seq"] for record in self.log.tail(2)], [21, 22])
        self.assertEqual(decode.call_count, 2)
        for limit, expected in ((0, range(3, 23)), (2, range(21, 23)), (-2, range(3, 23))):
            with self.subTest(limit=limit):
                code, output = self._audit_command("tail", "--limit", str(limit))
                self.assertEqual(code, 0)
                self.assertEqual([json.loads(line)["seq"] for line in output.splitlines()], list(expected))

    def _audit_command(self, action: str, *options: str) -> tuple[int, str]:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(["audit", action, str(self.log.path), *options])
        return code, stdout.getvalue()

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
