# Trina Commander 与通用 Worker

`trina swarm` 是任务级运行入口。Commander 负责 DAG、优先级、相似性路由和任务恢复；Worker 是可复用的通用执行单元，由 Capability Fabric 在每个任务开始时动态绑定能力。每个任务仍有独立的 ReAct 上下文、事实图、工作目录、状态、审计和报告。

Capability 的模型、清单与租约边界见 [Capability Fabric](CAPABILITY_FABRIC.md)。

## 快速运行

```powershell
.\.venv\Scripts\python.exe -m trina swarm --target http://127.0.0.1:9099 --goal "验证目标并获取 flag" --workers 2
```

默认只创建一个 `main` 任务，不再预设 Recon、Web、Verify 三类 Worker。Fabric 根据目标和意图选择少量可用能力；Worker 在任务结束后释放能力租约，但模型后端实例可被后续任务复用。

## 自定义任务图

```yaml
tasks:
  - id: surface
    target: http://127.0.0.1:9099
    goal: 梳理应用入口与参数
    capabilities: [category.web]
    priority: 20
    max_steps: 8

  - id: binary
    target: ./attachments/challenge
    goal: 定位 ELF 崩溃原因
    capabilities: [binary.debug]
    priority: 10
    max_steps: 8

  - id: verify
    target: http://127.0.0.1:9099
    goal: 根据前序证据验证最有价值方向
    depends_on: [surface]
    max_steps: 12
```

```powershell
.\.venv\Scripts\python.exe -m trina swarm --manifest tasks.yaml --workers 2
```

`capabilities` 是可选约束，接受精确 Capability ID 或 `provides` 标签；留空时按任务意图发现。能力是否存在、依赖是否满足和运行时是否可用，在签发租约时判定。任务 ID、重复目标、缺失依赖和依赖环仍在启动前拒绝。

## 运行生命周期

1. Commander 选择依赖已完成的就绪任务并领取空闲通用 Worker。
2. Catalog 解析意图；Broker 过滤依赖和策略，固定 `id/version/digest/source` 快照。
3. Worker 获得任务级租约、临时 `ToolRegistry` 以及 JIT Guidance。
4. 所有动作仍经参数校验、`ScopeGuard`、审计、超时和 `ToolResult` 归一。
5. Fabric 发布事实摘要哈希和证据引用；完整事实只留在本任务 `RunState`。
6. 任务收尾后释放租约；旧 Registry 的后续调用会被拒绝，Worker 可领取另一组能力。

## 独立上下文与证据交接

Commander 在 Worker 每次决策前比较已完成任务和当前任务：

- 规范化目标和端口相同：`0.95`；主机相同但端口不同：`0.90`。
- 当前任务显式依赖来源任务：至少 `0.75`。
- 目标完全相同：至少 `0.70`；有效词项高度重合：至少 `0.65`。
- 低于 `0.60` 不转发。

转发包包含来源任务、运行编号、相似度、理由、结构化情报和 `evidence_refs`。Flag、聊天历史、假设台账和其他题目的 `RunState` 不会直接合并。接收 Worker 必须在当前目标重新调用能力验证，才可写入自己的事实图。

## 恢复与产物

```powershell
.\.venv\Scripts\python.exe -m trina swarm --resume runs\<swarm-id>\swarm.json
```

检查点版本 2 保存每个任务的能力快照和证据引用。恢复保留已完成任务，只把中断时为 `running` 的任务改回 `pending`；新租约必须精确匹配原快照，清单版本或摘要漂移会失败关闭。恢复粒度仍是整个任务，不恢复某个 ReAct 中间步骤或旧进程。

| 产物 | 路径 |
|---|---|
| Commander 检查点 | `runs/<swarm-id>/swarm.json` |
| Commander 哈希链审计 | `logs/<swarm-id>.jsonl` |
| Commander 汇总报告 | `reports/<swarm-id>_report.{md,html}` |
| Worker 状态 | `runs/<swarm-id>-<task-id>-<attempt>/state.json` |
| Worker 哈希链审计 | `logs/<swarm-id>-<task-id>-<attempt>.jsonl` |
| Worker 报告 | `reports/<swarm-id>-<task-id>-<attempt>_report.{md,html}` |

当前首版已实现任务并行、通用 Worker 复用、能力发现、租约回收、版本快照、JIT Guidance、MCP/CLI 接入、证据索引、Commander 定向交接和任务级恢复。尚未实现运行中人工拆分新任务、长驻 MCP 服务池、语义向量检索或自动能力组合优化。
