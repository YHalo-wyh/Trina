# 架构与模块契约

## 1. 总览

```mermaid
flowchart TB
    CLI["trina.cli<br/>run/tool/guard/eval/report/lab/audit"]
    OMPEXT[".omp/extensions/trina.ts<br/>omp 工具 + bash 第二道防线"]
    AGENT["trina.brain.agent.PentestAgent<br/>ReAct 循环"]
    PLANNER["trina.brain.planner.Planner<br/>PTES 阶段机（事实驱动）"]
    PROMPTS["trina.brain.prompts<br/>系统提示 + JSON 动作协议 + 容错解析"]
    REG["trina.tools.registry.ToolRegistry<br/>参数校验→护栏→审计→超时→归一"]
    GUARD["trina.scope.ScopeGuard<br/>唯一放行判据"]
    AUDIT["trina.audit.AuditLog<br/>SHA-256 哈希链"]
    STATE["trina.state.RunState<br/>hosts/findings/creds/flags/evidence/timeline"]
    TOOLS["trina.tools.{recon,web,vuln,exploit,creds}<br/>17 个工具"]
    HTTP["trina.tools.http.HttpClient<br/>带护栏的 HTTP 出口"]
    EXT["trina.tools.external.run_external<br/>外部二进制（shell=False + 护栏）"]
    LLM["trina.llm<br/>omp / openai 兼容 / replay"]
    ADAPT["trina.adapters<br/>local/docker/http-range/tsecbench"]
    EVAL["trina.eval<br/>harness + metrics + adversarial"]
    LAB["trina.lab<br/>故意脆弱靶机"]
    REPORT["trina.report<br/>Markdown + 自包含 HTML"]

    CLI --> AGENT
    OMPEXT --> CLI
    AGENT --> PLANNER --> PROMPTS
    AGENT --> LLM
    AGENT --> REG
    REG --> GUARD
    REG --> AUDIT
    REG --> TOOLS
    TOOLS --> HTTP --> GUARD
    TOOLS --> EXT --> GUARD
    TOOLS --> STATE
    AGENT --> STATE
    STATE --> REPORT
    ADAPT --> CLI
    EVAL --> AGENT
    LAB -.-> EVAL
```

## 2. 单步数据流

```
RunState.summary_for_prompt()  ┐
Planner.directive(phase)       ├→ user prompt ┐
上一步观察 + 注入提示          ┘               │
                                              ├→ LLM → 文本
system = 角色 + 铁律 + 工具清单 + 授权边界 ────┘        │
                                                parse_action()（多级容错）
                                                        │
                        ┌───────────────────────────────┴────────────┐
                        │ {"action": …, "params": …}                 │ {"final": …}
                        ▼                                            ▼
        ToolRegistry.execute(name, params, ctx)                  收敛与终态判定
        ├─ 参数校验（未知/缺失/类型/enum）
        ├─ 可用性（外部二进制缺失→工具自述原因）
        ├─ 护栏：action_level → destructive → check_target
        ├─ 审计 tool.start
        ├─ 执行（线程池 + 超时；异常一律转为结果）
        ├─ state.absorb(result.facts) / evidence 入账
        └─ 审计 tool.end（含 stdout 摘要的哈希）
                        │
                        ▼
        ToolResult.to_observation() → 下一步的观察
```

## 3. 模块契约

| 模块 | 关键类型 / 函数 | 约定 |
|---|---|---|
| `trina.config` | `PAIConfig.load(path, overrides)`、`ScopeConfig`、`scope_prompt_block()` | 配置是护栏的**唯一**数据源；`TRINA_<SECTION>_<KEY>` 环境变量可覆盖标量；未知字段直接报错（宁可启动失败） |
| `trina.scope` | `ScopeGuard.check_target/check_argv/check_payload/check_http/check_action_level/check_destructive/check_brute_scale/throttle`；`Decision`；`ScopeViolation` | 所有判定返回 `Decision`（含 `reason`/`hint`/`category`），拒绝时把 `hint` 回灌给模型换路径 |
| `trina.audit` | `AuditLog.open/append/verify/summary` | 每条记录 `hash = sha256(canonical(record - hash))`，`prev_hash` 链接；写入即 fsync；敏感字段自动脱敏 |
| `trina.state` | `RunState.absorb(facts)`、`add_finding`、`summary_for_prompt` | 工具只声明 facts（`ports/services/paths/credentials/flags/findings/notes`），状态合并逻辑集中在此 |
| `trina.tools.registry` | `ToolSpec`/`ParamSpec`/`ToolResult`/`ToolContext`/`Tool`/`ToolRegistry.execute` | 执行链是**唯一**入口；工具实现 `run(ctx, **params)`，返回 `ToolResult` |
| `trina.tools.http` | `HttpClient.request/get/post`、`HttpResult.as_evidence()` | 出网唯一出口：护栏 → 限速 → 请求 → 证据化；护栏拒绝时返回带 `error` 的结果而不是抛异常 |
| `trina.tools.external` | `run_external(argv, ctx, …)`、`which/have` | `shell=False`、先护栏后执行、命令与输出摘要进审计 |
| `trina.brain.agent` | `PentestAgent(config, registry=, backend=, on_step=).run(target, goal=, max_steps=, max_seconds=, record_path=) -> RunState` | 评测框架与 CLI 都只依赖这个签名 |
| `trina.brain.planner` | `Planner.tick/objective_met/directive/force_advance/should_finish` | 阶段推进只看**事实**；每阶段有步数预算，停滞可强制推进 |
| `trina.brain.goal` | `GoalRequirements.parse(goal)`、`unsatisfied(state)/soft_hints/satisfied` | 把任务描述解析成硬条件（漏洞类 / 路径 / 端口）；未满足时不允许收敛，也不允许把"取巧拿到 flag"判为成功；**不使用 ground truth** |
| `trina.llm` | `make_backend(LLMConfig)`、`LLMBackend.complete(system, user)`、`LLMUsage` | 后端实现只管"给提示词 → 返回文本 + 用量"；`ledger` 汇总进报告 |
| `trina.adapters` | `load_adapter/list_adapters`、`RangeAdapter.prepare/status/ground_truth/check_flag`、`score_tsecbench` | 靶场自描述（目标 + ground truth + flag 模式），评测框架据此自动判分 |
| `trina.eval` | `EvalHarness(config, runner=).run(suite)`、`compute_metrics`、`AdversarialProxy` | `runner` 可注入，便于离线/回放评测 |
| `trina.report` | `build_report(state, cfg, meta=, audit_summary=) -> Report`、`Report.save()` | HTML 自包含（内联 CSS/SVG，无外部资源） |

