# ADR-0031：统一 Agent Runtime、任务状态与 Bench 调度

- 日期：2026-11-15
- 状态：已实施
- 关联：TR-04、TR-10、TR-12、TR-23

## 问题

此前任务 Agent 与 Chat 各自维护模型循环；原生响应虽然能包含多个 `tool_calls`，任务路径只执行第一个，其他调用仍占历史但没有实际推进。默认 CTF 路径还同时启用固定 PTES 阶段、假设调度和循环建议。Swarm 的 `TaskRecord` 与 Coordination Intent 分别保存任务终态，Bench 则在外层串行逐题运行。这些结构增加模型往返、状态同步和恢复分支。

## 决策

1. 新增唯一的 `AgentRuntime`。任务、对话和 Worker 只提供请求、动作执行、状态观察与收尾适配。原生响应中的调用按返回顺序全部处理；`_depends_on` 只能引用本批次更早的 1 基编号，依赖失败则跳过后继。每个 API `call_id` 都生成对应工具回执。`finish` 不能与工具动作同批混用。JSON 兼容协议继续保持单动作。
2. 批内顺序执行。多个调用仍逐项经过 AuthorizationController、ToolRegistry、ScopeGuard、审计和超时。不同 Worker 由 Worker Pool 并发；不在共享 `RunState` 上并发写入。
3. 默认 CTF 路径改为目标驱动：`run.phases=[]`、`run.hypothesis_scheduler=false`、`policy.stop_on_no_new_evidence=0`。Planner 与 HypothesisScheduler 保留为显式兼容配置。提示词分层默认开启。新增事实立即保存，其他状态默认每 5 个动作检查点一次。
4. `CoordinationRuntime.intents` 成为任务状态唯一来源。`TaskRecord` 绑定同一字典，仅保留旧属性 API；Todo、探索图和报告证据图是派生视图。Swarm 检查点升为 v4，删除重复 `records`；v1–v3 恢复时一次迁移。
5. Bench 只做平台适配。平台题目映射为 TaskSpec，由同一 SwarmRuntime 调度，默认 2 个 Worker、平台上限 3。每题使用独立控制面 HTTP 会话，容器在 `finally` 回收；只有平台 `submit.correct` 能把题目标记为成功。
6. Headless 核心的文本清洗移到 `trina.text`，状态投影不再导入终端展示模块。请求尾部只发送进度、工具和后台作业等紧凑动态字段，完整快照仍写入 RunState。

## 为什么这样做

模型已经一次给出独立动作时，重新调用模型只会增加延迟和上下文。依赖元数据允许执行器区分可直接批处理与必须等待结果的动作，同时不授予新权限。单一 Intent 消除了“记录已完成但协调图仍运行中”的双写窗口。Bench 复用同一调度器后，单 Agent 是一个 Worker，多 Agent 是多个相同 Worker，不再维护第三种执行模式。

参考 [Oh My Pi task 文档](https://github.com/can1357/oh-my-pi/blob/main/docs/tools/task.md) 的批量派发与后台任务边界、[ARTEX](https://github.com/RuoJi6/ARTEX) 的事件驱动 Intent 思路，以及 [Sapper-v2](https://github.com/N1nEmAn/sapper-v2) 的轻量任务/事实协作方式。这里只采用执行与状态契约，没有复制其 UI、存储或插件系统。

## 兼容性与限制

- JSON Action 仍一次一个动作；批量只用于供应商原生工具调用。
- 批内不是线程并行，适合独立读取和已知参数动作；需要根据上一步输出生成参数时应留到下一轮。
- Planner、Scheduler、Hypothesis Ledger 源码仍存在，旧配置可启用；默认路径不再让它们共同控制每一步。
- Intent 是任务生命周期权威来源，单个 Worker 内部的事实仍由各自 RunState 管理；跨题只转发来源明确的候选情报。
- 本轮离线测试验证请求轮次、状态一致性和并发领取，不代表真实模型成功率或线上 Benchmark 分数。

## 验证

- `tests/test_agent_runtime.py`：两个工具调用在一次决策中完成、依赖失败跳过、停止后补齐 API 回执、默认控制路径。
- `tests/test_swarm.py` / `tests/test_coordination.py`：Intent 与 TaskRecord 同源、v4 检查点、旧恢复、租约和依赖。
- `tests/test_bench.py`：两题由同一 Worker Pool 并发执行、分别提交并关闭。
- `tests/test_interactive.py` / `tests/test_brain.py`：Chat 与任务继续经过控制器和真实工具执行。

执行结果保存在 `evidence/20261115-runtime-slim/`。
