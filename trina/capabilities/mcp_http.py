"""MCP Streamable HTTP 适配；网络继续复用 ToolContext 的受控 HTTP 出口。"""
from __future__ import annotations

import json
import os
from typing import Any

from ..tools.registry import ToolResult


def invoke(capability: Any, ctx: Any, params: dict[str, Any]) -> ToolResult:
    """每次有界调用初始化会话并拉取实时 schema；不把服务端提示提升为系统规则。"""
    runtime = capability.runtime
    endpoint = os.environ.get(str(runtime.get("url_env", "")), "") or str(runtime.get("url", ""))
    key = os.environ.get(str(runtime.get("key_env", "")), "")
    if not endpoint or (runtime.get("key_env") and not key):
        return ToolResult(ok=False, category="unavailable", error="外部MCP未配置服务地址或凭据环境变量")
    level = str(capability.policy.get("action_level", "detect"))
    if level != "detect" and not ctx.config.scope.full_trust:
        return ToolResult(ok=False, denied=True, error="远端扫描执行无法逐请求裁决，restricted模式禁止外部MCP写操作")
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if key:
        headers[str(runtime.get("key_header", "X-API-Key"))] = key
    ids = iter(range(1, 1000))
    evidence = []
    session_id = ""

    def rpc(method: str, arguments: dict, notification: bool = False) -> dict:
        nonlocal session_id
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": arguments}
        if not notification:
            payload["id"] = next(ids)
        response = ctx.http.post(endpoint, json_body=payload, headers=headers, allow_redirects=False,
                                 timeout=float(capability.policy.get("timeout", 30)), required_level=level)
        evidence.append(response.as_evidence("mcp.http"))
        if response.error or not 200 <= response.status < 300:
            raise ValueError(f"MCP {method} 失败，HTTP {response.status}: {response.error}")
        received_session = next((v for k, v in response.headers.items() if k.lower() == "mcp-session-id"), "")
        if received_session:
            session_id = received_session
            headers["Mcp-Session-Id"] = session_id
        if notification:
            return {}
        messages = []
        try:
            messages.append(json.loads(response.body))
        except json.JSONDecodeError:
            for event in response.body.replace("\r", "").split("\n\n"):
                data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
                if data:
                    try:
                        messages.append(json.loads(data))
                    except json.JSONDecodeError:
                        continue
        message = next((m for m in messages if isinstance(m, dict) and m.get("id") == payload["id"]), None)
        if message is None or "error" in message:
            raise ValueError(f"MCP {method} 返回错误或响应ID不匹配：{str(message.get('error'))[:300] if message else 'missing response'}")
        result = message.get("result", {})
        if not isinstance(result, dict):
            raise ValueError("MCP result 必须为对象")
        return result

    try:
        initialized = rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                        "clientInfo": {"name": "trina", "version": "1.0"}})
        headers["MCP-Protocol-Version"] = str(initialized.get("protocolVersion", "2025-03-26"))
        rpc("notifications/initialized", {}, notification=True)
        tools = []
        cursor = ""
        for _ in range(10):
            page = rpc("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(t for t in page.get("tools", []) if isinstance(t, dict))
            cursor = str(page.get("nextCursor") or "")
            if not cursor:
                break
        allowed = set(runtime.get("allowed_tools") or [])
        tools = [tool for tool in tools if tool.get("name") in allowed]
        if params.get("action", "list_tools") == "list_tools":
            return ToolResult(summary=f"读取 {len(tools)} 个实时MCP工具Schema", data={"tools": tools}, evidence=evidence)
        name = str(params.get("tool_name", ""))
        selected = next((tool for tool in tools if tool.get("name") == name), None)
        if selected is None:
            raise ValueError("工具未在模块允许列表或实时服务Schema中；先 list_tools")
        arguments = params.get("arguments", {})
        schema = selected.get("inputSchema", {})
        missing = [k for k in schema.get("required", []) if k not in arguments]
        if missing:
            raise ValueError(f"MCP参数缺少字段：{missing}")
        result = rpc("tools/call", {"name": name, "arguments": arguments})
        return ToolResult(ok=not bool(result.get("isError")), summary=f"外部MCP {name} 已返回",
                          error="外部MCP工具返回isError" if result.get("isError") else "", data=result, evidence=evidence)
    except (ValueError, TypeError) as exc:
        return ToolResult(ok=False, error=str(exc), evidence=evidence)
    finally:
        if session_id:
            ctx.http.request("DELETE", endpoint, headers=headers, allow_redirects=False, required_level=level)
