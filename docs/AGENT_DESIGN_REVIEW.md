# Trina Agent 设计审查与优化决策

> 历史审查记录：下文的未提交状态、远端状态和受限环境测试结果描述审查当时的事实。随后产品基线已提交为 `24fb637`；本轮课程文档与现有环境验证见[开题与第一次汇报](../开题报告与第一次汇报.md)及[2026-10-21 验证事实](../evidence/20261021-course-pack/README.md)。

审查日期：2026-10-21。审查对象：当前本地工作树，包括已有但尚未提交的假设调度与证据链改动。
本地 `origin` 指向 `https://gitee.com/YHalo/Trina.git`；此次网页读取失败，未验证远端分支与本地一致，未向远端推送。

## 结论

Trina 应围绕“受控决策 → 可验证事实 → 可追溯报告”形成特点，
多 Agent 数量不应成为能力指标。本次采用主控按需咨询专职推理角色的轻量结构，
保留一个执行入口和现有事实库，不增加共享可写黑板。

`board` 是给人看的工作台，不是多个模型共写的黑板。保留它有实际价值：
操作者能查看运行进度、假设与证据，后台不用因此再调用一次模型。
假设台账也应该保留：它记录尚未证实的问题，不应与真实漏洞事实混存。

## 本次代码改动

| 发现 | 改动 | 主要验证 |
|---|---|---|
| 覆盖快照过旧，调度可能重复 | 用控制器实时记录参与调度，执行后更新快照 | 连续两轮选择不同未测检查项 |
| 同漏洞类后续检查没有被派发 | 归组前过滤已执行项 | 同类第一条跑过后，下一条仍可被调度 |
| 同工具不同检查被算成遵从 | 明确检查 ID 优先匹配，审批使用控制器解析结果 | 指定另一检查项时不能算遵从 |
| 长对话缺少稳定进展与动作来源 | 事实摘要、本轮请求和有界历史共同构成提示；补记动作 | 历史滚动后仍能看到已知端口与当前要求 |
| 失败建议只显示在控制台 | 回灌到下一轮实际观察 | 检查模型输入包含处理建议 |
| 工作台可能读到半份状态 | 临时文件写完后原子替换 | 正常落盘与替换失败保留旧快照 |
| 多智能体缺少实质封装 | 注册 `consult_specialist`，增加独立上下文角色、建议协议与调用上限 | 不自动执行、不提升权限、不伪造事实、可回放 |

实现详情分别见 [ADR-0017](../design/adr/0017-specialist-consultation-without-blackboard.md)
和 [ADR-0018](../design/adr/0018-fact-context-and-atomic-snapshots.md)。

## 为什么这些选择适合当前项目

| 决策点 | 采用 | 未采用 | 本项目理由与代价 |
|---|---|---|---|
| 总体控制 | 一个主控掌握执行决策 | 多个自治循环同时修改状态 | 更容易统一审批、撤权与预算；牺牲分支并行速度 |
| 专业分工 | recon / web / review 按需咨询 | 固定每步运行所有角色 | 把额外成本放在复杂线索上；主控需要判断何时咨询 |
| 状态共享 | 工具事实库 + 有界快照 | 第二套可写黑板 | 保持事实来源清晰；子角色看不到无限历史 |
| 推理结论 | 建议交回主控重新验证 | 子模型自报成功直接入账 | 防止自评替代证据；会增加后续确认步骤 |
| 权限边界 | 配置、控制器与 ScopeGuard | 用角色提示词约定权限 | 提示词不能授权；配置和注册项仍需维护 |
| 上下文 | 确定性事实摘要，记录动作与结果 | 每轮额外调用模型生成记忆 | 成本低、来源明确；目前会截断细节，无按需证据检索 |
| 工具接口 | 继续沿用 ToolSpec / ToolResult / registry | 再引入一套外部编排框架 | 复用参数、超时、审计和测试契约；通用工作流能力有限 |
| 默认模式 | 单主控，专职咨询显式开启 | 直接宣称多 Agent 默认更强 | 暂无真实任务收益数据；需要用户选择实验档 |
| 评估标准 | 真实证据、误报、覆盖、时间与总成本 | 以角色数、对话轮数、演示成功为主 | 成果可核查，但准备可靠场景与阴性样本更费工 |

这不是对其它架构的普遍否定。黑板适合需要多个长期角色反复交换增量状态的任务，
并行分支适合足够独立、可确定合并的子任务；当前项目尚未提供证明它们值得引入的评测。

## 使用专职 Agent

保留当前后端配置，增加开关即可。用户仍然只给目标和任务，由主控判断是否咨询。

```powershell
python -m trina run --target http://127.0.0.1:9099 --goal "分析 Web 线索并给出验证证据" --set run.specialists=true
```

控制台使用相同配置项。配置上限位于 `configs/trina.yaml#run`：默认最多 3 次咨询，
每次输出上限 1200 tokens，快照上限 6000 字符。具体边界和成本计算见 ADR-0017。

主控发送的普通工具动作示例：

```json
{
  "thought": "已有线索互相矛盾，先请 Web 角色比较验证路径",
  "action": "consult_specialist",
  "params": {
    "target": "http://127.0.0.1:9099",
    "role": "web",
    "task": "根据现有路径与任务要求，建议下一条能验证假设的无害检查；指出需要观察什么证据"
  }
}
```

返回的是带 `advisory_only=true` 的建议，后续工具调用仍需独立批准。
默认配置没有给 strict 模式添加咨询检查项；需要授权人先明确注册，不能临时切换策略绕过。

