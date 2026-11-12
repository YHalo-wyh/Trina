# ADR-0021：以 Capability Fabric 取代固定角色与插件式编排

- 状态：Accepted
- 日期：2026-11-12
- 取代：ADR-0017 中的固定 `recon/web/review` 咨询运行方案；原 ADR 仅保留历史背景

## 背景

旧方案把多 Agent 扩展理解为固定角色、咨询工具和能力类别映射。它让 Worker 的身份先于任务存在，并形成独立的配置、预算、协议与测试面。继续沿此方向增加 PluginManager、SkillLoader、AgentProvider 或 HookDispatcher，会重复实现一套插件平台，同时仍无法回答任务真正需要哪一组最小能力。

## 决策

Trina 采用任务驱动的 Capability Fabric：

1. Commander 负责理解目标、任务 DAG、相似性和调度。
2. `Catalog + Broker + Runtime` 统一描述、发现、授权并执行能力。
3. Worker 保持通用，不预设 Recon、Web、Pwn 等类型；本次租约决定临时专长。
4. 现有 Tool、外部 CLI 与 MCP 都投影为同一种 Capability，但实际调用必须回到 `ToolRegistry.execute()`。
5. 每个任务的事实图和对话继续隔离；共享层只保存带来源的证据索引与 Commander 选择的情报。
6. 任务快照固定能力版本和内容摘要；租约有 Worker、任务、TTL 和主动释放边界。
7. 删除 `consult_specialist`、固定角色实现和对应配置，不维护并行兼容系统。

## 为什么这样修改

- 减少概念和代码面：能力接入不再为工具、Skill、Agent 和 Hook 各造管理器。
- 降低上下文成本：只把本租约选择的 Schema 与 Guidance 交给 Worker。
- 保持真实执行边界：新能力复用已验证的参数、护栏、审计、超时和状态吸收链。
- 支持跨领域任务：同一 Worker 可以组合二进制、取证或密码能力，而不必派生新的固定角色。
- 保持证据可追溯：跨 Worker 交接引用证据，不复制整段对话或隐式合并事实。

## 结果与限制

现有 `trina swarm` 默认只创建一个 `main` 任务；显式清单仍可建立 DAG。旧 `recon/web/verify/full` 静态映射被移除，`capabilities` 字段现在接受 Capability ID 或 `provides` 选择器，留空时按意图发现。

首版没有 Marketplace、复杂依赖求解、Hook 总线、操作系统沙箱或长驻 MCP 进程池。MCP stdio 每次调用独立启动并回收。应用级租约不能代替容器或低权限宿主隔离。

## 验证

```powershell
python -m unittest tests.test_capabilities tests.test_swarm -v
python -m scripts.verify_contract --execute --json
```

详细清单格式、状态语义和测试映射见 `docs/CAPABILITY_FABRIC.md`。
