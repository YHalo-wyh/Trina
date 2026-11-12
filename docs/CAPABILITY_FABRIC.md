# Trina Capability Fabric

Capability Fabric 是 Commander 与通用 Worker 之间的能力平面。它不把插件安装成某种固定 Agent，而是在任务开始时发现最小能力集合、签发任务级租约、通过现有工具执行链调用，并只向 Commander 发布带来源的证据索引。

## 核心边界

运行时只新增三个模块：

| 模块 | 只负责什么 | 明确不负责什么 |
|---|---|---|
| `trina/capabilities/catalog.py` | 统一 Capability 模型、目录发现、意图匹配、版本快照 | 不执行、不授权 |
| `trina/capabilities/broker.py` | 依赖与策略过滤、租约、调用路由、证据索引 | 不直接启动进程、不合并 Worker 对话 |
| `trina/capabilities/runtime.py` | 把租约能力投影成 `ToolRegistry` 工具，调用既有 Tool、CLI 或 MCP stdio | 不建立第二套审计/护栏/状态系统 |

执行关系固定为：

```text
Commander.resolve(intent, constraints)
  → Fabric.lease(worker, task, ttl, pinned snapshot)
    → 通用 Worker + 本任务 ToolRegistry + JIT Guidance
      → ToolRegistry.execute()
        → ScopeGuard / AuditLog / timeout / ToolResult
          → Fabric.publish() → 证据引用
  → Fabric.release() → 后续调用失效
```

Worker 不再有 Recon、Web、Pwn 等固定类型。它在当前任务里拿到什么能力，就临时具有什么专长；事实图、对话、假设和凭据仍按任务隔离。Commander 只转发结构化情报和 `evidence_refs`，接收方必须重新验证。

## Capability 清单

现有 `ToolRegistry` 工具会自动投影为 `tool.<tool_name>` 能力。外部能力放在以下任一目录，无需修改 Commander 或 Worker 核心代码：

- `<runtime_root>/capabilities`
- `~/.trina/capabilities`
- 环境变量 `TRINA_CAPABILITY_DIRS` 指定的一个或多个目录

目录会递归发现 `.yaml`、`.yml` 和 `.json` 清单。示例：

```yaml
id: ctf.pwn.debug
version: 1.0.0
description: Debug and analyze ELF binaries
provides:
  - binary.debug
  - binary.crash_analysis

interface:
  name: pwn_debug
  params:
    binary:
      type: str
      required: true
      description: Worker 工作目录中的 ELF 路径

guidance: ./SKILL.md

runtime:
  type: mcp
  command: [python, ./server.py]
  tool: debug_elf
  protocol: 2025-11-25

policy:
  action_level: detect
  filesystem: workspace
  path_params: [binary]
  timeout: 120

requires:
  tools: [gdb, readelf]
```

五个侧面有明确语义：

| 字段 | 语义 |
|---|---|
| `description` / `provides` | Catalog 用于匹配意图的能力说明和稳定标签 |
| `interface` | 对模型暴露的工具名、参数类型和必填约束 |
| `guidance` | 只在能力被租用时读取的操作知识；相对路径不能越出清单目录 |
| `runtime` | `tool`、`cli` 或 `mcp`；命令使用 argv，不经过 shell |
| `policy` / `requires` | 动作等级、工作目录、路径参数、超时和外部依赖 |

未知顶层字段、重复 ID、非法运行时、缺失版本或说明会在发现阶段拒绝。任务快照固定 `id + version + digest + source`；恢复时任一清单漂移或依赖失效都会失败关闭，不会悄悄换成新版本。

## 三个核心操作

```python
caps = fabric.resolve(intent, constraints)
lease = fabric.lease(worker_id, task_id, intent, constraints=constraints)
result = fabric.invoke(capability_id, arguments, {"lease_id": lease.id, "ctx": tool_context})
fabric.release(lease.id)
```

- `resolve()`：按 ID、`provides`、工具类别或任务意图发现候选，再过滤禁用项、动作等级、依赖和运行时可用性。
- `lease()`：绑定单个 Worker 与单个任务，设置 TTL，固定版本快照，并只加载本租约的参数 Schema 与 Guidance。
- `invoke()`：必须携带有效租约和 `ToolContext`，最终仍由 `ToolRegistry.execute()` 做参数、护栏、审计、超时和事实吸收。
- `publish()`：工具返回后自动生成内容摘要、事实摘要哈希和证据引用；不复制完整对话，也不把另一个任务的事实直接写入当前 `RunState`。

## 执行与安全语义

- 能力租约在调用前和返回后各检查一次；释放或过期后不能复用。
- CLI 与 MCP 只继承启动所需的最小环境变量，宿主 API Key 不会默认传给能力进程；清单只能显式添加所需变量。
- `filesystem: workspace` 把工作目录设为当前 Worker 工作区；声明在 `path_params` 的路径不得越出工作区。`filesystem: manifest` 仅用于可信的能力包目录。
- MCP stdio 使用逐行 JSON-RPC，初始化后调用 `tools/call`；超时、错误和正常返回都会回收本次子进程，stderr 只作诊断，结果写入 `external.mcp` 审计事件。
- Trina 当前没有操作系统级沙箱。上述机制是应用级租约、目录和环境边界；能力进程仍以启动 Trina 的宿主账号运行。强隔离需要在容器、低权限账号或外部 MCP 服务层部署。

## 首版机器验收

```powershell
python -m unittest tests.test_capabilities tests.test_swarm -v
```

| 场景 | 对应测试 | 判定 |
|---|---|---|
| 新增清单，无需修改 Agent 核心即可发现和租用；Guidance 为 JIT 加载 | `test_manifest_is_discovered_without_core_change_and_guidance_is_jit` | Catalog 能发现，租约快照固定，读取租约时的 Guidance |
| 同一通用 Worker 在任务间切换能力，旧租约立即失效 | `test_worker_switches_capability_and_released_lease_cannot_be_reused` | 旧调用抛出租约错误，新任务获得不同租约并执行新能力 |
| 两个 Worker 复用同一能力定义并分别发布证据索引 | `test_two_workers_reuse_definition_and_publish_independent_evidence_refs` | 快照相同、引用按任务隔离、无完整对话字段 |
| 外部 MCP 清单通过真实 stdio 进程与统一注册表执行 | `test_mcp_stdio_manifest_invokes_real_server_through_registry` | JSON-RPC 调用成功、审计链有效、未显式授权的宿主秘密未继承 |

## 首版刻意不做

- Marketplace、安装器和远程包解析。
- 复杂依赖求解、自动升级和多版本并存。
- 独立 Agent 插件类型、通用 Hook 总线或另一套 Tool API。
- 向量数据库式语义检索；首版采用确定性标签与轻量词项匹配。
- 长驻 MCP 进程池。当前 stdio 能力每次调用独立启动并回收；Worker 与能力定义可复用，但服务进程不会跨调用常驻。

这些限制避免第一版重新造完整插件生态。后续只有在三项验收稳定、真实任务数据证明有收益后，才考虑长驻服务复用和更高级的组合策略。
