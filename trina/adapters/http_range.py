"""已在运行的 Web 靶场适配（Pikachu / DVWA / Bugku / Trina lab / 自建 Web 靶机）。

与其它适配器的区别
------------------
靶场已经由用户起好了，本适配器**不启动任何服务**，只做：

1. 读取 yaml 里的 `base_url` 与 `ground_truth`；
2. `prepare()` 做一次连通性探测（`GET base_url`），非 2xx/3xx 时在 detail/notes 标注不可达；
3. 可选 `login`：先 GET 登录页拿会话 cookie，再 POST 提交凭据，把 cookie 存进
   `self.session_cookies` 供 Agent 复用（靶场自带凭据登录，属于「无害验证」级别）。

说明：这里的 HTTP 探测/登录使用 `urllib`，因为适配器层面没有 `ToolContext`
（`ctx.http`），且目标是 yaml 里显式声明的靶场自身地址。Agent 后续所有攻击流量
仍然必须走 `ctx.http`。
"""

from __future__ import annotations

import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

from .base import RangeAdapter, RangeInfo, RangeTarget, parse_targets, split_authority, tcp_probe

#: 探测时使用的浏览器化 UA（避免被靶场的 UA 过滤直接 403）。
DEFAULT_UA = "Mozilla/5.0 (X11; Linux x86_64) TRINA-RangeAdapter/0.1"


