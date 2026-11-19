# Trina Agent 效率优化记录

## 审计范围

本轮检查了 `trina/brain/agent.py`、`trina/brain/runtime.py`、`trina/conversation.py`、`trina/tools/registry.py`、`trina/events.py`、`trina/state.py`、`trina/swarm.py`、`trina/subworkers.py`、`trina/capabilities/`、`trina/browser/`、`trina/traffic.py`、`trina/eval/` 和现有测试。

Trina 当前已经具备统一 AgentRuntime、原生批量 Tool Calling、标准结构化消息、工具执行单一入口、事件持久化、上下文压缩归档、增量状态投影、Swarm Worker Pool、Capability Fabric、BrowserSessions、TrafficStore、Casebook 资产与复测记录。当前最明确的效率缺口是运行指标没有形成统一报告，长工具结果的模型视图仍然由通用 JSON 截断处理，上下文增量虽已存在但缺少收益统计。

## 对照项目核实

OMP 使用事件边界上的上下文维护，保留近期工具结果并将完整历史放入可恢复的归档路径。它还分别记录压缩触发原因、恢复路径和上下文占用。参考 [OMP compaction 文档](https://github.com/can1357/oh-my-pi/blob/main/docs/compaction.md) 和 [OMP 设置文档](https://github.com/can1357/oh-my-pi/blob/main/docs/settings.md)。

ARTEX 的当前公开实现采用 `BaaRai/ARTEX`，仓库 README 明确说明它是 Autumn-27/ARTEX 的英文公开分支。其实现包含 Planner、Worker、资产图、任务探索图、流量证据绑定和统一审计。原始 `Autumn-27/ARTEX` 地址目前不能作为可读取的主实现，因此本轮只依据公开分支和其变更记录分析，不把名称或二手描述当作接口契约。参考 [ARTEX README](https://github.com/BaaRai/ARTEX) 和 [ARTEX CHANGELOG](https://github.com/BaaRai/ARTEX/blob/main/CHANGELOG.md)。

## P0 实施内容

### 运行剖析

新增 `trina/profiling.py` 中的 `AgentProfiler`。它订阅已有 EventBus，只记录运行身份、阶段耗时、工具状态、模型请求耗时、首 Token 延迟、已报告的输入输出与缓存 Token、重试次数、Agent 轮次和子任务生命周期。原始提示词、工具正文、Cookie、密钥和模型思考文本不会进入性能报告。

单 Agent 的结果写入任务工作目录中的 `performance.json`，Swarm Commander 的汇总写入 `commander-performance.json`。`RunState.performance` 和 Swarm 任务结果同时保存聚合快照，历史恢复可以读取这些字段。

### ToolResult 视图

`ToolResult` 增加 `key_findings`、`errors`、`artifact_ref`、`truncated` 和 `metadata` 字段，并由 `ToolResultView` 生成模型视图。字典和数组采用确定性递归摘要，保留数量、状态、错误和常用关键字段。长原文通过现有工作区工件机制保存，模型只收到摘要和原始证据路径。工具原有 Schema、`data`、`raw`、`facts` 和 `evidence` 字段保持兼容。

### 增量上下文指标

`ConversationContext.state_update()` 继续沿用已有的完整检查点与 JSON 差量模式，并记录完整更新次数、差量更新次数、两类字节量和压缩回收 Token。`prepare()` 记录上下文准备耗时和压缩次数，现有消息配对保护、稳定 system 前缀、原始历史归档和预算拒绝行为保持不变。

## 固定回放基准

基准入口是 `scripts/benchmarks/agent_efficiency.py`，固定使用 Replay Backend、32768 上下文窗口、1024 输出预算、80 条 JSON 记录和 5000 行原文。结果写入 `reports/benchmarks/agent-efficiency-p0.json`。

本轮回放结果如下。

| 指标 | 结果 |
|---|---:|
| 旧式 JSON 截断模型视图 | 2618 字符 |
| ToolResultView 模型视图 | 2034 字符 |
| 模型视图长度变化 | -22.3% |
| Agent 状态 | success |
| LLM 请求 | 2 |
| 工具调用 | 1 |
| 回放总耗时 | 约 0.36 秒 |
| Replay Token 用量 | unavailable |
| Prompt Cache 命中 | unavailable |

字符数下降来自确定性输出视图，不能直接等同于真实供应商 Token 降幅。真实 Token、缓存命中和首 Token 延迟必须由供应商回执或在线事件提供。该固定回放用于防止结果摘要回归，不用于宣称 OMP 级别的真实端到端速度。

## P1 能力差距

| 能力 | 当前判断 | 依据与下一步 |
|---|---|---|
| 项目级资产记忆 | 已实现 | `RunState.casebook`、资产工具和报告视图已存在，后续补跨运行索引即可 |
| 资产覆盖率与增量测试 | 已实现但仍可增强 | Casebook 有覆盖记录和复测记录，缺少统一的覆盖率汇总基线 |
| HTTP、Browser、外部工具证据索引 | 已实现 | TrafficStore、Browser 工件和 ToolResult evidence 已接入，后续可统一 evidence ref 解析 |
| 独立 Retester Agent | 部分实现 | `get_finding_retest_context` 与 `record_finding_retest_result` 已存在，尚未有独立复测调度器 |
| 多 Worker 去重与合并 | 已实现但仍可增强 | CoordinationRuntime、事实发布和能力记忆已存在，后续应增加结果摘要去重测试 |
| 授权、进程隔离、出站约束 | 已实现 | ScopeGuard、ExecutionLease、CapabilityRuntime、Browser/Binary 会话清理已覆盖 |
| 复杂任务优先级调度 | 已实现但仍可增强 | Intent Planner 和 Swarm 已存在，下一步做基于预算和信息增益的可测量策略 |
| MCP 进程复用 | 已实现 | CapabilityRuntime 和会话生命周期已有实现，需补初始化与等待耗时进性能报告的覆盖测试 |
| 按复杂度模型路由 | 部分实现 | TaskSpec 和 Subworker 支持模型、供应商和思考强度，尚未有自动复杂度分类器 |

本轮暂不实现新的平台化资产图、微服务拆分、独立模型记忆服务或第二套 Agent Loop。它们会重复现有的 Casebook、CoordinationRuntime、Capability Fabric 和 ConversationContext。

## 后续优先级

1. 为 `AgentProfiler` 增加在线真实供应商基准适配，记录 P50/P95 首 Token、缓存命中和上下文压缩收益。
2. 把独立复测流程接入现有 Casebook 与 Swarm，复用现有一次性复测上下文和证据校验。
3. 为 CoordinationRuntime 增加基于资产覆盖、失败分类、预算剩余和信息增益的 Intent 排序实验，并用固定 Trace 回放验收。

