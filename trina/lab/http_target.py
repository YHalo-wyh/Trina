"""故意脆弱的 HTTP 靶机 —— 仅监听 127.0.0.1，供 Trina 端到端自证与评测使用。

设计约束（安全）:
* 只绑定回环地址，绝不对外；
* **不**提供删除/写文件/任意命令执行能力：`/rce` 只模拟白名单内只读命令的输出，
  真正的系统调用一个都不发（不读 /etc/shadow，不写盘，不 spawn 子进程）；
* 上传内容仅存内存字典，**不落盘、不执行**（即使扩展名是 .php）。

脆弱点（学习/评测用）:
  1. ``GET  /sqli?id=1``          错误型 / 布尔型 / 时间型 SQL 注入（MySQL 风格报错）
  2. ``GET  /lfi?file=...``       本地文件包含（可读 /etc/passwd；php://filter 变体返回 base64）
  3. ``GET  /rce?cmd=id``         命令注入（``;`` ``|`` ``$(...)`` 均可，仅白名单命令）
  4. ``POST /upload``             任意扩展名文件上传（仅存内存，返回可访问 URL）
  5. ``POST /login``              表单弱口令 admin/admin123
  6. ``GET  /flag``               返回 flag{trina_lab_<random>}
  7. ``GET  /admin``              缺失安全响应头的管理页
  8. ``GET  /robots.txt`` ``GET /.git/HEAD``  信息泄露样张
  9. 未命中路由一律软 404：HTTP 200 且正文长度固定 137 字节（干扰扫描器判断）。
"""

from __future__ import annotations

import base64
import json
import os
import random
import re
import shlex
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

#: 靶机自报的软件版本（信息泄露的一部分，便于指纹识别工具命中）。
SERVER_SOFTWARE = "Apache/2.4.41 (Ubuntu)"

#: 随机 404 的固定正文长度（软 404 混淆）。
SOFT_404_LENGTH = 137
_SOFT_404_BASE = (
    "<!DOCTYPE html><html><head><title>404 Not Found</title></head>"
    "<body><h1>Not Found</h1><p>The requested URL "
    "was not found on this server.</p><hr><address>Apache Server</address></body></html>"
)

_INDEX_HTML = """<!DOCTYPE html><html><head><title>Trina Lab</title></head><body>
<h1>Trina 脆弱靶机</h1>
<ul>
<li><a href="/sqli?id=1">/sqli?id=1</a></li>
<li><a href="/lfi?file=readme.txt">/lfi?file=readme.txt</a></li>
<li><a href="/rce?cmd=id">/rce?cmd=id</a></li>
<li><a href="/login">/login</a></li>
<li><a href="/admin">/admin</a></li>
</ul>
</body></html>
"""

_ADMIN_HTML = """<!DOCTYPE html><html><head><title>管理后台</title></head><body>
<h1>管理后台</h1>
<p>仅限管理员访问。</p>
<form action="/admin" method="post">
  <input name="action" value="reboot"><button type="submit">重启服务</button>
</form>
<!-- TODO: 移除调试接口 /rce -->
</body></html>
"""

_ROBOTS_TXT = """User-agent: *
Disallow: /admin
Disallow: /backup
Disallow: /.git/
Disallow: /uploads/
"""

_GIT_HEAD = "ref: refs/heads/main\n"
_GIT_CONFIG = "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n[remote \"origin\"]\n\turl = git@lab.local:trina/lab.git\n"

_README_TXT = "Trina 脆弱靶机使用说明：仅供授权实训与自动化评测使用。\n"

_LOGIN_FORM_HTML = """<!DOCTYPE html><html><head><title>登录</title></head><body>
<h1>登录</h1>
<form action="/login" method="post">
  <input name="username" placeholder="用户名"><input name="password" type="password" placeholder="密码">
  <button type="submit">登录</button>
</form>
</body></html>
"""

#: MySQL 风格报错（错误型注入的判据）。
_MYSQL_ERROR_TEMPLATE = (
    "Warning: mysqli_query(): (42000/1064): You have an error in your SQL syntax; "
    "check the manual that corresponds to your MySQL server version for the right syntax "
    "to use near '{near}' at line 1 in /var/www/html/sqli.php on line 14"
)

#: 白名单只读命令的「模拟输出」。靶机不真的执行任何系统命令。
_SAFE_OUTPUTS: dict[str, str] = {
    "id": "uid=33(www-data) gid=33(www-data) groups=33(www-data)\n",
    "whoami": "www-data\n",
    "pwd": "/var/www/html\n",
    "uname": "Linux trina-lab 6.6.87.2-microsoft-standard-WSL2 #1 SMP x86_64 GNU/Linux\n",
    "ls": "index.php\nlogin.php\nrobots.txt\nsqli.php\nuploads\n.git\n",
    "date": "Thu Oct 14 12:00:00 UTC 2026\n",
    "hostname": "trina-lab\n",
}


