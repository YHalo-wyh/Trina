# ADR-0020：安全工程师系统提示词与 Commander 定向情报

- 日期：2026-11-11
- 状态：已实现
- 关联：[ADR-0017](0017-specialist-consultation-without-blackboard.md)、[ADR-0018](0018-fact-context-and-atomic-snapshots.md)

## 背景

Trina 0.3.0 曾把任务与对话 system 清空，动态动作协议和状态全部放在每轮输入中。该方案缺少任务、对话、OMP 和专职 Worker 共同遵循的稳定行为定义。Swarm 原先只把前序任务的事实摘要作为一次性提示转发，也没有说明不同题目的事实能否互相进入状态。

用户要求使用类似 Oh My Pi 的模块化 system 组织方式，把角色定义为获得最高执行权限的安全工程师。同时要求不同题目默认独立，由 Commander 判断相似性后转发有效情报。

## 决定

1. `trina.brain.prompts` 提供统一的安全工程师 system，包含 `Engineering`、`Tool Policy`、`Exploration`、`Delegation`、`Workflow`、`Delivery`、`Completeness` 七个模块。
2. `PentestAgent`、`ChatAgent` 和 `Specialist` 使用同一个基础 system。专职角色只追加本次委派说明。
3. 工具清单、范围、动作协议、状态、预算和观察继续作为动态运行数据，不写入稳定 system。
4. OMP 后端通过 `--system-prompt` 传递 system，stdin 只发送本轮运行上下文。
5. 每个 Swarm 题目保持独立 `RunState`、事实图、证据、假设、审计和报告。其他题目的状态不合并。
6. Commander 对已完成任务计算相似度，达到 `0.60` 才生成定向简报。相似性来源包括规范化目标、主机端口、显式依赖和任务目标词项。
7. 简报只包含资产观察、带证据的已验证发现、已验证凭据、结论和工件位置。Flag、聊天历史、开放假设和完整状态不转发。
8. 接收 Worker 必须重新验证简报线索。只有本题工具产生的事实能够进入当前题目的状态、证据图和报告。

## 结果

稳定 system 可以被不同入口复用，动态工具和任务数据仍保持实时。任务上下文不会因 Worker 复用而串题，Commander 的转发原因和内容具有审计指纹。相似性规则是确定性启发式，无法理解全部语义；漏转时当前 Worker仍可独立完成任务，误转时重新验证要求阻止情报直接污染事实图。

## 验证

- 捕获任务、对话、专职 Agent 的 system，断言角色和七个模块存在。
- 捕获 OMP 启动参数，断言 system 进入 `--system-prompt`，stdin 只含运行上下文。
- 同目标依赖任务应收到带来源和分数的 Commander 简报。
- 不相关目标和任务方向不得收到简报。
- 简报不得含 Flag，接收任务的 `RunState` 不得出现来源题目的主机或发现。

## 参考

- [Oh My Pi system prompt customization](https://github.com/can1357/oh-my-pi/blob/main/docs/system-prompt-customization.md)
- [Oh My Pi task/subagent context](https://github.com/can1357/oh-my-pi/blob/main/docs/tools/task.md)
