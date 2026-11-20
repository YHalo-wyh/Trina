"""命令行编码工具：前台执行、工作区读取、原子写入与精确编辑。"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .external import run_external
from .registry import ParamSpec, Tool, ToolResult, ToolSpec


_TEXT_LIMIT = 1_000_000
_FLAG_RE = re.compile(r"flag\{[^\r\n}]{1,256}\}", re.IGNORECASE)
_VERIFIER_NAME_RE = re.compile(
    r"(?:^|[\s/\\'\"(\[])"
    r"(?:verifier|validator|verify(?:[_-]?flag)?|check(?:er)?(?:[_-]?flag)?|validate(?:[_-]?flag)?)(?:\.py)?"
    r"(?:$|[\s/\\'\"),.;:}])",
    re.IGNORECASE,
)


def _command(argv: list[str]) -> tuple[str, list[str]]:
    """返回实际子命令名和参数；兼容 ``busybox find``。"""
    name = Path(argv[0]).name.lower()
    if name in {"busybox", "busybox.exe"} and len(argv) > 1:
        return argv[1].lower(), argv[2:]
    return name, argv[1:]


def _is_unbounded_find(argv: list[str]) -> bool:
    """识别以文件系统根目录为搜索根的 find，避免把整机扫描交给超时回收。"""
    name, args = _command(argv)
    if name not in {"find", "find.exe"}:
        return False
    roots: list[str] = []
    for item in args:
        if item == "--" or (not roots and item in {"-H", "-L", "-P"}):
            continue
        if item.startswith("-") or item in {"!", "("}:
            break
        roots.append(item)
    return "/" in roots


def _authority_verifier_facts(argv: list[str], stdout: str, stderr: str, *, exit_code: int | None,
                              evidence_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """识别本地题目验证器的确定性回执，不把普通命令输出当成成功证明。

    只有命令参数明确包含 verifier/validator/verify/check/checker/validate 入口、进程正常退出，
    并且输出存在独立的 ``OK`` 行时才登记权威验证结果。完整命令和输出仍保存在原始证据中。
    """
    command_text = " ".join(argv)
    verifier_match = _VERIFIER_NAME_RE.search(command_text)
    direct_execution = re.search(
        r"\b(?:python(?:\d+(?:\.\d+)?)?|pypy(?:\d+(?:\.\d+)?)?)(?:\.exe)?\s+[^\r\n]*"
        r"(?:verifier|validator|verify(?:[_-]?flag)?|check(?:er)?(?:[_-]?flag)?|validate(?:[_-]?flag)?)(?:\.py)?",
        command_text,
        re.IGNORECASE,
    )
    embedded_execution = "subprocess.run" in command_text or "subprocess.call" in command_text
    if not verifier_match or not (direct_execution or embedded_execution) or exit_code != 0:
        return {}, {}
    combined = "\n".join((stdout or "", stderr or ""))
    passed = bool(re.search(r"(?m)^\s*OK\s*$", combined))
    status = "passed" if passed else "failed"
    candidates = list(dict.fromkeys(_FLAG_RE.findall(command_text + "\n" + combined)))
    verification = {
        "status": status,
        "verifier": command_text[:600],
        "evidence_id": evidence_id,
        "flag_candidates": candidates[:8],
    }
    facts = {"goal_verifications": [verification]}
    if passed and candidates:
        facts["flags"] = [{"value": value, "source": "authority_verifier", "evidence_id": evidence_id}
                           for value in candidates]
    metadata = {"authority_verifier": verification}
    return facts, metadata


def _path(ctx, raw: str, root: str = "project", *, for_write: bool = False) -> Path:
    """所有文件路径统一交给 ScopeGuard 裁决，禁止跨项目/跨任务逃逸。"""
    if not raw or "\x00" in raw:
        raise ValueError("path 不能为空且不能包含 NUL")
    base = ctx.workspace if root == "run" else Path.cwd()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = base / candidate
    return Path(ctx.guard.check_workspace_path(str(candidate), ctx.workspace, ctx.config.runtime_root, for_write=for_write))


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
            "working_directory": ParamSpec("str", "可选的明确工作目录，例如已创建的 worktree；经过工作区护栏", required=False, default=""),
            "target": ParamSpec("str", "可选目标，用于审计与限速", required=False, default=""),
        },
        produces=["files"],
    )

    def run(self, ctx, argv, timeout=120, input="", cwd="project", target="", working_directory=""):
        if not argv or not all(isinstance(arg, str) and arg for arg in argv):
            return ToolResult(ok=False, error="argv 必须为非空字符串数组")
        if not 0 < float(timeout) <= 600:
            return ToolResult(ok=False, error="timeout 必须在 (0,600] 内")
        if len(input.encode("utf-8")) > 65_536:
            return ToolResult(ok=False, error="input 超过 64KiB")
        directory = Path.cwd() if cwd == "project" else ctx.workspace
        if working_directory:
            directory = _path(ctx, working_directory, for_write=True)
        if not directory.is_dir():
            return ToolResult(ok=False, error="执行目录不存在或不是目录")
        if _is_unbounded_find(argv):
            return ToolResult(
                ok=False,
                category="invalid",
                summary="已拒绝无界全盘搜索",
                error="find / 会扫描整个文件系统并产生长时间无效等待",
                data={"cwd": str(directory), "workspace": str(ctx.workspace)},
                next_hints=["先查看当前工作目录和任务合同给出的附件路径", "只对已知的具体目录执行 find"],
            )
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
        hints = []
        if call.timed_out:
            hints = [
                "不要原样重试或扩大搜索范围",
                f"先检查执行目录 {directory} 与任务合同中的具体附件路径，再缩小命令范围",
            ]
        import json
        import uuid
        evidence_id = "cmd-" + uuid.uuid4().hex
        transcript = json.dumps({"argv": argv, "cwd": str(directory), "exit_code": call.code,
                                 "stdout": call.stdout, "stderr": call.stderr}, ensure_ascii=False)
        artifact = ctx.save_artifact(evidence_id + ".json", transcript)
        verifier_facts, verifier_metadata = _authority_verifier_facts(
            argv, call.stdout or "", call.stderr or "", exit_code=call.code, evidence_id=evidence_id,
        )
        authority = verifier_metadata.get("authority_verifier")
        summary = f"命令完成，退出码 {call.code}" if call.ok else (error or "命令执行失败")
        key_findings: list[str] = []
        if authority:
            if authority["status"] == "passed":
                summary += "；权威验证器回执 OK"
                key_findings.append("权威验证器已通过，结果已写入事实库")
            else:
                key_findings.append("检测到验证器入口，但没有独立 OK 回执")
        return ToolResult(
            ok=call.ok,
            category="timed_out" if call.timed_out else "",
            summary=summary,
            error=error,
            command=call.command,
            data={"exit_code": call.code, "stdout": output, "stderr": stderr, "cwd": str(directory), "evidence_id": evidence_id},
            evidence=[{"id": evidence_id, "kind": "command", "path": str(artifact), "sha256": _digest(transcript),
                       "exit_code": call.code, "target": target, "successful": call.ok}],
            facts=verifier_facts,
            metadata=verifier_metadata,
            key_findings=key_findings,
            next_hints=hints,
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


class WorkspaceReadMany(Tool):
    """一次读取多个已知短文本文件，减少模型为独立读取反复往返。"""

    spec = ToolSpec(
        name="workspace_read_many",
        summary="一次批量读取多个项目或任务目录文本文件；适合 CTF 题面、源码和验证器的首轮检查",
        category="workspace",
        action_level="detect",
        params={
            "paths": ParamSpec("list", "相对路径或允许范围内的绝对路径，最多 12 个", required=False, default=[]),
            "path": ParamSpec("str", "兼容单文件读取的相对路径；批量场景优先使用 paths", required=False, default=""),
            "line_count": ParamSpec("int", "每个文件最多返回行数", required=False, default=240),
            "root": ParamSpec("str", "相对路径基准", required=False, default="project", enum=["project", "run"]),
        },
    )

    def run(self, ctx, paths=None, path="", line_count=240, root="project"):
        if not paths and path:
            paths = [path]
        if not isinstance(paths, list) or not 1 <= len(paths) <= 12:
            return ToolResult(ok=False, error="paths 需要1..12个文件路径")
        if not 1 <= int(line_count) <= 1000:
            return ToolResult(ok=False, error="line_count 必须在 [1,1000]")
        chunks: list[str] = []
        files: list[dict[str, Any]] = []
        try:
            for raw_path in paths:
                resolved = _path(ctx, str(raw_path), root)
                if not resolved.is_file():
                    return ToolResult(ok=False, error=f"文件不存在或不是普通文件: {raw_path}")
                if resolved.stat().st_size > _TEXT_LIMIT:
                    return ToolResult(ok=False, error=f"文件超过 1MiB: {raw_path}")
                lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
                selected = lines[:int(line_count)]
                numbered = "\n".join(f"{index + 1:>6}  {line}" for index, line in enumerate(selected))
                chunks.append(f"## {raw_path}\n{numbered}")
                files.append({"path": str(raw_path), "total_lines": len(lines), "returned_lines": len(selected)})
        except (OSError, UnicodeError, ValueError) as exc:
            return ToolResult(ok=False, error=str(exc))
        return ToolResult(
            summary=f"批量读取 {len(files)} 个文件",
            data={"text": "\n\n".join(chunks), "files": files, "count": len(files)},
        )


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
            resolved = _path(ctx, path, root, for_write=True)
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
            resolved = _path(ctx, path, root, for_write=True)
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


from .git import WorkspaceGit
from .search import WorkspaceSearch


TOOLS = [TerminalExec(), WorkspaceRead(), WorkspaceReadMany(), WorkspaceWrite(), WorkspaceEdit(), WorkspaceSearch(), WorkspaceGit()]
