# Trina · 全自动渗透测试 Agent

## [设计决策](设计决策.md)

**教师检查重点：[打开《设计决策》](设计决策.md)** · 共 27 节，逐步说明目标解析、阶段推进、假设排序、动作调度、授权审批、工具执行、证据更新和任务收尾的设计理由，并附计算案例、方案比较及测试依据。

[开题报告与第一次汇报](开题报告与第一次汇报.md) · [需求规格](docs/course/specification.md) · [课程材料目录](docs/course/README.md)

## 课程提交与第一次汇报（教师检查入口）

**[打开《开题报告与第一次汇报》](开题报告与第一次汇报.md)**：项目背景、目标与范围、需求规格、技术路线、双层 Harness、实施计划、当前成果、验证证据和后续问题。

| 重点材料 | 检查入口 |
|---|---|
| 完整开题 / 第一次汇报报告 | **[根目录主报告](开题报告与第一次汇报.md)** |
| 决策层每一步的设计理由与实现边界 | **[设计决策](设计决策.md)** |
| 20 条具体需求与规格 | [需求规格](docs/course/specification.md) |
| 26 个场景及逐条验证关系 | [验收场景](docs/course/scenarios.md) · [验收矩阵](docs/course/acceptance-matrix.md) |
| 计划、8 张任务卡、双层 Harness 等完整交付物 | [课程文档目录](docs/course/README.md) |
| 10 月 26 日决策文档核对及 267 项回归结果 | [决策层文档验证](evidence/20261026-decision-docs/README.md) |
| 10 月 21 日课程提交的测试、两轮靶机验证及审查 | [验证事实](evidence/20261021-course-pack/README.md) · [审查记录](review/20261021-course-pack-review.md) |

课程材料按章节 02 V1.1 要求整理，版本日期为 **2026-10-21**。产品能力和已知缺口分别说明；详细规格、计划、证据均在仓库中，可直接检查。

## 项目使用说明

自动化渗透测试 Agent：**大模型 ReAct 决策 + 全套工具封装 + 多靶场适配 + 硬性安全护栏 + 标准报告 + 评测框架**。

```bash
bash scripts/install.sh          # 装成全局命令：之后在任意目录敲 `trina` 即可（默认直连 DeepSeek）
trina                            # 交互控制台（人工可随时介入）
trina play --goal "拿到 flag"     # 一条命令直接开打（自动起靶机 → 渗透 → 报告 → 回收）
trina doctor                     # 环境自检（后端连通性、外部工具、护栏边界）
trina review                     # 评价 Agent：只读复核最近一次运行
```

也可以直接用本地启动器（不装全局命令）：

```bash
bash scripts/dev.sh              # 控制台（后端=omp）
bash scripts/dev.sh --api deepseek   # 控制台（直连 DeepSeek 官方 API）
```

也可以用完整流程（复用 omp 已配置的模型与鉴权，无需额外 API Key）：

```bash
scripts/dev.sh doctor            # 环境自检（后端=omp）
scripts/dev.sh                   # 交互控制台（后端=omp）
scripts/dev.sh play              # 一条命令直接开打（后端=omp）
```

### 换成直连 DeepSeek 官方 API（同一个模型）

```bash
mkdir -p ~/.trina && cp scripts/env.example ~/.trina/env && chmod 600 ~/.trina/env
# 编辑 ~/.trina/env → export DEEPSEEK_API_KEY=sk-xxxx

scripts/dev.sh doctor --api deepseek      # 应显示：后端=openai，模型=deepseek-v4-flash，连通性 OK
scripts/dev.sh --api deepseek             # 交互控制台（直连 DeepSeek）
scripts/dev.sh play --api deepseek --goal "拿到 flag 并验证 SQL 注入" --max-steps 12
```

| 后端 | 何时用 | 密钥 | 实测 |
|---|---|---|---|
| `omp`（默认） | 本机已有 omp 且配好模型 | 无需（复用 omp 鉴权） | 50s/次全流程 |
| `openai` + DeepSeek | 想独立于 omp、或要更低延迟/换机器 | `$DEEPSEEK_API_KEY`（`~/.trina/env`，0600，不入库） | **11s** 打穿 lab 并 CONFIRMED |

> 密钥只从环境变量读取（`scripts/dev.sh` 会 source `~/.trina/env`）；仓库里**不含任何密钥**（见 `docs/SUBMISSION.md`）。

## 五项能力

