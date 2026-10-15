"""凭据类工具：弱口令验证 / 表单登录验证 / flag 搜刮。

设计约束
--------
* HTTP 只能走 ``ctx.http.request()``（护栏 + 限速 + 证据），绝不直接 ``requests.*``。
* 爆破前必须过 ``ctx.guard.check_brute_scale()``；执行中每 5 次尝试 ``ctx.throttle()`` 一次。
* 缺依赖（paramiko / pymysql）时返回明确的失败结果，**绝不抛异常**。
* 「未发现弱口令 / 未发现 flag」不是错误：``ok=True``。
* 所有动作都是只读或无害验证，不生成任何破坏性载荷。
"""

from __future__ import annotations

import base64
import ftplib
import glob
import importlib.util
import itertools
import os
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urljoin

from ..scope import parse_target
from .http import normalize_url
from .registry import (
    ParamSpec,
    Tool,
    ToolContext,
    ToolResult,
    ToolSpec,
    denied_result,
    register,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SERVICES = ("ssh", "ftp", "mysql", "redis", "http_basic", "http_form")

DEFAULT_PORTS: dict[str, int] = {
    "ssh": 22,
    "ftp": 21,
    "mysql": 3306,
    "redis": 6379,
    "http_basic": 80,
    "http_form": 80,
}

#: 每 N 次尝试限速一次（令牌来自 ctx.guard/ctx.throttle）
THROTTLE_EVERY = 5
#: 最多报告的凭据组数
MAX_REPORTED_CREDS = 3
#: 显式给出的凭据组合数不超过该值视为「单凭据验证」（verify 级），超过视为爆破（exploit 级）
SINGLE_CREDENTIAL_LIMIT = 3
#: 单次连接超时（秒）
CONNECT_TIMEOUT = 5.0
#: flag 提取正则（与任务书一致）
FLAG_RE = re.compile(r"(?i)flag\{[^}\n]{1,120}\}|[A-Za-z0-9_]{8,40}\{[^}\n]{4,80}\}")

#: 缺依赖时使用的兜底小字典（词表文件不存在时）
_FALLBACK_USERS = ("admin", "root", "test", "guest")
_FALLBACK_PASSWORDS = ("123456", "admin", "password", "root", "12345678", "123456789", "guest")

#: 内置 flag 路径清单（本地文件系统 + Web 路径共用；仅读不写）
FLAG_PATHS: tuple[str, ...] = (
    "/flag",
    "/flag.txt",
    "/flag.php",
    "/flag.html",
    "/root/flag.txt",
    "/tmp/flag*",
    "/var/www/html/flag.php",
    "/var/www/html/flag.txt",
    "/home/*/flag.txt",
    "/opt/flag.txt",
    "/app/flag.txt",
    "/srv/flag.txt",
    "/data/flag.txt",
    "/flag/flag.txt",
    r"C:\flag.txt",
    r"C:\flag",
    r"C:\Users\Public\flag.txt",
)

#: 单文件读取上限（防止把大文件整段读进来）
_MAX_READ_BYTES = 2_000_000

_FAIL_HINTS = (
    "密码错误",
    "密码不正确",
    "用户名或密码",
    "登录失败",
    "incorrect",
    "invalid",
    "wrong",
    "failed",
    "failure",
    "denied",
    "错误",
    "失败",
)
_OK_HINTS = (
    "欢迎",
    "welcome",
    "dashboard",
    "退出",
    "logout",
    "注销",
    "登录成功",
    "控制台",
    "后台",
)


# ---------------------------------------------------------------------------
# 内部数据结构
# ---------------------------------------------------------------------------


@dataclass
class _Outcome:
    """单次凭据尝试的结果。"""

    ok: bool = False
    detail: str = ""
    evidence: dict[str, Any] | None = None
    #: 该次成功同时意味着「未授权访问」类漏洞（例：redis 无密码）
    unauthorized: bool = False
    #: 本次尝试因连接/服务错误而未能得出结论（区别于「认证被拒」）
    failed: bool = False


@dataclass
class _Endpoint:
    """归一化后的目标端点。"""

    service: str
    target: str
    host: str
    port: int
    base_url: str = ""
    form_path: str = ""
    username_field: str = "username"
    password_field: str = "password"
    success_keyword: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"service": self.service, "host": self.host, "port": self.port, "target": self.target}


# ---------------------------------------------------------------------------
# 字典 / 依赖
# ---------------------------------------------------------------------------


def _have_module(name: str) -> bool:
    """判断可选依赖是否可用（不导入，避免副作用）。"""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _read_wordlist(path: Path, fallback: Iterable[str]) -> list[str]:
    """读词表；文件不存在/为空时退回内置字典。"""
    try:
        if path.is_file():
            lines = [
                line.strip()
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            ]
            values = [line for line in lines if line and not line.startswith("#")]
            if values:
                return values
    except OSError:
        pass
    return list(fallback)


def _clean_list(values: Any) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    return [str(v) for v in values if str(v) != ""]


