"""外部专业工具的调用封装（nmap / nuclei / sqlmap / hydra / ffuf / gobuster ...）。

原则
----
1. **外部二进制优先，内置引擎兜底**：实训 WSL 环境常常没有安装这些工具，
   因此每个能力都必须有纯 Python 实现，外部工具只是「加速器」。
2. **shell=False**：永远用 argv 列表调用，杜绝命令注入与通配符意外展开。
3. **统一执行**：`run_external()` 内先过 `guard.check_argv()`（full 直接放行，restricted 应用边界），再执行、再审计。
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from .. import __version__
from ..scope import ScopeViolation


def which(binary: str) -> str | None:
    return shutil.which(binary)


def have(binary: str) -> bool:
    return which(binary) is not None


def installed(*binaries: str) -> dict[str, str | None]:
    return {name: which(name) for name in binaries}


@dataclass
class ExternalRun:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    code: int = -1
    command: str = ""
    duration: float = 0.0
    error: str = ""
    binary: str = ""

    @property
    def output(self) -> str:
        return self.stdout or self.stderr

    def to_dict(self, limit: int = 3000) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "command": self.command,
            "duration": round(self.duration, 2),
            "stdout": self.stdout[:limit],
            "stderr": self.stderr[:600],
            "error": self.error,
        }


@dataclass
class McpCall:
    """一次 MCP stdio 工具调用结果。"""

    ok: bool
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    stderr: str = ""
    command: str = ""
    duration: float = 0.0


def run_external(
    argv: Sequence[str],
    ctx: Any,
    *,
    tool: str = "",
    target: str = "",
    required_level: str = "detect",
    timeout: float | None = None,
    input_data: str | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    audit: bool = True,
) -> ExternalRun:
    """执行外部二进制。护栏拒绝时抛 `ScopeViolation`（由注册表统一转为拒绝结果）。"""
    argv = [str(a) for a in argv]
    binary = argv[0] if argv else ""
    resolved = which(binary)
    if resolved is None:
        return ExternalRun(ok=False, error=f"未安装外部工具 {binary}", binary=binary, command=" ".join(argv))
    argv = [resolved, *argv[1:]]

    decision = ctx.guard.check_argv(argv, tool=tool or binary, target=target, required_level=required_level)
    if decision.denied:
        raise ScopeViolation(decision)

    host = ""
    if target:
        try:
            from ..scope import parse_target

            host = parse_target(target).host
        except ValueError:
            host = target
    ctx.throttle(host)

    budget = timeout or ctx.config.run.tool_timeout
    if ctx.remaining() is not None:
        budget = min(budget, max(.001, ctx.remaining()))

    from ..scope import execution_budget
    budget = execution_budget(budget)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            input=input_data,
            capture_output=True,
            text=True,
            timeout=budget,
            check=False,
            errors="replace",
            cwd=cwd,
            env=env,
        )
        result = ExternalRun(
            ok=proc.returncode == 0,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            code=proc.returncode,
            command=" ".join(argv),
            duration=time.monotonic() - started,
            binary=binary,
        )
    except subprocess.TimeoutExpired:
        result = ExternalRun(ok=False, error=f"超时 {budget:.0f}s", command=" ".join(argv), duration=time.monotonic() - started, binary=binary)
    except OSError as exc:
        result = ExternalRun(ok=False, error=str(exc), command=" ".join(argv), duration=time.monotonic() - started, binary=binary)

    if audit:
        digest = hashlib.sha256(result.stdout.encode("utf-8", "replace")).hexdigest()
        ctx.audit.append(
            "external.run",
            tool=tool or binary,
            decision="allow",
            command=result.command,
            code=result.code,
            target=target,
            duration=round(result.duration, 2),
            stdout_digest=digest,
            stdout_bytes=len(result.stdout),
        )
    return result


def run_mcp_stdio(
    argv: Sequence[str],
    tool_name: str,
    arguments: dict[str, Any],
    ctx: Any,
    *,
    target: str = "",
    required_level: str = "detect",
    timeout: float | None = None,
    cwd: str | None = None,
    protocol_version: str = "2025-11-25",
    env: dict[str, str] | None = None,
) -> McpCall:
    """启动一次可取消的 MCP stdio 会话并调用一个工具。

    这是 Capability Fabric 的最小传输边界：每次调用独立启动进程，stdout
    只接受逐行 JSON-RPC，stderr 只作诊断。进程在结果、错误或超时后都会回收，
    不让能力进程脱离 ToolRegistry 的执行生命周期。
    """
    argv = [str(item) for item in argv]
    if not argv:
        return McpCall(ok=False, error="MCP command 不能为空")
    resolved = which(argv[0])
    if resolved is None:
        return McpCall(ok=False, error=f"未安装 MCP 启动程序 {argv[0]}", command=" ".join(argv))
    argv = [resolved, *argv[1:]]
    decision = ctx.guard.check_argv(argv, tool=f"mcp:{tool_name}", target=target, required_level=required_level)
    if decision.denied:
        raise ScopeViolation(decision)

    budget = timeout or ctx.config.run.tool_timeout
    if ctx.remaining() is not None:
        budget = min(budget, max(.1, ctx.remaining()))
    from ..scope import execution_budget

    budget = execution_budget(budget)
    started = time.monotonic()
    stderr_parts: list[str] = []
    lines: "queue.Queue[str | None]" = queue.Queue()
    process_env = _mcp_environment(env)
    creation: dict[str, Any] = {}
    if os.name == "nt":
        creation["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        creation["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=cwd,
            env=process_env,
            **creation,
        )
    except OSError as exc:
        return McpCall(ok=False, error=str(exc), command=" ".join(argv), duration=time.monotonic() - started)

    def read_stdout() -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    def read_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_parts.append(line)
            if sum(map(len, stderr_parts)) > 12_000:
                del stderr_parts[:-20]

    stdout_thread = threading.Thread(target=read_stdout, name="trina-mcp-stdout", daemon=True)
    stderr_thread = threading.Thread(target=read_stderr, name="trina-mcp-stderr", daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    deadline = time.monotonic() + budget

    def send(message: dict[str, Any]) -> None:
        if proc.stdin is None or proc.poll() is not None:
            raise RuntimeError("MCP 进程已退出")
        proc.stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
        proc.stdin.flush()

    def receive(request_id: int) -> dict[str, Any]:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"MCP 调用超时（{budget:.1f}s）")
            try:
                line = lines.get(timeout=min(.2, remaining))
            except queue.Empty:
                if proc.poll() is not None:
                    raise RuntimeError(f"MCP 进程提前退出（code={proc.returncode}）")
                continue
            if line is None:
                raise RuntimeError(f"MCP stdout 已关闭（code={proc.poll()}）")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and message.get("id") == request_id:
                return message

    call_result: dict[str, Any] = {}
    error = ""
    try:
        send({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "trina", "version": __version__},
            },
        })
        initialized = receive(1)
        if initialized.get("error"):
            raise RuntimeError(f"MCP initialize 失败: {initialized['error']}")
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        send({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        })
        response = receive(2)
        if response.get("error"):
            raise RuntimeError(f"MCP tools/call 失败: {response['error']}")
        call_result = response.get("result") if isinstance(response.get("result"), dict) else {}
    except (RuntimeError, TimeoutError, BrokenPipeError, OSError) as exc:
        error = str(exc)
    finally:
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1.0)
        stdout_thread.join(timeout=1.0)
        stderr_thread.join(timeout=1.0)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()

    duration = time.monotonic() - started
    stderr = "".join(stderr_parts)[-4000:]
    ok = not error and not bool(call_result.get("isError"))
    if call_result.get("isError") and not error:
        error = _mcp_text(call_result) or "MCP 工具返回错误"
    digest = hashlib.sha256(json.dumps(call_result, ensure_ascii=False, sort_keys=True).encode("utf-8", "replace")).hexdigest()
    ctx.audit.append(
        "external.mcp",
        tool=tool_name,
        decision="allow",
        command=" ".join(argv),
        target=target,
        ok=ok,
        error=error[:300],
        duration=round(duration, 3),
        output_digest=digest,
    )
    return McpCall(
        ok=ok,
        result=call_result,
        error=error,
        stderr=stderr,
        command=" ".join(argv),
        duration=duration,
    )


def _mcp_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """只继承启动运行时所需环境，避免把宿主 API Key 交给第三方能力。"""
    allowed = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
        "HOME", "USERPROFILE", "LANG", "LC_ALL", "PYTHONPATH", "VIRTUAL_ENV",
        "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH",
    }
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    result["PYTHONUNBUFFERED"] = "1"
    for key, value in (extra or {}).items():
        if not key or "\x00" in key or "\x00" in str(value):
            raise ValueError("MCP 环境变量非法")
        result[str(key)] = str(value)
    return result


def _mcp_text(result: dict[str, Any]) -> str:
    parts = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n".join(part for part in parts if part)


__all__ = ["ExternalRun", "McpCall", "have", "installed", "run_external", "run_mcp_stdio", "which"]
