# 仓库地图与权威来源

## 分层阅读入口

仓库根 [README](../../README.md) 提供使用入口，[AGENTS.md](../../AGENTS.md) 提供维护规则，本目录 [课程总索引](README.md) 提供提交材料入口。三者保持简短导航，不承担全部规格内容。按任务读取相关规格、模块、测试和 ADR；只有核对事实或定位失败时才读取原始日志与响应。

| 层级 | 内容和路径 | 读取时机 |
|---|---|---|
| L1 常驻入口 | README、AGENTS、课程索引 | 新成员接手、Agent 建立任务边界、教师开始检查 |
| L2 任务资料 | [规格](specification.md)、[任务卡](tasks.md)、[架构](../ARCHITECTURE.md)、[护栏](../GUARDRAILS.md)、相关 `design/adr` | 实施或复核对应行为之前 |
| L3 按需证据 | `evidence/<日期场景>`、`review/experiments`、本地产物 `runs/logs/reports` | 验证某项主张、对比历史或定位运行失败 |

## 代码与测试定位

| 问题 | 实现入口 | 相关测试/资料 |
|---|---|---|
| 命令怎样转成任务 | `trina/cli.py`、`trina/play.py`、`trina/interactive.py` | `tests/test_interactive.py`，运行手册 |
| 模型怎样选择动作 | `trina/brain/agent.py`、`chat.py`、`prompts.py`、`planner.py`、`goal.py` | `tests/test_brain.py`、`test_decision_evidence.py` |
| 检查从哪里获得授权 | `trina/config.py`、`trina/policy.py`、`configs/ranges/*.yaml` | `tests/test_policy.py`，REQ-03、REQ-11 |
| 范围和高风险动作在哪里拒绝 | `trina/scope.py`，HTTP 和外部工具包装器 | `tests/test_scope.py`、`test_http.py`，护栏手册 |
| 工具怎样装配和执行 | `trina/tools/__init__.py`、`registry.py` 及 recon/web/vuln/exploit/creds | `tests/test_registry.py`、`test_probe_integrity.py` |
| 事实和假设怎样区分 | `trina/state.py`、`trina/hypotheses.py`、`brain/failure.py` | `tests/test_state.py`、`test_insight.py`、`test_decision_evidence.py` |
| 下一检查如何调度 | `trina/brain/scheduler.py` 和主控调用处 | `tests/test_collaboration.py`、`test_insight.py`，ADR-0016/0018 |
| 子角色能做什么 | `trina/collaboration.py`、`trina/tools/specialists.py` | `tests/test_collaboration.py`，ADR-0017 |
| 模型与回放怎样接入 | `trina/llm/base.py`、`omp_backend.py`、`openai_backend.py`、`replay_backend.py` | 录制回归和固定决策验证，不以模型目录等同真实连通性 |
| 证据如何保存和展示 | `trina/audit.py`、`evidence_graph.py`、`board.py`、`report/`、`review.py` | `tests/test_audit.py`、`test_eval_report_lab.py` |
| 靶机和评测怎样运行 | `trina/lab/`、`trina/adapters/`、`trina/eval/` | `scripts/verify_decision_evidence.py`、评测场景 YAML |
| 规格引用如何保持有效 | `docs/course/traceability.json`、`scripts/verify_course_pack.py` | 课程文档检查及隔离坏引用试验 |

路径表中的同目录缩写用于阅读定位，精确文件引用保存在追踪清单中。目标分析工具总计 17 项，可选 `consult_specialist` 单列，不能把角色数加入目标工具数。

## Source of Truth

| 工件或状态 | 权威及修改主体 | 仓库角色与冲突规则 |
|---|---|---|
| Intent 和范围决定 | 维护者明确请求及确认的版本 | `intent.md` 保留来源和接受理由，模型归纳不能自行扩展范围 |
| 产品行为规格 | 维护的 `specification.md` 与课程审查反馈 | 规格定义目标契约；代码冲突时保留缺陷或修订记录，不静默选择有利表述 |
| 当前执行事实 | 对应版本的真实运行、工具结果、状态与审计 | evidence 存精选事实；注释、规划、模型回复不能覆盖实际行为 |
| 有效授权 | 当前加载的 ScopeConfig、策略注册表、操作者审批和撤权 | 提示词只是派生说明；截图和模型引用不授予执行权限 |
| 代码结构和接口 | 当前源码、配置模型及已运行测试 | 架构文档解释实现；旧方案不能证明代码中不存在的模块 |
| 报告和工作台 | 原始 RunState 的派生视图 | 视图不反向修改原始事实；发现不一致回溯到该 run 的状态与审计 |
| 架构取舍 | 版本化 ADR 及替代/后果记录 | 已采用决策与探索性方案分开，变更时保留被替代说明 |
| 课程最终接受 | 教师与真实同行审查者 | 仓库保存意见和签署记录；自动化自查不得代签 |

## 历史资料与新鲜度

`docs/TARGET_PROGRESS.md`、既有实验卡和设计审查记录描述其各自日期与代码状态，分数、测试数量和失败不能直接当作当前结果。当前课程基线以本目录和 `evidence/20261021-course-pack/` 为准，技术参考仍可沿旧资料查看来源。历史证据保留原始版本，不用新结果覆盖旧记录。

追踪检查可以发现失效链接、缺失文件、伪造测试名称、未连接的要求和任务。它不会自动验证段落语义或外部网址，文档出现与代码不一致的功能描述时仍需人工纠正。每次涉及配置默认值、协议、状态字段或验证口径的改动，应同步当前规格、矩阵和相关 ADR。