def _load_candidates(
    ctx: ToolContext,
    users: list[str] | None,
    passwords: list[str] | None,
    username: str | None,
    password: str | None,
) -> tuple[list[str], list[str]]:
    """组装 (用户名列表, 口令列表)；支持单用户 / 单口令验证模式。"""
    if username:
        user_list = [str(username)]
    else:
        user_list = _clean_list(users) or _read_wordlist(
            ctx.config.tools.wordlist_path("users"), _FALLBACK_USERS
        )

    if password is not None and password != "":
        pwd_list = [str(password)]
    elif password == "":
        pwd_list = [""]
    else:
        pwd_list = _clean_list(passwords) or _read_wordlist(
            ctx.config.tools.wordlist_path("passwords"), _FALLBACK_PASSWORDS
        )
    return user_list, pwd_list


def _build_endpoint(
    service: str,
    target: str,
    port: int | None,
    form_path: str = "",
    username_field: str = "username",
    password_field: str = "password",
    success_keyword: str | None = None,
) -> _Endpoint:
    """把 target / port 归一化成 _Endpoint。"""
    info = parse_target(target)
    host = info.host
    if port:
        resolved_port = int(port)
    elif info.port:
        resolved_port = int(info.port)
    else:
        resolved_port = DEFAULT_PORTS.get(service, 80)

    if info.scheme:
        base_url = target
    elif service in ("http_basic", "http_form"):
        base_url = f"http://{host}:{resolved_port}/"
    else:
        base_url = ""

    hostport = f"{host}:{resolved_port}"
    return _Endpoint(
        service=service,
        target=hostport,
        host=host,
        port=resolved_port,
        base_url=base_url,
        form_path=form_path or "",
        username_field=username_field or "username",
        password_field=password_field or "password",
        success_keyword=success_keyword or None,
    )


# ---------------------------------------------------------------------------
# 分服务探测实现
# ---------------------------------------------------------------------------


def _attempt_ssh(ctx: ToolContext, ep: _Endpoint, user: str, password: str) -> _Outcome:
    import paramiko  # 依赖可用性已在上层预检

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=ep.host,
            port=int(ep.port),
            username=user,
            password=password,
            timeout=CONNECT_TIMEOUT,
            allow_agent=False,
            look_for_keys=False,
        )
        return _Outcome(True, f"SSH 认证成功（{ep.host}:{ep.port} 用户 {user}）")
    except paramiko.AuthenticationException as exc:
        return _Outcome(False, f"SSH 认证失败: {exc}")
    except Exception as exc:  # noqa: BLE001 - 连接类异常一律降级为「本次失败」
        return _Outcome(False, f"SSH 连接错误: {type(exc).__name__}: {exc}", failed=True)
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def _attempt_ftp(ctx: ToolContext, ep: _Endpoint, user: str, password: str) -> _Outcome:
    ftp = ftplib.FTP()
    try:
        welcome = ftp.connect(ep.host, int(ep.port), timeout=CONNECT_TIMEOUT)
        ftp.login(user, password)
        return _Outcome(True, f"FTP 登录成功: {str(welcome or ftp.getwelcome())[:80]}")
    except ftplib.error_perm as exc:
        return _Outcome(False, f"FTP 认证被拒: {exc}")
    except Exception as exc:  # noqa: BLE001
        return _Outcome(False, f"FTP 连接错误: {type(exc).__name__}: {exc}", failed=True)
    finally:
        try:
            ftp.close()
        except Exception:  # noqa: BLE001
            pass


def _attempt_mysql(ctx: ToolContext, ep: _Endpoint, user: str, password: str) -> _Outcome:
    import pymysql  # 依赖可用性已在上层预检

    try:
        conn = pymysql.connect(
            host=ep.host,
            port=int(ep.port),
            user=user,
            password=password,
            connect_timeout=int(CONNECT_TIMEOUT),
        )
    except pymysql.err.OperationalError as exc:
        code = exc.args[0] if exc.args else 0
        if code in (1045, 1698):  # ER_ACCESS_DENIED_ERROR / ER_ACCESS_DENIED_NO_PASSWORD_ERROR
            return _Outcome(False, f"MySQL 认证失败: {exc}")
        return _Outcome(False, f"MySQL 连接错误: {exc}", failed=True)
    except Exception as exc:  # noqa: BLE001
        return _Outcome(False, f"MySQL 错误: {type(exc).__name__}: {exc}", failed=True)
    try:
        try:
            version = conn.get_server_info()
        except Exception:  # noqa: BLE001
            version = "?"
        return _Outcome(True, f"MySQL 登录成功（版本 {version}）")
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _recv_line(sock: socket.socket, limit: int = 8192) -> str:
    buf = b""
    while len(buf) < limit:
        try:
            chunk = sock.recv(1)
        except (socket.timeout, OSError):
            break
        if not chunk:
            break
        buf += chunk
        if buf.endswith(b"\r\n"):
            break
    return buf.decode("utf-8", "replace").strip()


