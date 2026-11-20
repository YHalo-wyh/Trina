"""Rust 原生只读工具桥接。

Rust 子进程只处理已经由 Python ScopeGuard 裁决过的路径。命令执行、网络、
审计和动作等级仍由现有 ToolRegistry 与 scope.py 控制，原生二进制缺失时自动回退。
"""
from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path
from typing import Any

def binary_path() -> str | None:
    override = os.environ.get("TRINA_NATIVE_BIN", "").strip()
    candidates = [Path(override)] if override else []
    root = Path(__file__).resolve().parent.parent / "native" / "target"
    if platform.system() == "Windows":
        candidates.extend([root / "release" / "trina-native.exe", root / "debug" / "trina-native.exe"])
        named = shutil.which("trina-native.exe")
    else:
        candidates.extend([root / "release" / "trina-native", root / "debug" / "trina-native"])
        named = shutil.which("trina-native")
    if named:
        candidates.append(Path(named))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def native_read(ctx: Any, path: Path, start_line: int, line_count: int) -> tuple[str, int] | None:
    binary = binary_path()
    if not binary:
        return None
    from .tools.external import run_external

    call = run_external([binary, "read", str(path), str(start_line), str(line_count)], ctx,
                        tool="trina_native_read", required_level=ctx.effective_level,
                        timeout=5, cwd=str(path.parent), stream_output=False)
    if not call.ok:
        return None
    lines = (call.stdout or "").splitlines()
    total = int(lines[0].split("=", 1)[1]) if lines and lines[0].startswith("@total=") else 0
    return "\n".join(lines[1:]), total


def native_search(ctx: Any, root: Path, query: str, max_results: int, case_sensitive: bool) -> list[dict[str, Any]] | None:
    binary = binary_path()
    if not binary:
        return None
    from .tools.external import run_external

    call = run_external([binary, "search", str(root), query, str(max_results), "1" if case_sensitive else "0"], ctx,
                        tool="trina_native_search", required_level=ctx.effective_level,
                        timeout=20, cwd=str(root), stream_output=False)
    if not call.ok:
        return None
    matches: list[dict[str, Any]] = []
    for row in (call.stdout or "").splitlines():
        parts = row.split("\t", 3)
        if len(parts) != 4:
            continue
        path, line, column, text_value = parts
        matches.append({"path": path, "line": int(line), "column": int(column),
                        "text": text_value.replace("\\t", "\t").replace("\\r", "\r")[:1000]})
    return matches


def native_write(ctx: Any, path: Path, content: str) -> bool | None:
    binary = binary_path()
    if not binary or len(content.encode("utf-8")) > 128 * 1024:
        return None
    from .tools.external import run_external

    call = run_external([binary, "write", str(path), content], ctx,
                        tool="trina_native_write", required_level=ctx.effective_level,
                        timeout=5, cwd=str(path.parent), stream_output=False)
    return bool(call.ok)


def native_edit(ctx: Any, path: Path, old: str, new: str, expected: int) -> bool | None:
    binary = binary_path()
    if not binary or max(len(old.encode("utf-8")), len(new.encode("utf-8"))) > 128 * 1024:
        return None
    from .tools.external import run_external

    call = run_external([binary, "edit", str(path), old, new, str(expected)], ctx,
                        tool="trina_native_edit", required_level=ctx.effective_level,
                        timeout=5, cwd=str(path.parent), stream_output=False)
    return bool(call.ok)
