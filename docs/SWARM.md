# Trina Commander 与通用 Worker

`trina swarm` 是任务级 Agent Runtime。Commander 决定什么 Intent 可以执行；事件驱动 Planner 只在任务图、事实或终态变化时推进；Capability Fabric 决定 Worker 本轮能调用哪些能力；通用 Worker 通过独立 `PentestAgent` 执行。每题的完整消息、`RunState`、工作目录、审计和报告保持隔离，协作平面只共享结构化待办、事实候选、观察、失败及证据引用。

本轮架构取舍参考 [ARTEX](https://github.com/RuoJi6/ARTEX) 的 Planner 唤醒、Todo、Intent 生命周期和 Worker Trace 思路，但没有复制其 Go/PostgreSQL/前端栈。Trina 使用现有 Python 运行时、JSON 检查点、`ToolRegistry` 和 Capability Fabric，避免新增第二套执行边界。

## 快速运行

交互入口为 `/subworker`。任务树直接复用下面的 Swarm Runtime，支持后台运行、模型/思考强度配置、追加任务、暂停/继续、停止、查看证据和检查点恢复；详见 [子 Worker 面板](CLI_UI.md#子-worker-面板)。主会话用量和子任务总用量分别显示，不能把主会话账本复制到每个 Worker。

清单中的任务可填写 `model`、`reasoning_effort`、`profile`。优先级为任务配置、`subworker` 默认配置、主模型；选择供应商先载入其连接，再应用本层模型/档位。仅相同 LLM 配置的空闲后端优先复用，不相同则换后端；每项任务仍新建 Agent。有效模型身份固定在检查点，恢复不因主会话切模型而漂移；凭据由当前供应商存储解析，不写进任务快照。

```powershell
.\.venv\Scripts\python.exe -m trina swarm --target http://127.0.0.1:9099 --goal "验证目标并获取 flag" --workers 2
```

默认创建一个 `main` Intent。需要并行分支时提供清单；运行中也可通过 `SwarmRuntime.add_task()` 增加任务。Fabric 根据任务发现少量能力；编码和协作查询工具作为所有 Worker 的核心运行时能力提供。Worker 完成后释放能力租约，模型后端实例可复用，但前一题的 Agent、状态和工具视图不会复用。

## 五个协作部件

| 部件 | 当前行为 | 持久位置 |
|---|---|---|
| 事件驱动 Planner | 仅在 `intent.added/settled/recovered/timeout` 或 `facts.published` 等修订变化时增加规划轮；轮询不触发重规划 | `coordination.planner_rounds/revision` |
| 共享 Todolist | 保存目标、依赖、`waiting/ready/in_progress/终态` 和证据引用；Worker 轮换后仍存在 | `coordination.todo` |
| Intent 队列 | 按目标、目标描述和能力生成稳定签名去重；按优先级领取，使用租约和 deadline，恢复时回收在途任务 | `coordination.intents` |
| 双图 | 事实图只保存从真实 `RunState` 投影的资产、带证据发现和已验证凭据；探索图保存 Intent、依赖、状态、事实引用和锚点 | `coordination.facts/exploration` |
| Worker Trace | 索引工具观察、失败、Intent 生命周期和来源 Worker；可查询其他 Intent，但不复制完整对话 | `coordination.traces` |

Planner 当前是确定性的就绪队列调度器，不会为每个工具步骤调用主模型，也不会自动从任意模型文本生成无限分支。`add_task()` 是经过 DAG 校验和等价去重的动态入口；自动任务分解仍属于后续能力。

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

`capabilities` 接受精确 Capability ID 或 `provides` 标签；留空时按任务意图发现。启动前拒绝空图、重复 ID、等价 Intent、未知能力、非法依赖、依赖环及非法预算。队列再次接收等价任务时返回去重结果，不会启动第二份执行。

## 执行与唤醒顺序

```text
清单 / add_task
  → 事件修订增加
  → Planner 消费到最新 revision，更新共享 Todo
  → Intent Queue 返回依赖已完成的 ready 项
  → WorkerPool + Capability Lease + Intent Lease
  → 每任务新建 PentestAgent / ToolRegistry / RunState
  → 工具结果提交统一事件流
  → Commander 实时消费工具观察与状态，更新 Worker Trace 和事实图
  → 新事实以 anchor 连接探索图并唤醒 Planner
  → Intent settle 产生事件，唤醒下一轮 Planner
  → 先提交 runtime.checkpoint，再原子保存 swarm.json
```

Worker 的普通 ReAct 步骤只写留痕，不唤醒 Planner。一个任务完成、失败、发布新事实、恢复或新增 Intent 时才产生可调度事件。这使主调度成本与图变化相关，而不是与所有工具调用数线性绑定。

Worker 与 Commander 共用 `events.sqlite3`。Commander 在本线程验证任务、Worker 和尝试运行 ID 后消费事件，Worker 尚未返回时产生的真实事实即可进入协作平面。Future 完成与协作变更经同一事件流唤醒调度；DAG 依赖按任务终态解锁，重复事实不增加规划轮。见[统一运行事件](EVENTS.md)。

## 命令行编码能力

通用 Worker 始终拥有以下核心工具，不依赖领域能力匹配：

- `terminal_exec`：以前台 argv 形式执行命令，返回真实退出码、stdout、stderr 和耗时；通过 `run_external`，不使用 shell 字符串拼接。
- `workspace_read`：按行有界读取项目文件。
- `workspace_write`：原子创建或覆盖 UTF-8 文件，并记录前后哈希。
- `workspace_edit`：按期望匹配次数精确替换；不匹配时不改文件。
- `shared_context`、`shared_trace_search`、`shared_trace_get`：查看共享 Todo/双图摘要，检索或展开其他 Worker 的观察与失败。

路径必须通过 `ScopeGuard.check_workspace_path`；外部命令仍走统一审计、超时和 ToolResult 归一。后台长任务继续使用 `terminal_start/status/stop`。

## 独立上下文与经验交接

Commander 在当前任务与已结束任务之间计算相关性：规范化目标与端口相同权重最高，显式依赖次之，再比较目标词项。达到阈值后，简报可以包含：

- 来源明确的资产、带证据发现、已验证凭据和工件引用；
- 成功观察，以及非零退出、超时、解析失败等失败经验；
- 来源任务、运行 ID、状态、相关性和错误摘要。

Flag、完整聊天、开放假设和来源题目的 `RunState` 不进入简报。失败经验只说明什么已经尝试、实际发生了什么；它不自动证明当前任务也会失败。接收 Worker 必须重新调用本题工具，结果才能进入自己的事实图。

协作查询工具提供第二条按需路径：Worker 可以用关键词搜索所有其他 Intent 的 trace，再按 Intent 和 step 展开细节。Trace ID 使用单调序号，即使旧记录因上限被裁剪或运行恢复也不重复。

## 双图定义

事实图和探索图不能互相替代：

- `facts`：实际工具状态投影，只有活跃资产、带证据且为 `verified/exploited` 的发现、已验证凭据及内部 Flag 记录。跨题简报会过滤 Flag。
- `exploration.nodes`：Intent 节点与事实引用节点。
- `exploration.edges`：`enables` 表示依赖，`yields` 表示某 Intent 产出事实。
- `exploration.anchors`：事实来源锚点，用于复盘“哪次探索产生哪条事实”。

待办、猜测和未验证线索只留在探索侧；模型文字不会直接写入事实图。

## 超时、恢复与产物

```powershell
.\.venv\Scripts\python.exe -m trina swarm --resume runs\<swarm-id>\swarm.json
```

检查点版本 3 保存任务记录、能力快照和完整 coordination 状态。恢复保留终态任务；检查点中的 `running` Intent 释放旧 Worker/Intent 租约并回到 `pending`，下一次领取增加尝试号。运行中的 `PentestAgent` 由任务 `max_seconds` 收尾；`expire_leases()` 只用于确认没有活跃执行器后的租约回收，不能并发杀死任意自定义 Python executor。

| 产物 | 路径 |
|---|---|
| Commander 检查点（含 Todo、Intent、双图、Trace） | `runs/<swarm-id>/swarm.json` |
| 统一事件流与消费者游标 | `runs/<swarm-id>/events.sqlite3` |
| Commander 哈希链审计 | `logs/<swarm-id>.jsonl` |
| Commander 汇总报告 | `reports/<swarm-id>_report.{md,html}` |
| Worker 状态 | `runs/<swarm-id>-<task-id>-<attempt>/state.json` |
| Worker 哈希链审计 | `logs/<swarm-id>-<task-id>-<attempt>.jsonl` |
| Worker 报告 | `reports/<swarm-id>-<task-id>-<attempt>_report.{md,html}` |

恢复粒度仍是整个任务，不恢复某个 ReAct 中间请求或旧原生进程。检查点中的能力版本或摘要漂移时失败关闭。

检查点事件先于 JSON 提交。恢复优先读取较新的已提交事件快照，再消费未处理的尾部；失去原执行器的工具标记为 `interrupted`，事件重放不重新执行历史命令。观察消费者可按序号或命名确认游标接续。

## 当前边界

已实现：事件驱动调度、共享 Todo、Intent 去重/租约/恢复、双图、跨 Worker 观察与失败检索、任务留痕、并行 Worker、能力装配和任务级恢复。

尚未实现：LLM 自动生成和裁剪任意探索分支、跨运行向量经验库、运行中 Worker 对话接管/定向补充、长驻 MCP 服务池、跨 Worker 自动冲突裁决，以及同模型同预算的完整 A/B/C/D 协作收益实验。`/subworker` 现已提供运行中任务查看、队列追加、等待任务改配置及整场暂停/停止。

### 控制台与调度器的并发边界

控制台通过 `submit_task/configure_task` 投递命令，只有 Commander 线程校验和修改 DAG；Future 返回接受、等价去重或明确拒绝。普通渲染只读小型状态投影，不遍历事实图或读写检查点。停止得到 `cancelled`，不能记录为 `completed`；取消、超时和其他失败依赖都会阻止后继任务执行。旧版本缺少模型元信息的检查点仍按当时 TaskSpec/当前默认值补齐，已保存明确模型身份的新快照保持固定。

每题运行产物独立；工具的 `root=project` / `cwd=project` 仍指向同一个仓库，并不意味着文件系统隔离。并行修改同一项目文件需要操作者划分任务文件，当前没有自动合并或写冲突仲裁。控制台 `/branch` 在子任务运行时拒绝切换分支。

## 上下文与缓存生命周期

Worker 使用任务独立的状态差量和继承检查点；完整旧消息保存在对应运行目录的 context 下。相同 Core 和工具快照生成稳定缓存标识，当前题目、运行编号和计时不进入该标识。工具快照不同则进入请求条件变化诊断。后端对象复用不等于服务端 KV Cache 已共享；当前也不为维持缓存加载全部领域工具。供应商策略和验证边界见[缓存感知运行时](CACHE_RUNTIME.md)。
