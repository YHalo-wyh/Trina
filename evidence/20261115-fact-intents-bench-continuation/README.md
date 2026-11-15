# 事实驱动 Intent 与 Bench 续做验证记录

- 日期：2026-11-15（Asia/Shanghai）
- 执行状态：本轮实际执行；平台接口使用本地 HTTP 夹具，模型使用固定回放。未运行付费模型对照或线上竞赛。
- 对应需求：TR-24；设计：[ADR-0032](../../design/adr/0032-fact-driven-intents-and-bench-continuation.md)
- 最终 Windows 验证树：`af6d4c9af6c71e2a76a999bb69409b39e41239fd`。从暂存树导出隔离快照执行，不包含工作目录中的其他未提交改动。验证后仅补充本证据、入口链接和 ADR 文案。

## 实际检查

| 检查 | 实际入口 | 结果 |
| --- | --- | --- |
| Windows 全量回归 | `python -X utf8 -m unittest discover -s tests` | 总计 602 项，592 项通过、10 项跳过；141.908 秒；退出码 0 |
| WSL 核心与真实 ELF | `python3 -m unittest tests.test_agent_runtime tests.test_bench tests.test_coordination tests.test_swarm tests.test_intent_planner tests.test_binary` | 53 项通过，无跳过；78.177 秒 |
| WSL 最终恢复与规约补充 | `python3 -m unittest tests.test_brain.TestReActLoop.test_resume_state_inherits_facts_but_starts_a_fresh_model_chain tests.test_contract tests.test_intent_planner` | 8 项通过；11.897 秒 |
| 产品机器规约 | `python -X utf8 -m scripts.verify_contract --json` | `ok=true`，6 类交付物、24 项需求、63 条规则、154 个测试引用、0 错误 |
| 课程工件结构 | `python -X utf8 -m scripts.verify_course_pack` | 通过；31 份文档、20 项要求、8 个任务、65 个去重测试引用 |

WSL 53 项基于前一暂存树 `0ac51cfa1fae2c5cfdc2820b65c648b6a4f8d2af`；随后补充了检查点深复制、原状态保护断言、24 项需求断言和恢复时关闭自动分支的 CLI 行为，最终树再执行上述 Windows 全量与 WSL 8 项检查。

Windows 原始输出见 [windows-tests.log](windows-tests.log)，静态结果见 [contract.log](contract.log) 和 [course-pack.log](course-pack.log)。首次完整回归发现测试仍期待 23 项需求，随 TR-24 改为 24 后重新执行；这里列出的 Windows 结果来自修复后的完整回归。

## 用例证明的行为

- 新资产、已验证发现和凭据通过现有任务入口生成后继 Intent，保留来源依赖和能力边界。未验证发现、候选凭据与 Flag 不生成分支。
- 同一来源链中的资产增量使用相同规划键；全局生成数量受限，后继预算不超过来源限制。已规划事实可从检查点恢复。
- Bench 续轮可以看到上一轮资产、假设和近期执行经验，步骤编号及用量连续；原状态不变。目标不一致在创建新运行前拒绝。
- 无事实进展、停止或阻断时能够释放 Worker；续轮仍受同一单题墙钟期限约束。平台提交回执决定题目是否完成。
- 运行结果保留逐轮决策、模型/工具调用、模型耗时、重复探索率和总耗时；Ledger/Loop Guard 的对照配置可使用相同验收函数。

## 结论边界

这些结果验证执行链、状态继承与调度契约，不证明真实模型的解题成功率或提速比例。自动提案仅覆盖三类确定性事实，任意语义分支仍需 Commander 显式委派；后继遵守来源任务依赖。续轮恢复的是结构化状态，不是旧进程或原消息的逐 Token 恢复。单题预算采用墙钟分配，尚无跨 Worker 硬费用控制。Windows 跳过项不计入通过；原生工具的可用性另由上述 WSL 真实 ELF 测试证明。
