# ADR-0040 Agent 效率剖析与结构化结果视图

## 状态

accepted

## 背景

Trina 已经有 EventBus、ConversationContext、ToolResult、RunState 和 Swarm。运行时可以得到模型、工具和上下文事件，但这些信息没有形成统一的性能报告。长工具结果由各工具自行决定可见内容，注册表只有通用 JSON 截断逻辑，导致模型视图大小和原始证据引用不稳定。

OMP 的公开实现把压缩、近期结果保护、完整历史恢复和压缩原因放在同一条上下文维护链路。ARTEX 的公开分支把资产、任务探索、流量证据和 Worker 结果绑定到任务生命周期。本 ADR 只吸收可复用的边界和观测方式，不引入新的 Agent 引擎或第二套事实库。

## 决策

1. `AgentProfiler` 只消费 EventBus 元数据和供应商已经报告的用量。它不读取模型提示词、工具正文或敏感凭据。
2. 性能报告以 `RunState.performance` 和工作目录 JSON 工件为权威快照。单 Agent 使用 `performance.json`，Swarm Commander 使用 `commander-performance.json`。
3. `ToolResultView` 作为 `ToolResult.to_observation()` 的确定性视图层。原始 `raw`、`data`、`facts` 和 `evidence` 继续留在工具结果和工作区。
4. 长原文没有已有证据引用时，由注册表在执行完成后写入工作区工件，并将路径、哈希和字节数放入结果元数据。
5. `ConversationContext` 继续维护稳定前缀、差量状态、完整检查点和原生 tool-call 配对。新增指标只描述更新量和压缩收益，不改变裁剪判据。

## 兼容性

旧工具可以继续只返回 `summary`、`data`、`raw`、`facts` 和 `evidence`。旧状态没有 `performance` 字段时按空字典恢复。Replay 后端没有真实 Token 回执时性能报告标为 `unavailable`。

## 已知限制

首 Token 延迟必须由流事件中的第一个正文或思考增量估计。没有流增量的后端只报告总请求耗时。真实 Token 与缓存命中率不从字符串长度推断。固定回放基准只比较模型视图和执行链完整性，不能替代同模型、同网关、同题集的在线基准。

## 验证

`tests/test_profiler.py` 覆盖事件聚合、敏感正文不进入报告和结构化结果视图。`tests/test_registry.py` 覆盖长原文工件归档。`tests/test_cache_runtime.py` 覆盖差量状态和压缩回收统计。`scripts/benchmarks/agent_efficiency.py` 提供固定回放基准。
