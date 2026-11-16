"""仓库命令与菜单：只组装参数，执行统一委托 workspace_git。"""
from __future__ import annotations

import argparse

from .console_ui import ChoiceSpec, edit_field, select_option


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def parse_repo_args(args: list[str], mode: str = "repo") -> dict:
    parser = _Parser(prog="/" + mode, add_help=False)
    parser.add_argument("operation", nargs="?", default="list" if mode == "worktree" else "status")
    parser.add_argument("values", nargs="*")
    parser.add_argument("--repo", dest="repository", default=".")
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--base", default="HEAD")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--existing", action="store_true")
    parsed = parser.parse_intermixed_args(args)
    action, values = parsed.operation, parsed.values
    result = {"action": action, "repository": parsed.repository}
    if mode == "worktree":
        result["action"] = {"list": "worktrees", "add": "worktree_add", "remove": "worktree_remove"}.get(action, "")
        if action == "list" and not values:
            return result
        if action not in {"add", "remove"} or not 1 <= len(values) <= (2 if action == "add" else 1):
            raise ValueError("/worktree list；/worktree add <路径> [新分支] [--base 引用]；/worktree remove <路径>")
        result.update(path=values[0], branch=values[1] if len(values) > 1 else "", base=parsed.base,
                      new_branch=not parsed.existing)
    elif action in {"stage", "unstage", "diff"}:
        result.update(paths=values, staged=parsed.staged)
    elif action == "commit":
        if not values:
            raise ValueError('/repo commit "提交说明"')
        result["message"] = " ".join(values)
    elif action in {"fetch", "pull", "push"}:
        if len(values) > 2:
            raise ValueError("/repo fetch|pull|push [远端] [分支]")
        result.update(remote=values[0] if values else parsed.remote,
                      branch=values[1] if len(values) > 1 else parsed.branch)
    elif action == "log":
        if values:
            raise ValueError("/repo log [--limit 1–100]")
        result["limit"] = parsed.limit
    elif action in {"status", "branches", "worktrees", "remotes"}:
        if values:
            raise ValueError(f"/repo {action} 不接收位置参数")
    else:
        raise ValueError("未知仓库操作；输入 /repo 查看菜单")
    return result


def show_repository(execute, branches=None, worktrees=None) -> None:
    """菜单每次只读实际状态，网络与写入操作必须由用户选择。"""
    notice = ""
    while True:
        status = execute({"action": "status"})
        if not status.ok:
            return
        choices = [ChoiceSpec(key, label) for key, label in (
            ("diff", "工作区 Diff"), ("staged", "暂存区 Diff"), ("stage", "暂存文件"), ("unstage", "撤销暂存"),
            ("commit", "提交暂存区"), ("log", "提交历史"), ("branches", "本地分支"), ("worktrees", "Worktree 列表"),
            ("fetch", "Fetch：获取远端"), ("pull", "Pull：仅快进"), ("push", "Push：推送当前 HEAD"), ("back", "返回"))]
        selected = select_option("Repository", choices, subtitle=(status.summary[:1600] + "\n" + notice).strip())
        if selected in {None, "back"}:
            return
        if selected == "branches" and branches is not None:
            branches([])
            continue
        if selected == "worktrees" and worktrees is not None:
            worktrees()
            continue
        params = {"action": "diff" if selected == "staged" else selected}
        if selected == "staged":
            params["staged"] = True
        if selected in {"stage", "unstage"}:
            files = status.data.get("changes", [])
            options = [ChoiceSpec(item["path"], f"{item['index']}{item['worktree']}  {item['path']}") for item in files
                       if (item["index"] not in {" ", "?"} if selected == "unstage" else item["worktree"] != " " or item["index"] == "?")]
            path = select_option("选择文件", options, subtitle="每次操作一个文件；命令支持多个路径")
            if path is None:
                continue
            params["paths"] = [path]
        elif selected == "commit":
            try:
                params["message"] = edit_field("提交说明")
            except KeyboardInterrupt:
                continue
            if not params["message"].strip():
                continue
        elif selected in {"fetch", "pull", "push"}:
            remotes = execute({"action": "remotes"})
            remote = select_option("远端", [ChoiceSpec(name, name) for name in remotes.data.get("remotes", [])]) if remotes.ok else None
            if remote is None:
                continue
            params["remote"] = remote
        result = execute(params)
        notice = (result.summary or "完成")[-1200:] if result.ok else result.error
        if selected in {"diff", "staged", "log", "branches", "worktrees"}:
            # 结果已写入主会话；返回查看可滚动的完整工具窗口。
            return


def show_worktrees(execute) -> None:
    result = execute({"action": "worktrees"})
    if not result.ok:
        return
    entries = result.data.get("worktrees", [])
    selected = select_option("Worktrees", [ChoiceSpec(item["worktree"], f"{item.get('branch', 'detached')} · {item['worktree']}")
                                           for item in entries if item.get("worktree")]
                             + [ChoiceSpec("action:add", "+ 创建工作树"), ChoiceSpec("action:remove", "− 移除干净工作树")],
                             subtitle="选择目录查看状态；创建独立目录后，用 --repo 指向它继续操作")
    if selected is None:
        return
    if selected == "action:add":
        try:
            path = edit_field("工作树目录")
            branch = edit_field("新分支名（留空创建 detached 工作树）") if path else ""
        except KeyboardInterrupt:
            return
        if path:
            execute({"action": "worktree_add", "path": path, "branch": branch})
    elif selected == "action:remove":
        path = select_option("移除工作树", [ChoiceSpec(item["worktree"], item["worktree"]) for item in entries[1:] if item.get("worktree")],
                             subtitle="Git 只移除干净且未锁定的工作树")
        if path:
            execute({"action": "worktree_remove", "path": path})
    else:
        execute({"action": "status", "repository": selected})
