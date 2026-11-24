# design · ADR、AI 规则、状态与权限

章节 02 对应[双层 Harness](../harness/harness-manifest.md)、[实际状态模型](../docs/course/state-model.md)及[完整汇报](../docs/开题报告与第一次汇报/开题报告与第一次汇报.md)。具体生命周期见状态模型。

## 目录

- **[设计决策](../docs/设计决策/设计决策.md)**：按时间持续编号，逐步解释目标约束、阶段、排序与调度、模型协议、授权审批、证据更新、失败与收尾，并附案例和方案比较。
- [ADR](../ADR/README.md) — 关键决策记录
- `ai-rules.md` — AI 使用规则与人机责任（O1）
- 架构与护栏细节：[`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md)、[`docs/GUARDRAILS.md`](../docs/GUARDRAILS.md)

## ADR 一览

当前共有 **44 份 ADR**。`Accepted / 已实施` 表示仍是当前契约；“历史基线”保留当时证据与取舍，但默认行为以其注明的后续 ADR 为准。新增能力必须更新索引和关联 ADR。

### 当前运行时决策（ADR-0021—0044）

| 编号 | 决定 | 当前作用 |
|---|---|---|
| [ADR-0021](../ADR/0021-capability-fabric.md) | Capability Fabric 取代固定专职角色 | 按任务租约装配 Tool/CLI/MCP；同目录 `AGENT.md` JIT 进入 user 合同 |
| [ADR-0022](../ADR/0022-standard-messages-and-streaming.md) | 标准消息、直接 API 与 SSE | 移除 OMP 模型后端，保留原生角色、工具回执和稳定前缀 |
| [ADR-0023](../ADR/0023-transcript-cache-and-agent-status.md) | 会话视口、选区与末尾运行状态 | 缓存稳定块，动态状态只追加在请求尾部 |
| [ADR-0024](../ADR/0024-cache-aware-context-lifecycle.md) | 增量状态、缓存策略与继承检查点 | 长上下文确定性压缩并保留可审计归档 |
| [ADR-0025](../ADR/0025-persistent-chat-resume.md) | 项目级聊天保存与 `/resume` | SQLite 事务、独占打开，不重放工具或旧进程 |
| [ADR-0026](../ADR/0026-native-binary-runtime-and-model-windows.md) | r2/GDB 会话、任务模块与模型窗口 | 原生调试经注册表；平台说明外置；上下文窗口可配置 |
| [ADR-0027](../ADR/0027-unified-runtime-events.md) | 统一运行事件、工具窗口与游标续流 | 模型、工具、调度共用事务事件与可恢复游标 |
| [ADR-0028](../ADR/0028-console-roles-and-status-line.md) | 0.4.0 分段底栏、角色与 Git 分支 | 宽度自适应状态、独立角色聊天和字符首页 |
| [ADR-0029](../ADR/0029-subworker-console-and-model-binding.md) | 子 Worker 控制台与独立模型绑定 | 后台 Swarm、逐任务模型/思考配置、取消与用量 |
| [ADR-0030](../ADR/0030-commander-delegation-and-live-grid.md) | Commander 委派与四格观察 | 真实 `worker_tasks`、主动并行、消息/取消和事件投影 |
| [ADR-0031](../ADR/0031-unified-agent-runtime-and-state.md) | 统一 Agent Runtime、Intent 与 Bench 调度 | 原生批量工具、单一任务状态、默认目标驱动路径 |
| [ADR-0032](../ADR/0032-fact-driven-intents-and-bench-continuation.md) | 事实驱动 Intent 与结构化续做 | 从事实派生后继任务，续做继承事实而非旧进程 |
| [ADR-0033](../ADR/0033-frame-coalesced-stream-output.md) | 按帧合并流式输出 | 思考、正文、工具事件限频刷新并保留首块低延迟 |
| [ADR-0034](../ADR/0034-direct-fragment-terminal-rendering.md) | 直接样式片段与稳定块复用 | 避免反复 Markdown/ANSI 排版，缩小重绘范围 |
| [ADR-0035](../ADR/0035-repository-shell-notifications.md) | 仓库工作流、Shell 检索与完成提醒 | Git/rg 使用统一工具契约，终态通知按能力降级 |
| [ADR-0036](../ADR/0036-budget-summary-follow-tail.md) | 动作预算后收尾与显式尾随 | 无工具总结真实观察；阅读历史时不抢视口 |
| [ADR-0037](../ADR/0037-proactive-workers-and-pluggable-task-modules.md) | 主动并行 Worker 与外置任务模块 | 两个以上独立分支主动 spawn；平台 `AGENT.md` 不进入 system |
| [ADR-0038](../ADR/0038-tool-evidence-and-retest.md) | 成果、流量、API侦察与复测 | 脚本证据登记、流量绑定、资产覆盖、复测版本和外置ScopeSentry |
| [ADR-0039](../ADR/0039-bench-commander-dynamic-budget.md) | Bench Commander 轮次边界动态预算 | 部分 Flag/新增事实可续做或加时；全局储备与次数上限约束，默认关闭 |
| [ADR-0040](../ADR/0040-agent-efficiency-profiler-and-result-views.md) | 运行性能与工具结果视图 | 记录运行耗时，保留原始工具输出的引用 |
| [ADR-0041](../ADR/0041-console-plan-queue-and-worker-grid.md) | 执行前澄清、草稿队列与实时网格 | 由用户确认和发送草稿，主会话显示后台终态 |
| [ADR-0042](../ADR/0042-worker-viewport-and-harness-entry.md) | Worker 四格视口与项目级 Harness 入口 | 按行滚动当前批次，工程规则与验收统一入口 |
| [ADR-0043](../ADR/0043-session-todo-and-sticky-task-card.md) | 会话任务清单与固定任务卡 | 工具、恢复和 TUI 共用清单状态；完整清单由 `/tasks` 查看 |
| [ADR-0044](../ADR/0044-tool-control-center-and-resolution-view.md) | 工具控制中心与可见性解析 | 搜索注册工具，解释当前 Schema 绑定并显示会话统计 |

### 基础与历史决策（ADR-0001—0020）
| 编号 | 决定 | 关键理由 |
|---|---|---|
| [ADR-0001](../ADR/0001-brain-own-react-loop.md) | 宿主掌握决策循环，模型后端可替换 | 明确执行控制、事实写入与模型候选的职责；附方案比较、代价和迁移条件 |
| [ADR-0002](../ADR/0002-text-json-action-protocol.md) | 文本 JSON 单动作协议（现为兼容回退） | 历史跨模型基线；当前默认原生工具调用见 ADR-0031 |
| [ADR-0003](../ADR/0003-single-execution-chokepoint.md) | 工具执行单一收口（`ToolRegistry.execute`） | 护栏若有第二个入口等于没有 |
| [ADR-0004](../ADR/0004-policy-from-config-not-prompt.md) | 授权判据来自配置与注册表，不来自提示词 | 模型只能申请，不能授权 |
| [ADR-0005](../ADR/0005-hash-chain-audit.md) | 审计用 SHA-256 哈希链 | 全程可追溯且能检出事后篡改 |
| [ADR-0006](../ADR/0006-planner-controller-separation.md) | 计划器与控制器分离，只接受注册 target_id/test_id | P3 核心：模型不能自行扩大范围 |
| [ADR-0007](../ADR/0007-three-verdict-states.md) | 结论三分类 + 覆盖说明（已测/未测/原因） | 「没有结果」不等于「没有问题」 |
| [ADR-0008](../ADR/0008-goal-requirements.md) | 把任务描述解析为硬条件（漏洞类/路径/端口/凭据） | 防止"顺手拿到 flag"替代要求的验证 |
| [ADR-0015](../ADR/0015-insight-layer-hypotheses-failure-graph.md) | 假设台账、失败分类与证据关系图 | 待验证问题与已验证事实分开，结论可追溯 |
| [ADR-0016](../ADR/0016-deterministic-hypothesis-scheduling.md) | 确定性假设调度（当前默认关闭） | 保留历史实验与显式兼容路径，默认目标驱动见 ADR-0031 |
| [ADR-0017](../ADR/0017-specialist-consultation-without-blackboard.md) | 固定专职 Agent 咨询的历史方案 | 已由 ADR-0021 的任务级 Capability 租约取代 |
| [ADR-0018](../ADR/0018-fact-context-and-atomic-snapshots.md) | 事实摘要、实时覆盖与原子快照 | 减少重复调度、对话丢进展和状态读取竞态 |
| [ADR-0019](../ADR/0019-course-contract-and-dual-harness.md) | 同仓库规格、追踪检查与双层 Harness | 让教师从需求直接追到代码、验证事实和未决问题 |
| [ADR-0020](../ADR/0020-security-engineer-prompt-and-commander-intelligence.md) | 七模块安全工程师 system；事实隔离与定向简报 | 核心仍有效；OMP 后端和固定专职角色部分已被后续 ADR 取代 |

本轮完整审查、外部资料与待解决问题见 [Agent 设计审查](../docs/AGENT_DESIGN_REVIEW.md)。

### 实证决策（都有实验卡与逐次数据，对应 O4：实验卡＋逐次数据＋ADR）
| 编号 | 决定 | 实验卡 | 关键数据 |
|---|---|---|---|
| [ADR-0009](../ADR/0009-evidence-over-status-code.md) | 验收判定以响应证据为准，状态码只是前置条件 | [E1](../review/experiments/E1-verdict-evidence-vs-status.md) | 软 404：误报 1 → **0**；目录爆破 700 请求 0 误报 |
| [ADR-0010](../ADR/0010-credential-verification-scope.md) | 动作等级按**尝试规模**判定，被拒时回灌等价做法 | [E2](../review/experiments/E2-credential-verification-scope.md) | 弱口令场景 partial(0/1) → **success(1/1)**，等级拒绝 8–10 → **0** |
| [ADR-0011](../ADR/0011-eval-scoring-scope.md) | 评测口径：按声明范围评分 + 端口感知 AND 匹配 + 基础设施类不计误报 | — | web 套件 precision **57.1% → 100%**、FPR **42.9% → 0%** |
| [ADR-0012](../ADR/0012-fixed-vs-dynamic-checks.md) | 固定阶段机的历史评测基线（当前默认目标驱动） | [E3](../review/experiments/E3-fixed-vs-dynamic.md) | 修前 GT 命中 0/N → 修后 7/8 场景通过；**固定臂未跑**（缺口如实标注） |
| [ADR-0013](../ADR/0013-human-approval-gates.md) | 人工介入＝审批闸门 + 引导式介入，超时即拒绝 | — | 审批/介入/停止/撤权均有测试与实测会话 |
| [ADR-0014](../ADR/0014-fixture-self-healing.md) | 夹具自愈：谁拉起谁回收，评测器自动起靶机 | — | 冷环境一键跑通；靶机被回收不再污染指标 |

## 状态与权限（一页图）

```
目标清单 + 检查注册表（操作者通过 policy.registry_dirs 显式提供）
        │
        ├─► 计划器（模型，只建议）──► 控制器（确定性：注册/预算/撤权）──► 执行器（受控工具）
        │                                        │
        │                                        └─► 拒绝时记录 boundary_events（请求越界，未实际越界）
        └─► 护栏（范围/等级/破坏性/硬禁止清单/限速）──► 审计哈希链

任务状态：PLANNED → ALLOWED → RUNNING → EVIDENCE_COLLECTED → CONFIRMED / NOT_CONFIRMED / INCONCLUSIVE → STOPPED
权限分离：产品 Agent（被评对象，跑注册检查）≠ 评价 Agent（复核者，`trina review`，只读证据 + 请求补测）
```

> 关键约束：模型没有写权限（不能改注册表/白名单/预算）；工具输出只是**数据**，不是执行许可；
> 执行环境不挂宿主凭据、不暴露 Docker socket（见 `release/README.md` 的部署边界）。
