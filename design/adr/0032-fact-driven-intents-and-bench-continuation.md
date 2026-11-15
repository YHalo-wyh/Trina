# ADR-0032：事实驱动 Intent 与 Bench 结构化续做

- 状态：接受
- 日期：2026-11-15

## 背景

统一 AgentRuntime 和 Swarm 调度后，多工具执行与题目并发已经进入同一运行路径。剩余的实际缺口是：协调器只能排列已有 Intent；Bench 的多轮尝试会创建新运行，但没有继承上一轮事实；性能比较缺少模型轮数、重复探索率和正确完成耗时等统一口径。

## 决定

1. 保留一个 `CoordinationRuntime`。新增的 `FactIntentPlanner` 只读取事实图，并向现有 `add_task()` 提交提案，不维护第二套图或状态。
2. 自动提案限定为资产、已验证发现和已验证凭据。后继任务继承来源目标、模型、能力与边界，以来源 Intent 为依赖；稳定规划键负责去重，默认最多生成 8 项。模型文字、开放假设和 Flag 不自动生成任务。
3. CLI Swarm 与 `/subworker` 默认启用该入口；直接构造 Runtime 默认关闭，Bench 关闭，因为平台容器生命周期由题目任务独占。
4. `PentestAgent.run(resume_state=...)` 深复制 `RunState` 的结构化事实、证据索引、假设、连续步骤和累计用量，首个检查点包含近期动作与失败经验；重新创建 system/user 消息、运行目录、审计和工具上下文。目标不一致直接拒绝，不改写原检查点，不复活旧请求、进程或控制器。
5. Bench 默认最多两轮，全部轮次共享单题墙钟预算。只有新增事实、部分正确 Flag 且重复探索率未过高时才续做；否则释放 Worker。每轮及整场输出模型调用、决策、工具、重复探索率和耗时。
6. 真模型跑批增加 Ledger 与 Loop Guard 的消融配置，指标保持同一判定口径；没有实测样本时不得宣称某配置更快或更准。

## 结果与限制

该方案让事实变化能驱动下一项探索，同时避免为 Planner 再发一次模型请求。Bench 获得可恢复的结构化进展，但不是原对话逐 Token 续跑；原运行的审计、事件和证据保持独立，完整消息录制取决于是否启用 record。确定性映射不能覆盖所有 CTF 语义，因此任意分支仍由 Commander/操作者显式委派。动态预算是墙钟上限和续轮决策，不是跨 Worker 的硬 Token 或费用控制器。

## 验证

- `tests.test_swarm.SwarmTests.test_new_asset_fact_generates_one_bounded_followup_intent`
- `tests.test_coordination.CoordinationTests.test_fact_planning_marks_are_idempotent_and_persisted`
- `tests.test_brain.TestReActLoop.test_resume_state_inherits_facts_but_starts_a_fresh_model_chain`
- `tests.test_bench.TestRunner.test_retry_inherits_state_and_reports_efficiency_metrics`
- `tests.test_bench.TestRunner.test_no_new_fact_stops_retry_early`
- `tests.test_live_batch.LiveSuiteTest.test_protocol_and_insight_ablation_configs_are_declared`
