"""命令行编码工具：前台执行、工作区读取、原子写入与精确编辑。"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .external import run_external
from .registry import ParamSpec, Tool, ToolResult, ToolSpec


_TEXT_LIMIT = 1_000_000


def _path(ctx, raw: str, root: str = "project") -> Path:
    """所有文件路径统一交给 ScopeGuard 裁决，禁止跨项目/跨任务逃逸。"""
    if not raw or "\x00" in raw:
        raise ValueError("path 不能为空且不能包含 NUL")
    base = ctx.workspace if root == "run" else Path.cwd()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = base / candidate
    return Path(ctx.guard.check_workspace_path(str(candidate), ctx.workspace, ctx.config.runtime_root))


def _digest(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".trina-tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


class TerminalExec(Tool):
    spec = ToolSpec(
        name="terminal_exec",
        summary="在项目或本次运行目录前台执行 argv 命令，返回真实退出码与标准输出；适合编译、测试和代码检查",
        category="utility",
        action_level="exploit",
        params={
            "argv": ParamSpec("list", "程序及参数数组，不经过 shell"),
            "timeout": ParamSpec("float", "最长秒数", required=False, default=120),
            "input": ParamSpec("str", "可选标准输入，最多 64KiB", required=False, default=""),
            "cwd": ParamSpec("str", "执行目录", required=False, default="project", enum=["project", "run"]),
            "target": ParamSpec("str", "可选目标，用于审计与限速", required=False, default=""),
        },
        produces=["files"],
    )

    def run(self, ctx, argv, timeout=120, input="", cwd="project", target=""):
        if not argv or not all(isinstance(arg, str) and arg for arg in argv):
            return ToolResult(ok=False, error="argv 必须为非空字符串数组")
        if not 0 < float(timeout) <= 600:
            return ToolResult(ok=False, error="timeout 必须在 (0,600] 内")
        if len(input.encode("utf-8")) > 65_536:
            return ToolResult(ok=False, error="input 超过 64KiB")
        directory = Path.cwd() if cwd == "project" else ctx.workspace
        call = run_external(
            argv,
            ctx,
            tool=self.spec.name,
            target=target,
            required_level=ctx.effective_level,
            timeout=float(timeout),
            input_data=input or None,
            cwd=str(directory),
        )
        error = call.error or (f"退出码 {call.code}" if call.code not in (0, None) else "")
        output = (call.stdout or "")[-12_000:]
        stderr = (call.stderr or "")[-4_000:]
        return ToolResult(
            ok=call.ok,
            category="timed_out" if call.timed_out else "",
            summary=f"命令完成，退出码 {call.code}" if call.ok else (error or "命令执行失败"),
            error=error,
            command=call.command,
            data={"exit_code": call.code, "stdout": output, "stderr": stderr, "cwd": str(directory)},
        )


class WorkspaceRead(Tool):
    spec = ToolSpec(
        name="workspace_read",
        summary="按 UTF-8 读取项目或本次运行目录中的文本文件，可限定行号",
        category="workspace",
        action_level="detect",
        params={
            "path": ParamSpec("str", "相对路径或允许范围内的绝对路径"),
            "start_line": ParamSpec("int", "起始行（1-based）", required=False, default=1),
            "line_count": ParamSpec("int", "最多返回行数", required=False, default=240),
            "root": ParamSpec("str", "相对路径基准", required=False, default="project", enum=["project", "run"]),
        },
    )

    def run(self, ctx, path, start_line=1, line_count=240, root="project"):
        try:
            resolved = _path(ctx, path, root)
            if not resolved.is_file():
                return ToolResult(ok=False, error="文件不存在或不是普通文件")
            if resolved.stat().st_size > _TEXT_LIMIT:
                return ToolResult(ok=False, error="文件超过 1MiB，请使用命令行工具做分段检查")
            if int(start_line) < 1 or not 1 <= int(line_count) <= 1000:
                return ToolResult(ok=False, error="start_line 必须大于 0，line_count 必须在 [1,1000]")
            lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
            start = int(start_line) - 1
            selected = lines[start:start + int(line_count)]
            numbered = "\n".join(f"{start + index + 1:>6}  {line}" for index, line in enumerate(selected))
            return ToolResult(
                summary=f"读取 {resolved.name} 第 {start + 1}-{start + len(selected)} 行",
                data={"path": str(resolved), "text": numbered, "total_lines": len(lines)},
            )
        except (OSError, ValueError) as exc:
            return ToolResult(ok=False, error=str(exc))


class WorkspaceWrite(Tool):
    spec = ToolSpec(
        name="workspace_write",
        summary="在项目或本次运行目录原子创建/覆盖 UTF-8 文本文件",
        category="workspace",
        action_level="exploit",
        params={
            "path": ParamSpec("str", "相对路径或允许范围内的绝对路径"),
            "content": ParamSpec("str", "完整文件内容，最多 1MiB"),
            "root": ParamSpec("str", "相对路径基准", required=False, default="project", enum=["project", "run"]),
        },
        produces=["files"],
    )

    def run(self, ctx, path, content, root="project"):
        try:
            if len(content.encode("utf-8")) > _TEXT_LIMIT:
                return ToolResult(ok=False, error="content 超过 1MiB")
            resolved = _path(ctx, path, root)
            before = resolved.read_text(encoding="utf-8", errors="replace") if resolved.is_file() else ""
            _atomic_write(resolved, content)
            ctx.audit.append(
                "workspace.write", decision="allow", path=str(resolved), bytes=len(content.encode("utf-8")),
                before_sha256=_digest(before), after_sha256=_digest(content),
            )
            return ToolResult(summary=f"已写入 {resolved}", data={"path": str(resolved), "bytes": len(content.encode('utf-8'))})
        except (OSError, ValueError) as exc:
            return ToolResult(ok=False, error=str(exc))


class WorkspaceEdit(Tool):
    spec = ToolSpec(
        name="workspace_edit",
        summary="用精确文本匹配修改项目文件；默认要求旧文本只出现一次，避免误改",
        category="workspace",
        action_level="exploit",
        params={
            "path": ParamSpec("str", "相对路径或允许范围内的绝对路径"),
            "old": ParamSpec("str", "必须存在的原文本"),
            "new": ParamSpec("str", "替换后的文本"),
            "expected": ParamSpec("int", "期望匹配次数", required=False, default=1),
            "root": ParamSpec("str", "相对路径基准", required=False, default="project", enum=["project", "run"]),
        },
        produces=["files"],
    )

    def run(self, ctx, path, old, new, expected=1, root="project"):
        try:
            resolved = _path(ctx, path, root)
            if not resolved.is_file():
                return ToolResult(ok=False, error="文件不存在或不是普通文件")
            if not old:
                return ToolResult(ok=False, error="old 不能为空")
            source = resolved.read_text(encoding="utf-8", errors="strict")
            matches = source.count(old)
            if matches != int(expected) or int(expected) < 1:
                return ToolResult(ok=False, error=f"匹配次数为 {matches}，期望 {expected}；未修改")
            updated = source.replace(old, new, int(expected))
            if len(updated.encode("utf-8")) > _TEXT_LIMIT:
                return ToolResult(ok=False, error="修改后文件超过 1MiB")
            _atomic_write(resolved, updated)
            ctx.audit.append(
                "workspace.edit", decision="allow", path=str(resolved), replacements=int(expected),
                before_sha256=_digest(source), after_sha256=_digest(updated),
            )
            return ToolResult(summary=f"已修改 {resolved}（{expected} 处）", data={"path": str(resolved), "replacements": int(expected)})
        except (OSError, UnicodeError, ValueError) as exc:
            return ToolResult(ok=False, error=str(exc))


TOOLS = [TerminalExec(), WorkspaceRead(), WorkspaceWrite(), WorkspaceEdit()]
