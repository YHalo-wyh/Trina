# Trina · 全自动渗透测试 Agent

## 项目检查入口（0.3.0）

| 文档 | 检查内容 |
|---|---|
| [需求](需求.md) | 使用者、场景、21 项功能需求、8 项质量要求、当前状态与验收口径 |
| [规约](规约.md) · [机器规约](规约.json) | CLI 与 Agent 接口、状态转换、规则 ID、实现路径、测试方法与白名单验证命令 |
| [设计决策](设计决策.md) | 原有单 Agent 决策链，以及 0.2.0—0.3.0 的更新记录、修改原因与兼容边界 |
| [更新日志](CHANGELOG.md) | 各版本面向使用者的变更摘要、兼容说明与验证状态 |

《设计决策》第 1—27 节保留早期基线，第 28—43 节记录 0.2.0 演进，第 44—45 节记录 0.3.0 的 full trust、系统提示词与 Commander 情报决策。旧课程规格与历史证据继续按原版本保存。

六项提交内容均有仓库入口，并由机器规约检查是否缺失：

| 完成项 | 仓库证据 |
|---|---|
| 需求 | [当前需求](需求.md) |
| 规约 | [行为规约](规约.md) · [机器规约](规约.json) |
| ADR（含前 3 项前置决策） | [ADR 索引](design/README.md) · `design/adr/0001—0003` |
| 项目级 Harness | [双层 Harness](docs/course/harness-manifest.md) · [项目约定](AGENTS.md) · [AI 规则](design/ai-rules.md) |
| Global Plan / Tasks | [全局计划](docs/PLAN.md) · [实施计划](docs/course/plan.md) · [任务卡](docs/course/tasks.md) |
| 可执行代码与评测证据 | `trina/` · `tests/` · [证据索引](evidence/README.md) · [评测记录](评测/README.md) |

```powershell
.\.venv\Scripts\python.exe -m scripts.verify_contract --execute --json
```

[开题报告与第一次汇报](开题报告与第一次汇报.md) · [需求规格](docs/course/specification.md) · [课程材料目录](docs/course/README.md)

## 课程提交与第一次汇报（教师检查入口）

**[打开《开题报告与第一次汇报》](开题报告与第一次汇报.md)**：项目背景、目标与范围、需求规格、技术路线、双层 Harness、实施计划、当前成果、验证证据和后续问题。

| 重点材料 | 检查入口 |
|---|---|
| 完整开题 / 第一次汇报报告 | **[根目录主报告](开题报告与第一次汇报.md)** |
| 决策层每一步的设计理由与实现边界 | **[设计决策](设计决策.md)** |
| 20 条历史课程需求与规格 | [需求规格](docs/course/specification.md) |
| 26 个场景及逐条验证关系 | [验收场景](docs/course/scenarios.md) · [验收矩阵](docs/course/acceptance-matrix.md) |
| 计划、8 张任务卡、双层 Harness 等完整交付物 | [课程文档目录](docs/course/README.md) |
| 10 月 26 日决策文档核对及 267 项回归结果 | [决策层文档验证](evidence/20261026-decision-docs/README.md) |
| 10 月 21 日课程提交的测试、两轮靶机验证及审查 | [验证事实](evidence/20261021-course-pack/README.md) · [审查记录](review/20261021-course-pack-review.md) |

课程材料按章节 02 V1.1 要求整理，版本日期为 **2026-10-21**。产品能力和已知缺口分别说明；详细规格、计划、证据均在仓库中，可直接检查。

## 项目使用说明

**CLI 界面更新（2026-10-28）**：[首页、审批与工作台使用说明](docs/CLI_UI.md) · [单主控 / 多 Agent / 黑板研究复核](design/adr/0017-specialist-consultation-without-blackboard.md)

**V2 智能体群首版**：[Swarm 使用说明](docs/SWARM.md) · `trina swarm --target http://127.0.0.1:9099 --goal "验证目标" --workers 2`

自动化渗透测试 Agent：**大模型 ReAct 决策 + 全套工具封装 + 多靶场适配 + 默认完全信任 + 标准报告 + 评测框架**。