class LabState:
    """靶机的内存状态：flag、上传文件、认证失败计数、请求日志。"""

    def __init__(self, flag: str | None = None):
        self.flag = flag or f"flag{{trina_lab_{random.randbytes(4).hex()}}}"
        self.uploads: dict[str, bytes] = {}
        self.requests: list[dict[str, Any]] = []
        self.login_failures = 0
        self.login_successes = 0
        self.lock = threading.Lock()

    def record(self, entry: dict[str, Any]) -> None:
        with self.lock:
            self.requests.append(entry)
            if len(self.requests) > 2000:
                self.requests = self.requests[-1000:]


# ---------------------------------------------------------------------------
# 命令注入的「模拟 shell」（不真正执行任何命令）
# ---------------------------------------------------------------------------


def _run_single_command(state: LabState, segment: str) -> str:
    """解析单条命令，仅对白名单命令返回模拟输出。"""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        tokens = segment.split()
    if not tokens:
        return ""
    name = tokens[0].strip().lower()
    if name == "echo":
        return " ".join(tokens[1:]) + "\n"
    if name == "cat":
        # 只允许读 flag，绝不读 /etc/shadow 等敏感文件。
        target = tokens[1] if len(tokens) > 1 else ""
        if target in ("/flag", "flag", "./flag"):
            return state.flag + "\n"
        return f"cat: {target or '(空)'}: Permission denied\n"
    if name in _SAFE_OUTPUTS:
        return _SAFE_OUTPUTS[name]
    return f"sh: {tokens[0]}: command not found\n"


def run_command(state: LabState, cmd: str) -> str:
    """支持 ``;`` ``|`` 换行分隔与 ``$(...)`` 替换的「模拟」命令执行。"""
    out: list[str] = []
    for segment in re.split(r"[;|\n]", cmd or ""):
        segment = segment.strip()
        if not segment:
            continue
        # $(...) 命令替换（不支持嵌套）
        def _sub(match: re.Match[str]) -> str:
            return _run_single_command(state, match.group(1)).strip()

        replaced = re.sub(r"\$\(([^()]*)\)", _sub, segment)
        out.append(_run_single_command(state, replaced))
    return "".join(out)


# ---------------------------------------------------------------------------
# 各脆弱点的处理逻辑
# ---------------------------------------------------------------------------


def _sqli_row(user: str | None, uid: int | None, extra: str = "") -> str:
    body = ["<html><body><h2>用户查询结果</h2>"]
    if user is None:
        body.append("<p>未找到用户</p>")
    else:
        body.append(f"<p>用户名: {user}</p><p>ID: {uid}</p>")
        if extra:
            body.append(f"<p>DB版本: {extra}</p>")
    body.append("</body></html>\n")
    return "".join(body)


def handle_sqli(state: LabState, raw_id: str) -> tuple[int, str]:
    """错误型 + 布尔型 + 时间型注入。"""
    raw = (raw_id or "").strip()
    low = raw.lower()

    # 时间型：命中 sleep(N) 就真的睡（上限 3s，避免自伤 DoS）。
    match = re.search(r"sleep\s*\(\s*([0-9]+(?:\.[0-9]+)?)\s*\)", low)
    if match:
        try:
            delay = float(match.group(1))
        except ValueError:
            delay = 0.0
        time.sleep(min(max(delay, 0.0), 3.0))
        return 200, _sqli_row("admin", 1)

    # 正常查询
    if re.fullmatch(r"[0-9]+", raw):
        return (200, _sqli_row("admin", 1)) if int(raw) == 1 else (200, _sqli_row(None, None))

    # 布尔型：恒真 / 恒假
    if (
        re.search(r"(and|or)\s+1\s*=\s*1", low)
        or re.search(r"'\s*or\s*'1'\s*=\s*'1", low)
        or re.search(r'"\s*or\s*"1"\s*="1', low)
    ):
        return 200, _sqli_row("admin", 1)
    if re.search(r"(and|or)\s+1\s*=\s*2", low) or re.search(r"'\s*and\s*'1'\s*=\s*'2", low):
        return 200, _sqli_row(None, None)

    # 联合查询：泄露版本
    if "union" in low and "select" in low:
        return 200, _sqli_row("admin", 1, extra="5.7.44-log")

    # 其余（含落单引号）→ 错误型注入，返回 MySQL 风格报错
    near = raw[:24]
    return 500, _MYSQL_ERROR_TEMPLATE.format(near=near)


