# ADR-0044：工具控制中心与可见性解析视图

状态：Accepted / 已实施
日期：2026-11-24

## 背景

Trina 已有完整 `ToolRegistry`、按任务路由裁剪的模型 Schema、Capability 租约和运行统计，但它们分散在执行层。旧 `/tools` 只输出一段静态文本，不能回答当前 Agent 实际看见哪些工具、某工具为何隐藏、公开名称如何映射到内部标识，也不能把调用表现与工具定义放在一起查看。

ARTEX 的工具卡片证明“定义、绑定、调用次数同屏”很适合运维；Oh My Pi 的模型选择器证明大目录应使用虚拟化行、搜索、侧栏筛选和固定详情，而不是一次渲染全部卡片。Trina 采用这些交互原则，不复制它们的存储、前端或执行架构。

## 决定

1. 新增只读 `tool_catalog` 投影。它从真实 `ToolRegistry`、当前 `tool_allowlist`、任务路由、Agent 角色和 `RunState.steps` 生成目录行，不保存第二份工具状态。
2. `/tools` 空参数在增强终端打开工具控制中心。支持名称、说明、参数与来源搜索，以及可见、隐藏、不可用、类别和来源筛选；列表使用 `UIControl` 按视口取行。
3. 每项显示注册来源、动作等级、参数、当前可用性、会话调用/失败/p50/p95，并解释 `ToolRegistry → 路由 → Agent → 白名单 → 模型 Schema`。
4. `read`、`write`、`shell` 继续只是公开名称；执行和审计使用原内部标识。选择器确认只保留详情，不执行工具。
5. 显式 `/tools --json|--verbose|--all|--route` 保持 CLI 入口；JSON 增加可见性、来源和解析链字段。

## 边界

- 目录不得修改 handler、工具名、参数类型、必填项、动作等级、护栏或审计语义。
- “隐藏”表示没有进入当前模型 Schema，不表示工具从注册表删除；Capability/任务租约仍由现有运行时决定。
- 会话统计来自已经落入 `RunState.steps` 的真实执行，不把目录计数当作成功率或质量证明。
- 自定义工具编辑、MCP/Skill 创建和元数据覆盖不在本 ADR 内；后续若实现，只能保存描述/默认值/绑定覆盖，执行仍回到 `ToolRegistry.execute()`。

## 验证

- `tests/test_tool_catalog.py`：公开名、路由解释、统计和搜索筛选。
- `tests/test_tool_profile.py`：目录显示不能改变实际 Schema 白名单。
- `tests/test_registry.py`：执行单一收口与参数/护栏契约保持。
- `tests/test_terminal_ui.py`、`tests/test_console_session.py`：弹窗、命令目录和终端恢复。
