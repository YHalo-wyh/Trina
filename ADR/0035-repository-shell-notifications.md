# ADR-0035：仓库工作流、Shell 检索与完成提醒

- 状态：Accepted
- 日期：2026-11-16
- 版本：0.4.0

## 背景

Trina 已能编辑项目文件和切换本地分支，但仓库状态、差异审查、暂存、提交、远端同步及 worktree 没有统一入口。Agent 可以通过通用命令调用 `rg` 和 Bash，却缺少稳定 Schema，模型需要重复拼接命令。长任务结束后，操作者离开当前终端时也没有完成提醒。输出虽然使用粉紫主题，工具终态和 Diff 缺少一致的语义颜色。

Oh My Pi 的实现表明，仓库生命周期适合由交互入口调用统一执行器；完成事件应只在真正终态触发，并根据终端能力选择 OSC 9/99 或降级。Trina 保留现有 Python Runtime、ToolRegistry 和 prompt_toolkit，不引入插件管理器或第二套 Git/Shell 引擎。

## 决策

1. 扩展 `workspace_git`，所有 Git 子命令仍由 `run_external` 执行。读操作与写操作使用不同动作等级；文件采用 literal pathspec。暂存需要显式路径，提交只消费暂存区，pull 仅 fast-forward，push 不带 force，worktree 移除不带 force。
2. `/repo`、`/branch` 和 `/worktree` 只负责参数与选择界面，调用相同的注册工具。创建 worktree 不迁移当前会话；命令通过 `working_directory` 显式进入。
3. 新增 `workspace_search`。优先调用 ripgrep JSON 输出并解析为结构化位置；缺少 rg 时使用有界 Python 检索。两种实现共享工作区检查。
4. Bash 继续通过 `terminal_exec` 的 argv 调用，不新增字符串 Shell 执行器。Shell 超时、取消、审计和退出码语义保持一致。
5. 输出颜色按正文、运行、成功、警告、失败和 Diff 语义映射；切换主题只改变样式片段。
6. Agent 轮次和后台任务到达终态后显示应用内通知条。终端能力明确时发送 OSC 99 或 OSC 9，未知时发送 BEL；非 TTY 不发送。错误提醒与完成提醒独立配置，停止操作不伪装为完成。

## 影响

Agent 获得可审计的代码搜索和完整日常 Git 工作流，控制台与模型不会产生两套仓库语义。原有 `workspace_git` 名称保持兼容。新增工具 Schema 会增加少量稳定提示词长度；工作区检索减少重复 Bash 片段。通知协议是否形成系统桌面弹窗取决于终端，应用内通知条提供确定的可见反馈。

## 未采用方案

- 不在仓库工具中自动 stash、reset、merge 或 force push。这些动作可能覆盖用户工作，且无法从简短模型参数确认意图。
- 不在创建 worktree 后改变全局 `cwd`。当前聊天、历史索引、缓存前缀和其他 Worker 会因此产生隐式变化。
- 不依赖平台通知第三方库。终端协议和应用内通知可跨平台运行，保持依赖不变。

## 验证

`tests/test_repository.py` 使用真实临时仓库和本地裸远端检查中文/空格文件、冲突保护、分支、worktree 与非快进拒绝。`tests/test_workspace_search.py` 检查注册、结构化位置、路径拒绝和真实 Bash。`tests/test_notifications.py` 检查协议选择、控制字符净化、重定向纯净与配置；`tests/test_output_colors.py` 检查工具终态和 Diff 配色。
