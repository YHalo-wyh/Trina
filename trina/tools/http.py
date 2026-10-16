"""带护栏的 HTTP 客户端：所有 Web 类请求的唯一出口。

特性：自动过 ScopeGuard 校验 → 限速 → 记录证据 → 统一超时/UA/重定向策略。
工具层不要直接 `requests.get`，否则绕过审计与护栏。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from ..scope import Decision, ScopeGuard

DEFAULT_UA = "Trina/0.1 (+authorized-pentest; contact=lab)"
DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "close",
}


@dataclass
class HttpResult:
    """一次 HTTP 交互的完整记录（同时作为证据）。"""

    url: str
    method: str = "GET"
    status: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    elapsed: float = 0.0
    error: str = ""
    payload: str = ""
    request_headers: dict[str, str] = field(default_factory=dict)
    request_body: str = ""
    history: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error and 0 < self.status < 600

    @property
    def length(self) -> int:
        return len(self.body or "")

    @property
    def title(self) -> str:
        body = self.body or ""
        lower = body.lower()
        start = lower.find("<title")
        if start < 0:
            return ""
        start = lower.find(">", start) + 1
        end = lower.find("</title>", start)
        return body[start:end].strip()[:120] if end > start else ""

    def excerpt(self, limit: int = 400) -> str:
        text = (self.body or "").replace("\r", "")
        return text[:limit]

    def to_dict(self, *, include_body: bool = True, body_limit: int = 1200) -> dict[str, Any]:
        data = {
            "url": self.url,
            "method": self.method,
            "status": self.status,
            "length": self.length,
            "elapsed": round(self.elapsed, 3),
            "error": self.error,
            "title": self.title,
            "redirects": self.history[:5],
        }
        if self.payload:
            data["payload"] = self.payload
        if self.request_body:
            data["request_body"] = self.request_body[:body_limit]
        if include_body:
            data["body_excerpt"] = self.excerpt(body_limit)
        return data

    def as_evidence(self, kind: str = "http", note: str = "", **extra: Any) -> dict[str, Any]:
        evidence = {
            "kind": kind,
            "target": self.url,
            "method": self.method,
            "status": self.status,
            "request": {
                "method": self.method,
                "url": self.url,
                "headers": {k: v for k, v in (self.request_headers or {}).items() if k.lower() not in ("cookie", "authorization", "proxy-authorization")},
                "body": (self.request_body or "")[:600],
            },
            "response": {"status": self.status, "length": self.length, "body_excerpt": self.excerpt(600)},
            "payload": self.payload,
            "note": note,
        }
        evidence.update(extra)
        return evidence


class HttpClient:
    """受护栏约束的 HTTP 会话。"""

    def __init__(
        self,
        guard: ScopeGuard,
        *,
        timeout: float = 15.0,
        retries: int = 1,
        verify_tls: bool = False,
        headers: dict[str, str] | None = None,
        actor: str = "trina",
    ):
        self.guard = guard
        self.timeout = timeout
        self.retries = retries
        self.actor = actor
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        if headers:
            self.session.headers.update(headers)
        self.session.verify = verify_tls
        self.session.max_redirects = 5
        self.requests_made = 0
        self.bytes_received = 0
        self.denials: list[Decision] = []

    # ------------------------------------------------------------------ 核心
    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: Any = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        allow_redirects: bool = True,
        timeout: float | None = None,
        required_level: str = "detect",
        payload_label: str = "",
        files: Any = None,
    ) -> HttpResult:
        """逐跳、逐次重试校验实际请求；护栏拒绝作为失败结果返回。"""
        started = time.monotonic()
        history: list[str] = []

        def failure(message: str, request: requests.PreparedRequest | None = None) -> HttpResult:
            return HttpResult(
                url=request.url if request is not None else url,
                method=request.method if request is not None else method.upper(),
                error=message, elapsed=time.monotonic() - started,
                payload=payload_label, history=list(history),
            )

        def denied(decision: Decision, request: requests.PreparedRequest) -> HttpResult:
            self.denials.append(decision)
            return failure(f"[护栏拒绝:{decision.category}] {decision.reason}", request)

        try:
            prepared = self.session.prepare_request(requests.Request(
                method.upper(), url, params=params, data=data, json=json_body,
                files=files, headers=headers, cookies=cookies,
            ))
        except (requests.exceptions.RequestException, ValueError) as exc:
            return failure(f"{type(exc).__name__}: {exc}")

        while True:
            body_text = prepared.body or ""
            if isinstance(body_text, bytes):
                body_text = body_text.decode("utf-8", "replace")
            elif not isinstance(body_text, str):
                return failure("不支持无法校验和重放的流式请求体", prepared)
            # 写入类请求连接失败时可能已被服务端处理，不自动重复提交。
            retries = max(0, self.retries) if prepared.method in ("GET", "HEAD", "OPTIONS") else 0
            for attempt in range(retries + 1):
                decision = self.guard.check_http(
                    prepared.url, method=prepared.method, body=body_text, required_level=required_level,
                )
                if decision.denied:
                    return denied(decision, prepared)
                wait = self.guard.throttle(urlsplit(prepared.url).hostname or "")
                if wait > 0:
                    time.sleep(min(wait, 5.0))
                self.requests_made += 1
                try:
                    settings = self.session.merge_environment_settings(prepared.url, {}, False, None, None)
                    response = self.session.send(
                        prepared, allow_redirects=False, timeout=timeout or self.timeout, **settings,
                    )
                    break
                except requests.exceptions.RequestException as exc:
                    if attempt >= retries:
                        return failure(f"{type(exc).__name__}: {exc}", prepared)
                    time.sleep(0.4 * (attempt + 1))

            try:
                body = response.text[:200_000]
                self.bytes_received += len(body)
                # Requests 只准备下一跳（含方法改写、Cookie 和 307/308 请求体），不发送。
                following = response.next if allow_redirects else None
                if following is None:
                    return HttpResult(
                        url=prepared.url, method=prepared.method, status=response.status_code,
                        headers=dict(response.headers), body=body, elapsed=time.monotonic() - started,
                        payload=payload_label or body_text[:300], request_headers=dict(prepared.headers),
                        request_body=body_text[:2000], history=list(history),
                    )
                if len(history) >= self.session.max_redirects:
                    return failure(f"重定向次数超过上限 {self.session.max_redirects}", prepared)
                history.append(prepared.url)
                decision = self.guard.check_http_redirect(prepared.url, following.url)
                if decision.denied:
                    return denied(decision, following)
                prepared = following
            finally:
                response.close()

    # ------------------------------------------------------------------ 便捷
    def get(self, url: str, **kwargs: Any) -> HttpResult:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> HttpResult:
        return self.request("POST", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> HttpResult:
        return self.request("HEAD", url, **kwargs)

    def stats(self) -> dict[str, Any]:
        return {
            "requests": self.requests_made,
            "bytes": self.bytes_received,
            "denied": len(self.denials),
        }


def normalize_url(base: str, path: str) -> str:
    """把 base + path 组合成绝对 URL（保留 base 的 scheme/host）。"""
    if not path:
        return base
    if path.startswith(("http://", "https://")):
        return path
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def origin(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))
