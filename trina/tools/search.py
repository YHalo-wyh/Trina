"""工作区文本检索：优先 ripgrep，缺失时使用有界 Python 降级。"""
from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path
import shutil

from .external import run_external
from .registry import ParamSpec, Tool, ToolResult, ToolSpec


_SKIP_DIRS = {".git", ".hg", ".svn", ".venv", "node_modules", "__pycache__"}
_MAX_FILE_BYTES = 2 * 1024 * 1024


def _root(ctx, raw: str) -> Path:
    if not raw or "\x00" in raw:
        raise ValueError("path 不能为空且不能包含 NUL")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    resolved = Path(ctx.guard.check_workspace_path(str(candidate), ctx.workspace, ctx.config.runtime_root))
    if not resolved.is_dir():
        raise ValueError("检索路径不存在或不是目录")
    return resolved


def _matches_globs(path: Path, globs: list[str]) -> bool:
    rendered = path.as_posix()
    return not globs or any(fnmatch.fnmatch(rendered, pattern) or fnmatch.fnmatch(path.name, pattern)
                            for pattern in globs)


def _python_search(root: Path, query: str, *, globs: list[str], case_sensitive: bool,
                   fixed: bool, hidden: bool, max_results: int) -> list[dict]:
    """无 rg 时的有界降级；跳过大文件、二进制和依赖目录。"""
    import re

    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(re.escape(query) if fixed else query, flags)
    matches: list[dict] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in _SKIP_DIRS and (hidden or not name.startswith("."))]
        base = Path(current)
        for name in files:
            if len(matches) >= max_results:
                return matches
            if not hidden and name.startswith("."):
                continue
            path = base / name
            relative = path.relative_to(root)
            if not _matches_globs(relative, globs):
                continue
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    continue
                raw = path.read_bytes()
                if b"\x00" in raw[:8192]:
                    continue
                text = raw.decode("utf-8", errors="replace")
            except OSError:
                continue
            for line_no, line in enumerate(text.splitlines(), 1):
                for found in pattern.finditer(line):
                    matches.append({"path": relative.as_posix(), "line": line_no,
                                    "column": found.start() + 1, "text": line[:1000]})
                    if len(matches) >= max_results:
                        return matches
    return matches


def _parse_rg_json(stdout: str, root: Path, max_results: int) -> list[dict]:
    matches: list[dict] = []
    for line in stdout.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") != "match":
            continue
        data = record.get("data", {})
        raw_path = str((data.get("path") or {}).get("text", ""))
        path = Path(raw_path)
        if path.is_absolute():
            try:
                raw_path = path.relative_to(root).as_posix()
            except ValueError:
                raw_path = path.as_posix()
        else:
            raw_path = path.as_posix()
        text = str((data.get("lines") or {}).get("text", "")).rstrip("\r\n")[:1000]
        line_no = int(data.get("line_number") or 0)
        submatches = data.get("submatches") or [{}]
        for submatch in submatches:
            matches.append({"path": raw_path, "line": line_no,
                            "column": int(submatch.get("start") or 0) + 1, "text": text})
            if len(matches) >= max_results:
                return matches
    return matches


class WorkspaceSearch(Tool):
    spec = ToolSpec(
        name="workspace_search",
        summary="用 ripgrep 在当前项目内检索文本，返回文件、行号和列号；rg 缺失时自动有界降级",
        category="workspace",
        action_level="detect",
        params={
            "query": ParamSpec("str", "检索文本或正则表达式"),
            "path": ParamSpec("str", "项目内检索目录", required=False, default="."),
            "globs": ParamSpec("list", "可选文件 glob，例如 *.py 或 docs/**", required=False, default=[]),
            "fixed": ParamSpec("bool", "按字面量匹配", required=False, default=False),
            "case_sensitive": ParamSpec("bool", "区分大小写", required=False, default=False),
            "hidden": ParamSpec("bool", "包含隐藏文件，仍排除版本库和依赖目录", required=False, default=False),
            "max_results": ParamSpec("int", "最多返回结果数（1–500）", required=False, default=100),
        },
    )

    def run(self, ctx, query, path=".", globs=None, fixed=False, case_sensitive=False,
            hidden=False, max_results=100):
        try:
            if not isinstance(query, str) or not query or "\x00" in query:
                return ToolResult(ok=False, category="invalid", error="query 不能为空且不能包含 NUL")
            globs = list(globs or [])
            if any(not isinstance(item, str) or not item or "\x00" in item for item in globs):
                return ToolResult(ok=False, category="invalid", error="globs 必须是非空字符串数组")
            limit = int(max_results)
            if not 1 <= limit <= 500:
                return ToolResult(ok=False, category="invalid", error="max_results 必须在 [1,500] 内")
            root = _root(ctx, path)
            rg = shutil.which("rg")
            if rg:
                argv = [rg, "--json", "--max-filesize", "2M", "--max-columns", "1000",
                        "--max-columns-preview", "--no-messages"]
                argv += ["--fixed-strings"] if fixed else []
                argv += ["--case-sensitive"] if case_sensitive else ["--ignore-case"]
                argv += ["--hidden"] if hidden else []
                for pattern in globs:
                    argv.extend(["--glob", pattern])
                for skipped in sorted(_SKIP_DIRS):
                    argv.extend(["--glob", "!" + skipped + "/**"])
                argv.extend(["--", query, "."])
                call = run_external(argv, ctx, tool=self.spec.name, required_level=ctx.effective_level,
                                    timeout=20, cwd=str(root), stream_output=False)
                # rg: 0=命中，1=无命中，其他为执行错误。
                if call.code not in (0, 1):
                    return ToolResult(ok=False, error=call.error or call.stderr or f"rg 退出码 {call.code}",
                                      command=call.command, data={"exit_code": call.code, "engine": "ripgrep"})
                matches = _parse_rg_json(call.stdout or "", root, limit)
                engine, command = "ripgrep", call.command
            else:
                try:
                    matches = _python_search(root, query, globs=globs, case_sensitive=bool(case_sensitive),
                                             fixed=bool(fixed), hidden=bool(hidden), max_results=limit)
                except Exception as exc:  # 正则错误等输入问题
                    return ToolResult(ok=False, category="invalid", error=str(exc))
                engine, command = "python", ""
            rendered = "\n".join(f"{item['path']}:{item['line']}:{item['column']}: {item['text']}"
                                   for item in matches)
            return ToolResult(summary=f"{engine} 检索完成，命中 {len(matches)} 处", command=command,
                              data={"engine": engine, "root": str(root), "matches": matches,
                                    "text": rendered, "truncated": len(matches) >= limit})
        except (OSError, ValueError) as exc:
            return ToolResult(ok=False, category="invalid", error=str(exc))


TOOLS = [WorkspaceSearch()]
