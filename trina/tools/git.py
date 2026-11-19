"""仓库操作的统一工具入口：显式参数、真实回执和非强制 Git 操作。"""
from __future__ import annotations

from pathlib import Path

from .external import run_external
from .registry import ParamSpec, Tool, ToolResult, ToolSpec


READ_ACTIONS = {"status", "branches", "diff", "log", "worktrees", "remotes"}
GIT_ACTIONS = ["status", "branches", "diff", "log", "remotes", "stage", "unstage", "commit", "switch", "create", "delete",
               "worktrees", "worktree_add", "worktree_remove", "fetch", "pull", "push"]


def _value(value: str, label: str) -> str:
    if not value or value.startswith("-") or any(char in value for char in "\x00\r\n"):
        raise ValueError(f"{label} 不能为空、不能以 - 开头或包含控制字符")
    return value


def _status(output: str) -> dict:
    """读取 NUL 分隔状态，保留含空格及中文的路径与重命名前路径。"""
    records, changes, header = iter(output.split("\0")), [], ""
    for row in records:
        if row.startswith("## "):
            header = row[3:]
        elif len(row) >= 3:
            item = {"index": row[0], "worktree": row[1], "path": row[3:]}
            if row[0] in "RC" or row[1] in "RC":
                item["original_path"] = next(records, "")
            changes.append(item)
    lines = ["## " + header] if header else []
    lines.extend(f"{item['index']}{item['worktree']} {item['path']}" +
                 (f" ← {item['original_path']}" if item.get("original_path") else "") for item in changes)
    return {"branch_header": header, "changes": changes, "output": "\n".join(lines)}


def _worktrees(output: str) -> list[dict]:
    result, current = [], {}
    for field in output.split("\0"):
        if not field:
            if current:
                result.append(current)
                current = {}
            continue
        key, _, value = field.partition(" ")
        current[key] = value if value else True
    if current:
        result.append(current)
    return result


