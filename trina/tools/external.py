"""外部专业工具的调用封装（nmap / nuclei / sqlmap / hydra / ffuf / gobuster ...）。

原则
----
1. **外部二进制优先，内置引擎兜底**：实训 WSL 环境常常没有安装这些工具，
   因此每个能力都必须有纯 Python 实现，外部工具只是「加速器」。
2. **shell=False**：永远用 argv 列表调用，杜绝命令注入与通配符意外展开。
3. **必经护栏**：`run_external()` 内先过 `guard.check_argv()`（含硬禁止清单），再执行、再审计。
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

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


def run_external(
    argv: Sequence[str],
    ctx: Any,
    *,
    tool: str = "",
    target: str = "",
    required_level: str = "detect",
    timeout: float | None = None,
    input_data: str | None = None,
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
        budget = min(budget, max(5.0, ctx.remaining()))

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


__all__ = ["ExternalRun", "have", "installed", "run_external", "which"]
