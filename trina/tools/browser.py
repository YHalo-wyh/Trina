"""基于 Playwright CLI 的结构化浏览器工具。"""

from __future__ import annotations

import json
import hashlib
import re
import uuid
from pathlib import Path
from typing import Any

from ..browser import BrowserCall, BrowserError, playwright_launcher
from .registry import ParamSpec as P, Tool, ToolResult, ToolSpec


def _available() -> tuple[bool, str]:
    if playwright_launcher():
        return True, ""
    return False, "未安装 playwright-cli，且找不到 Node.js/npm 提供的 npx"


def _pool(ctx: Any):
    if not ctx.config.browser.enabled:
        raise BrowserError("DISABLED", "browser.enabled=false，浏览器能力已关闭")
    if not ctx.config.scope.full_trust:
        raise BrowserError(
            "RESTRICTED_UNSUPPORTED",
            "Playwright 页面可能自动加载跨源子资源；restricted 模式下禁用真实浏览器，请使用受控 HTTP 工具",
        )
    return ctx.extra["browser_sessions"]


def _preview(payload: Any, limit: int = 4000) -> str:
    if not payload:
        return ""
    if isinstance(payload, dict) and isinstance(payload.get("result"), str):
        text = payload["result"]
    else:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= limit else text[:limit] + "…"


def _snapshot_lines(payload: Any, limit: int = 70) -> list[str]:
    roots = payload.get("snapshot", []) if isinstance(payload, dict) else []
    lines: list[str] = []

    def visit(node: Any, depth: int = 0) -> None:
        if len(lines) >= limit or not isinstance(node, dict):
            return
        role = str(node.get("role", "node"))
        ref = str(node.get("ref", ""))
        label = node.get("name") or node.get("text") or node.get("value") or ""
        label = re.sub(r"\s+", " ", str(label)).strip()
        if len(label) > 180:
            label = label[:177] + "…"
        suffix = f" ref={ref}" if ref else ""
        if node.get("url"):
            suffix += f" url={node['url']}"
        lines.append(f"{'  ' * min(depth, 6)}- {role}{suffix}" + (f" :: {label}" if label else ""))
        for child in node.get("children", []) or []:
            visit(child, depth + 1)

    for root in roots if isinstance(roots, list) else [roots]:
        visit(root)
    return lines


def _call_result(
    call: BrowserCall,
    *,
    session_id: str,
    summary: str,
    kind: str,
    url: str = "",
    data: dict[str, Any] | None = None,
    hints: list[str] | None = None,
) -> ToolResult:
    base = {
        "synthetic": call.synthetic,
        "session_id": session_id,
        "evidence_ref": call.evidence_ref,
        **(data or {}),
    }
    evidence = [{"kind": kind, "session_id": session_id, "url": url, "evidence_ref": call.evidence_ref,
                 "successful": call.ok, "synthetic": call.synthetic or kind == "browser.mock"}]
    if call.evidence_ref and Path(call.evidence_ref).is_file():
        evidence[0].update(path=call.evidence_ref, sha256=hashlib.sha256(Path(call.evidence_ref).read_bytes()).hexdigest())
    if not call.ok:
        return ToolResult(
            ok=False,
            summary=summary + "失败",
            error=call.error,
            command=call.command,
            data=base,
            evidence=evidence,
            next_hints=["检查 trina doctor 的 Node/Playwright 状态和 evidence_ref 中的完整诊断"],
        )
    return ToolResult(
        ok=True,
        summary=summary,
        command=call.command,
        data=base,
        evidence=evidence,
        next_hints=hints or [],
    )


def _browser_error(exc: BrowserError, *, session_id: str = "") -> ToolResult:
    return ToolResult(
        ok=False,
        summary=f"浏览器操作失败：{exc}",
        error=str(exc),
        category="environment",
        data={"error_code": exc.code, "session_id": session_id},
        next_hints=["失效会话需重新 browser_open；依赖缺失先运行 trina doctor"],
    )


