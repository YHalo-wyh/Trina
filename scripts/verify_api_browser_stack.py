"""真实回环靶机验证：静态API→运行时采集→mock隔离→上传→trace→证据登记。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trina.audit import AuditLog
from trina.config import TrinaConfig
from trina.scope import ScopeGuard
from trina.state import RunState
from trina.tools import build_registry
from trina.tools.http import HttpClient
from trina.tools.registry import ToolContext


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        html = '<html><title>Trina local fixture</title><script src="/main.js"></script><input type="file" id="file"><button id="fetch" onclick="fetch(\'/api/users?id=7\')">fetch</button></html>'
        body = html if self.path == "/" else 'const route={path:"/admin"};' if self.path == "/main.js" else '{"source":"real","id":7}'
        self.send_response(200)
        self.send_header("Content-Type", "text/html" if self.path == "/" else "application/javascript" if self.path == "/main.js" else "application/json")
        self.end_headers()
        self.wfile.write(body.encode())


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="trina-browser-smoke-") as temporary:
        root = Path(temporary)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = TrinaConfig()
        config.scope.trust_mode = "full"
        config.scope.max_qps = 1000
        config.browser.command_timeout = 45
        guard = ScopeGuard(config.scope)
        registry = build_registry()
        url = f"http://127.0.0.1:{server.server_port}"
        ctx = ToolContext(config=config, guard=guard, audit=AuditLog.open(root / "logs", "smoke"),
            state=RunState("smoke", url), http=HttpClient(guard), workspace=root / "run", tools=registry)
        verified = []

        def run(name: str, **params):
            result = registry.execute(name, params, ctx, timeout=60)
            if not result.ok:
                evidence = result.data.get("evidence_ref", "")
                diagnostic = Path(evidence).read_text(encoding="utf-8") if evidence and Path(evidence).is_file() else ""
                raise RuntimeError(f"{name}: {result.error} {result.data} {diagnostic}")
            verified.append(name)
            return result.data

        def evaluate(code: str):
            return run("browser_evaluate", session_id=sid, mode="playwright", code=code)

        try:
            recon = run("api_recon_static", url=url)
            assert "/admin" in recon["routes"]
            sid = run("browser_open", url=url)["session_id"]
            run("api_recon_runtime", session_id=sid, action="start")
            evaluate("async page => { await Promise.all([page.waitForResponse(r=>r.url().includes('/api/users')), page.locator('#fetch').click()]); return 'clicked'; }")
            collected = run("api_recon_runtime", session_id=sid, action="stop")
            assert any("/api/users" in r["url"] for r in collected.get("requests", [])), collected
            assert run("list_assets")["total"] >= 2
            run("browser_recording", session_id=sid, kind="trace", action="start")
            run("browser_route", session_id=sid, action="set", pattern="**/api/users*", body='{"source":"mock"}')
            mocked = evaluate("async page => page.evaluate(async()=> (await fetch('/api/users?id=9')).json())")
            assert mocked["synthetic"]
            mock_ref = mocked["evidence_ref"]
            rejected = registry.execute("report_finding", {"title": "mock is not proof", "vuln_class": "idor", "target": url, "evidence_refs": [mock_ref]}, ctx)
            assert not rejected.ok
            run("browser_route", session_id=sid, action="remove")
            still_tainted = evaluate("async page => page.url()")
            assert still_tainted["synthetic"]
            run("browser_interact", session_id=sid, action="reload")
            real = evaluate("async page => page.evaluate(async()=> (await fetch('/api/users?id=9')).json())")
            assert not real["synthetic"]
            assert "real" in real["output"]
            fixture_file = ctx.workspace / "upload.txt"
            fixture_file.write_text("fixture upload", encoding="utf-8")
            evaluate("async page => { page.locator('#file').click(); return 'filechooser'; }")
            run("browser_upload", session_id=sid, paths=[str(fixture_file)])
            upload = evaluate("async page => page.locator('#file').evaluate(el=>el.files[0].name)")
            assert "upload.txt" in upload["output"]
            trace = run("browser_recording", session_id=sid, kind="trace", action="stop")
            assert list(Path(trace["directory"]).rglob("*.trace")), trace
            run("record_fact", text="真实浏览器API回环请求已复核", evidence_refs=[real["evidence_ref"]])
            run("browser_close", session_id=sid)
            assert ctx.audit.verify()[0]
            print(json.dumps({"ok": True, "checks": sorted(set(verified)), "request_count": collected["request_count"],
                              "assets": len(ctx.state.casebook["assets"]), "mock_proof_rejected": True}, ensure_ascii=False))
        finally:
            ctx.extra["browser_sessions"].close()
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    main()
