# 课件要求与仓库交付对照

## 来源和适用对象

参考资料为用户提供的《AI 原生工程实践课程：章节 02，Intent、Specification 与双层 Harness》V1.1，文件版本标识 20260830，共 94 页。原始文件 SHA-256 为 `9b711c132d3d7a2bb1919190c974ef90099940b18e2795d93e9aba5330a26d08`。课堂照片对应第 5 页的工程转换链，完整提交清单还包括附录及第 84–94 页修订补充。

本文仅转述提交要求并定位 Trina 工件，不分发课件全文。课件的 PaperManagement-Lab 是教学示例，Trina 使用相同分析方法描述授权安全测试，不新增论文、邀请或匿名评审业务。以下页码为 PPT 顺序页码。

## 12 项交付物

| 课件要求（第 75 页） | 本仓库位置 | 具体检查内容 |
|---|---|---|
| 一页 Problem Frame | [problem-frame.md](problem-frame.md) | 现有基线、问题、现实结果、用户冲突、非目标和责任 |
| 带稳定 ID 的 Specification | [specification.md](specification.md) | 20 条要求各含触发、强度、行为、异常、结果与边界 |
| Acceptance Matrix | [acceptance-matrix.md](acceptance-matrix.md) | REQ 到 GWT、可观察结果、测试、Task 和证据 |
| 状态模型或状态图 | [state-model.md](state-model.md) | 运行、发现、假设、结论、授权生命周期及非法解释 |
| 遗留影响分析 | [impact-map.md](impact-map.md) | CLI、动作、配置、字段、调用者、兼容和回滚 |
| 至少一份 ADR | [ADR-0019](../../design/adr/0019-course-contract-and-dual-harness.md) | 采用与替代方案、代价、限制、回滚及重决策条件 |
| Plan | [plan.md](plan.md) | M0–M5、依赖、检查点、DoR/DoD、风险和预算 |
| 5–8 张 Task Card | [tasks.md](tasks.md) | 8 个纵向切片，各有目标、范围、验证、证据、责任和停止条件 |
| Repository Map | [repository-map.md](repository-map.md) | L1/L2/L3、主要模块、测试入口与权威冲突规则 |
| Harness Manifest | [harness-manifest.md](harness-manifest.md) | 双层职责、Guide/Sensor、命令、权限、预算、配置回归、恢复 |
| AI 使用披露 | [ai-disclosure.md](ai-disclosure.md) | 辅助活动、人类决定、排除主张和未发生的签署 |
| 未解决问题 | [open-questions.md](open-questions.md) | 风险、阻塞范围、责任角色、验证时点及关闭条件 |

## V1.1 修订项

第 84–85 页的 Intent 与接受门对应 `intent.md`，分别保存产品既有意图与本次文档请求，明确接受理由和纠正主体。第 86 页的双层 Harness 对应 `harness-manifest.md` 第一节。第 87 页的权威来源对应 `repository-map.md`，第 88 页的信息可见性对应 Harness 第七节。

第 89 页的 Instructions、Skills、Hooks/Policy、Profiles 按真实实现分层，未安装的 hook 不冒称存在。第 90 页 Host Contract 由现有 Python 宿主、配置及操作者承担，没有虚构身份审批服务。第 91 页连续性对应 `continuity.md`，产品没有的会话字段明确说明。第 92 页配置回归给出基线、代表性任务、结果和预算判断，真实效果未测部分保留问题。第 93–94 页的工件追踪链由验收矩阵和 JSON 清单连接。

## 最低质量八条红线的落实

| 红线（第 76 页） | 落实方法和证据边界 |
|---|---|
| 每条 MUST 有验证路径 | 矩阵和追踪清单逐条连接；脚本检查缺失关系，人工判断测试与要求是否真正相关 |
| 授权不能依赖界面隐藏 | Trina 为 CLI 产品，裁决在控制器与护栏；没有前端权限主张，strict 限制明确列出 |
| 不使用真实账号和密钥作课程数据 | 机制样本仅内置虚构 lab；配置中的实验口令有来源，真实 Key 不入库 |
| AI 自述不是完成证据 | 公开测试原始输出、真实请求、状态和链分别留存，模型伪造字段有阴性回归 |
| 核心身份变更需兼容和回滚 | 本次不修改身份和状态模型；影响分析给出未来迁移前置条件及当前反向提交回滚 |
| 假设有负责人和验证时点 | A-01 至 A-03 在问题框架及未决问题登记，不能当作已验证事实 |
| 问题说明是否阻塞 | 每个 OQ 分开列出阻塞的产品主张、责任与关闭条件 |
| 命令、路径、规则实际可用 | 文档/测试引用检查及真实命令执行；独立机器复现待人工，不伪填通过 |

## 评分项与审查入口

第 77 页的 Problem Framing、Scope、Specification、可验证性、安全隐私、遗留影响、Plan/Tasks、Harness 和工程解释分别由上述独立工件支撑。不自行评定课程分数。课件没有要求 Trina 增加 Web 页面或复杂外部管理平台，文档数量也不能代替内容可执行性。

第 79–80 页同行审查需要真实人员完成：第一轮核对边界和假设；第二轮抽三条 MUST 反向追踪；第三轮独立执行至少两条命令并模拟停止与恢复。当前 [审查记录](../../review/20261021-course-pack-review.md) 区分维护侧自查和待人工签署，教师对 Blocking Finding 保留最终判断。