class BrowserOpen(Tool):
    spec = ToolSpec(
        name="browser_open",
        summary="打开真实 Chromium/Chrome/Firefox/WebKit 会话并执行页面 JavaScript",
        category="browser",
        action_level="detect",
        external_bin="playwright-cli",
        produces=["files"],
        params={
            "url": P("str", "http(s) 页面 URL", required=True),
            "headed": P("bool", "显示浏览器窗口", required=False, default=False),
            "browser": P(
                "str",
                "浏览器；chromium 使用随 Playwright 安装的版本",
                required=False,
                default="chromium",
                enum=["chromium", "chrome", "firefox", "webkit", "msedge"],
            ),
        },
    )

    def available(self) -> tuple[bool, str]:
        return _available()

    def run(self, ctx: Any, url: str, headed: bool = False, browser: str = "chromium") -> ToolResult:
        try:
            call = _pool(ctx).open(ctx, url, headed=headed, browser=browser)
            return _call_result(
                call,
                session_id=call.session_id,
                summary=f"已打开 {browser}：{url}",
                kind="browser.open",
                url=url,
                data={"browser": browser, "headed": headed, "output": _preview(call.payload, 1200)},
                hints=["先调用 browser_snapshot 获取当前页面的稳定元素 ref"],
            )
        except BrowserError as exc:
            return _browser_error(exc)


class BrowserSnapshot(Tool):
    spec = ToolSpec(
        name="browser_snapshot",
        summary="获取当前页面可访问性快照和稳定元素 ref，完整结果保存为证据",
        category="browser",
        action_level="detect",
        external_bin="playwright-cli",
        produces=["files"],
        params={
            "session_id": P("str", "browser_open 返回的会话句柄", required=True),
            "ref": P("str", "可选：只快照某个现有元素 ref", required=False, default=""),
        },
    )

    def available(self) -> tuple[bool, str]:
        return _available()

    def run(self, ctx: Any, session_id: str, ref: str = "") -> ToolResult:
        try:
            call = _pool(ctx).invoke(ctx, session_id, ["snapshot", *([ref] if ref else [])])
            lines = _snapshot_lines(call.payload)
            return _call_result(
                call,
                session_id=session_id,
                summary=f"页面快照已获取（{len(lines)} 行摘要）",
                kind="browser.snapshot",
                data={"snapshot": "\n".join(lines), "truncated": len(lines) >= 70},
            )
        except BrowserError as exc:
            return _browser_error(exc, session_id=session_id)


class BrowserInteract(Tool):
    spec = ToolSpec(
        name="browser_interact",
        summary="使用最新快照 ref 导航、点击、填写、按键、选择或管理标签页",
        category="browser",
        action_level="detect",
        external_bin="playwright-cli",
        produces=["files"],
        params={
            "session_id": P("str", "浏览器会话句柄", required=True),
            "action": P(
                "str",
                "结构化交互动作",
                required=True,
                enum=[
                    "goto", "reload", "back", "forward", "click", "dblclick", "fill", "type",
                    "press", "hover", "select", "check", "uncheck", "tab_list", "tab_new",
                    "tab_select", "tab_close",
                ],
            ),
            "ref": P("str", "最新 browser_snapshot 中的元素 ref", required=False, default=""),
            "input_value": P("str", "fill/type/select 的输入值", required=False, default=""),
            "url": P("str", "goto/tab_new 的 http(s) URL", required=False, default=""),
            "key": P("str", "press 的键名，例如 Enter", required=False, default=""),
            "index": P("int", "标签页索引；-1 表示当前标签页", required=False, default=-1),
            "button": P("str", "click/dblclick 鼠标键", required=False, default="left", enum=["left", "right", "middle"]),
        },
    )

    _VERIFY = {"click", "dblclick", "fill", "type", "press", "select", "check", "uncheck"}

    def available(self) -> tuple[bool, str]:
        return _available()

    def required_level(self, params: dict[str, Any]) -> str:
        return "verify" if params.get("action") in self._VERIFY else "detect"

    def run(self, ctx: Any, **params: Any) -> ToolResult:
        sid = params["session_id"]
        action = params["action"]
        ref = params.get("ref", "")
        value = params.get("input_value", "")
        try:
            pool = _pool(ctx)
            if action == "goto":
                if not params.get("url"):
                    raise BrowserError("INVALID_ARGUMENT", "goto 需要 url")
                call = pool.navigate(ctx, sid, params["url"])
            elif action == "tab_new":
                call = pool.navigate(ctx, sid, params["url"], new_tab=True) if params.get("url") else pool.invoke(ctx, sid, ["tab-new"])
            elif action in {"reload", "back", "forward"}:
                call = pool.invoke(ctx, sid, [{"back": "go-back", "forward": "go-forward"}.get(action, action)])
            elif action in {"click", "dblclick"}:
                if not ref:
                    raise BrowserError("INVALID_ARGUMENT", f"{action} 需要最新快照中的 ref")
                call = pool.invoke(ctx, sid, [action, ref, params.get("button", "left")])
            elif action == "fill":
                if not ref:
                    raise BrowserError("INVALID_ARGUMENT", "fill 需要最新快照中的 ref")
                call = pool.invoke(ctx, sid, ["fill", ref, value])
            elif action == "type":
                call = pool.invoke(ctx, sid, ["type", value])
            elif action == "press":
                if not params.get("key"):
                    raise BrowserError("INVALID_ARGUMENT", "press 需要 key")
                call = pool.invoke(ctx, sid, ["press", params["key"]])
            elif action in {"hover", "check", "uncheck"}:
                if not ref:
                    raise BrowserError("INVALID_ARGUMENT", f"{action} 需要最新快照中的 ref")
                call = pool.invoke(ctx, sid, [action, ref])
            elif action == "select":
                if not ref:
                    raise BrowserError("INVALID_ARGUMENT", "select 需要最新快照中的 ref")
                call = pool.invoke(ctx, sid, ["select", ref, value])
            elif action == "tab_list":
                call = pool.invoke(ctx, sid, ["tab-list"])
            elif action in {"tab_select", "tab_close"}:
                index = params.get("index", -1)
                if action == "tab_select" and index < 0:
                    raise BrowserError("INVALID_ARGUMENT", "tab_select 需要非负 index")
                call = pool.invoke(ctx, sid, [action.replace("_", "-"), *([str(index)] if index >= 0 else [])])
            else:
                raise BrowserError("INVALID_ARGUMENT", f"未实现动作 {action}")
            output = _preview(call.payload, 1800)
            if value and action in {"fill", "type", "select"}:
                output = output.replace(value, "<redacted>")
            return _call_result(
                call,
                session_id=sid,
                summary=f"浏览器动作 {action} 已执行",
                kind="browser.interaction",
                url=params.get("url", ""),
                data={"action": action, "output": output},
                hints=["导航或页面显著变化后重新调用 browser_snapshot，旧 ref 可能失效"],
            )
        except BrowserError as exc:
            return _browser_error(exc, session_id=sid)


