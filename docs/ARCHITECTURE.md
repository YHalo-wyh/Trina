# 架构与模块契约

决策流程及每一步的设计理由详见[《设计决策》](设计决策/设计决策.md)。该文按实际执行顺序展开输入输出、规则、替代方案、失败分支和验证依据；本页保留总体模块契约。系统提示词与 Commander 情报部分于 2026-11-11 对照实现核对。

## V2：统一 Agent Runtime 与事件驱动任务层

任务、对话与 Worker 共用 `AgentRuntime`：一次模型决策可以提交多个原生工具调用，执行器按顺序处理，并用 `_depends_on` 表达本批次前序依赖；每个动作仍独立经过控制器、`ToolRegistry`、护栏、审计和超时。JSON 兼容模式保持单动作。默认 CTF 路径按目标与证据推进，不启用固定 PTES 阶段或额外假设调度；两者只作为显式兼容配置保留。

`SwarmRuntime` 是统一 Task Scheduler 和 Worker Pool。`CoordinationRuntime.intents` 是任务状态唯一来源，`TaskRecord` 只是同一字典的兼容字段视图；Todo 与探索图是派生视图，事实索引只收工具验证结果。检查点 v4 不再保存第二份 `records`，恢复旧 v1–v3 时一次迁入 Intent。Bench 只负责平台启停、提交与计分，题目同样交给 Swarm 调度。Worker 的模型上下文、`RunState`、工作目录与事实按题隔离，Commander 只转发有来源的相关情报；Swarm Worker 可按需调用 `shared_context` 读取事实索引和待办前沿，仍不会把共享事实写入当前题的 `RunState`。详见 [Swarm 使用说明](SWARM.md)。

## 1. 总览

```mermaid
flowchart TB
    CLI["trina.cli<br/>run/tool/guard/eval/report/lab/audit"]
    OMPEXT[".omp/extensions/trina.ts<br/>omp 工具桥接"]
    AGENT["trina.brain.runtime.AgentRuntime<br/>统一批量动作循环"]
    SCHED["SwarmRuntime / CoordinationRuntime<br/>任务队列、租约、Worker Pool"]
    PROMPTS["trina.brain.prompts<br/>通用 Agent system + 运行协议 + 容错解析"]
    POLICY["AuthorizationController<br/>注册、预算、撤权"]
    APPROVAL["可选人工审批<br/>批准 / 拒绝"]
    REG["trina.tools.registry.ToolRegistry<br/>参数校验→护栏→审计→超时→归一"]
    GUARD["trina.scope.ScopeGuard<br/>full / restricted 信任模式"]
    AUDIT["trina.audit.AuditLog<br/>SHA-256 哈希链"]
    STATE["trina.state.RunState<br/>hosts/findings/creds/flags/evidence/timeline"]
    COORD["trina.coordination<br/>事件 Planner / Todo / Intent / 双图 / Trace"]
    TOOLS["trina.tools：注册工具（browser、binary、API侦察、成果、编码、Git、协作）"]
    HTTP["trina.tools.http.HttpClient<br/>带护栏的 HTTP 出口"]
    EXT["trina.tools.external.run_external<br/>外部二进制（shell=False + 护栏）"]
    LLM["trina.llm<br/>标准 API 流式传输 / replay"]
    ADAPT["trina.adapters<br/>local/docker/http-range/tsecbench"]
    EVAL["trina.eval<br/>harness + metrics + adversarial"]
    LAB["trina.lab<br/>故意脆弱靶机"]
    REPORT["trina.report<br/>Markdown + 自包含 HTML"]

    CLI --> AGENT
    CLI --> SCHED --> COORD
    OMPEXT --> CLI
    SCHED --> AGENT --> PROMPTS
    AGENT --> LLM
    COORD --> AGENT
    AGENT --> POLICY --> APPROVAL --> REG
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

通用成果与流量新增契约：`RunState.casebook` 保存资产、观察、绑定和带版本复测；原生HTTP执行窗口将各跳写入任务的 `traffic.sqlite3`，不替代事实库。脚本工件有证据编号与哈希，报告消费同一状态中的资产覆盖、流量关联和复测记录。
ScopeSentry由外置Capability通过 `mcp_http` 复用 `ctx.http`；浏览器采集仍保留自己的证据工件。覆盖与限制见[工具栈](TOOL_STACK/TOOL_STACK.md)及[ADR-0038](../ADR/0038-tool-evidence-and-retest.md)。

```
人工信号与任务剩余时间
    → GoalRequirements 缺口 + 状态差量（显式兼容配置才加入阶段/假设调度）
    → RunState 摘要 + 上一步观察 + 提示 + 预算
    → LLM 返回标准 tool_calls → AgentRuntime.decode_batch()
        ├─ 非法协议：记录错误并反馈下一轮
        ├─ finish：不得与工具调用混用；检查目标缺口后按事实收尾
        └─ actions：逐项检查本批前序依赖，提取 test_id / target，登记或引用假设
             → AuthorizationController.check（注册 / 预算 / 撤权）
             → 可选人工审批
             → ToolRegistry.execute(name, params, ctx)
                 ├─ 参数校验（未知 / 缺失 / 类型 / enum）
                 ├─ 工具可用性
                 ├─ 根据规范化参数计算等级，检查护栏与目标
                 ├─ tool.start → 工具执行与等待超时
                 └─ facts / evidence 吸收 → tool.end
             → 知识摘要变化、假设更新、失败分类、重复检测、调度遵从
             → 预算停止合同与周期快照
             → 为每个 API call_id 写对应 tool 回执 → 下一轮或统一收尾