`review` 专职角色与 `trina review` 不是同一个东西：前者是模型提出的证据缺口建议，
后者是对既有状态、审计和覆盖的确定性复核。模型复核通过不能替代审计校验或独立 PoC 重放。

## 本轮验证记录

使用仓库已有 `.venv/Scripts/python.exe`，未安装新依赖，也未修改任何原有拒绝断言。
Windows 沙箱默认临时目录不支持此次文件替换测试，测试临时目录明确设在仓库 `artifacts/test-tmp`。

| 检查 | 结果 | 范围与限制 |
|---|---|---|
| 新增协作与一致性测试 | 23 / 23 通过 | 包含角色隔离、权限与审批、预算、伪造事实、回放、调度和原子落盘 |
| 核心离线回归 | 174 / 174 通过 | 大脑、注册表、洞察层、审计、授权、范围与状态；显式排除一条依赖真实端口扫描的原有测试 |
| 全量 unittest | 267 项：247 通过，14 失败、6 错误 | 本地靶机 HTTP、重定向、夹具与实际扫描相关场景受 socket / 扫描环境限制，不能视作通过 |
| Python 编译与变更空白检查 | 通过 | `compileall` 与 `git diff --check` |

离线回归排除项是
`tests.test_policy.TestVerdictAndReview.test_not_confirmed_when_checks_ran_without_findings`：
该测试真实运行端口扫描；本环境未获得正常扫描结果，实际为 `INCONCLUSIVE`，
因此不能断言 `NOT_CONFIRMED`。该断言保留原样。

完整测试执行了 `python -m unittest discover -s tests`，本轮为避免受限扫描长时间挂起，
通过已有环境变量接口把单工具超时设为 5 秒，未变更仓库默认超时或任何授权配置。
本地记录：`artifacts/agent_review_full_final.log` 与 `artifacts/agent_review_validation.json`。
全量失败场景仍需在可连接回环靶机、可正常运行扫描器的环境补测；本轮没有运行真实模型收益对照实验。

## 仍需优先处理的问题

| 优先级 | 问题 | 下一步及验收 |
|---|---|---|
| 高 | 覆盖的批准与实际完成尚未彻底分开 | 独立迁移控制器生命周期；人工拒绝、护栏拒绝、超时都不能冒充完成；保留逐项失败原因 |
| 高 | 发现仍依赖可信注册工具的验证逻辑 | 加独立复现器与阴性样本，区分原始观察、工具判断和复现结果；先选择少量现有 PoC 做闭环 |
| 高 | 通用线程超时不能硬取消所有在途行为 | 为 HTTP、外部进程和 LLM 分别补取消/截止语义；不得把停止主循环描述成所有操作已经终止 |
| 中 | 调度主要按漏洞类绑定检查项 | 进一步绑定 URL、参数、端口与前置条件，避免同类不同位置的检查分配不准 |
| 中 | 新目标或多轮任务变更的会话语义有限 | 明确继续旧任务与创建新任务，避免复用过期预算、撤权或旧目标偏好；配套场景回归 |
| 中 | 尚无多 Agent 的真实收益证据 | 同任务、同模型、同总预算进行开关对照，记录收益和额外成本，按任务难度分层 |
| 中 | 上下文字符截断不等于 token 预算 | 按目标选择相关证据、按 ID 读取原始证据，保留原文与摘要的来源对应关系 |

本次没有扫描该 Gitee 服务或用户未指定的线上目标，没有调用真实模型密钥做付费试跑。
这些缺口属于明确的后续验收项，不能写成已经实现的特性。

## 外部资料与如何采用

以下均为开发团队自己的工程文章；文中的框架建议与指标属于原作者语境，
迁移到 Trina 的方案是本次工程判断，需要由本项目测试和评测验证。

| 来源 | 在本次设计中承担的依据 |
|---|---|
| [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents) | 先保持简单、可组合的执行循环 |
| [Anthropic — How we built our multi-agent research system](https://www.anthropic.com/research/multi-agent-research-system) | 控制多角色带来的额外调用成本 |
| [Anthropic — Effective context engineering for AI agents](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents) | 选择与当前任务相关的上下文，避免无限复制历史 |
| [Anthropic — Writing effective tools for agents](https://www.anthropic.com/research/writing-tools-for-agents) | 工具协议、参数与返回反馈保持明确 |
| [Anthropic — Demystifying evals for AI agents](https://www.anthropic.com/research/demystifying-evals-for-ai-agents) | 用可核查的任务轨迹和结果检验改进 |
| [LangChain — Choosing the Right Multi-Agent Architecture](https://www.langchain.com/blog/choosing-the-right-multi-agent-architecture) | 采用主控调用子角色的上下文隔离方式 |
| [LangChain — How and when to build multi-agent systems](https://www.langchain.com/blog/how-and-when-to-build-multi-agent-systems) | 区分汇总读取结果与协调多方写入 |
| [Cognition — Don't Build Multi-Agents](https://cognition.com/blog/dont-build-multi-agents) | 审视分工造成的上下文损失与隐含决策冲突 |
| [Cognition — Multi-Agents: What's Actually Working](https://cognition.com/blog/multi-agents-working) | 专业推理可拆分，执行写入保持集中 |

本次没有据此宣称超过 TSecBench 或其它 Agent。项目特点应由证据来源、边界与恢复质量体现，
跨项目性能比较须先统一模型、工具、授权范围、目标和预算。