class BrowserStorage(Tool):
    spec = ToolSpec(
        name="browser_storage",
        summary="读取或修改 Cookie、localStorage、sessionStorage",
        category="browser",
        action_level="detect",
        external_bin="playwright-cli",
        produces=["files"],
        params={
            "session_id": P("str", "浏览器会话句柄", required=True),
            "area": P("str", "存储区域", required=True, enum=["cookies", "local", "session"]),
            "action": P("str", "操作", required=True, enum=["list", "get", "set", "delete", "clear"]),
            "key": P("str", "get/set/delete 的键名", required=False, default=""),
            "storage_value": P("str", "set 的值", required=False, default=""),
            "domain": P("str", "Cookie domain", required=False, default=""),
            "path": P("str", "Cookie path", required=False, default=""),
        },
    )

    def available(self) -> tuple[bool, str]:
        return _available()

    def required_level(self, params: dict[str, Any]) -> str:
        return "verify" if params.get("action") in {"set", "delete", "clear"} else "detect"

    def run(self, ctx: Any, **params: Any) -> ToolResult:
        sid, area, action = params["session_id"], params["area"], params["action"]
        try:
            if action in {"get", "set", "delete"} and not params.get("key"):
                raise BrowserError("INVALID_ARGUMENT", f"{action} 需要 key")
            prefix = {"cookies": "cookie", "local": "localstorage", "session": "sessionstorage"}[area]
            command = f"{prefix}-{action}"
            args = [command]
            if action in {"get", "delete"}:
                args.append(params["key"])
            elif action == "set":
                args.extend([params["key"], params.get("storage_value", "")])
                if area == "cookies":
                    if params.get("domain"):
                        args.extend(["--domain", params["domain"]])
                    if params.get("path"):
                        args.extend(["--path", params["path"]])
            call = _pool(ctx).invoke(ctx, sid, args)
            output = _preview(call.payload)
            secret = params.get("storage_value", "")
            if secret and action == "set":
                output = output.replace(secret, "<redacted>")
            return _call_result(
                call,
                session_id=sid,
                summary=f"{area} {action} 已执行",
                kind="browser.storage",
                data={"area": area, "action": action, "output": output},
            )
        except BrowserError as exc:
            return _browser_error(exc, session_id=sid)


