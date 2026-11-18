"""每任务隔离的 Playwright CLI 会话与证据归档。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..scope import ensure_execution_active, execution_budget
from ..tools.external import run_external

PLAYWRIGHT_PACKAGE = "@playwright/cli@0.1.24"


def _safe_args(args: list[str]) -> list[str]:
    """命令证据保留形状，但不把填写值或存储值写入审计。"""
    sensitive = {
        "fill": {2},
        "type": {1},
        "select": {2},
        "cookie-set": {2},
        "localstorage-set": {2},
        "sessionstorage-set": {2},
    }.get(args[0] if args else "", set())
    safe = list(args)
    if args and args[0] in {"eval", "run-code"}:
        sensitive = {1}
    if args and args[0] == "route" and "--body" in args:
        sensitive = {args.index("--body") + 1}
    for index in sensitive:
        if index < len(safe):
            digest = hashlib.sha256(safe[index].encode("utf-8", "replace")).hexdigest()[:8]
            safe[index] = f"<redacted:{digest}>"
    return safe


def playwright_launcher() -> list[str]:
    """优先使用镜像内全局 CLI，本机没有时通过固定版本 npx 启动。"""
    resolved = shutil.which("playwright-cli")
    if resolved:
        # npm 的 Windows .cmd 会再次经过 cmd.exe，破坏多行JS和引号；直接交给Node。
        script = Path(resolved).parent / "node_modules" / "@playwright" / "cli" / "playwright-cli.js"
        node = shutil.which("node")
        if os.name == "nt" and script.is_file() and node:
            return [node, str(script)]
        return [resolved]
    npx = shutil.which("npx")
    if npx:
        script = Path(npx).parent / "node_modules" / "npm" / "bin" / "npx-cli.js"
        node = shutil.which("node")
        if os.name == "nt":
            if not script.is_file() or not node:
                return []
            return [node, str(script), "--yes", "--package", PLAYWRIGHT_PACKAGE, "playwright-cli"]
        return ["npx", "--yes", "--package", PLAYWRIGHT_PACKAGE, "playwright-cli"]
    return []


class BrowserError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class BrowserCall:
    ok: bool
    session_id: str = ""
    payload: Any = field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    command: str = ""
    evidence_ref: str = ""
    session_dir: str = ""
    synthetic: bool = False


def _decode_json(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for match in re.finditer(r"[\[{]", text):
            try:
                value, _ = decoder.raw_decode(text[match.start():])
                return value
            except json.JSONDecodeError:
                continue
    return {"result": text}


class BrowserSessions:
    """持有不透明会话句柄；CLI 名称、进程和工件路径不交给模型控制。"""

    def __init__(self, ctx: Any):
        self.ctx = ctx
        self.sessions: dict[str, dict[str, Any]] = {}
        self.previous: list[dict[str, Any]] = []
        self.lock = threading.RLock()
        self.closed = threading.Event()

    def check(self, ctx: Any) -> None:
        ensure_execution_active()
        if (
            self.closed.is_set()
            or ctx.state.run_id != self.ctx.state.run_id
            or ctx.workspace.resolve() != self.ctx.workspace.resolve()
        ):
            raise BrowserError("SESSION_EXPIRED", "浏览器会话不属于当前任务或已关闭")
        capability_check = getattr(ctx.tools, "capability_check", None)
        if capability_check:
            capability_check()
        stop = self.ctx.extra.get("stop_event")
        deadline = self.ctx.extra.get("task_deadline")
        if (stop is not None and stop.is_set()) or (deadline and time.monotonic() >= deadline):
            self.close()
            raise BrowserError("SESSION_EXPIRED", "任务已停止或超时")

    def open(self, ctx: Any, url: str, *, headed: bool = False, browser: str = "chromium") -> BrowserCall:
        self.check(ctx)
        launcher = playwright_launcher()
        if not launcher:
            raise BrowserError("DEPENDENCY_MISSING", "需要 playwright-cli，或 Node.js/npm 提供的 npx")
        with self.lock:
            live = [row for row in self.sessions.values() if row.get("live") or row.get("state") == "OPENING"]
            if len(live) >= ctx.config.browser.max_sessions:
                raise BrowserError("SESSION_LIMIT", "浏览器会话已达上限，请先调用 browser_close")
            sid = "web-" + uuid.uuid4().hex[:12]
            cli_name = "trina-" + uuid.uuid4().hex[:16]
            directory = (ctx.workspace / "browser" / sid).resolve()
            directory.mkdir(parents=True, exist_ok=True)
            row = {
                "session_id": sid,
                "cli_name": cli_name,
                "url": url,
                "browser": browser,
                "headed": headed,
                "directory": directory,
                "lock": threading.Lock(),
                "owner": ctx.extra.get("_execution_owner"),
                "used": time.monotonic(),
                "sequence": 0,
                "live": False,
                "state": "OPENING",
            }
            self.sessions[sid] = row
        args = ["open", url, "--idle-timeout", str(int(ctx.config.browser.idle_timeout * 1000))]
        if headed:
            args.append("--headed")
        # CLI 自身默认寻找系统Chrome；明确指定，才能使用已安装的Playwright Chromium。
        args.extend(["--browser", browser])
        call = self._invoke(ctx, row, args, target=url, lock=False)
        with self.lock:
            if call.ok:
                row.update(owner=None, live=True, state="READY", used=time.monotonic())
            else:
                self._best_effort_close(row)
                self.sessions.pop(sid, None)
        return call

    def invoke(self, ctx: Any, sid: str, args: list[str], *, target: str = "") -> BrowserCall:
        self.check(ctx)
        with self.lock:
            row = self.sessions.get(sid)
        if row is None or not row.get("live"):
            raise BrowserError("SESSION_EXPIRED", "浏览器句柄失效或属于其他 Worker，请重新打开")
        return self._invoke(ctx, row, args, target=target or str(row.get("url", "")), lock=True)

    def navigate(self, ctx: Any, sid: str, url: str, *, new_tab: bool = False) -> BrowserCall:
        call = self.invoke(ctx, sid, ["tab-new" if new_tab else "goto", url], target=url)
        if call.ok:
            with self.lock:
                self.sessions[sid]["url"] = url
        return call

    def close_session(self, ctx: Any, sid: str) -> BrowserCall:
        self.check(ctx)
        with self.lock:
            row = self.sessions.get(sid)
        if row is None:
            raise BrowserError("SESSION_EXPIRED", "浏览器句柄不存在或已关闭")
        call = self._invoke(ctx, row, ["close"], target=str(row.get("url", "")), lock=True)
        self._retire(sid, row, "CLOSED" if call.ok else "LOST")
        return call

    def cancel_owner(self, owner: Any) -> None:
        with self.lock:
            rows = [(sid, row) for sid, row in self.sessions.items() if row.get("owner") is owner]
        for sid, row in rows:
            row["state"] = "CANCELLED"
            row["live"] = False
            self._best_effort_close(row)
            self._retire(sid, row, "CANCELLED")

    def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        with self.lock:
            rows = list(self.sessions.items())
        for sid, row in rows:
            self._best_effort_close(row)
            self._retire(sid, row, "CLOSED")

    def snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            current = [
                {
                    "session_id": sid,
                    "url": row.get("url", ""),
                    "browser": row.get("browser", "chromium"),
                    "headed": bool(row.get("headed")),
                    "state": row.get("state", "READY"),
                    "live": bool(row.get("live")),
                    "evidence_dir": str(row.get("directory", "")),
                }
                for sid, row in self.sessions.items()
            ]
            return (list(self.previous) + current)[-16:]

    def _invoke(
        self,
        ctx: Any,
        row: dict[str, Any],
        args: list[str],
        *,
        target: str,
        lock: bool,
    ) -> BrowserCall:
        self.check(ctx)
        acquired = False
        if lock:
            acquired = row["lock"].acquire(timeout=execution_budget(ctx.config.browser.command_timeout))
            if not acquired:
                raise BrowserError("SESSION_BUSY", "浏览器会话正在执行另一项操作")
        try:
            self.check(ctx)
            row["owner"] = ctx.extra.get("_execution_owner")
            row["used"] = time.monotonic()
            row["sequence"] += 1
            launcher = playwright_launcher()
            if not launcher:
                raise BrowserError("DEPENDENCY_MISSING", "需要 playwright-cli，或 Node.js/npm 提供的 npx")
            argv = [*launcher, f"-s={row['cli_name']}", "--json", *args]
            result = run_external(
                argv,
                ctx,
                tool="browser:" + (args[0] if args else "unknown"),
                target=target,
                required_level=ctx.effective_level,
                timeout=ctx.config.browser.command_timeout,
                cwd=str(row["directory"]),
                audit=False,
                stream_output=False,
                allow_detached_children=bool(args and args[0] == "open"),
            )
            safe_command = " ".join([*launcher, "-s=<managed-session>", "--json", *_safe_args(args)])
            ctx.audit.append(
                "external.run",
                tool="browser:" + (args[0] if args else "unknown"),
                decision="allow",
                command=safe_command,
                code=result.code,
                target=target,
                duration=round(result.duration, 2),
                stdout_digest=hashlib.sha256(result.stdout.encode("utf-8", "replace")).hexdigest(),
                stdout_bytes=len(result.stdout),
                allow_detached_children=bool(args and args[0] == "open"),
            )
            result.command = safe_command
            raw = result.stdout or result.stderr or result.error
            evidence = self._archive(ctx, row, args[0] if args else "command", raw)
            if not result.ok:
                return BrowserCall(
                    ok=False,
                    session_id=str(row["session_id"]),
                    stdout=result.stdout,
                    stderr=result.stderr,
                    error=result.error or result.stderr or f"playwright-cli 退出码 {result.code}",
                    command=result.command,
                    evidence_ref=str(evidence),
                    session_dir=str(row["directory"]),
                )
            if args and args[0] in {"goto", "reload", "go-back", "go-forward", "tab-new"} and not row.get("mock_patterns"):
                row["mock_tainted"] = False
            return BrowserCall(
                ok=True,
                session_id=str(row["session_id"]),
                payload=_decode_json(result.stdout),
                stdout=result.stdout,
                stderr=result.stderr,
                command=result.command,
                evidence_ref=str(evidence),
                session_dir=str(row["directory"]),
                synthetic=bool(row.get("mock_patterns") or row.get("mock_tainted")),
            )
        finally:
            row["owner"] = None
            if acquired:
                row["lock"].release()

    def _archive(self, ctx: Any, row: dict[str, Any], command: str, raw: str) -> Path:
        ensure_execution_active()
        safe = re.sub(r"[^a-z0-9_-]+", "-", command.lower()).strip("-") or "command"
        path = Path(row["directory"]) / f"{row['sequence']:04d}-{safe}.json"
        path.write_text(raw or "{}", encoding="utf-8", errors="replace")
        ctx.state.artifacts.setdefault("browser_evidence", []).append(str(path))
        return path

    def _best_effort_close(self, row: dict[str, Any]) -> None:
        launcher = playwright_launcher()
        if not launcher:
            return
        try:
            run_external(
                [*launcher, f"-s={row['cli_name']}", "--json", "close"],
                self.ctx,
                tool="browser:close",
                target=str(row.get("url", "")),
                required_level="detect",
                timeout=min(10.0, self.ctx.config.browser.command_timeout),
                cwd=str(row["directory"]),
                audit=False,
                stream_output=False,
            )
        except Exception:
            pass

    def _retire(self, sid: str, row: dict[str, Any], state: str) -> None:
        snapshot = {
            "session_id": sid,
            "url": row.get("url", ""),
            "browser": row.get("browser", "chromium"),
            "headed": bool(row.get("headed")),
            "state": state,
            "live": False,
            "evidence_dir": str(row.get("directory", "")),
        }
        with self.lock:
            self.sessions.pop(sid, None)
            self.previous = (self.previous + [snapshot])[-16:]
