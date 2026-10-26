# 2026-10-26 决策层文档核对

对象为[决策层设计说明](../../docs/DECISION_LAYER.md)、[架构文档](../../docs/ARCHITECTURE.md)、[ADR-0001](../../design/adr/0001-brain-own-react-loop.md)及相关索引。核对起点为 `cede4efea31edd23f07b18aa7ef351e4594cc9bc`。本轮只修改文档、导航、文档追踪清单及归档属性，产品实现、配置和测试代码保持原样。

## 实际验证结果

| 验证 | 结果与文件 | 结论范围 |
|---|---|---|
| 标准 unittest 完整回归 | **267 项通过，0 failures，0 errors，0 skipped**；[原始输出](unit-tests.log)、[逐项摘要](unit-tests.json) | 当前 Windows / Python 3.10.11 既有环境中的用例行为 |
| 课程文档关系检查 | [doc-check.json](doc-check.json) | 当前登记文档、REQ/SC/TASK、文件路径、测试声明及命令覆盖关系 |
| 详解中的测试引用与原始证据核对 | [validation.json](validation.json) | 明示的完整测试 ID 存在并在本轮通过；历史运行字段与归档一致 |
| 文档内容和版本定位 | validation 中保存文本摘要及代码参考 | 换行按 CRLF→LF 规范化；不能替代完整人工语义审查 |

完整回归真实执行命令为当前虚拟环境的 `python -X utf8 -m unittest discover -s tests -v`。原始 stderr 后接 stdout 按字节保存，JSON 从真实日志解析，逐项数量与 runner 报告一致。准确 argv、UTC 开始结束时间、耗时、平台及日志 SHA-256 在逐项摘要中。

## 核对的主要内容

文档按照 `PentestAgent.run()` 的调用顺序解释控制器、人工审批和注册表参数校验，避免把控制器允许直接当作工具成功。模型重试按照 `MAX_LLM_RETRIES=4` 的实际循环展开为最多 5 次尝试；阶段机、目标硬条件、假设状态、运行 status 和漏洞 verdict 分别说明。

假设调度分别解释候选筛选、目标类优先、目标驱动派发、旧检查过滤条件和 observe 分母。知识摘要、无新信息、重复指纹、无进展停止和强取消的区别依据实现核对。对话与任务、主控与专职建议角色也分别列出权限及预算差异。

关于 strict 空注册表、目标关键词语义、台账容量淘汰、暂停退出后的检查位置等边界，本轮记录的是源码可见行为和限制，没有宣称已新增修复或已为全部限制增加专项测试。原有测试通过也不能替代这些未完成的产品工作。

## 历史证据的使用方式

[2026-10-21 两轮 lab](../20261021-course-pack/README.md)保留原日期、端口、run_id 和文件字节，本轮没有重新生成或覆盖。两轮都包含一条 verified sqli 发现、4 条发现证据、confirmed 假设和 success / CONFIRMED 结果。

两轮 scheduler.decisions 均为 0。其固定机制夹具未提供供调度绑定的 expect_finding，因此只能支持执行、证据和报告机制的说明。调度选择与遵从使用当前测试中的专门断言验证，未进行新的真实模型成功率、费用或延迟对照实验。

## 复核命令

在仓库根目录运行：

```bash
python -m scripts.verify_course_pack
python -m unittest discover -s tests
python -m trina audit verify evidence/20261021-course-pack/round-1/audit.jsonl
git diff --check
```

需要重做机制实验时使用 `python -m scripts.verify_decision_evidence --output artifacts/decision-docs-demo`，输出目录必须尚不存在。本文档任务没有因再次引用旧样本就重跑或改写历史审计。

`.gitattributes` 对本目录保留归档字节，原始测试输出保留生成时的空白；课程正文和派生元数据仍进行普通差异检查。旧证据目录中的文档摘要描述旧版本，当前文件内容以本目录新摘要与交付提交定位。