def _attempt_redis(ctx: ToolContext, ep: _Endpoint, user: str, password: str) -> _Outcome:
    """Redis：AUTH + PING 判定；空口令直连 PING 成功即未授权访问。"""
    with socket.create_connection((ep.host, int(ep.port)), timeout=CONNECT_TIMEOUT) as sock:
        sock.settimeout(CONNECT_TIMEOUT)
        if password:
            sock.sendall(f"AUTH {password}\r\n".encode("utf-8"))
            reply = _recv_line(sock)
            if reply.startswith("-"):
                if "no password is set" in reply.lower():
                    return _Outcome(
                        True,
                        "Redis 未设置密码（AUTH 被忽略），服务可未授权直接访问",
                        unauthorized=True,
                    )
                return _Outcome(False, f"Redis AUTH 被拒: {reply}")
        sock.sendall(b"PING\r\n")
        reply = _recv_line(sock)
    if reply.upper().startswith("+PONG"):
        if password:
            return _Outcome(True, f"Redis AUTH+{password!r} 后 PING 成功")
        return _Outcome(True, "Redis 无密码，PING 直接返回 +PONG（未授权访问）", unauthorized=True)
    return _Outcome(False, f"Redis 响应异常: {reply or '(空)'}", failed=True)


def _base_evidence(ep: _Endpoint, user: str, password: str, detail: str) -> dict[str, Any]:
    return {
        "kind": f"{ep.service}_credential",
        "target": f"{ep.host}:{ep.port}",
        "service": ep.service,
        "request": {
            "service": ep.service,
            "host": ep.host,
            "port": ep.port,
            "username": user,
            "password": password,
        },
        "response": {"detail": detail},
        "note": f"{ep.service} 弱口令命中 {user}:{password}",
    }


def _attempt_http_basic(ctx: ToolContext, ep: _Endpoint, user: str, password: str) -> _Outcome:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    res = ctx.http.request(
        "GET",
        ep.base_url,
        headers={"Authorization": f"Basic {token}"},
        required_level=ctx.effective_level,
        payload_label=f"basic:{user}:<redacted>",
    )
    evidence = res.as_evidence(kind="http_basic", note="")
    if not res.ok:
        evidence["note"] = "请求失败"
        return _Outcome(False, f"请求失败: {res.error or res.status}", evidence, failed=True)
    lower_headers = {k.lower(): v for k, v in (res.headers or {}).items()}
    ok = res.status in (200, 204, 301, 302, 303, 307, 308) and "www-authenticate" not in lower_headers
    detail = (
        f"HTTP {res.status}，响应无 WWW-Authenticate，Basic 认证通过"
        if ok
        else f"HTTP {res.status}，仍要求认证"
    )
    evidence["note"] = "Basic 认证通过" if ok else "Basic 认证失败"
    return _Outcome(ok, detail, evidence)