class BrowserRequests(Tool):
    spec = ToolSpec(
        name="browser_requests",
        summary="读取页面真实网络请求、请求/响应头及正文，并把完整输出保存为证据",
        category="browser",
        action_level="detect",
        external_bin="playwright-cli",
        produces=["files"],
        params={
            "session_id": P("str", "浏览器会话句柄", required=True),
            "part": P(
                "str",
                "summary 列请求；其他值读取指定 index 的细节",
                required=False,
                default="summary",
                enum=["summary", "request", "request_headers", "request_body", "response_headers", "response_body"],
            ),
            "index": P("int", "请求编号；summary 时忽略", required=False, default=-1),
            "include_static": P("bool", "列表包含成功的静态资源", required=False, default=False),
            "all": P("bool", "列表包含会话开始以来的请求", required=False, default=False),
            "filter": P("str", "列表 URL 正则过滤", required=False, default=""),
        },
    )

    def available(self) -> tuple[bool, str]:
        return _available()

    def run(self, ctx: Any, **params: Any) -> ToolResult:
        sid, part, index = params["session_id"], params["part"], params.get("index", -1)
        try:
            if part == "summary":
                args = ["requests"]
                if params.get("include_static"):
                    args.append("--static")
                if params.get("all"):
                    args.append("--all")
                if params.get("filter"):
                    args.extend(["--filter", params["filter"]])
            else:
                if index < 0:
                    raise BrowserError("INVALID_ARGUMENT", f"{part} 需要非负 index")
                args = [part.replace("_", "-"), str(index)]
            call = _pool(ctx).invoke(ctx, sid, args)
            return _call_result(
                call,
                session_id=sid,
                summary=f"浏览器网络记录 {part} 已获取",
                kind="browser.network",
                data={"part": part, "index": index, "output": _preview(call.payload, 5000)},
            )
        except BrowserError as exc:
            return _browser_error(exc, session_id=sid)


class BrowserScreenshot(Tool):
    spec = ToolSpec(
        name="browser_screenshot",
        summary="保存当前页面或指定元素的 PNG 截图",
        category="browser",
        action_level="detect",
        external_bin="playwright-cli",
        produces=["files"],
        params={
            "session_id": P("str", "浏览器会话句柄", required=True),
            "ref": P("str", "可选元素 ref", required=False, default=""),
            "full_page": P("bool", "截取完整滚动页面", required=False, default=False),
            "label": P("str", "工件短标签", required=False, default="page"),
        },
    )

    def available(self) -> tuple[bool, str]:
        return _available()

    def run(self, ctx: Any, session_id: str, ref: str = "", full_page: bool = False, label: str = "page") -> ToolResult:
        try:
            safe = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-.")[:40] or "page"
            filename = f"screenshot-{safe}-{uuid.uuid4().hex[:8]}.png"
            args = ["screenshot", *([ref] if ref else []), "--filename", filename]
            if full_page:
                args.append("--full-page")
            call = _pool(ctx).invoke(ctx, session_id, args)
            path = str((Path(call.session_dir) / filename).resolve())
            result = _call_result(
                call,
                session_id=session_id,
                summary="浏览器截图已保存",
                kind="browser.screenshot",
                data={"path": path, "full_page": full_page},
            )
            if call.ok and Path(path).is_file():
                result.evidence.append({"kind": "screenshot", "session_id": session_id, "path": path})
                ctx.state.artifacts.setdefault("browser_screenshots", []).append(path)
            return result
        except BrowserError as exc:
            return _browser_error(exc, session_id=session_id)


class BrowserClose(Tool):
    spec = ToolSpec(
        name="browser_close",
        summary="关闭并回收当前任务的浏览器会话",
        category="browser",
        action_level="detect",
        external_bin="playwright-cli",
        params={"session_id": P("str", "浏览器会话句柄", required=True)},
    )

    def available(self) -> tuple[bool, str]:
        return _available()

    def run(self, ctx: Any, session_id: str) -> ToolResult:
        try:
            call = _pool(ctx).close_session(ctx, session_id)
            return _call_result(call, session_id=session_id, summary="浏览器会话已关闭", kind="browser.close")
        except BrowserError as exc:
            return _browser_error(exc, session_id=session_id)


class BrowserEvaluate(Tool):
    spec = ToolSpec(name="browser_evaluate", category="browser", action_level="exploit",
        summary="执行页面 JS 或 Playwright page 函数；完整输出保存为证据，仅 full 模式",
        params={"session_id": P("str", "会话句柄"), "code": P("str", "JS函数；playwright 模式为 async page => {...}"),
                "mode": P("str", "执行上下文", required=False, default="page", enum=["page", "playwright"])})

    def available(self):
        return _available()

    def run(self, ctx, session_id, code, mode="page"):
        try:
            if not code.strip() or len(code) > 64000:
                return ToolResult(ok=False, error="code 需要1..64000字符")
            call = _pool(ctx).invoke(ctx, session_id, ["run-code" if mode == "playwright" else "eval", code])
            return _call_result(call, session_id=session_id, summary="浏览器代码已执行", kind="browser.code",
                                data={"output": _preview(call.payload, 8000)})
        except BrowserError as exc:
            return _browser_error(exc, session_id=session_id)