| 能力 | 落地位置 | 关键设计 |
|---|---|---|
| 1. 自动决策大脑 | `trina/brain/` | ReAct 循环 + PTES 阶段机（事实驱动推进）+ **假设台账**（按价值×置信度×信息增量排序验证）+ 失败分类与循环守卫（无新信息就打转→换参数→换工具→请人介入）+ 预算收敛；动作协议是**文本 JSON**，跨模型可移植、可回放 |
| 2. 工具统一封装 | `trina/tools/` | 17 个目标分析工具 + 1 个可选专职 Agent 咨询工具，统一 `ToolSpec/ToolResult` 契约；**外部二进制优先 + 内置 Python 引擎兜底**（本机无 nmap/nuclei/sqlmap/hydra 也能跑） |
| 3. 多靶场适配 | `trina/adapters/` + `configs/ranges/` | `local` / `docker-compose` / `http-range` / `tsecbench` 四类适配器，内置 Pikachu、DVWA、Vulhub、VulnStack、lab、TSecBench 六套注册表（含 ground truth） |
| 4. 安全合规护栏 | `trina/scope.py` + `trina/audit.py` | 授权网段白名单硬校验、动作分级、破坏性闸门、DoS/删库硬禁止、限速与爆破规模上限、SHA-256 哈希链审计 |
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
和 [ADR-0018](design/adr/0018-fact-context-and-atomic-snapshots.md)。

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
| **对话式控制台** | `trina`（或 `trina console`） | **直接说人话**：`你好` / `帮我看看 127.0.0.1:9099 有什么漏洞` → Trina 回答；需要动手时它自己调工具（照样过授权控制器、护栏、审批与审计）。`/help` 看命令；`/model` 换模型、`/backend` 换后端、`/history` 看历史、`/usage` 看用量、`/save` 持久化 |
| 一条命令开打 | `trina play [--target T] [--range NAME] [--goal G] [--approve MODE]` | 自动准备靶场（默认内置 lab）→ 跑完整任务（含阶段机与结论/覆盖统计）→ 出报告 |
| 运行中介入 | `hint <文本>`（或直接说人话）· `pause/resume` · `stop` · `status` · `/revoke <target_id>` 撤权 | 运行中说人话＝介入提示；审批 `y/n`，**超时即拒绝** |
| 评价 Agent | `trina review [--state runs/<id>/state.json]` | 只读复核：缺证据的声明、结论一致性、覆盖缺口、边界事件、审计链 |

> 对话模式与任务模式共用同一套安全机制：`ToolRegistry`（护栏+审计收口）、`AuthorizationController`（注册表/预算/撤权）、
> 人工审批闸门。模型在对话里同样**不能**扩大范围，也不能把工具输出当指令。

## 授权策略：计划器提出，控制器批准

- 目标与检查项来自**人可读的注册表**：`configs/ranges/*.yaml` 的 `targets:` 与 `tests:` 段。
- 控制器（`trina/policy.py`）在执行层逐动作批准：注册 → 预算 → 撤权；模型**没有写权限**，
  也不能用"这应该安全"当许可。模式：`free` / `guided`(默认) / `strict`（只接受注册 `target_id` + `test_id`）。
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

## 常用命令

```bash
python3 -m trina doctor                                   # 环境自检（后端连通性、外部工具、护栏边界）
python3 -m trina tools --verbose                          # 工具清单与参数表
python3 -m trina guard check 8.8.8.8 127.0.0.1            # 目标是否授权（8.8.8.8 会被拒）
python3 -m trina guard argv -- sqlmap -u http://x --os-shell   # 命令是否会被拒
python3 -m trina tool port_scan --target 127.0.0.1        # 单工具执行（带护栏与审计）
python3 -m trina run --target 127.0.0.1:9099 --allow-exploit   # 放开到 exploit 等级
python3 -m trina watch                                    # 实时监视最近一次运行（工作台视图）
python3 -m trina watch <run_id> --once                    # 只打印一次（脚本留档）
python3 -m trina ranges list                              # 靶场适配器状态
python3 -m trina eval run --suite web                     # 跑评测套件（指标 + 报告）
python3 -m trina eval adversarial --port 9100             # 蓝队对抗代理（WAF/封禁/限速/篡改/蜜罐）
python3 -m trina lab --http --ssh                         # 本地靶机（HTTP + 弱口令 SSH）
python3 -m unittest discover -s tests                   # 完整回归，数量和结果以本轮原始输出为准
```

## 可选 omp 集成（在本目录里开启会话）

- `.omp/extensions/trina.ts`：把 Trina 暴露成 omp 工具 —— `trina_run` / `trina_tool` / `trina_guard` / `trina_tools` / `trina_report`，并注册 `/pentest` 命令；
  同时对 omp 内建 `bash` 做**第二道防线**拦截（DoS/删库/`--os-shell` 等参数、对公网地址的攻击性工具调用）。
- `.omp/skills/pentest-agent/SKILL.md`：渗透作业技能（何时用、怎么用、护栏规则、报告规范）。
- `.omp/config.yml`：项目级配置覆盖（默认留空，示例见注释）。

## 目录

```
trina/            核心包（brain / tools / adapters / report / eval / lab；洞察层：hypotheses / failure / evidence_graph / board）
configs/        主配置 + 靶场注册表 + 字典
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
- Docker（Vulhub/VulnStack 一键起靶场）可选；WSL 需启用 Docker Desktop 集成，或把靶场跑在宿主机后按 `configs/ranges/*.yaml` 填 URL。

## 安全边界（务必阅读 `docs/GUARDRAILS.md`）

- 只允许 `configs/trina.yaml#scope.authorized_cidrs` / `authorized_hosts` 内的目标；公网默认拒绝；回环可控。
- 动作分级 `detect < verify < exploit`，默认上限 `verify`；破坏性动作默认关闭，且需显式主机白名单。
- **永不允许**：DoS/洪泛、删库删表、`--os-shell/--file-write/--dump-all`、写 webshell、格式化/删除系统文件、执行远端下载脚本。
- 每次动作（含被拒绝的）都写入审计哈希链，`trina audit verify` 可检出篡改。

> 仅可用于**已获授权**的实训/竞赛靶场。越界测试是违法行为，本项目的第一道防线就在 `trina/scope.py`。
