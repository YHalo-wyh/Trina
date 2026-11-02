"""TSecBench 答题 API 客户端。

平台在托管/本地模式下注入两个环境变量：

* ``BENCHMARK_BASE_URL`` —— 答题 API 根地址
* ``BENCHMARK_TOKEN``    —— 本次跑分任务的鉴权 token

接口文档（`/openapi/v1/challenges/*`）要求所有请求带 ``BENCHMARK_TOKEN`` 头。本模块是
Trina 里唯一允许访问控制面的代码：每次请求都经 ``ScopeGuard.check_control_plane`` 判定，
只允许 ``scope.control_plane_hosts`` 中登记的主机，不走靶场目标网段逻辑。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..scope import ScopeGuard
from ..tools.http import HttpClient, HttpResult


class BenchmarkError(RuntimeError):
    """答题 API 调用失败。``code`` 为平台返回的机器可读错误码。"""

    def __init__(self, message: str, *, status: int = 0, code: str = "", detail: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail


@dataclass
class BenchmarkClient:
    """`/openapi/v1/challenges` 的最小客户端。"""

    base_url: str
    token: str
    guard: ScopeGuard
    timeout: float = 20.0
    verify_tls: bool = True

    def __post_init__(self) -> None:
        if not self.base_url:
            raise BenchmarkError("BENCHMARK_BASE_URL 未配置")
        if not self.token:
            raise BenchmarkError("BENCHMARK_TOKEN 未配置")
        self.base_url = self.base_url.rstrip("/")
        self.http = HttpClient(
            self.guard,
            timeout=self.timeout,
            retries=2,
            verify_tls=self.verify_tls,
            actor="trina-bench",
        )

    # ------------------------------------------------------------------ 传输
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        result: HttpResult = self.http.request(
            method,
            url,
            params=params,
            json_body=json_body,
            headers={"BENCHMARK_TOKEN": self.token, "Accept": "application/json"},
            control_plane=True,
            payload_label=f"bench:{path}",
            timeout=self.timeout,
        )
        if result.error:
            raise BenchmarkError(f"{method} {path} 失败: {result.error}", status=result.status)
        body: Any = None
        if result.body:
            try:
                body = json.loads(result.body)
            except json.JSONDecodeError:
                body = result.body
        if not (200 <= result.status < 300):
            code = ""
            message = ""
            detail: Any = body
            if isinstance(body, dict):
                code = str(body.get("code") or "")
                message = str(body.get("message") or "")
            if not message:
                message = f"HTTP {result.status}: {str(body)[:300]}"
            raise BenchmarkError(message, status=result.status, code=code, detail=detail)
        if isinstance(body, dict) and body.get("code") and body.get("message") and result.status >= 400:
            raise BenchmarkError(str(body["message"]), status=result.status, code=str(body["code"]), detail=body)
        return body

    # ------------------------------------------------------------------ 接口
    def list_challenges(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/openapi/v1/challenges")
        if not isinstance(data, list):
            raise BenchmarkError(f"题目列表响应格式异常: {str(data)[:200]}")
        return [item for item in data if isinstance(item, dict)]

    def start(self, unique_code: str) -> dict[str, Any]:
        data = self._request("POST", "/openapi/v1/challenges/start", params={"unique_code": unique_code})
        return data if isinstance(data, dict) else {}

    def hint(self, unique_code: str) -> dict[str, Any]:
        data = self._request("GET", "/openapi/v1/challenges/hint", params={"unique_code": unique_code})
        return data if isinstance(data, dict) else {}

    def submit(self, unique_code: str, flag: str) -> dict[str, Any]:
        data = self._request(
            "POST",
            "/openapi/v1/challenges/submit",
            json_body={"unique_code": unique_code, "flag": flag},
        )
        return data if isinstance(data, dict) else {}

    def close(self, unique_code: str) -> dict[str, Any]:
        data = self._request("POST", "/openapi/v1/challenges/close", params={"unique_code": unique_code})
        return data if isinstance(data, dict) else {}


__all__ = ["BenchmarkClient", "BenchmarkError"]
