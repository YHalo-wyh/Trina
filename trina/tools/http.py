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
                "headers": {k: v for k, v in (self.request_headers or {}).items() if k.lower() not in ("cookie", "authorization")},
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
        """发起请求。护栏拒绝时返回带 error 的 HttpResult（不抛异常，交由工具层处置）。"""
        body_text = ""
        if isinstance(data, str):
            body_text = data
        elif isinstance(data, dict):
            body_text = "&".join(f"{k}={v}" for k, v in data.items())
        elif json_body is not None:
            import json as _json

            body_text = _json.dumps(json_body, ensure_ascii=False)

        if params:
            from urllib.parse import urlencode

            joiner = "&" if urlsplit(url).query else "?"
            url = f"{url}{joiner}{urlencode(params, doseq=True)}"

        decision = self.guard.check_http(url, method=method, body=body_text, required_level=required_level)
        if decision.denied:
            self.denials.append(decision)
            return HttpResult(url=url, method=method.upper(), error=f"[护栏拒绝:{decision.category}] {decision.reason}")

        host = urlsplit(url).hostname or ""
        wait = self.guard.throttle(host)
        if wait > 0:
            time.sleep(min(wait, 5.0))

        started = time.monotonic()
        last_error = ""
        for attempt in range(self.retries + 1):
            try:
                response = self.session.request(
                    method.upper(),
                    url,
                    data=data if files is None else None,
                    json=json_body,
                    files=files,
                    headers=headers,
                    cookies=cookies,
                    allow_redirects=allow_redirects,
                    timeout=timeout or self.timeout,
                )
                result = HttpResult(
                    url=url,
                    method=method.upper(),
                    status=response.status_code,
                    headers={k: v for k, v in response.headers.items()},
                    body=response.text[:200_000],
                    elapsed=time.monotonic() - started,
                    payload=payload_label or body_text[:300],
                    request_headers=dict(headers or {}),
                    request_body=body_text[:2000],
                    history=[r.url for r in response.history],
                )
                self.requests_made += 1
                self.bytes_received += len(result.body)
                return result
            except requests.exceptions.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.retries:
                    time.sleep(0.4 * (attempt + 1))
        self.requests_made += 1
        return HttpResult(url=url, method=method.upper(), error=last_error, elapsed=time.monotonic() - started, payload=payload_label)

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