def handle_lfi(state: LabState, raw_file: str) -> tuple[int, str]:
    """本地文件包含：可读 /etc/passwd；php://filter 变体返回 base64。"""
    raw = (raw_file or "").strip().replace("\x00", "")
    if not raw:
        return 200, _INDEX_HTML

    if raw.startswith("php://filter"):
        if "convert.base64-encode" in raw and "etc/passwd" in raw:
            try:
                data = Path("/etc/passwd").read_bytes()
            except OSError:
                return 200, ""
            return 200, base64.b64encode(data).decode("ascii")
        return 200, ""

    decoded = urllib.parse.unquote(raw).replace("\x00", "")

    # 目录穿越 → /etc/passwd
    if "etc/passwd" in decoded or "etc/passwd" in os.path.normpath(decoded):
        try:
            return 200, Path("/etc/passwd").read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return 500, f"Warning: include(): failed to open stream: {exc}"

    if decoded.endswith("readme.txt"):
        return 200, _README_TXT

    # 其余路径一律当作包含失败（不读真实文件系统）
    return 200, (
        f"Warning: include({decoded}): failed to open stream: "
        "No such file or directory in /var/www/html/lfi.php on line 12\n"
    )


def _parse_multipart(content_type: str, body: bytes) -> list[tuple[str, str, bytes]]:
    """极简 multipart/form-data 解析，返回 (field_name, filename, data)。"""
    match = re.search(r'boundary=("?)([^";]+)\1', content_type or "")
    if not match:
        return []
    boundary = ("--" + match.group(2)).encode("latin-1")
    out: list[tuple[str, str, bytes]] = []
    for chunk in body.split(boundary):
        chunk = chunk.strip(b"\r\n")
        if not chunk or chunk == b"--" or b"\r\n\r\n" not in chunk:
            continue
        head, data = chunk.split(b"\r\n\r\n", 1)
        data = data.rstrip(b"\r\n")
        text = head.decode("utf-8", "replace")
        name_m = re.search(r'name="([^"]*)"', text)
        file_m = re.search(r'filename="([^"]*)"', text)
        out.append((name_m.group(1) if name_m else "", file_m.group(1) if file_m else "", data))
    return out


def _safe_name(name: str) -> str:
    """防目录穿越的落库名（仅内存字典的 key）。"""
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return base or f"upload_{random.randbytes(4).hex()}.bin"


def handle_upload(state: LabState, content_type: str, body: bytes, query: dict[str, list[str]]) -> tuple[int, str]:
    """任意扩展名上传：仅存内存，返回可访问 URL，绝不执行。"""
    files = _parse_multipart(content_type, body)
    stored: list[dict[str, Any]] = []
    with state.lock:
        for _field, filename, data in files:
            if not data and not filename:
                continue
            name = _safe_name(filename)
            if name in state.uploads:
                stem, dot, ext = name.partition(".")
                name = f"{stem}_{random.randbytes(2).hex()}{dot}{ext}"
            state.uploads[name] = data
            stored.append({"name": name, "size": len(data), "url": f"/uploads/{name}"})
        if not stored and body:
            # 非 multipart 兜底：用 ?name= 或 X-Filename 命名
            hint = (query.get("name") or query.get("filename") or [""])[0]
            name = _safe_name(hint)
            state.uploads[name] = body
            stored.append({"name": name, "size": len(body), "url": f"/uploads/{name}"})
    payload = {"ok": bool(stored), "uploaded": stored, "count": len(stored)}
    return (200 if stored else 400), json.dumps(payload, ensure_ascii=False)