class BrowserUpload(Tool):
    spec = ToolSpec(name="browser_upload", category="browser", action_level="verify",
        summary="向当前页面文件选择器上传工作区文件；先点击上传控件",
        params={"session_id": P("str", "会话句柄"), "paths": P("list", "要上传的现有文件绝对路径")})

    def available(self):
        return _available()

    def run(self, ctx, session_id, paths):
        try:
            from .workspace import _path
            if not 1 <= len(paths) <= 20 or not all(isinstance(p, str) for p in paths):
                return ToolResult(ok=False, error="paths 需要1..20条文件路径")
            files = [_path(ctx, p) for p in paths]
            if not all(p.is_file() for p in files):
                return ToolResult(ok=False, error="上传路径必须为已存在的普通文件")
            call = _pool(ctx).invoke(ctx, session_id, ["upload", *map(str, files)])
            return _call_result(call, session_id=session_id, summary="文件上传操作已执行", kind="browser.upload")
        except (ValueError, BrowserError) as exc:
            return ToolResult(ok=False, error=str(exc))


class BrowserRoute(Tool):
    spec = ToolSpec(name="browser_route", category="browser", action_level="verify",
        summary="按URL模式 mock/移除/查看响应；仅用于前端侦察，mock 结果不能证明后端权限绕过",
        params={"session_id": P("str", "会话句柄"),
            "action": P("str", "操作", enum=["set", "remove", "list"]),
            "pattern": P("str", "URL匹配模式", required=False, default=""),
            "body": P("str", "模拟响应正文", required=False, default="{}"),
            "status": P("int", "HTTP状态码", required=False, default=200),
            "content_type": P("str", "模拟响应类型", required=False, default="application/json")})

    def available(self):
        return _available()

    def required_level(self, params):
        return "detect" if params.get("action") == "list" else "verify"

    def run(self, ctx, session_id, action, pattern="", body="{}", status=200, content_type="application/json"):
        try:
            if action == "set" and (not pattern or not 100 <= status <= 599 or len(body) > 64000):
                return ToolResult(ok=False, error="set 需要 pattern、合法状态码和不超过64K的body")
            args = (["route", pattern, "--status", str(status), "--body", body, "--content-type", content_type] if action == "set"
                    else ["route-list"] if action == "list" else ["unroute", *([pattern] if pattern else [])])
            call = _pool(ctx).invoke(ctx, session_id, args)
            if call.ok:
                row = ctx.extra["browser_sessions"].sessions.get(session_id, {})
                mocks = row.setdefault("mock_patterns", set())
                if action == "set":
                    mocks.add(pattern)
                    row["mock_tainted"] = True
                elif action == "remove":
                    mocks.discard(pattern) if pattern else mocks.clear()
            return _call_result(call, session_id=session_id, summary="浏览器路由操作已完成（模拟数据须独立验证）",
                                kind="browser.mock", data={"output": _preview(call.payload), "synthetic": True})
        except BrowserError as exc:
            return _browser_error(exc, session_id=session_id)


class BrowserRecording(Tool):
    spec = ToolSpec(name="browser_recording", category="browser", action_level="detect",
        summary="开始或停止 Playwright trace、交互代码录制或视频；工件留在本会话目录",
        params={"session_id": P("str", "会话句柄"),
            "kind": P("str", "录制类型", enum=["trace", "actions", "video"]),
            "action": P("str", "操作", enum=["start", "stop"])})

    def available(self):
        return _available()

    def run(self, ctx, session_id, kind, action):
        try:
            prefix = {"trace": "tracing", "actions": "recording", "video": "video"}[kind]
            call = _pool(ctx).invoke(ctx, session_id, [prefix + "-" + action])
            return _call_result(call, session_id=session_id, summary=f"{kind} {action} 已执行", kind="browser.recording",
                                data={"directory": call.session_dir, "output": _preview(call.payload)})
        except BrowserError as exc:
            return _browser_error(exc, session_id=session_id)


TOOLS = [
    BrowserOpen(),
    BrowserSnapshot(),
    BrowserInteract(),
    BrowserStorage(),
    BrowserRequests(),
    BrowserScreenshot(),
    BrowserClose(),
    BrowserEvaluate(), BrowserUpload(), BrowserRoute(), BrowserRecording(),
]