class HttpRangeAdapter(RangeAdapter):
    """面向已在运行的 Web 靶场。"""

    def __init__(self, config, spec: Mapping[str, Any] | None = None, path=None):
        super().__init__(config, spec, path)
        self.base_url = str(self.spec.get("base_url") or self.spec.get("url") or "").rstrip("/")
        self.timeout = float(self.spec.get("timeout") or 8.0)
        self.probe_path = str(self.spec.get("probe_path") or "")
        self.login_spec: dict[str, Any] = dict(self.spec.get("login") or {})
        self.after_login: list[dict[str, Any]] = list(self.spec.get("after_login") or [])
        #: 供 Agent 复用的会话 cookie（name → value）。
        self.session_cookies: dict[str, str] = {}
        self._jar = http.cookiejar.CookieJar()
        self._probe: dict[str, Any] = {}
        self._info: RangeInfo | None = None

    # ------------------------------------------------------------ 内部工具
    def _opener(self) -> urllib.request.OpenerDirector:
        handlers: list[Any] = [urllib.request.HTTPCookieProcessor(self._jar)]
        # 靶场通常在本地/私网，默认绕开环境代理，避免 WSL 代理把 127.0.0.1 打歪。
        if not self.spec.get("use_env_proxy"):
            handlers.append(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener(*handlers)

    def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """发一个请求，返回 `{ok, status, body, error, headers}`（绝不抛异常）。"""
        merged = {"User-Agent": DEFAULT_UA, "Accept": "*/*"}
        merged.update(headers or {})
        req = urllib.request.Request(url, data=data, headers=merged, method=method.upper())
        try:
            with self._opener().open(req, timeout=self.timeout) as resp:
                body = resp.read(65536)
                return {
                    "ok": True,
                    "status": int(getattr(resp, "status", 0) or resp.getcode() or 0),
                    "body": body.decode("utf-8", "replace"),
                    "headers": dict(resp.headers.items()),
                    "url": resp.geturl(),
                    "error": "",
                }
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = exc.read(16384)
            except Exception:  # pragma: no cover - 读取 body 失败不影响结论
                pass
            return {
                "ok": False,
                "status": int(exc.code),
                "body": body.decode("utf-8", "replace"),
                "headers": dict(getattr(exc, "headers", {}) or {}),
                "url": url,
                "error": f"HTTP {exc.code} {exc.reason}",
            }
        except urllib.error.URLError as exc:
            return {"ok": False, "status": 0, "body": "", "headers": {}, "url": url, "error": f"连接失败: {exc.reason}"}
        except OSError as exc:
            return {"ok": False, "status": 0, "body": "", "headers": {}, "url": url, "error": f"连接失败: {exc}"}
        except Exception as exc:  # pragma: no cover - 兜底，保证不抛给调用方
            return {"ok": False, "status": 0, "body": "", "headers": {}, "url": url, "error": f"请求异常: {exc}"}

    def _authority(self) -> tuple[str, int | None]:
        return split_authority(self.base_url, 80)

    # ------------------------------------------------------------ 生命周期
    def status(self) -> dict:
        targets = self._targets()
        if not self.base_url:
            return self.status_dict(False, "yaml 缺少 base_url，无法定位靶场", targets)
        host, port = self._authority()
        reachable = tcp_probe(host, port, timeout=min(self.timeout, 2.0))
        detail = (
            f"{self.base_url} TCP 可连通（{host}:{port}）；HTTP 探测在 prepare() 中进行"
            if reachable
            else f"{self.base_url} 不可达：TCP {host}:{port} 连接失败（确认 Web 靶场已启动、端口映射正确）"
        )
        if self.login_spec:
            detail += f"；已配置登录账号 {self.login_spec.get('username', '?')}"
        return self.status_dict(reachable, detail, targets)

    def _targets(self) -> list[RangeTarget]:
        targets: list[RangeTarget] = []
        if self.base_url:
            host, port = self._authority()
            targets.append(
                RangeTarget(
                    host=host,
                    port=port,
                    service="http",
                    url=self.base_url,
                    notes=self.description or self.notes,
                )
            )
        # 混合靶场：yaml 可额外声明主机（如 lab 自带的 SSH 弱口令靶机）。
        targets.extend(parse_targets(self.spec.get("targets")))
        return targets

    def prepare(self) -> RangeInfo:
        """连通性探测（+ 可选登录），返回目标与 ground truth。**不抛异常**。"""
        notes: list[str] = []
        probe_ok = False
        if not self.base_url:
            notes.append("yaml 缺少 base_url，无法探测靶场")
        else:
            probe_url = self.base_url + (self.probe_path or "")
            result = self._request(probe_url)
            self._probe = {k: v for k, v in result.items() if k != "body"}
            self._probe["body_head"] = result.get("body", "")[:200]
            code = result.get("status", 0)
            if result.get("ok") and 200 <= code < 400:
                probe_ok = True
                notes.append(f"连通性探测通过：GET {probe_url} → HTTP {code}")
            else:
                notes.append(
                    f"连通性探测失败：GET {probe_url} → {result.get('error') or f'HTTP {code}'}；"
                    "靶场可能未启动或 base_url 有误（prepare() 仍返回配置中的目标供人工确认）"
                )
            if self.login_spec and probe_ok and self.login_spec.get("auto", True):
                logged = self.login()
                notes.append("靶场自带凭据登录成功，会话 cookie 已保存到 session_cookies" if logged else "靶场自带凭据登录失败（不影响 target 清单）")
        if self.notes:
            notes.append(self.notes)
        info = RangeInfo(
            name=self.name,
            kind=self.kind,
            targets=self._targets(),
            ground_truth=self.ground_truth(),
            flag_patterns=self.flag_patterns(),
            notes="；".join(n for n in notes if n),
            source=self.source,
        )
        self._info = info
        self._set_detail(info.notes)
        return info

    def info(self) -> RangeInfo:
        return self._info or self._info_snapshot()

    def primary_target(self) -> str:
        if self.base_url:
            return self.base_url
        targets = self._targets()
        return targets[0].url or targets[0].authority if targets else ""

    # ------------------------------------------------------------ 登录
    def login(self) -> bool:
        """靶场自带凭据登录：GET 登录页 → POST 凭据 → 保存 cookie 到 `session_cookies`。"""
        spec = self.login_spec
        if not spec:
            return False
        base = self.base_url
        login_url = str(spec.get("url") or urllib.parse.urljoin(base + "/", "login.php"))
        if login_url.startswith("/"):
            login_url = base + login_url
        user_field = str(spec.get("user_field") or spec.get("username_field") or "username")
        pass_field = str(spec.get("pass_field") or spec.get("password_field") or "password")
        username = str(spec.get("username") or "")
        password = str(spec.get("password") or "")
        payload: dict[str, str] = {user_field: username, pass_field: password}
        for key, value in (spec.get("extra_fields") or {}).items():
            payload[str(key)] = str(value)
        method = str(spec.get("method") or "POST").upper()

        # 1) 先 GET 登录页，拿到 PHPSESSID 一类的会话 cookie / CSRF token。
        page = self._request(login_url)
        if spec.get("csrf_field") and page.get("ok"):
            token = self._extract_csrf(page.get("body", ""), str(spec["csrf_field"]))
            if token:
                payload[str(spec["csrf_field"])] = token

        # 2) 提交凭据。GET 模式把凭据拼进 query。
        data = urllib.parse.urlencode(payload).encode()
        if method == "GET":
            sep = "&" if "?" in login_url else "?"
            result = self._request(login_url + sep + urllib.parse.urlencode(payload))
        else:
            result = self._request(
                login_url,
                method=method,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        self.session_cookies = {cookie.name: cookie.value for cookie in self._jar}
        marker = str(spec.get("success_marker") or "")
        ok = bool(result.get("ok")) and (marker in result.get("body", "") if marker else 200 <= result.get("status", 0) < 400)
        if not ok:
            return False
        # 3) 登录后的附加动作（如 DVWA 设置 security=low）。
        for step in self.after_login:
            if not isinstance(step, Mapping):
                continue
            url = str(step.get("url") or "")
            if url.startswith("/"):
                url = base + url
            step_method = str(step.get("method") or "GET").upper()
            step_data = None
            if step.get("data"):
                step_data = urllib.parse.urlencode({str(k): str(v) for k, v in step["data"].items()}).encode()
            self._request(
                url,
                method=step_method,
                data=step_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"} if step_data else None,
            )
        self.session_cookies = {cookie.name: cookie.value for cookie in self._jar}
        return True

    def cookie_header(self) -> str:
        """把会话 cookie 拼成 `Cookie:` 头，供 Agent 复用登录态。"""
        return "; ".join(f"{k}={v}" for k, v in self.session_cookies.items())

    @staticmethod
    def _extract_csrf(body: str, field: str) -> str:
        import re

        match = re.search(rf'name=["\']{re.escape(field)}["\'][^>]*value=["\']([^"\']+)', body or "")
        return match.group(1) if match else ""


__all__ = ["DEFAULT_UA", "HttpRangeAdapter"]
