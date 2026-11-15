# Trina 统一 Runtime 与执行层瘦身验证记录

- 日期：2026-11-15（Asia/Shanghai）
- 验证对象：暂存树 `c97d9dfd5cae76489d0c5dbb2c9019dc044c2043`
- 对应需求：TR-23
- 范围：统一 Agent Runtime、原生批量工具调用、Intent 单一任务状态、Bench 统一调度、默认目标驱动配置

## 实际检查

| 检查项 | 实际命令 | 结果 |
| --- | --- | --- |
| Windows 全量单元/集成回归 | `python -X utf8 -m unittest discover -s tests` | 592 项通过，10 项跳过，耗时 100.375 秒 |
| WSL 核心跨平台回归 | `python3 -m unittest tests.test_agent_runtime tests.test_bench tests.test_coordination tests.test_swarm tests.test_tool_protocol` | 45 项通过，耗时 21.636 秒 |
| 机器规约静态校验 | `python -X utf8 -m scripts.verify_contract --json` | `ok=true`；6 类交付物、23 条需求、60 条规则、143 个测试引用、0 错误 |
| CTFY 真实网关夹具 | `tests.test_ctfy.CTFYTests.test_actual_gateway_chat_registry_submit_and_result_file` | 平台提交返回正确，结果文件记录为完成 |

## 质量结论

- 同一模型响应中的多个原生 `tool_calls` 会全部执行，并为每个 API `call_id` 生成对应回执；依赖失败时，后继动作明确跳过。
- 任务、聊天与 Worker 复用同一个 `AgentRuntime`，但 Worker 的上下文、事实、工作目录和工具租约继续隔离。
- `CoordinationRuntime.intents` 是任务状态唯一来源；检查点 v4 不再持久化重复 `records`，旧 v1–v3 可迁移恢复。
- Bench 只保留平台适配职责，题目经同一个 Swarm Worker Pool 调度；测试覆盖两题并发执行。
- 原生协议不再重复发送完整文本工具说明：合同只保留名称索引，参数和描述以标准 API `tools` Schema 为准。该修复已消除未知 32K 网关首轮上下文超预算。

## 已知边界

- 同一 Worker 的批内工具调用当前按顺序执行，以避免共享 `RunState` 发生数据竞争；跨 Worker 仍并行。
- `_depends_on` 只允许引用当前批次中编号更小的调用，不提供跨轮隐式依赖。
- JSON 兼容协议仍保持单动作；批量执行只面向标准 API 原生 tool calling。
- 跳过项依赖当前环境未安装或未启用的原生二进制工具，不计为通过，也未用模拟结果替代。