## 4. 关键设计决策与理由

1. **大脑自研（Python ReAct）而不是把决策交给 omp 内建 agent**
   护栏、审计、指标、超时、停滞检测都需要确定性控制；评测要求可复现。
   omp 仍然承担它最强的部分——**模型接入与鉴权**（`omp -p --mode=json`，含 usage/cost）。
2. **动作协议用文本 JSON**：不依赖各家 function-calling，换模型不改协议；单步动作可解析、可校验、可回放（`--record` / `--replay` 是离线回归的基础设施）。
3. **工具"外部优先 + 内置兜底"**：实训环境普遍缺 nmap/nuclei/sqlmap/hydra；把能力都实现一遍不现实，
   但**评测必须能跑**，所以每个能力都有一个纯 Python 的最小可用实现，外部工具存在时自动升级并在结果里标注 `engine`。
4. **执行链单点收口**：护栏如果有第二个入口，就等于没有。`ToolRegistry.execute()` 之外不允许有出网/执行路径（新增工具走注册表）。
5. **事实驱动阶段机**：让模型自称"我现在在利用阶段"不可验证；用可观测事实（有端口 / 有技术栈 / 有已验证漏洞 / 有 flag）来推进阶段，并给每阶段步数预算避免原地打转。
6. **哈希链审计**：实训/竞赛都要求"全程可追溯"，且需要能证明日志未被事后修改；链式哈希是最小代价的方案。

## 5. 错误隔离与收敛

| 情形 | 行为 |
|---|---|
| 工具抛异常 | 捕获 → `ToolResult(ok=False, error=…)`，计入 `counters.errors`，审计 `tool.error`，循环继续 |
| 工具超时 | 线程池 `future.result(timeout)` → 超时结果 + 提示换更快工具（预算取 `min(run.tool_timeout, 剩余时间)`） |
| LLM 调用失败 | 重试 2 次（1.5s/3s 退避）；累计 3 次失败则终态 `failed` |
| 模型输出非 JSON | 多级容错解析 → 仍失败则注入"只输出 JSON"提示并继续（计入 errors） |
| 动作被护栏拒绝 | 拒绝结果 + `hint` 回灌；计入 `counters.denied`；写入审计 `tool.denied` |
| 同一动作重复 | 连续 ≥ `llm.stall_limit` 次 → 注入纠正提示 + 强制阶段推进 |
| 步数/时长耗尽 | 循环退出，按已有事实判定终态（`success` / `partial` / `failed`） |
| 目标越界 | 预检阶段直接 `blocked`，**不进入 LLM**（模型无权授权） |

## 6. 状态与终态判定

```
flags 或 存在 verified/exploited 漏洞 → success
仅 detected 级漏洞                    → partial
无任何发现                            → failed
目标越界被预检拒绝                    → blocked
```

`RunState` 同时是报告的输入：`hosts / findings / credentials / flags / evidence / timeline / steps / ledger / counters / artifacts`。

## 7. 扩展点

| 想加什么 | 改哪里 | 注意 |
|---|---|---|
| 新工具 | `trina/tools/<module>.py` 定义 `Tool` 子类 + 导出 `TOOLS` | 声明 `action_level`、`params`、`produces`；用 `ctx.http`/`run_external` |
| 新靶场 | `configs/ranges/<name>.yaml`（+ 必要时 `trina/adapters/` 新适配器类） | 给出 `targets`、`ground_truth`、`flag_patterns` |
| 新评测场景 | `trina/eval/scenarios/<suite>.yaml` | 必须声明 ground truth 与 `scoped_classes`，否则 precision 口径失真 |
| 新 LLM 后端 | `trina/llm/<name>_backend.py` + `make_backend` 分支 | 只需实现 `complete()` 并填 `usage` |
| 新报告格式 | `trina/report/` | 保持 HTML 自包含 |
| omp 侧新工具 | `.omp/extensions/trina.ts` | 一律转调 `python3 -m trina …`，不要绕过护栏 |