```

批内动作当前顺序执行，目的是避免同一 `RunState` 上的数据竞争；不同 Worker 仍可并行。控制器允许发生在完整工具参数校验和实际完成之前，故 `executed` 不能直接用作成功覆盖证明。对话与任务共用同一模型—动作循环，只保留各自的展示与收尾适配。

等待外部输出时，通过 `terminal_start` 返回句柄，让后续模型决策推进独立工作；依赖结果时再用 `terminal_status` 回收，独立分析分支用 `worker_tasks`。同步调用本身不触发并行模型决策。后台线程只维护作业状态和输出事件，`terminal_status` 在作业与输出流完成后经注册表归档证据，保持事实提交和执行租约契约。

控制台的 `/jobs` 合并当前对话的终端作业和控制台的 Worker。命令使用列表中的完整 ID，例如 `/jobs wait any 30 terminal:job-…` 或 `/jobs wait all 30`，省略 ID 时按开始等待时的活跃任务取样。完成事件唤醒等待并刷新所选任务的状态。Esc 和 `/jobs unwait` 只结束用户的等待，`/jobs stop <ID>` 才向指定任务发停止请求。用 `/jobs logs <ID>` 查看近期输出及原有事件记录位置，用 `/jobs message worker:<ID> <文本>` 向指定 Worker 发补充。

## 3. 模块契约

| 模块 | 关键类型 / 函数 | 约定 |
|---|---|---|
| `trina.config` | `TrinaConfig.load(path, overrides)`、`ScopeConfig`、`scope_prompt_block()`、`runtime_root` | `scope.trust_mode` 默认 `full`，`restricted` 恢复旧边界；环境变量可覆盖标量；未知字段直接报错；源码产物写项目根、Wheel 产物写 `~/.trina` |
| `trina.scope` | `ScopeGuard.check_target/check_argv/check_payload/check_http/check_action_level/check_destructive/check_brute_scale/throttle`；`Decision`；`ScopeViolation` | full 模式对合法格式直接放行；restricted 按网段、等级、破坏性、禁止模式和规模裁决；两者都记录审计并保留限速与执行租约 |
| `trina.audit` | `AuditLog.open/append/verify/summary` | 每条记录 `hash = sha256(canonical(record - hash))`，`prev_hash` 链接；写入即 fsync；敏感字段自动脱敏 |
| `trina.events` | `EventBus.publish/replay/subscribe/wait`、`EventSubscription.poll/ack`、`ModelEventStream` | 模型、工具、后台作业和调度共用持久事件流；SQLite 提交后按序投递，以 `(stream_id, seq)` 标识事件；显式确认命名消费者游标，重放不执行动作；详见[事件协议](EVENTS.md) |
| `trina.event_view` | `EventTranscript.consume` | 实时与重放共用投影；按调用 ID 更新工具窗口，正文直接排版，按事件身份去重 |
| `trina.state` | `RunState.absorb(facts)`、`add_finding`、`knowledge_digest`、`summary_for_prompt`、`to_dict/from_dict` | 工具声明 facts，状态集中合并；`knowledge_digest` 排除时间、重复条目与笔记，用事实变化计算进展；`from_dict` 供工作台/复核读历史运行 |
| `trina.tools.registry` | `ToolSpec`/`ParamSpec`/`ToolResult`/`ToolContext`/`Tool`/`ToolRegistry.execute` | 执行链是**唯一**入口；工具实现 `run(ctx, **params)`，返回 `ToolResult`；按题型和能力租约导出最小 Schema 与执行白名单；超时结果给出当前工作目录和缩小范围提示 |
| `trina.tools.workspace` | `terminal_exec`、`workspace_read/write/edit` | 前台 argv 命令和项目文件编码工具；命令经 `run_external`，路径经 `ScopeGuard.check_workspace_path`，写入原子替换，编辑要求精确匹配数 |
| `trina.tools.web_search` | `web_search` | Google/Bing 免费搜索与 DeepSeek Search 适配；查询过滤、结构化结果、统一 `[n]` 引用和原始交通证据 |
| `trina.coordination` / `trina.tools.coordination` | `CoordinationRuntime.plan/claim/settle/publish_state/worker_view/search_traces`、`shared_context/shared_trace_*` | Intent 是唯一任务状态；事件驱动规划、共享 Todo 派生视图、租约/去重/恢复、事实索引与探索视图、跨 Worker 可追踪观察和失败；共享事实只通过按需索引读取，不共享完整对话或直接合并当前题事实 |
| `trina.binary` / `trina.tools.binary` | `BinarySessions`、`R2Backend`、`GDBBackend`、`AddressRef`、10 个语义化工具 | r2 stdio JSON 与 GDB/MI3 持久会话；按任务和能力租约隔离，地址空间显式，完整协议写证据；见 [二进制工具链](BINARY_TOOLS.md) |
| `trina.browser` / `trina.tools.browser` | `BrowserSessions`、7 个结构化工具 | Playwright CLI 命名会话；快照 ref、交互、存储、网络记录与截图；完整输出写证据，恢复不复活进程；见[浏览器工具链](BROWSER_TOOLS.md) |
| `trina.capabilities` / 外部任务模块 | `CapabilityCatalog`、`CapabilityFabric`、`CapabilityRuntime`、`AGENT.md` | 靶场/平台能力放在仓库外清单中，按 Worker/Task 租约 JIT 加载；`AGENT.md` 只进入 user 任务合同，不修改全局 system；见[临时任务模块](TASK_MODULES.md) |
| `trina.tools.http` | `HttpClient.request/get/post`、`HttpResult.as_evidence()` | 出网唯一出口：护栏 → 限速 → 请求 → 证据化；护栏拒绝时返回带 `error` 的结果而不是抛异常 |
| `trina.tools.external` | `run_external(argv, ctx, …)`、`which/have` | `shell=False`、先护栏后执行、命令与输出摘要进审计 |
| `trina.brain.runtime` / `trina.brain.agent` / `trina.brain.chat` | `AgentRuntime.run/decode_batch`、`PentestAgent.run`、`ChatAgent.ask` | 同一核心循环执行原生批量调用并完整配对 tool 回执；依赖只引用本批前序编号；任务/聊天只适配状态、显示与收尾 |
| `trina.brain.planner` | `Planner.tick/objective_met/force_advance/should_finish` | 旧 PTES 兼容模块；默认 `run.phases=[]` 不进入核心路径，显式配置阶段后才启用 |
| `trina.brain.goal` | `GoalRequirements.parse(goal)`、`unsatisfied(state)/soft_hints/satisfied` | 漏洞类与显式 `port:数字` / `host:port` 形成硬条件；`AUTHORED-*`、UUID、文件路径数字不参与端口解析。路径在没有类要求时为硬条件，否则为提示；凭据形成验证提示。最终成功依据已表达的条件，不读取 ground truth；关键词解析不能表达全部自然语言语义 |
| `trina.hypotheses` | `HypothesisLedger.add/from_action/record_tool_outcome/record_outcome/prompt_block/finalize/summary` | 假设的排序/状态机与证据判定；任务/对话共用验证对象匹配；`hypothesis_id` 复用台账；无新信息只降优先级，明确反证才能否定；默认不额外接管动作调度 |
| `trina.brain.failure` | `classify_failure`、`LoopGuard.observe`、`advice_for`、`alternatives_for` | 失败归因（boundary/tool/environment/budget/hypothesis）+ 无新信息重复识别，给出递进建议（换参数→换工具→人工） |
| `trina.brain.scheduler` | `HypothesisScheduler.decide(...)` | 显式兼容模块；默认 `run.hypothesis_scheduler=false`，假设台账记录证据但不再成为第二个逐步调度中心 |
| `trina.evidence_graph` | `build(state, registry=, hypotheses=)`、`EvidenceGraph.to_mermaid/to_dict` | 由权威 `RunState` 按需生成的报告视图，不在执行期维护第二份事实状态 |
| `trina.bench` | `BenchmarkRunner.run`、`BenchmarkClient.fork`、`BenchCommander` | 平台适配层；每题映射为 Task，由 Swarm Worker Pool 并发执行（默认 2、上限 3）；独立控制面会话，submit 的 `correct` 是唯一成功判据；轮次边界 Commander 按部分 Flag/新增事实决定续做或加时（ADR-0039） |
| `trina.board` | `render_board(state, hypotheses=)`、`summarise`、`latest_state_file`、`load_run_state` | 工作台视图的**唯一**渲染实现（控制台 `/board` 与 `trina watch` 共用）；`load_run_state` 把 state.json 还原成 `RunState` |
| `trina.brain.prompts` | `action_example`、`render_system_prompt`、`build_system_prompt`、`build_runtime_context`、`build_step_prompt`、`parse_action` | 任务、对话与通用 Worker 共用七模块比赛 Agent system，覆盖多类 CTF；聊天允许直接回答，二进制说明按需适用；动作协议、范围、工具与任务事实仍由运行数据提供，详见 [提示词说明](PROMPTS.md) |
| `trina.context` | `normalize_briefs`、`render_commander_briefs`、`brief_digest` | 只处理 Commander 定向情报；不合并其他题目的事实、证据、Flag、假设或历史；接收方重新验证后才可写入本题状态 |
| `trina.conversation` | `ConversationContext.state_update/prepare/before_request/after_response` | 每任务状态差量、周期完整状态、原子归档及继承检查点；保护用户约束、事实和最近工具链；区分可见前缀变化与实际用量 |
| `trina.llm.cache` | `CachePolicy.for_request/apply` | 供应商缓存字段在请求边界适配；内部前缀标识、明确能力及一次有界降级；不推测服务端 KV 命中 |
| `trina.chat_store` | `ChatStore.save/list/load/acquire` | 按项目保存 SQLite 事务快照，列表只查元数据；对话独占锁先于读取；不导出供应商配置、不执行历史动作 |
| `trina.session_title` | `generate_session_title`、`fallback_session_title`、`parse_generated_title` | 首条有效请求用隔离的小型标题协议生成目标级短标题；标题请求不进入主对话上下文，失败时确定性降级，低信息输入保持“新对话” |
| `trina.brain.chat` | `ChatAgent.ask/save/close`、`ChatAgent.resume(config, run_id, **kwargs)` | **对话式大脑**：使用标准 API 的原生 `tools` / `tool_calls` 并维护 assistant-tool 配对；同一原生响应的多个调用按顺序执行后一次回灌，避免共享状态并发写；JSON/DSML 仅为兼容回退且不会显示成正文；共用护栏、控制器、审计和审批；自动保存及跨实例恢复，但不恢复旧进程或重放工具 |
| `trina.llm` | `complete_messages(messages, on_event=, tools=)`、`LLMStreamEvent`、`LLMUsage` | 标准结构化消息与三个 API 方言的 SSE 解析；稳定前缀、终止事件校验与真实用量汇总；OMP 后端已删除 |
| `trina.providers` / `trina.model_ui` | `ProviderPreset`、`ProviderProfile`、`ProfileStore`、`ModelManager` | 以供应商整条配置管理地址、单个 Key、模型、备注、思考档位与上下文窗口；只接受仓库外的 v2 文件，旧格式明确拒绝；编辑、切换与删除由同一管理层原子保存 |
| `trina.provider_form` | `ProviderEditor` | 单页草稿表单；测试通过回调复用 LLM 后端，字段修改使旧结果失效，保存前校验，取消和测试不写入配置，不切换当前供应商 |
| `trina.adapters` | `load_adapter/list_adapters`、`RangeAdapter.prepare/status/ground_truth/check_flag`、`score_tsecbench` | 靶场自描述（目标 + ground truth + flag 模式），评测框架据此自动判分 |
| `trina.policy` | `PolicyRegistry.load()`、`AuthorizationController.check/note_evidence/revoke/coverage`、`Budget` | **计划器提出、控制器批准**：注册目标/检查项 + 预算 + 撤权，逐动作裁决；模型无写权限 |
| `trina.session` | `Session.request_approval/answer/steer/toggle_pause/request_stop`、`level_needs_approval` | 人机协同原语；审批**超时即拒绝**（fail-closed） |
| `trina.play` | `build_plan(config, target=, range_name=)`、`run_plan(config, plan, approval=, steer_queue=, …)` | 一条命令开打的准备与执行；谁拉起靶机谁负责回收 |
| `trina.terminal` | `render_home/render_panel/render_message/render_step/render_approval` | CLI 统一展示；按显示宽度折行并清除外部控制字符，正式输出支持 Markdown；无色和无 rich 时降级；不参与模型上下文或动作执行 |
| `trina.console_state` | `ConsoleContext`、`ConsoleState`、`TrackedBackend` | 底栏读取当前运行模型、目录、短会话名和步骤；横线展示最近一次当前输入量相对上下文窗口的占用，累计 Token 另行统计；跨模型累计、建新会话清零，未知用量不伪造，不修改提示词、动作、授权或后端账本 |
| `trina.worker_roles` / `Console._cmd_worker` | `WORKER_ROLES`、角色会话 ID 表 | 交互 Worker 各自独立 ChatAgent，切换通过已有持久历史恢复，角色随聊天快照保存；运行中不切换，Swarm 执行调度保持独立入口 |
| `trina.subworkers` / `trina.subworker_ui` | `Subworkers.start/add/configure/control`、`/subworker` | 后台复用 Swarm；主会话不阻塞；每任务模型/思考强度与供应商引用独立，等待任务可修改，运行任务冻结；UI 只读取事件驱动的小型投影 |
| `WorkerTasks` / 四格对话投影 | `worker_tasks`、`render_grid`、`Subworkers.dialog_lines` | Commander 对两个及以上独立分支主动委派并回收，支持定向补充与取消；四格仅投影可见事件，按任务和宽度缓存；历史恢复无执行副作用 |
| `SwarmRuntime.submit_task/configure_task` | 命令队列 + Future | 控制台跨线程只提交，Commander 单线程修改任务图；收尾拒绝迟到提交，取消不记成功；配置相同的闲置后端优先复用，快照固定模型身份 |
| `trina.config.UIConfig` | `status_line_preset/status_line_separator/context_bar/motion` | 分段单行底栏；角色优先，窄屏收起次要指标；首页动画只更新等待输入的唯一字标块，提交后冻结 |
| `trina.console_ui` | `EnhancedConsoleInput.run/write/update_stream/update_tool` | 持续全屏内容区与固定底部输入、索引和用量；正文增量与工具窗口原位更新，保留输入草稿；模型表单临时接管终端后恢复 |
| `trina.transcript` | `TranscriptControl.sync/create_content/precondition_scroll/selected_text/copy_selection` | 脏块一次完成 ANSI/Unicode 物理行折行，视口使用直接行偏移；VT 终端以滚动区域搬运不变行并同步差分旧屏，失败时退回普通重绘；流式更新只重排变化块；支持拖选复制与原生鼠标模式切换 |
| `trina.runtime_status` | `status_snapshot/render_agent_status` | 模型请求末尾的动态运行快照，独立于屏幕底栏；保持消息前缀，任务、工具和作业数据只取当前上下文 |
| `trina.tools.terminal` / `BackgroundJobs` | `terminal_start/status/stop`、`run_external(background=True)` | 当前任务专属的有界后台作业；真实退出码、超时和进程树回收；监视线程不写漏洞事实 |
| `trina.interactive` | `Console(config, input_fn=, output=).run()/handle(line)` | 行式交互控制台；空闲命令复用 CLI，运行中支持介入、状态和只读工作台；2026-10-28 展示更新见 [CLI 说明](CLI_UI.md) |
| `trina.review` | `review_state(state, audit_path=, registry=) -> ReviewNote`、`ReviewNote.to_markdown()` | 评价 Agent（只读）：核对声明/证据、结论一致性、覆盖缺口、边界事件、审计链 |
| `trina.eval` | `EvalHarness(config, runner=).run(suite)`、`compute_metrics`、`AdversarialProxy` | `runner` 可注入，便于离线/回放评测 |
| `trina.report` | `build_report(state, cfg, meta=, audit_summary=) -> Report`、`Report.save()` | HTML 自包含（内联 CSS/SVG，无外部资源）；章节含"六、证据链与假设台账"（Markdown 带 mermaid，HTML 用起点-关系-终点表） |

## 4. 关键设计决策与理由

| 设计选择 | 解决的问题及理由 | 代价和限制 |
|---|---|---|
| 宿主自有循环、后端可替换 | 授权、状态和收尾有明确程序入口，模型连接与业务控制解耦 | 需要维护解析、反馈、上下文和不同入口的一致性；通用框架并非天然不能满足要求 |
| 单步 JSON 动作 | 每次执行后使用新观察，参数可校验，轨迹可录制 | 增加模型往返；解析器仍需容错与语义校验 |
| 目标约束独立于 final | 防止顺路 flag 或模型自评代替指定验证 | 当前是关键词规则，复杂目标和精确位置语义有限 |
| 阶段骨架加动态动作 | 用事实和阶段预算组织重点，保留根据新线索调整的空间 | 阶段可以预算推进，进入 exploit 不等于利用成功 |
| 假设台账与方向调度 | 记录待验证对象，比较期望检查与实际动作 | 优先级是启发式，绑定主要按漏洞类，调度不直接执行 |
| 控制器、审批与护栏分层 | 注册及预算、操作者判断、实际参数及传输分别裁决 | 提前允许记录与成功完成仍需区分，入口调用关系需要单独核对 |
| 工具统一契约 | 避免各工具遗漏参数、异常、审计和状态处理 | 仍信任工具返回的事实，尚无通用独立重放验证 |
| 内置能力与外部工具适配 | 常见能力在缺少外部二进制时可有降级路径 | 各工具能力与依赖不同，不能承诺全部外部能力均有等价内置实现 |
| 知识摘要与重复指纹 | 时间变化、重复发现不能持续冒充新进展 | 字段归一化不理解全部语义，raw 中变化文本仍可能影响指纹 |
| 哈希链、快照和报告 | 分别保存过程一致性、当前状态和可读派生视图 | 哈希链没有外部可信锚；快照读取不等于进程续跑 |

各选择的完整依据、计算示例、方案比较和验证见[设计决策](设计决策/设计决策.md)；控制归属见 [ADR-0001](../ADR/0001-brain-own-react-loop.md)。

## 5. 错误隔离与收敛

| 情形 | 行为 |
|---|---|
| 工具抛异常 | 捕获 → `ToolResult(ok=False, error=…)`，计入 `counters.errors`，审计 `tool.error`，循环继续 |
| 工具超时 | 共享有界执行器返回超时错误并撤销执行租约；隔离状态不提交，后续 guard/http/audit/log/工件写入被拒绝。Python 线程不会被强制终止，在途 I/O 仍受自身超时约束 |
| LLM 调用失败 | 鉴权拒绝和余额/额度不足直接停止；协议不支持按配置有界降级；恢复性故障单次决策最多尝试 5 次，间隔 2/4/6/8 秒。每次错误和重试均进入统一事件流并回显，外层仍受错误分类预算及共享 errors 上限约束 |
| 模型输出非 JSON | 多级容错解析 → 仍失败则注入"只输出 JSON"提示并继续（计入 errors） |
| 动作被护栏拒绝 | 拒绝结果 + `hint` 回灌；计入 `counters.denied`；写入审计 `tool.denied` |
| 同一动作重复 | stall 首次为 0，默认阈值 3 通常在连续第 4 次相同动作触发纠正与阶段推进；不等于直接结束任务 |
| 步数/时长耗尽 | 在相应检查点退出并按事实收尾；模型重试、线程和在途请求具有独立等待边界 |
| 目标越界 | 预检阶段直接 `blocked`，**不进入 LLM**（模型无权授权） |

## 6. 状态与终态判定

`status` 描述任务执行结果：初始化越界直接 blocked，人工停止保留 stopped；存在非空硬性目标时，全部满足为 success，未满足但已有 flag/发现为 partial，其余为 failed。无硬性目标且状态仍为 running 时，有工具 flag 或带证据 verified/exploited 发现为 success，只有发现、主机或拒绝记录为 partial，其余为 failed。

主控收到 final 后最多两次拒绝未达成目标的提前结束，之后可以退出，但仍按实际目标和事实计算状态。模型 `final.status` 与 `final.flags` 不改写事实。任务目标、阶段和漏洞结论是不同概念，具体分支及现有边界见决策层说明第 19 节。

`RunState` 同时是报告的输入：`hosts / findings / credentials / flags / evidence / timeline / steps / ledger / counters / artifacts / verdict / coverage / boundary_events`。

### 6.1 结论状态与覆盖（P3 口径）

| 结论 | 判定 | 报告里必须同时给出 |
|---|---|---|
| `CONFIRMED` | 有控制器 executed 记录，且存在 verified/exploited、非空证据发现；优先于后续错误分支 | 已确认对象及证据，同时保留其他检查的缺口 |
| `NOT_CONFIRMED` | 有 executed 记录、无上述发现，且未命中 errors/failures/撤权/denied 分支 | 结论限于本次记录；不代表全部检查完成或全系统安全 |
| `INCONCLUSIVE` | 没有 executed 记录，或未确认发现且存在上述受限条件 | 缺口及原因，不将失败当作阴性证明 |

实际任务状态包括 running、success、partial、failed、blocked、stopped，verdict 单独保存；对话入口使用 chat 状态。PLANNED、ALLOWED、EVIDENCE_COLLECTED 可用于概念流程说明，不是当前 RunState 的一套完整已实现状态枚举。控制器在实际工具完成前写 executed，故覆盖与结论还受 [OQ-01](course/open-questions.md) 限制。

三类越界的记录方式（`RunState.boundary_events`）：
- **建议越界**：模型在 thought 里提到越界（只存在于审计的 `react.action.thought`）；
- **请求越界**：动作参数越界 → 控制器/护栏拒绝 → `boundary_events` 记 `stage=request, result=blocked`；
- **实际越界**：设计要求通过执行裁决阻止；如果观察到未获允许的目标行为，必须按实现缺陷记录并修复，不能仅凭架构图排除其可能。

### 6.2 人工介入点

| 介入点 | 位置 | 语义 |
|---|---|---|
| 审批闸门 | `PentestAgent(approval=…)` ← `Session.request_approval` | 按动作等级拦截，超时即拒绝 |
| 实时提示 | `PentestAgent(steer_queue=…)` ← 控制台 `hint` | 下一步注入 `[人工介入]` 提示 |
| 暂停/停止 | `pause_event` / `stop_event` | 循环边界检查；停止后按已获证据给出结论 |
| 撤权 | `AuthorizationController.revoke(target_id)` | 后续控制器 check 拒绝匹配目标，不承诺取消已放行或已在途动作 |

## 7. 扩展点

| 想加什么 | 改哪里 | 注意 |
|---|---|---|
| 新工具 | `trina/tools/<module>.py` 定义 `Tool` 子类 + 导出 `TOOLS` | 声明 `action_level`、`params`、`produces`；用 `ctx.http`/`run_external` |
| 新靶场 | `~/.trina/ranges/<name>.yaml` 或显式 YAML（+ 必要时 `trina/adapters/` 新适配器类） | 仓库不预装注册表；按需给出 `targets`、`ground_truth`、`flag_patterns` |
| 新评测场景 | `trina/eval/scenarios/<suite>.yaml` | 必须声明 ground truth 与 `scoped_classes`，否则 precision 口径失真 |
| 新 LLM 后端 | `trina/llm/<name>_backend.py` + `make_backend` 分支 | 实现结构化 `complete_messages()`、流事件、终止校验及 `usage` |
| 新报告格式 | `trina/report/` | 保持 HTML 自包含 |
| omp 侧新工具 | `.omp/extensions/trina.ts` | 一律转调 `python3 -m trina …`，不要绕过护栏 |

## 8. Capability Fabric 与上下文

Commander 通过 `trina/capabilities/catalog.py` 发现统一能力；`broker.py` 根据依赖、策略与版本快照签发任务租约；`runtime.py` 将 Tool、CLI 或 MCP 能力投影为当前 Worker 的 ToolRegistry。Schema 与 Guidance 按租约加载，调用前后验证 TTL 和释放状态。清单同目录的 `AGENT.md` 是约定式 Guidance，只进入当前任务的 user 合同。通用 Worker 可在任务间复用执行环境，事实图、凭据、假设和对话仍默认独立。Commander 只转发相关证据索引与带来源的线索。清单、API、运行限制与验收见 [Capability Fabric](CAPABILITY_FABRIC.md)、[临时任务模块](TASK_MODULES.md)、[ADR-0021](../ADR/0021-capability-fabric.md)和[ADR-0037](../ADR/0037-proactive-workers-and-pluggable-task-modules.md)。

任务与对话使用结构化消息，稳定 system 和初始合同处于前缀，动态状态与观察只追加到后方。接近本地窗口预算时创建可复核的继承检查点，不调用模型生成摘要；不同任务不复用完整对话。DeepSeek 自动预算为 500K，明确识别的百万窗口型号为 1M，其他型号保守回退且可由 `/model context` 覆盖。详见 [模型传输与缓存](MODELS.md#上下文窗口)与[缓存运行时](CACHE_RUNTIME.md)。

## 9. 浏览器能力

2026-11-17 起注册表提供 7 个 Playwright CLI 浏览器工具。采用 CLI 而非 Python API，是为了让浏览器启动继续经过 `run_external` 与 `ScopeGuard`，不在工具模块新增进程或网络出口。命名会话由 `BrowserSessions` 管理，模型只看到任务内句柄；完整快照和网络输出写入运行目录，返回上下文的是有界摘要和 `evidence_ref`。

`browser_open` 启动 Chromium 等浏览器；`browser_snapshot` 返回稳定元素 ref；`browser_interact` 负责导航、点击、填写和标签页；`browser_storage`、`browser_requests`、`browser_screenshot`提供状态和取证；`browser_close`显式回收。任务结束、超时、取消和能力租约释放也会关闭会话，历史恢复只保留失效快照。

Playwright 页面会自行加载跨源子资源，当前 CLI 无法把每个子请求同步交回 Trina 裁决，因此浏览器工具在 `restricted` 模式明确不可用，避免伪造完整边界；默认 `full` 模式仍保留参数、动作等级、速率、超时与审计。轻量 HTTP 工具继续适用于需要逐跳 URL 裁决的场景。完整契约见[浏览器工具链](BROWSER_TOOLS.md)。

资料检索入口选用 **Google / Bing**，检索后打开来源页面核对内容。搜索引擎负责找资料，Playwright 负责浏览器操作；该选择记录在工具接入约定中，不增加模型角色或搜索攻略提示词。