```bash
scripts/install.sh               # 装成全局命令，使用 /model 选择已保存连接
trina                            # 交互控制台（人工可随时介入）
trina goal set "验证登录流程"     # 保存当前项目目标；后续 run/play 默认沿用
trina play --target 127.0.0.1    # 一条命令执行并生成报告
trina schedule add --at +10m --target 127.0.0.1  # 一次性定时启动
trina doctor                     # 环境自检（后端连通性、外部工具、护栏边界）
trina review                     # 评价 Agent：只读复核最近一次运行
```

也可以直接用本地启动器（不装全局命令）：

```bash
scripts/dev.sh                   # 控制台
scripts/dev.sh doctor            # 环境自检
```

也可以用完整流程（复用 omp 已配置的模型与鉴权，无需额外 API Key）：

```bash
scripts/dev.sh doctor
scripts/dev.sh
scripts/dev.sh play --target 127.0.0.1
```

直连模型在 `/model` 中以一条供应商配置保存请求地址、API Key、模型和思考强度；无需再维护启动脚本参数或旧 `~/.trina/env` 选择项。

| 后端 | 何时用 | 密钥 | 实测 |
|---|---|---|---|
| `omp`（可选） | 本机已有 omp 且配好模型 | 无需（复用 omp 鉴权） | 50s/次全流程 |
| `openai` + DeepSeek（项目默认） | 想独立于 omp、或要更低延迟/换机器 | `/model` 保存供应商，或设置 `$DEEPSEEK_API_KEY` 环境变量 | **11s** 打穿 lab 并 CONFIRMED |

> `/model` 以供应商为单位添加、编辑、删除和切换配置。名称、请求地址、API Key、模型与备注在同一表单中管理，支持 Key 显隐、地址连通测试及模型验证；主页面保留模型和思考强度选择。配置保存在仓库外 `~/.trina/providers.json`，详见 [供应商配置与 30 个预设](docs/MODELS.md)。临时产物的整理见 [仓库清理记录](docs/REPOSITORY_LAYOUT.md)。