class WorkspaceGit(Tool):
    spec = ToolSpec(
        name="workspace_git",
        summary="操作真实 Git 仓库：状态、差异、历史、暂存/撤暂存、提交、分支、worktree 与显式远端同步；不强制覆盖本地修改",
        category="workspace", action_level="exploit",
        params={
            "action": ParamSpec("str", "仓库操作", enum=GIT_ACTIONS),
            "repository": ParamSpec("str", "仓库目录，默认当前项目", required=False, default="."),
            "branch": ParamSpec("str", "分支名；create/worktree_add 表示新分支，push/pull 默认当前分支", required=False, default=""),
            "paths": ParamSpec("list", "diff/stage/unstage 的字面文件路径；stage/unstage 必填", required=False, default=[]),
            "staged": ParamSpec("bool", "diff 是否查看暂存区", required=False, default=False),
            "message": ParamSpec("str", "commit 的提交说明；只提交当前暂存区，不自动 add", required=False, default=""),
            "base": ParamSpec("str", "create/worktree_add 的起点", required=False, default="HEAD"),
            "path": ParamSpec("str", "worktree_add/remove 的目录", required=False, default=""),
            "new_branch": ParamSpec("bool", "worktree_add 是否创建新分支；false 使用已有分支", required=False, default=True),
            "remote": ParamSpec("str", "fetch/pull/push 的已配置远端名称", required=False, default="origin"),
            "limit": ParamSpec("int", "log 条数，1–100", required=False, default=20),
        },
    )

    def required_level(self, params: dict) -> str:
        return "detect" if params.get("action") in READ_ACTIONS else "exploit"

    def run(self, ctx, action, repository=".", branch="", paths=None, staged=False, message="", base="HEAD",
            path="", new_branch=True, remote="origin", limit=20):
        try:
            if action not in GIT_ACTIONS:
                raise ValueError("未知 Git 操作")
            busy = ctx.extra.get("repository_busy")
            if action not in READ_ACTIONS and callable(busy) and busy():
                raise ValueError("其他 Worker 正在使用项目；等待任务结束后再修改仓库")
            write_scope = action not in READ_ACTIONS
            requested = Path(repository)
            directory = requested if requested.is_absolute() else Path.cwd() / requested
            directory = Path(ctx.guard.check_workspace_path(
                str(directory), ctx.workspace, ctx.config.runtime_root, for_write=write_scope))
            if not directory.is_dir():
                raise ValueError("仓库目录不存在")

            def execute(args, timeout=30, *, stream=False):
                return run_external(["git", "-c", "color.ui=false", "-c", "core.quotepath=false",
                                     "--no-optional-locks", "--literal-pathspecs", *args], ctx,
                                    tool=self.spec.name, required_level=ctx.effective_level,
                                    timeout=timeout, cwd=str(directory), stream_output=stream)

            root = execute(["rev-parse", "--show-toplevel"])
            if not root.ok:
                return self._result(root, str(directory))
            directory = Path(root.stdout.strip()).resolve()
            ctx.guard.check_workspace_path(str(directory), ctx.workspace, ctx.config.runtime_root, for_write=write_scope)
            selected = []
            for item in paths or []:
                if not isinstance(item, str) or not item or any(char in item for char in "\0\r\n"):
                    raise ValueError("paths 必须是非空字面路径数组")
                candidate = Path(item)
                resolved = (candidate if candidate.is_absolute() else directory / candidate).resolve()
                ctx.guard.check_workspace_path(
                    str(resolved), ctx.workspace, ctx.config.runtime_root, for_write=write_scope)
                try:
                    selected.append(resolved.relative_to(directory).as_posix())
                except ValueError as exc:
                    raise ValueError("文件路径必须位于当前仓库内") from exc
            if action in {"stage", "unstage"} and not selected:
                raise ValueError("stage/unstage 必须明确指定文件路径")
            if action in {"switch", "create", "delete"} or branch:
                _value(branch, "分支名")
                checked = execute(["check-ref-format", "--branch", branch])
                if not checked.ok:
                    raise ValueError("不是合法的本地分支名")
            if action in {"create", "worktree_add"}:
                _value(base, "起点")
            args = {
                "status": ["status", "--porcelain=v1", "-z", "--branch"],
                "branches": ["for-each-ref", "--format=%(refname:short)", "refs/heads/"],
                "remotes": ["remote"],
                "diff": ["diff", "--no-ext-diff", "--no-textconv", *(["--cached"] if staged else []), "--", *selected],
                "log": ["log", f"-n{limit}", "--date=iso-strict", "--format=%h %ad %s"],
                "stage": ["add", "--", *selected],
                "unstage": ["restore", "--staged", "--", *selected],
                "commit": ["commit", "-m", message],
                "switch": ["switch", "--no-guess", "--", branch],
                "create": ["switch", "--no-guess", "-c", branch, *([] if base == "HEAD" else [base])],
                "delete": ["branch", "-d", "--", branch],
                "worktrees": ["worktree", "list", "--porcelain", "-z"],
            }.get(action)
            if not 1 <= limit <= 100:
                raise ValueError("limit 必须在 1–100")
            if action == "commit" and (not message.strip() or "\0" in message):
                raise ValueError("提交说明不能为空或包含 NUL")
            if action == "unstage" and not execute(["rev-parse", "--verify", "HEAD"]).ok:
                args = ["rm", "--cached", "--", *selected]
            if action in {"worktree_add", "worktree_remove"}:
                _value(path, "worktree 路径")
                destination = Path(path)
                destination = (destination if destination.is_absolute() else directory / destination).resolve()
                ctx.guard.check_workspace_path(str(destination), ctx.workspace, ctx.config.runtime_root)
                if action == "worktree_add":
                    args = (["worktree", "add", str(destination), branch] if branch and not new_branch else
                            ["worktree", "add", *(["-b", branch] if branch else ["--detach"]), str(destination), base])
                else:
                    if destination == directory or Path.cwd().resolve().is_relative_to(destination):
                        raise ValueError("不能移除当前使用中的工作树")
                    args = ["worktree", "remove", "--", str(destination)]
            if action in {"fetch", "pull", "push"}:
                _value(remote, "远端名称")
                remotes = execute(["remote"])
                if not remotes.ok or remote not in remotes.stdout.splitlines():
                    raise ValueError("远端不存在；先查看仓库的 remote 配置")
                if action == "fetch":
                    args = ["fetch", "--", remote, *([branch] if branch else [])]
                else:
                    current = execute(["symbolic-ref", "--quiet", "--short", "HEAD"])
                    if not current.ok:
                        raise ValueError("detached HEAD 需要先切换本地分支")
                    selected_branch = branch or current.stdout.strip()
                    args = (["pull", "--ff-only", "--no-rebase", "--", remote, selected_branch] if action == "pull" else
                            ["push", "--", remote, f"HEAD:refs/heads/{selected_branch}"])
            result = execute(args, 120 if action in {"fetch", "pull", "push"} else 30,
                             stream=action in {"diff", "log", "commit", "fetch", "pull", "push"})
            outcome = self._result(result, str(directory))
            if result.ok and action == "status":
                outcome.data.update(_status(result.stdout))
                outcome.data["stdout"] = outcome.data["output"]
                outcome.summary = outcome.data["output"] or "工作区干净"
            elif result.ok and action == "branches":
                outcome.data["branches"] = result.stdout.splitlines()
            elif result.ok and action == "remotes":
                outcome.data["remotes"] = result.stdout.splitlines()
            elif result.ok and action == "worktrees":
                outcome.data["worktrees"] = _worktrees(result.stdout)
                outcome.data["output"] = "\n".join(f"{item.get('worktree', '')}  {item.get('branch', 'detached')}" for item in outcome.data["worktrees"])
                outcome.summary = outcome.data["output"]
                outcome.data["stdout"] = outcome.data["output"]
            return outcome
        except (ValueError, OSError) as exc:
            return ToolResult(ok=False, error=str(exc))

    @staticmethod
    def _result(result, repository: str) -> ToolResult:
        output = result.stdout[-24_000:]
        return ToolResult(ok=result.ok, summary=output.strip() or (result.stderr.strip() if result.ok else "Git 操作失败"),
                          error=result.error or (result.stderr.strip() if not result.ok else ""), command=result.command,
                          category="timed_out" if result.timed_out else "",
                          data={"repository": repository, "output": output, "stdout": output, "stderr": result.stderr[-4000:],
                                "exit_code": result.code, "truncated": len(result.stdout) > len(output)})
