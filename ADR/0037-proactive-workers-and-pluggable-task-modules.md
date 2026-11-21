# ADR-0037：主动并行 Worker 与外置临时任务模块

- 日期：2026-11-17
- 状态：Accepted
- 关联：[ADR-0020](0020-security-engineer-prompt-and-commander-intelligence.md)、[ADR-0021](0021-capability-fabric.md)、[ADR-0026](0026-native-binary-runtime-and-model-windows.md)、[ADR-0030](0030-commander-delegation-and-live-grid.md)、[ADR-0031](0031-unified-agent-runtime-and-state.md)

## 背景

Trina 已提供真实 `worker_tasks` 宿主和并发 Worker Pool，但旧 Delegation 合同只说明“可以委派”，没有要求模型在一个请求包含多个独立目标时主动并发。结果可能是能力存在却仍逐项串行。

另一方面，把某个比赛平台的题目读取、网关、令牌、提交接口、专属镜像和模型预算直接放进核心，会让所有普通任务携带无关工具与规则。CTFY 集成暴露了这一问题：平台生命周期短，核心命令、工具、提示词和测试却形成长期维护面。

## 决策

1. 当请求包含两个及以上互不依赖的交付物、目标或验证分支，且入口真实提供 `worker_tasks` 宿主时，Commander 应在当前并发额度内分别提交 `worker_tasks(spawn)`；主 Agent 同时推进关键路径。
2. 不为单步小事创建 Worker。存在真实先后关系时继续使用 `depends_on`，不得为了看起来并行而破坏依赖。
3. 核心删除 CTFY 命令、运行器、工具、镜像、专属提示词、快速 `nc` 分支与平台测试。删除一个平台模块后，默认 ToolRegistry 和 system 不应留下其名称或合同。
4. 新的靶场、比赛平台和一次性 Harness 以仓库外 Capability 包接入，目录包含清单、适配器以及可选的同目录 `AGENT.md`。
5. 清单省略 `guidance` 时，Catalog 只记录同目录 `AGENT.md` 的相对路径；正文在任务获得租约时 JIT 读取。Guidance 进入首条 user 任务合同，不进入稳定 system。
6. 平台地址、令牌、题目状态和权威提交结果由适配器的受控输入与工具结果传递，不写进 `AGENT.md`。所有执行仍经过 Capability Runtime、`ToolRegistry`、护栏、审计与超时。
7. 外部 Harness 若把模型调用次数视为硬预算，使用通用 `hard_model_budget` 运行标志；核心不再按平台名称分支。

## 结果与边界

多目标请求能够使用现有 Worker Pool 并行推进，而单个 Worker 内的 `RunState`、对话、工具租约和事实仍隔离。跨 Worker 只共享带来源的证据索引或 Commander 简报，不能把其他任务的结论直接写成本题事实。

平台模块可通过增加或删除目录完成安装与卸载，不需要修改 Commander、稳定 system 或核心工具注册表。`AGENT.md` 被视为任务数据，不具有提升权限、修改身份或覆盖全局规则的能力。

本决策不承诺模型对所有自然语言请求都能完美拆分；确定性调度只保证已经提交的无依赖 Intent 会被 Worker Pool 并发领取。现有通用 Bench/Adapter 接口可以保留，但新增平台专属合同不得继续进入核心；历史平台证据不等于当前默认运行依赖。

## 验证

- `tests/test_brain.py::TestParseAction.test_system_messages_define_the_general_agent_modules`：Delegation 合同要求主动识别两个及以上独立分支。
- `tests/test_worker_dashboard.py::WorkerDashboardTests.test_targeted_cancel_and_message_do_not_stop_sibling`：两个无依赖任务同时进入 `running` 并占用两个 Worker，单项取消不停止兄弟任务。
- `tests/test_capabilities.py::CapabilityFabricTests.test_manifest_is_discovered_without_core_change_and_guidance_is_jit`：自动发现同目录 `AGENT.md`、租约时读取，并验证内容只进入 user 合同。
- `tests/test_capabilities.py::CapabilityFabricTests.test_worker_switches_capability_and_released_lease_cannot_be_reused`：释放模块租约后旧调用失效。
- `tests/test_interactive.py::TestChatAgent.test_external_harness_can_preserve_a_hard_model_call_budget_without_platform_code`：通用硬预算不依赖平台代码。

模块格式与生命周期见 [`docs/TASK_MODULES.md`](../docs/TASK_MODULES.md)，主动调度与租约语义见 [`docs/SWARM.md`](../docs/SWARM.md)。
