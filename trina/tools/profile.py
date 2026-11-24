"""按任务动态裁剪工具可见面，避免把整套 CTF/企业工具注入每轮上下文。"""
from __future__ import annotations

from typing import Any, Iterable


# CTF 默认不暴露的企业审计、资产图和重复执行工具。实现仍保留，显式能力租约
# 或专用企业配置可以继续使用它们。
CTF_PRUNED_TOOLS = frozenset({
    "port_scan", "service_probe", "tls_info",
    "traffic_blob", "traffic_get", "traffic_search",
    "bind_finding_traffic", "get_finding_retest_context", "get_finding_traffic",
    "record_finding_retest_result",
    "insert_assets", "list_assets", "list_untested_assets", "mark_asset_tested",
    "api_route_tree", "record_intelligence",
    "shared_context", "shared_trace_get", "shared_trace_search",
    "terminal_start", "terminal_status", "terminal_stop",
    "flag_hunt", "rce_exec",
    "browser_recording", "browser_requests", "browser_route", "browser_storage",
    "browser_upload", "browser_snapshot", "browser_screenshot", "browser_interact",
    "browser_evaluate",
})

CTF_CORE_TOOLS = frozenset({
    "http_fingerprint", "web_search", "terminal_exec",
    "workspace_read", "workspace_write", "workspace_edit", "workspace_search", "workspace_git",
    "record_fact", "report_finding", "prove_goal", "list_goals",
    "poc_scan", "sqli_test", "lfi_test", "cmd_inject_test", "file_upload_test",
    "http_form_login", "weak_password_brute",
    "browser_open", "browser_close", "worker_tasks", "todo",
})

CTF_FILE_TOOLS = frozenset({
    "terminal_exec", "workspace_read", "workspace_read_many", "workspace_search", "workspace_write", "workspace_edit", "workspace_edit_many", "todo",
})

CTF_WEB_TOOLS = frozenset({
    "api_recon_static", "dir_bruteforce", "param_reflect_scan", "web_surface", "nuclei_scan",
})

CTF_BROWSER_TOOLS = frozenset({
    "api_recon_runtime", "browser_interact", "browser_requests", "browser_route",
    "browser_storage", "browser_upload", "browser_snapshot", "browser_screenshot",
    "browser_evaluate",
})

CTF_BINARY_WEB_TOOLS = frozenset({
    "web_search", "http_fingerprint", "browser_open", "browser_close",
    "api_recon_runtime", "api_recon_static", "api_route_tree", "web_surface",
    "dir_bruteforce", "param_reflect_scan", "nuclei_scan", "poc_scan",
    "sqli_test", "lfi_test", "cmd_inject_test", "file_upload_test",
    "http_form_login", "weak_password_brute", "rce_exec",
    *CTF_WEB_TOOLS, *CTF_BROWSER_TOOLS,
})

CTF_BINARY_COORDINATION_TOOLS = frozenset({"list_goals", "workspace_git", "worker_tasks"})

CODE_CORE_TOOLS = frozenset({
    "terminal_exec", "workspace_read", "workspace_read_many", "workspace_search",
    "workspace_write", "workspace_edit", "workspace_edit_many", "workspace_git", "todo",
})


def visible_tool_names(registry: Any, route: str = "general", *, browser: bool = False,
                       terminal_session: bool = False) -> set[str]:
    """返回当前 Worker 的工具白名单。

    自定义小注册表原样保留，避免测试替身和 Capability 租约被默认 CTF 剪裁。
    完整内置注册表使用 CTF 核心集合，二进制、Web 深度工具和浏览器按需加入。
    """
    available = set(registry.names())
    if not available.intersection(CTF_CORE_TOOLS | CTF_PRUNED_TOOLS | CTF_WEB_TOOLS):
        return available
    selected = (available & CTF_CORE_TOOLS) - CTF_PRUNED_TOOLS
    if route == "ctf":
        # 题目文件型 CTF 只需要读取、执行、登记和验证。隐藏 Web、浏览器、
        # 爆破及企业台账 Schema，避免模型在首轮选择无关能力。
        selected = available & CTF_FILE_TOOLS
    if route == "web":
        selected.update(available & CTF_WEB_TOOLS)
    if route == "code":
        # 编程任务只需要读、搜、改、测试和可选 Git 操作。
        selected = available & CODE_CORE_TOOLS
    if route == "binary":
        # Binary/Pwn 题也需要一次性读取题目目录里的说明和已有利用脚本，
        # 否则模型只能先调用低信息目录探针，再重复读取同一份脚本。
        selected.update(available & {"workspace_read_many"})
        selected.update(name for name in available if name.startswith("binary_") or name.startswith("debug_"))
        # Binary/Pwn/Reverse 任务走本地文件、终端、r2 和 GDB。把 Web 搜索、
        # 浏览器和 HTTP 测试工具从本轮 Schema 移除，避免模型在无关分支上
        # 消耗模型请求和上下文预算。用户明确要求资料检索时可切换到 General。
        selected.difference_update(available & CTF_BINARY_WEB_TOOLS)
        selected.difference_update(available & CTF_BINARY_COORDINATION_TOOLS)
    if browser:
        selected.update(available & CTF_BROWSER_TOOLS)
    if terminal_session:
        selected.update(available & {"terminal_start", "terminal_status", "terminal_stop"})
    # binary 以及被裁剪的工具不因“全注册”自动出现。动态能力租约仍可传入自定义注册表。
    return selected & available


def excluded_from_prompt(registry: Any, visible: Iterable[str], *, hidden: Iterable[str] = ()) -> set[str]:
    """把按需白名单转换成现有 prompt/schema 接口的排除集合。"""
    return set(registry.names()) - set(visible) | set(hidden)


__all__ = ["CTF_PRUNED_TOOLS", "CTF_CORE_TOOLS", "CTF_FILE_TOOLS", "visible_tool_names", "excluded_from_prompt"]