任务 Agent、对话 Assistant 和专职 Worker 共用稳定的安全工程师系统提示词，角色具有当前任务环境中的最高执行权限，并按 `Engineering / Tool Policy / Exploration / Delegation / Workflow / Delivery / Completeness` 七个模块工作。动作协议、工具参数、范围、事实和预算继续作为每轮运行数据注入，避免把动态内容固化进 system。当前网页工具使用 `requests` / `aiohttp` 与 `BeautifulSoup`，尚未封装真实浏览器；浏览器自动化拟采用 **Playwright Python + Chromium**，状态说明见 [架构文档](docs/ARCHITECTURE.md#9-浏览器能力状态)。

资料检索使用 **Google / Bing**；项目搜索工具尚未接入，这两个来源已纳入浏览器接入约定。

首页使用斜切填充的 TRINA 大字，带切角、深色侧边和稀疏星点，字面呈粉紫渐变与柔和高光，下方只显示版本号 `v0.3.0`。字形直接由文本绘制，窄窗口自动使用紧凑字形；`--plain` 保留相同字符布局并关闭颜色。运行中仍可输入补充，按一次 Enter 发送，多行粘贴与发送回显保持。

```text
       ·                  ✦                  ·
   ▄█████▓▄  ▄████▓▄    ▄███▓▄   █▓▄   █▓    ▄█▓▄
  ▀▀ █▓ ▀▀  █▓   ▀█▓     █▓     ██▓▄  █▓   ▄█▀▀█▄
     █▓     █▓   ▄█▓     █▓     █▓▀█▄ █▓  █▓    █▓
    █▓     █████▓▀      █▓     █▓ ▀█▄█▓  █▓    █▓
    █▓     █▓ ▀█▄       █▓     █▓  ▀██▓  ███████▓
   █▓     █▓  ▀█▄      █▓     █▓   ▀█▓  █▓    █▓
  ▄█▓▄    █▓   ▀█▓   ▀███▓▀   █▓    █▓  █▓    █▓
                      v0.3.0
```

交互终端的输入与会话用量固定在屏幕底部，持续显示 Token、输入 / 输出和缓存命中率；回复卡片只呈现正式输出。思考状态使用动态星形标记，工具过程与最终回答分开排版。`/new` 清零会话用量，切换模型继续累计；`/usage` 查看详情。布局和统计口径见 [CLI 界面说明](docs/CLI_UI.md#固定底栏与过程排版)。

## 五项能力

| 能力 | 落地位置 | 关键设计 |
|---|---|---|
| 1. 自动决策大脑 | `trina/brain/` | ReAct 循环 + PTES 阶段机（事实驱动推进）+ **假设台账**（按价值×置信度×信息增量排序验证）+ 失败分类与循环守卫（无新信息就打转→换参数→换工具→请人介入）+ 预算收敛；动作协议是**文本 JSON**，跨模型可移植、可回放 |
| 2. 工具统一封装 | `trina/tools/` | 17 个目标分析工具 + 1 个可选专职 Agent 咨询工具，统一 `ToolSpec/ToolResult` 契约；**外部二进制优先 + 内置 Python 引擎兜底**（本机无 nmap/nuclei/sqlmap/hydra 也能跑） |
| 3. 靶场适配 | `trina/adapters/` | `local` / `docker-compose` / `http-range` / `tsecbench` 四类适配器；仓库不再携带静态靶场注册表，按需从 `~/.trina/ranges` 或显式 YAML 载入 |
| 4. 执行信任与审计 | `trina/scope.py` + `trina/audit.py` | 默认 `full` 信任并自动执行；可切换 `restricted` 恢复网段、动作和载荷边界；两种模式都保留超时、取消、限速和 SHA-256 哈希链审计 |
| 5. 标准报告 | `trina/report/` | Markdown + 自包含 HTML（内联 CSS/SVG，无外部依赖），含执行摘要、攻击路径、漏洞详情（CVSS/CWE/证据/复现/修复）、**证据链与假设台账**（mermaid 关系图，结论可反向追溯）、凭据与 flag、工具时间线、合规声明 |

### 决策可解释（洞察层）

- **假设台账**（`trina/hypotheses.py`）：模型每步可带 `hypothesis`，系统按 `置信度×价值×信息增量/(成本+λ·风险)` 排序，
  用结果确定性推进状态（`open → verifying → confirmed/refuted/blocked`）；收尾时未答完的问题显式标为"未解决"。
  自动确认要求 `vuln_class/target/parameter` 与本次带证据的 `verified/exploited` 发现匹配；
  后续动作通过 `hypothesis_id` 复用台账。连续无新信息只降低优先级，保留未解决状态。
- **失败分类与循环守卫**（`trina/brain/failure.py`）：结果归因成 boundary/tool/environment/budget/hypothesis 五类并计数；
  同工具+同参数+同结果视为无新信息，递进提示"换参数 → 换工具 → 请人介入"。
- **证据关系图**（`trina/evidence_graph.py`）：目标→主机→端口→路径→发现→证据（+ 检查项/假设→发现、→flag），
  既是报告章节也是复核依据。
- **确定性调度**（`trina/brain/scheduler.py`）：每步把台账里"能绑定注册检查项"的最高优先级假设派下去
  （任务要求的漏洞类优先；模型没提假设时直接派该类的注册检查项），并记录**遵从率**（未遵从进审计哈希链）；
  调度只指定方向，执行仍经控制器与护栏。关掉它：`--set run.hypothesis_scheduler=false`。
- **工作台**：`trina/brain` 每步落盘 `state.json`，`/board`、`/hypotheses`、`/graph` 与 `trina watch` 共用一份渲染，
  另给四项内部指标：Cost per Success、Time to Evidence、Redundant Action Rate、Recovery Rate（口径见 `docs/EVAL.md`）。
- **成本实话**：A/B 实测（`review/experiments/E4-insight-layer.md`，两臂各 3 次）洞察层让同任务 token **+44%**、
  Time to Evidence +3.8s，换来 3 次里 2 次 CONFIRMED（关臂 1 次）——方向性数据，样本量不足以下"提效"结论；
  预算紧时可用 `--set run.hypothesis_ledger=false --set run.loop_guard=false` 跑基线档。

目标完成度、已验证缺口与实施顺序见 [目标差距评估](docs/TARGET_PROGRESS.md)。
固定决策与真实 lab 请求的两轮机制回归：`python -m scripts.verify_decision_evidence`。

### 按需咨询专职 Agent

主控可以通过注册工具 `consult_specialist` 咨询独立上下文的 `recon`、`web`、`review` 角色。
子角色只返回建议；采纳后仍需由主控发出普通工具动作，重新经过控制器、人工审批、护栏和审计。
不新增共享可写黑板，现有 `/board` 与 `watch` 保留为只读进度视图。

默认关闭；开启：`--set run.specialists=true`。每段会话默认最多咨询 3 次，模型失败照样扣额度；
strict 模式下咨询工具也需要显式注册检查项。尚无实测数据证明多角色提高成功率，因此保留单主控基线。

完整取舍、实现边界与后续缺口见 [Agent 设计审查](docs/AGENT_DESIGN_REVIEW.md)，
关键决策见 [ADR-0017](design/adr/0017-specialist-consultation-without-blackboard.md)
、[ADR-0018](design/adr/0018-fact-context-and-atomic-snapshots.md)
和 [ADR-0020](design/adr/0020-security-engineer-prompt-and-commander-intelligence.md)。

`trina swarm` 的每个题目也保持独立上下文和事实图。Commander 会检查已完成任务与当前任务的目标、显式依赖和验证方向，只在相似度达到阈值时转发资产、已验证发现、已验证凭据和工件位置；Flag、聊天历史和其他题目的 `RunState` 不会被合并。接收情报的 Worker 必须在自己的目标上重新验证后才能入账。

## 已封装的工具（17 项目标分析 + 1 项可选协作）

| 类别 | 工具 | 动作等级 |
|---|---|---|
| recon | `port_scan` `service_probe` `http_fingerprint` `tls_info` | detect |
| web | `dir_bruteforce` `web_surface` `param_reflect_scan` | detect |
| vuln | `poc_scan`（21 个只读 PoC） `nuclei_scan`（缺 nuclei 自动降级内置引擎） | detect |
| exploit | `sqli_test` `cmd_inject_test` `lfi_test` `file_upload_test` `rce_exec` | verify / exploit |
| creds | `weak_password_brute`（ssh/ftp/mysql/redis/http_basic/http_form） `http_form_login` `flag_hunt` | exploit / verify |
| collaboration | `consult_specialist`（recon / web / review，仅建议，默认关闭） | detect |

## 人的位置：审批、介入、复核

| 能力 | 命令 | 说明 |
|---|---|---|
| **对话式控制台** | `trina`（或 `trina console`） | **直接说人话**：需要动手时直接调用工具。默认 `full` 信任且人工审批关闭；只有显式 `--approve` 才进入确认。`/help` 看命令，`/model` 管理模型 |
| 一条命令开打 | `trina play [--target T] [--range NAME] [--goal G] [--approve MODE]` | 自动准备靶场（默认内置 lab）→ 跑完整任务（含阶段机与结论/覆盖统计）→ 出报告 |
| 运行中介入 | `hint <文本>`（或直接说人话）· `pause/resume` · `stop` · `status` · `/revoke <target_id>` 撤权 | 运行中说人话＝介入提示；显式开启审批后可用 `y/n`，**超时即拒绝** |
| 评价 Agent | `trina review [--state runs/<id>/state.json]` | 只读复核：缺证据的声明、结论一致性、覆盖缺口、边界事件、审计链 |

> 对话模式与任务模式共用 `ToolRegistry`、执行超时和审计链。默认 `scope.trust_mode=full`、`approve=off`，不会逐目标或逐动作申请；显式切换 `restricted` 或 `--approve` 才启用对应限制。

## 授权策略：计划器提出，控制器批准

- 默认 `policy.mode=free` 且 `scope.trust_mode=full`，已注册工具直接执行。需要旧范围边界时使用 `--set scope.trust_mode=restricted`；需要检查项约束时再配置 `policy.registry_dirs`。
- 控制器（`trina/policy.py`）在执行层逐动作批准：注册 → 预算 → 撤权；模型**没有写权限**，
  也不能用"这应该安全"当许可。模式：`free`（默认）/ `guided` / `strict`（只接受显式注册的 `target_id` + `test_id`；空注册表直接拒绝）。
- 预算与停止合同：`policy.max_checks` / `logic_seconds` / `stop_on_no_new_evidence`，到点即停并留下**已测/未测/原因**。
- 撤权 = 减权不绕路：`revoke <target_id>` 后对该目标的一切检查被拒（换地址/换工具同样被拒）。
- 结论三分类：`CONFIRMED`（有证据）/ `NOT_CONFIRMED`（**仅限已执行检查**）/ `INCONCLUSIVE`（缺口、工具失败、授权受限）。

## 交付物与提交边界

六类内容类别与其映射、**什么该提交/什么不该提交**、证据与复核分离：见 [`docs/SUBMISSION.md`](docs/SUBMISSION.md)。

| 类别 | 位置 |
|---|---|
| brief（问题/范围/验收ID/责任） | [`brief/README.md`](brief/README.md)、`docs/PLAN.md` |
| design（ADR/AI 规则/状态与权限） | [`design/`](design/README.md)（**14 篇 ADR**：8 篇架构 + 6 篇实证；[`ai-rules.md`](design/ai-rules.md)） |
| eval（用例与判定） | [`eval/README.md`](eval/README.md)、`trina/eval/scenarios/*.yaml`、`docs/EVAL.md` |
| evidence（运行事实） | [`evidence/README.md`](evidence/README.md) + 精选样本 |
| release（启动/重置/停止/恢复/已知问题） | [`release/README.md`](release/README.md)、`docs/RUNBOOK.md` |
| review（实验/Change/Finding/互评签署） | [`review/README.md`](review/README.md) + 复核记录 |

> 全量 `runs/`、`logs/`、`reports/` **不入库**（可重复生成、体积大）；令牌与真实目标数据**永不提交**。

## 评测

两轮评测（详见 `docs/EVAL.md`）：

```bash
python3 -m trina eval run --suite web           # Web 专项（7 场景：sqli/lfi/rce/upload/弱口令/信息泄露/夺旗 + 对抗场景）
python3 -m trina eval run --suite host          # 主机专项（SSH 弱口令/端口覆盖/服务识别）
python3 -m trina eval run --suite integrated    # 综合（蓝队对抗 + TSecBench bundle 回放）
python3 -m trina eval adversarial --port 9100   # 只起对抗代理（WAF/封禁/限速/篡改/蜜罐）
```

- 指标：通过率、平均/P50/P90 耗时、precision、recall、误报率、遗漏率、**范围外发现（不计误报）**、端口覆盖度、服务识别准确率、flag 获取率、被拒动作数。
- **无需手工起靶机**：场景用 `fixtures` 声明依赖，评测器自动拉起/回收本地 lab 靶机。
- **零成本离线回归**：`--set llm.backend=replay --set llm.replay_script=<录制脚本>`（`trina run --record` 录制）。
- 结果落盘：`reports/eval/<suite>-<ts>.md`（中文指标表）与 `.json`（逐场景明细，可机器分析）。

### TSecBench 平台接入（官方 Agent 答题 API）

```bash
export BENCHMARK_BASE_URL='...' BENCHMARK_TOKEN='...'
python3 -m trina bench --dry-run                # 只列题目
python3 -m trina bench                          # 逐题起容器 → 渗透 → 提交 flag → 回收
```

托管模式镜像与平台操作见 [`docs/TSECBENCH_AGENT.md`](docs/TSECBENCH_AGENT.md)；镜像入口为
`docker build -t trina-tsecbench .`，平台注入 `BENCHMARK_*` 后容器自动开始解题。
OpenCode Go 的接入与两轮单题冒烟结果见 [2026-11-04 评测报告](评测/2026-11-04_TSecBench_OpenCode_Go/评测报告.md)。
按日期整理的评测报告、截图和脱敏结果数据见 [评测目录](评测/README.md)。

## 常用命令

```bash
python3 -m trina doctor                                   # 环境自检（后端连通性、外部工具、护栏边界）
python3 -m trina tools --verbose                          # 工具清单与参数表
python3 -m trina guard check 8.8.8.8 127.0.0.1            # full 模式下均直接放行
python3 -m trina tool port_scan --target 127.0.0.1        # 单工具执行并记录审计
python3 -m trina run --target 127.0.0.1:9099              # 默认无需 --allow-exploit
python3 -m trina run --target 127.0.0.1 --set scope.trust_mode=restricted  # 显式恢复旧边界
python3 -m trina watch                                    # 实时监视最近一次运行（工作台视图）
python3 -m trina watch <run_id> --once                    # 只打印一次（脚本留档）
python3 -m trina ranges list                              # 靶场适配器状态
python3 -m trina eval run --suite web                     # 跑评测套件（指标 + 报告）
python3 -m trina eval adversarial --port 9100             # 蓝队对抗代理（WAF/封禁/限速/篡改/蜜罐）
python3 -m trina lab --http --ssh                         # 本地靶机（HTTP + 弱口令 SSH）
python3 -m unittest discover -s tests                   # 完整回归，数量和结果以本轮原始输出为准
```

## 可选 omp 集成（在本目录里开启会话）

- `.omp/extensions/trina.ts`：把 Trina 暴露成 omp 工具 —— `trina_run` / `trina_tool` / `trina_guard` / `trina_tools` / `trina_report`，并注册 `/pentest` 命令；扩展不再额外拦截 omp 内建 `bash`。
- `.omp/skills/pentest-agent/SKILL.md`：渗透作业技能（何时用、怎么用、护栏规则、报告规范）。
- `.omp/skills/solve-challenge` 与 `.omp/skills/ctf-*`：项目级 CTF 分类、解题参考、相似题检索与 writeup 技能；固定上游提交及 MIT 许可见 `.omp/skills/CTF_SKILLS_SOURCE.md`。
- `.omp/config.yml`：项目级配置覆盖（默认留空，示例见注释）。

## 目录

```
trina/            核心包（brain / tools / adapters / report / eval / lab；洞察层：hypotheses / failure / evidence_graph / board）
configs/        主配置
docs/           方案与文档（PLAN / ARCHITECTURE / GUARDRAILS / EVAL / RUNBOOK）
.omp/           可选 omp 集成（扩展 + 技能 + 配置覆盖）
logs/           审计哈希链（每 run 一个 JSONL）
runs/           运行工作区（state.json 等）
reports/        报告（Markdown / HTML / 评测报告）
tests/          单元测试（护栏/审计/注册表/大脑/状态/报告/评测）
```

## 环境依赖

- Python ≥ 3.10，已装：`pyyaml requests paramiko rich`（本机另有 `bs4 aiohttp httpx`）。
  缺失可选依赖时对应能力会**明确降级**而不是崩溃（如无 `pymysql` 则跳过 MySQL 弱口令验证）。
- 外部工具（nmap / nuclei / sqlmap / hydra / ffuf / nmap 等）**非必需**：存在即优先使用，缺失自动降级到内置引擎并在结果里标注 `engine=builtin`。
- Docker 靶场可选；WSL 需启用 Docker Desktop 集成。自定义适配配置放在 `~/.trina/ranges` 或通过 `--range <YAML路径>` 显式传入。

## 执行信任模式（详见 `docs/GUARDRAILS.md`）

- 默认 `scope.trust_mode=full`：公网、`exploit`、破坏性方法、载荷模式和尝试规模不触发授权拒绝，也不弹人工审批。
- `scope.trust_mode=restricted`：恢复原来的网段白名单、动作等级、破坏性列表、禁止模式和规模限制。
- 两种模式都保留参数类型校验、工具注册、超时、取消、速率控制和审计哈希链。

> 信任模式决定软件是否阻塞执行；任务授权范围由使用者负责。