def handle_login(state: LabState, username: str, password: str) -> tuple[int, str]:
    if username == "admin" and password == "admin123":
        with state.lock:
            state.login_successes += 1
        return 200, "欢迎 admin，登录成功！\n"
    with state.lock:
        state.login_failures += 1
    return 200, "密码错误\n"


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class LabHTTPHandler(BaseHTTPRequestHandler):
    """单文件路由：全部脆弱点集中在 ``_route`` 里。"""

    protocol_version = "HTTP/1.1"
    server_version = SERVER_SOFTWARE
    sys_version = ""

    # ---------------------------------------------------------------- 基础
    def version_string(self) -> str:  # noqa: D102 - 覆盖为固定指纹
        return SERVER_SOFTWARE

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D102 - 静默 + 记内存
        state: LabState = getattr(self.server, "state", None)
        if state is not None:
            state.record(
                {
                    "ts": round(time.time(), 3),
                    "client": self.client_address[0],
                    "request": self.requestline,
                    "message": fmt % args,
                }
            )

    def _send(self, status: int, body: str | bytes, content_type: str = "text/html; charset=utf-8", headers: dict[str, str] | None = None) -> None:
        data = body if isinstance(body, bytes) else body.encode("utf-8", "replace")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _soft_404(self) -> None:
        """软 404：200 + 固定 137 字节正文。"""
        body = (_SOFT_404_BASE + " " * SOFT_404_LENGTH)[:SOFT_404_LENGTH]
        self._send(200, body, "text/html; charset=utf-8")

    # ---------------------------------------------------------------- 路由
    def do_GET(self) -> None:  # noqa: N802
        self._route()

    def do_HEAD(self) -> None:  # noqa: N802
        self._route()

    def do_POST(self) -> None:  # noqa: N802
        self._route()

    def _route(self) -> None:
        state: LabState = self.server.state
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path or "/"
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

        try:
            if path == "/" and self.command in ("GET", "HEAD"):
                self._send(200, _INDEX_HTML)
            elif path == "/sqli" and self.command in ("GET", "HEAD"):
                status, body = handle_sqli(state, (query.get("id") or [""])[0])
                self._send(status, body)
            elif path == "/lfi" and self.command in ("GET", "HEAD"):
                status, body = handle_lfi(state, (query.get("file") or [""])[0])
                self._send(status, body)
            elif path == "/rce" and self.command in ("GET", "HEAD"):
                output = run_command(state, (query.get("cmd") or [""])[0])
                self._send(200, output, "text/plain; charset=utf-8")
            elif path == "/flag" and self.command in ("GET", "HEAD"):
                self._send(200, state.flag + "\n", "text/plain; charset=utf-8")
            elif path == "/admin" and self.command in ("GET", "HEAD"):
                # 故意缺失 X-Frame-Options / CSP / X-Content-Type-Options
                self._send(200, _ADMIN_HTML)
            elif path == "/robots.txt" and self.command in ("GET", "HEAD"):
                self._send(200, _ROBOTS_TXT, "text/plain; charset=utf-8")
            elif path == "/.git/HEAD" and self.command in ("GET", "HEAD"):
                self._send(200, _GIT_HEAD, "text/plain; charset=utf-8")
            elif path == "/.git/config" and self.command in ("GET", "HEAD"):
                self._send(200, _GIT_CONFIG, "text/plain; charset=utf-8")
            elif path == "/login" and self.command in ("GET", "HEAD"):
                self._send(200, _LOGIN_FORM_HTML)
            elif path == "/login" and self.command == "POST":
                form = self._read_form()
                status, body = handle_login(state, form.get("username", [""])[0], form.get("password", [""])[0])
                self._send(status, body, "text/plain; charset=utf-8")
            elif path == "/upload" and self.command == "POST":
                body = self._read_body()
                status, text = handle_upload(state, self.headers.get("Content-Type", ""), body, query)
                self._send(status, text, "application/json; charset=utf-8")
            elif path.startswith("/uploads/") and self.command in ("GET", "HEAD"):
                name = os.path.basename(urllib.parse.unquote(path[len("/uploads/") :]))
                with state.lock:
                    data = state.uploads.get(name)
                if data is None:
                    self._soft_404()
                else:
                    self._send(
                        200,
                        data,
                        "application/octet-stream",
                        {"Content-Disposition": f'attachment; filename="{name}"'},
                    )
            else:
                self._soft_404()
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - 靶机永不因单请求崩溃
            try:
                self._send(500, f"Internal Server Error: {exc}", "text/plain; charset=utf-8")
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------------- 解析
    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        return self.rfile.read(length) if length > 0 else b""

    def _read_form(self) -> dict[str, list[str]]:
        body = self._read_body()
        ctype = self.headers.get("Content-Type", "")
        if "application/x-www-form-urlencoded" in ctype or (body and b"=" in body):
            return urllib.parse.parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
        if "multipart/form-data" in ctype:
            merged: dict[str, list[str]] = {}
            for field, _filename, data in _parse_multipart(ctype, body):
                merged.setdefault(field, []).append(data.decode("utf-8", "replace"))
            return merged
        # 兜底：也允许 query string 传参
        parsed = urllib.parse.urlparse(self.path)
        return urllib.parse.parse_qs(parsed.query, keep_blank_values=True)


class LabHTTPServer(ThreadingHTTPServer):
    """带状态与优雅关闭的多线程 HTTP 服务。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], state: LabState):
        self.state = state
        super().__init__(addr, LabHTTPHandler)


def create_server(port: int = 9099, host: str = "127.0.0.1", flag: str | None = None):
    """启动靶机，返回 ``(server, thread)``；调用 ``server.shutdown()`` 停止。"""
    state = LabState(flag=flag)
    server = LabHTTPServer((host, int(port)), state)
    thread = threading.Thread(target=server.serve_forever, name=f"trina-lab-http-{port}", daemon=True)
    thread.start()
    return server, thread


__all__ = [
    "LabState",
    "LabHTTPServer",
    "LabHTTPHandler",
    "SOFT_404_LENGTH",
    "SERVER_SOFTWARE",
    "create_server",
    "handle_lfi",
    "handle_login",
    "handle_sqli",
    "handle_upload",
    "run_command",
]