def _parse_form(html: str, base_url: str, password_field: str) -> dict[str, Any] | None:
    """用 bs4 找到含密码字段的表单，返回 {action, method, fields}（含隐藏字段/CSRF token）。"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    fallback: dict[str, Any] | None = None
    for form in soup.find_all("form"):
        action = form.get("action") or ""
        method = (form.get("method") or "post").strip().lower()
        fields: dict[str, str] = {}
        has_password_type = False
        for tag in form.find_all(["input", "select", "textarea"]):
            name = tag.get("name")
            if not name:
                continue
            if tag.name == "select":
                option = tag.find("option")
                if option is None:
                    fields[name] = ""
                else:
                    value = option.get("value")
                    fields[name] = str(value if value is not None else option.text or "")
            elif tag.name == "textarea":
                fields[name] = tag.text or ""
            else:
                input_type = (tag.get("type") or "text").strip().lower()
                if input_type == "password":
                    has_password_type = True
                if input_type in ("submit", "button", "image", "file", "reset"):
                    continue
                fields[name] = str(tag.get("value") or "")
        entry = {
            "action": urljoin(base_url, action) if action else base_url,
            "method": method,
            "fields": fields,
        }
        if password_field in fields or has_password_type:
            return entry
        if fallback is None and fields:
            fallback = entry
    return fallback


def _judge_login(body: str, success_keyword: str | None) -> tuple[bool, str]:
    """判定一次登录是否成功。给了 success_keyword 就用它，否则按关键词自动判定。"""
    text = body or ""
    lowered = text.lower()
    if success_keyword:
        hit = success_keyword in text or success_keyword.lower() in lowered
        if hit:
            return True, f"命中 success_keyword={success_keyword!r}"
        return False, f"未命中 success_keyword={success_keyword!r}"
    for hint in _FAIL_HINTS:
        if hint in lowered:
            return False, f"响应包含失败关键词 {hint!r}"
    for hint in _OK_HINTS:
        if hint in lowered:
            return True, f"响应包含成功关键词 {hint!r}"
    return False, "响应既无失败也无成功关键词，判定为未通过"


def _attempt_http_form(ctx: ToolContext, ep: _Endpoint, user: str, password: str) -> _Outcome:
    url = normalize_url(ep.base_url, ep.form_path)
    page = ctx.http.request("GET", url, required_level=ctx.effective_level, payload_label="form:discover")
    if not page.ok:
        return _Outcome(
            False,
            f"表单页获取失败: {page.error or page.status}",
            page.as_evidence(kind="http_form", note="表单发现失败"),
            failed=True,
        )
    form = _parse_form(page.body, url, ep.password_field)
    if form is None:
        return _Outcome(
            False,
            "页面中未发现可提交的表单",
            page.as_evidence(kind="http_form", note="未发现表单"),
            failed=True,
        )
    payload = dict(form["fields"])  # 隐藏字段 / CSRF token 原样回填
    payload[ep.username_field] = user
    payload[ep.password_field] = password
    label = f"form:{user}&{ep.password_field}=<redacted>"
    if form["method"] == "get":
        res = ctx.http.request(
            "GET", form["action"], params=payload, required_level=ctx.effective_level, payload_label=label
        )
    else:
        res = ctx.http.request(
            "POST", form["action"], data=payload, required_level=ctx.effective_level, payload_label=label
        )
    if not res.ok:
        return _Outcome(
            False,
            f"表单提交失败: {res.error or res.status}",
            res.as_evidence(kind="http_form", note="提交失败"),
            failed=True,
        )
    ok, why = _judge_login(res.body, ep.success_keyword)
    return _Outcome(
        ok,
        f"HTTP {res.status}；{why}",
        res.as_evidence(kind="http_form", note=f"{user} 登录{'成功' if ok else '失败'}：{why}"),
    )


_ATTEMPTS: dict[str, Callable[[ToolContext, _Endpoint, str, str], _Outcome]] = {
    "ssh": _attempt_ssh,
    "ftp": _attempt_ftp,
    "mysql": _attempt_mysql,
    "redis": _attempt_redis,
    "http_basic": _attempt_http_basic,
    "http_form": _attempt_http_form,
}


def _dependency_error(service: str) -> str:
    """返回缺依赖的错误说明（空串表示依赖齐备）。"""
    if service == "ssh" and not _have_module("paramiko"):
        return "未安装 paramiko，跳过 ssh 弱口令验证"
    if service == "mysql" and not _have_module("pymysql"):
        return "未安装 pymysql，跳过 mysql 弱口令验证"
    if service == "http_form" and not _have_module("bs4"):
        return "未安装 bs4，无法解析表单，跳过 http_form 弱口令验证"
    return ""


# ---------------------------------------------------------------------------
# 工具 1：weak_password_brute
# ---------------------------------------------------------------------------


class WeakPasswordBrute(Tool):
    """小规模弱口令验证（受规模护栏 + 限速约束）。"""

    spec = ToolSpec(
        name="weak_password_brute",
        summary="对 ssh/ftp/mysql/redis/http_basic/http_form 做小规模弱口令验证，命中即产出凭据与漏洞",
        category="exploit",
        action_level="exploit",
        params={
            "service": ParamSpec(
                "str", "服务类型", required=True, enum=list(SERVICES), example="ssh"
            ),
            "target": ParamSpec("str", "目标 host / host:port / URL", required=True, example="127.0.0.1"),
            "port": ParamSpec("int", "端口（默认按服务：ssh22/ftp21/mysql3306/redis6379/http80）", required=False, default=None),
            "users": ParamSpec("list", "用户名列表（默认读 users 词表）", required=False, default=None),
            "passwords": ParamSpec("list", "口令列表（默认读 passwords 词表）", required=False, default=None),
            "username": ParamSpec("str", "单用户模式：只试这一个用户名", required=False, default=None),
            "password": ParamSpec("str", "单口令验证模式：只试这一个口令", required=False, default=None),
            "form_path": ParamSpec("str", "http_form 必填：登录表单页路径", required=False, default=None),
            "username_field": ParamSpec("str", "表单用户名字段名", required=False, default="username"),
            "password_field": ParamSpec("str", "表单口令字段名", required=False, default="password"),
            "success_keyword": ParamSpec("str", "登录成功关键词（缺省自动判定）", required=False, default=None),
            "threads": ParamSpec("int", "单服务并发数", required=False, default=2),
        },
        produces=["credentials", "findings"],
        examples=[
            '{"service": "ssh", "target": "127.0.0.1", "username": "root", "passwords": ["123456", "toor"]}',
            '{"service": "http_form", "target": "http://127.0.0.1:8080", "form_path": "/login", "users": ["admin"], "passwords": ["admin"]}',
        ],
        notes="尝试次数 = 用户数 × 口令数，超过 scope.max_brute_attempts 会被护栏拒绝；最多报告 3 组凭据。",
    )

    # ------------------------------------------------------------------ 入口
    def required_level(self, params: dict[str, Any]) -> str:
        """「少量显式凭据验证」属 verify；字典爆破属 exploit。

        判据是**尝试规模**而不是参数写法：`username+password`、`username+[pwd]`、
        `[user]+password` 这些等价写法都应放行，只有真正的大字典才需要 exploit 等级。
        """
        users = params.get("users")
        if not users and params.get("username"):
            users = [params["username"]]
        passwords = params.get("passwords")
        if not passwords and params.get("password"):
            passwords = [params["password"]]
        users = users if isinstance(users, list) else ([users] if users else [])
        passwords = passwords if isinstance(passwords, list) else ([passwords] if passwords else [])
        attempts = len(users) * len(passwords)
        if 0 < attempts <= SINGLE_CREDENTIAL_LIMIT:
            return "verify"
        return self.spec.action_level

    def level_hint(self, params: dict[str, Any]) -> str:
        """字典爆破被等级拒绝时，告诉模型同一目标可以用哪条 verify 级路径。"""
        if self.required_level(params) != self.spec.action_level:
            return ""
        return (
            "可改走 verify 级等价路径：weak_password_brute 只传显式凭据（username+password，或 users/passwords 各 ≤3 项）为单凭据验证；"
            "已知账号口令时用 http_form_login（HTTP 表单）也能直接验证"
        )

    def run(
        self,
        ctx: ToolContext,
        service: str,
        target: str,
        port: int | None = None,
        users: list[str] | None = None,
        passwords: list[str] | None = None,
        username: str | None = None,
        password: str | None = None,
        form_path: str | None = None,
        username_field: str = "username",
        password_field: str = "password",
        success_keyword: str | None = None,
        threads: int = 2,
    ) -> ToolResult:
        started = time.monotonic()
        service = str(service or "").strip().lower()
        if service not in SERVICES:
            return ToolResult(
                ok=False,
                error=f"不支持的服务 {service!r}",
                summary=f"不支持的服务 {service!r}，仅支持 {', '.join(SERVICES)}",
                data={"supported": list(SERVICES)},
                duration=time.monotonic() - started,
            )

        dep_error = _dependency_error(service)
        if dep_error:
            return ToolResult(
                ok=False,
                error=dep_error,
                summary=dep_error,
                data={"service": service, "dependency_missing": True},
                next_hints=["安装缺失依赖后重试，或改用其他服务/工具"],
                duration=time.monotonic() - started,
            )

        if service == "http_form" and not form_path:
            return ToolResult(
                ok=False,
                error="http_form 需要 form_path 参数",
                summary="http_form 缺少 form_path，无法定位登录表单",
                data={"required": ["form_path"]},
                duration=time.monotonic() - started,
            )

        try:
            ep = _build_endpoint(
                service,
                target,
                port,
                form_path=form_path or "",
                username_field=username_field,
                password_field=password_field,
                success_keyword=success_keyword,
            )
        except ValueError as exc:
            return ToolResult(
                ok=False,
                error=str(exc),
                summary=f"目标无法解析: {exc}",
                duration=time.monotonic() - started,
            )

        user_list, pwd_list = _load_candidates(ctx, users, passwords, username, password)
        attempts = len(user_list) * len(pwd_list)
        if attempts == 0:
            return ToolResult(
                ok=False,
                error="用户字典或口令字典为空",
                summary="用户字典或口令字典为空，未执行任何尝试",
                duration=time.monotonic() - started,
            )

        # 规模护栏：被拒则直接冒泡为 denied 结果
        decision = ctx.guard.check_brute_scale(attempts, target=ep.target)
        if decision.denied:
            result = denied_result(decision, tool=self.spec.name)
            result.data.update({"attempts": attempts, "service": service, "target": ep.target})
            result.duration = time.monotonic() - started
            return result

        workers = max(1, min(int(threads or 1), int(ctx.config.scope.max_host_concurrency), 8))
        attempted, hits, failures = self._execute(ctx, ep, user_list, pwd_list, workers)
        error_note = f"（其中 {len(failures)} 次因连接/服务错误未得出结论）" if failures else ""

        if not hits:
            return ToolResult(
                ok=True,
                summary=f"已尝试 {attempted} 次组合，未发现弱口令{error_note}",
                data={
                    "service": service,
                    "target": ep.target,
                    "attempts": attempted,
                    "users_tried": len(user_list),
                    "passwords_tried": len(pwd_list),
                    "credentials": [],
                    "errors": failures[:5],
                },
                next_hints=(
                    ["目标服务不可达或端口错误，先确认 host/port 与服务版本"]
                    if failures and not attempted - len(failures)
                    else ["换用更贴合目标的字典，或转而去枚举服务版本与已知 CVE"]
                ),
                duration=time.monotonic() - started,
            )

        return self._hit_result(ctx, ep, hits, attempted, started, failures)

    # ------------------------------------------------------------------ 执行
    def _execute(
        self,
        ctx: ToolContext,
        ep: _Endpoint,
        user_list: list[str],
        pwd_list: list[str],
        workers: int,
    ) -> tuple[int, list[tuple[str, str, _Outcome]], list[str]]:
        """按用户并发执行尝试；同一用户命中后不再尝试其余口令，最多收集 3 组。"""
        pairs = [(user, pwd) for user in user_list for pwd in pwd_list]
        counter = itertools.count()
        lock = threading.Lock()
        cracked: set[str] = set()
        hits: list[tuple[str, str, _Outcome]] = []
        failures: list[str] = []
        attempted = 0

        def worker(pair: tuple[str, str]) -> tuple[str, str, _Outcome] | None:
            user, pwd = pair
            with lock:
                if user in cracked or len(hits) >= MAX_REPORTED_CREDS:
                    return None
            if next(counter) % THROTTLE_EVERY == 0:
                ctx.throttle(ep.host)  # 每 5 次尝试限速一次
            try:
                outcome = _ATTEMPTS[ep.service](ctx, ep, user, pwd)
            except Exception as exc:  # noqa: BLE001 - 任何探测异常都只影响本次尝试
                outcome = _Outcome(False, f"{type(exc).__name__}: {exc}", failed=True)
            return user, pwd, outcome

        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="trina-brute")
        try:
            futures = {pool.submit(worker, pair): pair for pair in pairs}
            for future in as_completed(futures):
                result = future.result()
                if result is None:
                    continue
                attempted += 1
                user, pwd, outcome = result
                if not outcome.ok:
                    if outcome.failed:
                        with lock:
                            if len(failures) < 50:
                                failures.append(outcome.detail)
                    continue
                with lock:
                    if user not in cracked:
                        cracked.add(user)
                    hits.append((user, pwd, outcome))
                    if len(hits) >= MAX_REPORTED_CREDS:
                        for pending in futures:
                            if pending is not future:
                                pending.cancel()
        finally:
            pool.shutdown(wait=False)
        return attempted, hits, failures

    # ------------------------------------------------------------------ 结果
    def _hit_result(
        self,
        ctx: ToolContext,
        ep: _Endpoint,
        hits: list[tuple[str, str, _Outcome]],
        attempted: int,
        started: float,
        failures: list[str] | None = None,
    ) -> ToolResult:
        credentials: list[dict[str, Any]] = []
        findings: list[dict[str, Any]] = []
        evidence: list[dict[str, Any]] = []
        for user, pwd, outcome in hits:
            ev = outcome.evidence or _base_evidence(ep, user, pwd, outcome.detail)
            ev.setdefault("detail", outcome.detail)
            evidence.append(ev)
            credentials.append(
                {
                    "target": ep.target,
                    "service": ep.service,
                    "username": user,
                    "password": pwd,
                    "verified": True,
                    "source": f"weak_password_brute/{ep.service}",
                }
            )
            findings.append(
                {
                    "title": f"{ep.service} 弱口令：{user}",
                    "vuln_class": "weak_password",
                    "target": ep.target,
                    # 弱口令的位置就是接受凭据的那个端点（HTTP 表单要带上表单路径）
                    "url": (
                        normalize_url(ep.base_url, ep.form_path)
                        if ep.service == "http_form" and ep.form_path
                        else (ep.base_url or ep.target)
                    ),
                    # 弱口令漏洞的定位点就是口令字段（HTTP 表单用真实字段名，其它服务用 password）
                    "parameter": ep.password_field if ep.service == "http_form" else "password",
                    "method": "POST" if ep.service == "http_form" else "",
                    "severity": "high",
                    "confidence": "high",
                    "status": "verified",
                    "tool": self.spec.name,
                    "payload": f"{user}:{pwd}",
                    "description": (
                        f"{ep.service} 服务在 {ep.target} 上使用弱口令/默认口令，"
                        f"攻击者可凭 {user} 直接登录。"
                    ),
                    "impact": "攻击者可获得服务的合法访问权限，进而读取或篡改数据、横向移动。",
                    "remediation": "禁用弱口令与默认口令，启用登录失败锁定与多因素认证，限制来源 IP。",
                    "cwe": "CWE-521",
                    "evidence": [ev],
                }
            )
            if outcome.unauthorized:
                findings.append(
                    {
                        "title": f"{ep.service} 未授权访问 @ {ep.target}",
                        "vuln_class": "unauthorized_access",
                        "target": ep.target,
                        "severity": "high",
                        "confidence": "high",
                        "status": "verified",
                        "tool": self.spec.name,
                        "description": f"{ep.service} 未启用认证（无口令即可访问）：{outcome.detail}",
                        "impact": "任何可达该端口的攻击者都能读写服务数据。",
                        "remediation": "启用认证并设置强口令，绑定管理接口到内网并加访问控制。",
                        "cwe": "CWE-306",
                        "evidence": [ev],
                    }
                )

        creds_text = ", ".join(f"{u}:{p}" for u, p, _ in hits)
        return ToolResult(
            ok=True,
            summary=(
                f"发现 {len(hits)} 组弱口令（{ep.service}@{ep.target}）：{creds_text}；"
                f"共尝试 {attempted} 次"
            ),
            data={
                "service": ep.service,
                "target": ep.target,
                "attempts": attempted,
                "credentials": credentials,
                "unauthorized": [u for u, _, o in hits if o.unauthorized],
                "errors": (failures or [])[:5],
            },
            evidence=evidence,
            facts={"credentials": credentials, "findings": findings},
            next_hints=[
                "用命中凭据登录并枚举权限与敏感数据（保持只读）",
                "检查同网段其他主机是否复用同一口令",
            ],
            duration=time.monotonic() - started,
        )


# ---------------------------------------------------------------------------
# 工具 2：http_form_login
# ---------------------------------------------------------------------------


class HTTPFormLogin(Tool):
    """单次表单登录验证（验证已知/猜到的凭据，不爆破）。"""

    spec = ToolSpec(
        name="http_form_login",
        summary="向登录表单提交一次用户名/口令，验证给定凭据是否有效",
        category="exploit",
        action_level="verify",
        params={
            "url": ParamSpec("str", "登录页 URL", required=True, example="http://127.0.0.1/login"),
            "username": ParamSpec("str", "用户名", required=True),
            "password": ParamSpec("str", "口令", required=True),
            "username_field": ParamSpec("str", "用户名字段名", required=False, default="username"),
            "password_field": ParamSpec("str", "口令字段名", required=False, default="password"),
            "extra_fields": ParamSpec("dict", "附加表单字段（如 CSRF token）", required=False, default=None),
            "success_keyword": ParamSpec("str", "登录成功关键词（缺省自动判定）", required=False, default=None),
        },
        produces=["credentials"],
        examples=['{"url": "http://127.0.0.1/login", "username": "admin", "password": "admin888"}'],
        notes="先 GET 页面自动发现隐藏字段（含 CSRF token），再提交一次；不产生任何破坏性动作。",
    )

    def run(
        self,
        ctx: ToolContext,
        url: str,
        username: str,
        password: str,
        username_field: str = "username",
        password_field: str = "password",
        extra_fields: dict[str, Any] | None = None,
        success_keyword: str | None = None,
    ) -> ToolResult:
        started = time.monotonic()
        page = ctx.http.request(
            "GET", url, required_level="verify", payload_label="form:discover"
        )
        if not page.ok:
            return ToolResult(
                ok=False,
                error=page.error or f"HTTP {page.status}",
                summary=f"登录页获取失败（{page.error or page.status}）: {url}",
                evidence=[page.as_evidence(kind="http_form", note="登录页获取失败")],
                duration=time.monotonic() - started,
            )

        form = _parse_form(page.body, page.url or url, password_field or "password")
        if form is None:
            return ToolResult(
                ok=False,
                error="未在页面中发现表单",
                summary=f"未在 {url} 发现可提交的登录表单",
                data={"status": page.status},
                evidence=[page.as_evidence(kind="http_form", note="未发现表单")],
                duration=time.monotonic() - started,
            )

        payload = dict(form["fields"])
        if extra_fields:
            payload.update({str(k): v for k, v in extra_fields.items()})
        payload[username_field or "username"] = username
        payload[password_field or "password"] = password

        label = f"form:{username}&{password_field or 'password'}=<redacted>"
        if form["method"] == "get":
            res = ctx.http.request(
                "GET", form["action"], params=payload, required_level="verify", payload_label=label
            )
        else:
            res = ctx.http.request(
                "POST", form["action"], data=payload, required_level="verify", payload_label=label
            )
        evidence = res.as_evidence(kind="http_form", note="")
        if not res.ok:
            return ToolResult(
                ok=False,
                error=res.error or f"HTTP {res.status}",
                summary=f"登录请求失败（{res.error or res.status}）",
                evidence=[evidence],
                duration=time.monotonic() - started,
            )

        ok, why = _judge_login(res.body, success_keyword)
        evidence["note"] = f"登录{'成功' if ok else '失败'}：{why}"
        evidence["credential_verified"] = ok
        info = parse_target(res.url or url)
        target = f"{info.host}:{info.port}" if info.port else info.host

        if not ok:
            return ToolResult(
                ok=True,
                summary=f"凭据无效：{username} 登录 {url} 失败（{why}）",
                data={"url": url, "username": username, "verified": False, "reason": why, "status": res.status},
                evidence=[evidence],
                next_hints=["确认字段名/成功关键词，或换用 weak_password_brute 做小规模尝试"],
                duration=time.monotonic() - started,
            )

        credential = {
            "target": target,
            "service": "http_form",
            "username": username,
            "password": password,
            "verified": True,
            "source": "http_form_login",
        }
        # 验证成功的凭据本身就是一条「弱口令/默认口令」漏洞：只记凭据不记漏洞，
        # 会让"验证弱口令"这类任务在报告与评测里都看不到结论。
        finding = {
            "title": f"Web 表单弱口令：{username}",
            "vuln_class": "weak_password",
            "target": target,
            "url": res.url or url,
            "parameter": password_field or "password",
            "method": (form["method"] or "post").upper(),
            "severity": "high",
            "confidence": "high",
            "status": "verified",
            "tool": self.spec.name,
            "payload": f"{username}:{password}",
            "description": f"{url} 的登录表单接受弱口令/默认口令（用户 {username}），攻击者可直接取得合法会话。",
            "impact": "攻击者可用该账号登录后台，读取或篡改数据，并以此为跳板进一步渗透。",
            "remediation": "禁用弱口令与默认口令，启用登录失败锁定与多因素认证，限制后台来源 IP。",
            "cwe": "CWE-521",
            "evidence": [evidence],
        }
        return ToolResult(
            ok=True,
            summary=f"凭据有效：{username} 成功登录 {url}",
            data={"url": url, "username": username, "verified": True, "reason": why, "status": res.status},
            evidence=[evidence],
            facts={"credentials": [credential], "findings": [finding]},
            next_hints=["登录后枚举可用功能与权限边界（保持只读）", "把该凭据写入报告"],
            duration=time.monotonic() - started,
        )


# ---------------------------------------------------------------------------
# 工具 3：flag_hunt
# ---------------------------------------------------------------------------


def _local_candidate(path: str) -> bool:
    """本地读取白名单判定：只接受绝对路径（Windows 盘符仅在本机为 Windows 时）。"""
    candidate = str(path).strip()
    if not candidate:
        return False
    if candidate.startswith("/"):
        return True
    return os.name == "nt" and bool(re.match(r"^[A-Za-z]:[\\/]", candidate))


def _extract_flags(text: str, limit: int = 20) -> list[str]:
    out: list[str] = []
    for match in FLAG_RE.finditer(text or ""):
        value = match.group(0).strip()
        if value and value not in out:
            out.append(value)
        if len(out) >= limit:
            break
    return out


def _read_local_file(path: Path) -> tuple[str, str]:
    """返回 (内容, 错误)。仅读不写，超过上限的文件跳过。"""
    try:
        if not path.is_file():
            return "", "not-a-file"
        if path.stat().st_size > _MAX_READ_BYTES:
            return "", "文件过大，跳过"
        return path.read_text(encoding="utf-8", errors="replace"), ""
    except OSError as exc:
        return "", f"{type(exc).__name__}: {exc}"


class FlagHunt(Tool):
    """flag 搜刮：本地白名单路径 + Web 路径两条腿。"""

    spec = ToolSpec(
        name="flag_hunt",
        summary="在内置白名单路径（Web 路径 + 可选本地文件系统）中搜索 flag 并提取",
        category="exploit",
        action_level="verify",
        params={
            "target": ParamSpec("str", "目标主机（用于记录与范围校验）", required=True, example="127.0.0.1"),
            "base_url": ParamSpec("str", "Web 探测根 URL（可选，如 http://127.0.0.1:8080）", required=False, default=None),
            "paths": ParamSpec("list", "自定义路径清单（默认内置 flag 清单）", required=False, default=None),
            "include_local": ParamSpec(
                "bool",
                "是否同时读取 Trina 所在主机的本地文件（默认 False；仅当目标就是本机时才应开启，"
                "否则会把宿主机残留文件误算成靶机战果）",
                required=False,
                default=False,
            ),
        },
        produces=["flags"],
        examples=[
            '{"target": "127.0.0.1", "base_url": "http://127.0.0.1:8080"}',
            '{"target": "127.0.0.1", "base_url": "http://127.0.0.1:8080", "include_local": true}',
        ],
        notes="本地读取默认关闭，需 include_local=true 显式开启且严格限定在清单内的绝对路径，只读不写；Web 探测走 ctx.http（护栏+限速）。",
    )

    def run(
        self,
        ctx: ToolContext,
        target: str,
        base_url: str | None = None,
        paths: list[str] | None = None,
        include_local: bool = False,
    ) -> ToolResult:
        started = time.monotonic()
        catalog = _clean_list(paths) or list(FLAG_PATHS)
        if not base_url and "://" in str(target or ""):
            base_url = target

        found: list[dict[str, Any]] = []
        errors: list[str] = []
        local_checked = 0
        web_checked = 0

        # ① 本地文件系统（默认关闭：远程目标下读取本机文件会把宿主机残留误算成战果）
        if include_local:
            for entry in catalog:
                if not _local_candidate(entry):
                    continue
                matches = sorted(glob.glob(entry)) if "*" in entry or "?" in entry else [entry]
                if not matches:
                    continue
                for candidate in matches:
                    path = Path(candidate)
                    local_checked += 1
                    content, err = _read_local_file(path)
                    if err:
                        if err != "not-a-file":
                            errors.append(f"{candidate}: {err}")
                        continue
                    for value in _extract_flags(content):
                        found.append({"value": value, "source": f"local:{candidate}", "kind": "local"})

        # ② Web 路径
        if base_url:
            for entry in catalog:
                if not _local_candidate(entry):
                    continue
                url = normalize_url(base_url, entry)
                res = ctx.http.request(
                    "GET", url, required_level="verify", payload_label=f"flag_hunt:{entry}"
                )
                if not res.ok:
                    errors.append(f"{url}: {res.error or res.status}")
                    continue
                web_checked += 1
                if res.status >= 400:
                    continue
                for value in _extract_flags(res.body):
                    found.append({"value": value, "source": url, "kind": "web"})

        # 去重
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in found:
            if item["value"] in seen:
                continue
            seen.add(item["value"])
            deduped.append(item)

        data = {
            "found": deduped,
            "local_checked": local_checked,
            "web_checked": web_checked,
            "local_enabled": bool(include_local),
            "base_url": base_url or "",
            "errors": errors[:10],
        }
        if not deduped:
            return ToolResult(
                ok=True,
                summary=(
                    f"未发现 flag（本地检查 {local_checked} 个路径"
                    + (f"，Web 检查 {web_checked} 个路径" if base_url else "，未做 Web 探测")
                    + "）"
                ),
                data=data,
                next_hints=[
                    "换个 base_url 或提供自定义 paths 清单",
                    "先做目录枚举，找到 Web 根后再回来搜刮",
                ],
                duration=time.monotonic() - started,
            )

        return ToolResult(
            ok=True,
            summary=f"找到 {len(deduped)} 个 flag：{', '.join(item['value'] for item in deduped[:3])}",
            data=data,
            facts={"flags": [{"value": item["value"], "source": item["source"]} for item in deduped]},
            next_hints=["确认 flag 归属的任务与提交格式"],
            duration=time.monotonic() - started,
        )


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [WeakPasswordBrute(), HTTPFormLogin(), FlagHunt()]

for _tool in TOOLS:  # 模块级注册（import 即生效）
    register(_tool)
