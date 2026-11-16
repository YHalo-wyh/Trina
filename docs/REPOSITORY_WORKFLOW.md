# 仓库、Shell 与工作区检索

Trina 的仓库能力是现有 Agent Runtime 的工具层能力。模型、斜杠命令和子 Worker 使用同一个 `ToolRegistry`，Git 与 Shell 命令均进入 `run_external`，路径继续经过工作区护栏和审计。

## 控制台入口

| 入口 | 用途 | 关键约束 |
|---|---|---|
| `/repo` | 状态、Diff、历史、远端、暂存、提交、fetch/pull/push | 暂存必须给文件；pull 仅 fast-forward；push 不强制 |
| `/branch` | 列出、创建、切换、删除已合并分支 | 不自动 stash/reset；冲突由 Git 拒绝 |
| `/worktree` 或 `/wt` | 列出、创建、移除工作树 | 目录必须位于当前项目允许范围；脏工作树不强制移除 |
| `/theme` | trina、contrast、mono 输出配色 | 只改变显示，不改变工具结果 |
| `/notify` | 开启、关闭或测试完成提醒 | 默认开启完成提醒；错误提醒由 YAML 单独配置 |

创建 worktree 不改变 Trina 进程的当前目录，也不把当前聊天和缓存搬进新目录。后续命令使用 `terminal_exec.working_directory` 指向该工作树；文件工具使用该目录内的明确路径。这样主会话的历史、项目标识和缓存前缀保持稳定，Git 分支仍由实际工作树决定。

## Agent 工具

`terminal_exec` 接受 argv 数组，不额外拼接 Shell 字符串。需要 Bash 管道、重定向或复合命令时，模型调用：

```json
{"argv": ["bash", "-lc", "rg -n 'pattern' src && python -m unittest"], "timeout": 120}
```

普通代码检索优先调用 `workspace_search`，避免每次自行拼接命令：

```json
{"query": "ToolRegistry", "path": ".", "globs": ["*.py"], "max_results": 100}
```

有 ripgrep 时结果的 `engine` 为 `ripgrep`；缺失时为 `python`。两条路径都限制在工作区，默认跳过版本库、依赖目录、超过 2 MiB 的文件和二进制文件。结果包含 `path`、`line`、`column` 和 `text`。

## 提醒与输出

完成提醒始终先在增强终端的右上角显示短时通知条。Kitty 使用 OSC 99，已知支持 legacy OSC 9 的终端使用 OSC 9；未知终端回退 BEL。非 TTY 和重定向输出不写控制序列。该通知只表示一轮 Agent 或任务执行已经到达终态，结果是否成功仍以状态、工具退出码和权威验证器为准。

语义配色以真实状态为来源：运行使用蓝色，成功使用绿色，警告使用琥珀色，失败使用红色；Diff 的元数据、删除行和新增行分别着色。`mono` 保留同一文字和排版。

## 已知限制

- 不自动复制未提交修改到新 worktree，也不自动合并、重置或强推。
- worktree 目录需位于当前项目允许的工作区范围。
- OSC 通知由终端能力和系统设置决定；未知终端仍有应用内通知条与 BEL。
- 内置检索降级面向文本源码，不代替 ripgrep 的完整正则和编码能力。

实现见 `trina/tools/git.py`、`trina/tools/search.py`、`trina/repo_ui.py`、`trina/notifications.py`，回归见 `tests/test_repository.py`、`tests/test_workspace_search.py`、`tests/test_notifications.py` 与 `tests/test_output_colors.py`。
