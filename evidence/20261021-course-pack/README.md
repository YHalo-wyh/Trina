# 2026-10-21 课程提交验证事实

对应[根目录开题与第一次汇报](../../开题报告与第一次汇报.md)及[课程规格 1.0](../../docs/course/specification.md)。产品代码基线为 `24fb6371a18ed59c905de25bbf0ac69ba69ec256`，本轮课程交付不修改产品实现、配置或原有测试。新增文档与检查器的内容以 `doc-check.json` 中的 SHA-256 和包含本目录的 Git 提交定位。

## 1. 实际结果

| 验证 | 本轮实际结果 | 原始记录与限制 |
|---|---|---|
| 完整 unittest | **267 项通过，0 failures，0 errors，0 skipped** | [原始输出](unit-tests.log)、[逐测试摘要](unit-tests.json)；Python 3.10.11、Windows、现有虚拟环境 |
| 全新 lab 第一轮 | **success / CONFIRMED，1 条发现，假设 confirmed，审计链完整** | [便携摘要](round-1/summary.json)、[原始状态](round-1/state.json)、[原始审计](round-1/audit.jsonl)、[报告](round-1/report.md) |
| 全新 lab 第二轮 | **success / CONFIRMED，1 条发现，假设 confirmed，审计链完整** | [便携摘要](round-2/summary.json)、[原始状态](round-2/state.json)、[原始审计](round-2/audit.jsonl)、[报告](round-2/report.md) |
| 文档与关系检查 | 以 `doc-check.json` 的 `ok`、计数和错误列表为准 | [机器结果](doc-check.json)，覆盖 20 条要求、26 个场景及 8 张任务卡 |
| 结构检查的失败实验 | 基线通过；缺文件、无效测试名称两种变体均应被拒绝 | [隔离实验记录](structure-negative-check.json)，不会修改真实仓库 |
| 汇总与归档校验 | 命令、退出码、环境和产物摘要逐项保存 | [validation.json](validation.json) |

267 项结果来自实际 `unittest discover` 子进程。原始日志按原始 stderr 后接 stdout 保存；JSON 是从日志解析出的派生结果。部分 unittest 用例有跨行 docstring 描述，解析时保留完整测试 ID，逐条数量与 runner 的 267 项相等；不能只数输出中同一行带 `ok` 的记录。

## 2. 命令、环境与复现

本次完整回归实际执行当前虚拟环境的 Python：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -X utf8 -m scripts.verify_decision_evidence --output artifacts/course_docs/mechanism-20261021
.\.venv\Scripts\python.exe -X utf8 -m scripts.verify_course_pack --json
.\.venv\Scripts\python.exe -X utf8 -m trina audit verify evidence/20261021-course-pack/round-1/audit.jsonl
.\.venv\Scripts\python.exe -X utf8 -m trina audit verify evidence/20261021-course-pack/round-2/audit.jsonl
git diff --check
```

完整回归的 UTC 开始和结束时间、Python 版本、操作系统、准确 argv、退出码及原始日志 SHA-256 在 `unit-tests.json`。运行耗时是本机本次测量，不是性能承诺。本次不调用真实收费模型，未进行全新安装或跨系统测试；Python 依赖和可选外部工具差异可能改变耗时及结果，详见 [Harness 环境约定](../../docs/course/harness-manifest.md)。

复现机制验证时，必须选择尚不存在的新输出目录，例如 `python -m scripts.verify_decision_evidence --output artifacts/course-pack-demo`；如果已经运行过就换新目录。脚本每轮以 `port=0` 创建新 lab，并在 finally 中回收服务与线程。端口由操作系统分配，因此新结果的 URL、时间、run_id 和审计末端哈希预期不同。

## 3. 每轮证据的来源和完整性

每轮归档以下文件：

| 文件 | 性质与用途 |
|---|---|
| `replay.json` | 实际执行的两步固定决策：SQL 注入检查和 final；没有真实模型自主推理 |
| `state.json` | 原始运行状态、发现、证据、假设和时间线，逐字节复制 |
| `audit.jsonl` | 原始哈希链，逐字节复制，使用 CLI 可校验 |
| `report.md` / `report.html` | 原始 Markdown 与自包含 HTML 报告，逐字节复制 |
| `original-summary.json` | 脚本原始摘要，包含运行时本机路径，逐字节复制 |
| `summary.json` | 另建的便携索引，使用仓库相对文件位置，明确标记为 derived |
| `provenance.json` | 原件来源位置、归档文件名、SHA-256 与复制模式 |

原件引用的 `artifacts/course_docs/...` 是历史运行时位置，克隆后使用同目录的便携索引定位归档文件，不能把旧路径当作仍在监听的目标。归档没有为美化路径而改写原始审计、状态或报告。机制输入为项目自建 lab 和虚构数据，不包含真实业务响应或真实用户凭据。

根 `.gitattributes` 对本证据目录禁用文本换行转换，以保留原件字节及摘要。`doc-check.json` 对代码和文档采用 CRLF 规范为 LF 后的文本摘要，避免 Windows 与 Linux 的工作树换行差异被误判为内容变化；该规则不用于改写证据原件。

原始工具产物和原始测试日志保留生成时的空白，不参与代码空白风格检查；派生 JSON、课程正文和检查脚本仍进行正常差异检查。归档原件的复制完整性由 SHA-256 核对。

## 4. 结论范围与复核位置

固定回放仍会实际调用目标工具及回环 HTTP，能够验证执行、证据与假设闭环。它不衡量大模型规划水平，不产生公开基准分数，也不证明可选咨询提高真实成功率或降低费用。哈希链只证明当前记录内部一致，不能独立证明外部事实或抵御整条链重新计算。

自动只读复核原始输出保存在 [第一轮复核](../../review/20261021-course-pack/round-1-review.md)和[第二轮复核](../../review/20261021-course-pack/round-2-review.md)。本轮 AI 辅助自查、发现处理和待人工关卡位于[课程审查记录](../../review/20261021-course-pack-review.md)。证据事实与对事实的解释分别保存；同行及教师签署未伪造为完成。
