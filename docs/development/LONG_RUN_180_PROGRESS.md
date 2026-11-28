# Trina 长时工程研发记录

## 2026-11-28 会话

- 开始计时：2026-11-28 02:17:17 +08:00（工具 UTC 时间 2026-11-27 18:17:17）。目标约 180 分钟；只记录实际工作时间。
- 分支：`main`；HEAD：`595a01ccb00b037e714707a720f64147f54cdd2a`。
- 工作区已有 Commander V0.8、UI V0.7、完成收件箱、执行时间线等未提交改动，以及已有 evidence 文件删除；本轮保留，不提交、重置或恢复删除。
- 修改前源码与测试快照：`C:/Users/WYH/AppData/Local/Temp/trina-longrun-baseline-20261128-yvv6y1et/source.zip`；同目录 `hashes.json` 保存 SHA-256。共 217 个 Python 文件。
- 已读：项目 AGENTS、README、实际更新日志 `docs/CHANGELOG/CHANGELOG.md`、架构契约、Harness 入口及 ADR-0045。项目根目录没有 CHANGELOG.md。
- 正在运行 Commander / Coordination / Planner / Swarm 基线测试。

### 阶段安排与当前证据

1. Commander：已有明确工作列表拆解、依赖图、增量优先级与事实后继；验证后只补缺口。
2. Worker：已有租约、并发上限、完成复用、进展统计及有证据的停滞止损；重点检查异常与恢复边界。
3. 后台接续：已有 `CompletionInbox` 和 Runtime resume 回调；核对完成去重、关闭/取消、持久恢复及事件归属。
4. 时间线：已有 `execution.phase` 与 `event_latency`；核对真正的交接耗时和容量瓶颈，未发现前不引入 Provider Scheduler。
5. 经验：ADR-0045 仍为提议，先检查 Capability Fabric 的已有经验，再决定最小实现。
6. Benchmark：保留现有调度基准；增加 A/B/C/D 与消融的固定条件、来源及可重复运行记录。没有真模型/竞赛样本的数据必须标为待验证。

### 尚未完成

六阶段实施和验收仍在进行；后续阶段结果追加到本文件。

### 阶段一验证与修改

- 基线：Commander 34、Coordination 5、Planner 3、Swarm 16，共 58 项通过。
- 已有拆解、增量优先级、依赖和事实扩展通过验证，未另建调度器。
- 发现：Swarm 每次事件数据库连接关闭都可能触发 WAL 收尾；重复 state.updated 反复重建相同事实。
- 修改 `trina/swarm.py`：沿用 EventBus.keep_open；事实投影仅在保真事实字段变化时更新，计数/时间/步骤事件照常留存。
- 新增重复状态与有效证据变化测试。阶段一 Commander 35、Swarm 16 项通过。
- 三次交替本地调度对照：单任务 287.24→226.06 ms；依赖任务 1051.10→911.78 ms；连续任务 1407.57→1226.61 ms；事件突发 2095.35→1417.71 ms。四任务同时完成 544.33→551.28 ms，未观察到收益。
- 原始样本与源码哈希：`C:/Users/WYH/AppData/Local/Temp/trina-longrun-stage1-benchmark.json`。这是实际调度/SQLite 加受控 Worker 的测量，不是竞赛解题成绩。

### 阶段二验证与修改

- 发现：旧 task.finished 早于结果快照，等待方可能读到 running；取消导致执行器抛异常会误报 failed。
- 修改 `trina/swarm.py`：结果与 UI 快照提交后发送幂等 task.result_ready；取消异常不重试，保留 cancelled；取消分组仍汇总已完成分支的证据。
- 新增完成快照时序和取消异常资源释放测试。
- Commander 37、Subworkers 9、CompletionInbox 3 项通过。并发上限、租约和模型配置保留。
- 下一步：完成收件箱消费 result_ready，补齐恢复去重、原 Tool Call 关联和无后续工作时的续接规则。

### 外部参考检查状态

- 已检查本地 `ctf-similar-search`、`solve-challenge` 的职责及 ADR-0045；前者是 Tavily 资料检索，不能代替本地已验证经验存储。Capability Fabric 已有本次运行的证据索引，尚无跨运行的条件化策略检索。
- 两个联网检索工具均返回传输连接错误，尚未获得 OMP 最新源码；这是工具连接问题，不是安全审查拒绝。继续本地实现和验证，并尝试项目现有只读资料入口。

### 阶段三验证与修改

- 后续通过项目 `HttpClient`/`ScopeGuard` 成功只读获取 OMP `packages/coding-agent/src/session/agent-session.ts`。核对尾部排队消息唤醒、会话归属和用户中断抑制，没有复制框架。
- 修改 `completion_inbox.py`、`brain/chat.py`、`brain/agent.py`、`tools/terminal.py`、`tools/coordination.py`：收件箱消费状态随会话消息共同保存；恢复重放并去重；关联原始 Tool Call；人工取结果后不重复自动唤醒；Worker 等结果快照提交后再通知。
- 取消或权威验收收尾时不额外请求模型。恢复旧 Worker 仅报告 interrupted，不重新执行旧作业。
- 新增持久恢复、手动确认、事件归属、完成可读和取消测试。收件箱 8、会话恢复 25、后台视图 7、Worker 工作台 8 项全部通过。

### 阶段四时间线

- `swarm.py` 新增执行线程内的 worker.executing，记录真实排队起点；完成事件补齐 run/task/worker 关联，执行时间扣除领取后落盘等待。
- `event_latency.py` 增加 task_queue、worker_to_request、worker_result_merge，原有模型/工具交接指标保留。6 项时间线测试、37 项 Commander 测试通过。
- 模型适配层未发现全局请求锁；Profiler 的短时统计锁不包住模型调用。目前没有供应商容量瓶颈证据，因此未新增 Provider Scheduler。
- 本地实测延迟和最终性能对照将在 Benchmark 阶段补齐；尚无真实供应商/竞赛耗时结论。

### 阶段五经验卡

- 新增 `trina/strategy_memory.py` 与 10 项专项测试；只保存实际步骤和独立验收回执，拒绝单独 success/flag 声明。验收正文沿用 workspace 工具的判据，并复查工件哈希、归属目录和大小。
- 分类复用任务路由，关键词索引存于项目运行目录的 `.strategy-memory.sqlite3`。按上下文条件排除冲突，缺失条件要求复验；最多 3 张卡、总计 3000 字符。相同任务/库版本缓存查询，异常不影响任务收尾。
- 已接入 Swarm 的现有 Commander briefs 和真实完成链路；不写入当前题事实图。`run.strategy_memory=false` 可关闭检索与写入，Benchmark 使用只读冻结库。
- 记录特征、分类、带证据线索、已执行成功/失败步骤、适用条件、验证方式、耗时和 Token。记录的是“验收通过运行中实际执行的路径”，不假定每步都导致成功。
- 10 项经验专项、7 项配置路径测试通过。范围限制：没有可校验工件的旧历史不自动导入；当前不把普通 HTTP 观察或模型总结升级为成功策略。

### 全量回归与接续边界

- 首轮全量：952 项，938 通过、11 跳过、3 失败，耗时 270.587 秒。三项均为会话开始时已有问题：合同版本/删除证据、上下文预算提示未显示、旧后台状态测试仍假定不会自动续接。
- 修正后台状态测试为有明确释放信号的实际子进程，验证 running → 自动续接 → completed。后台原 Tool Call ID 在派发时固定，防止完成时读到后续调用归属。相关 13 项通过。
- 开始 A/B/C/D 和四项消融工程回放。使用真实 AgentRuntime、注册表和本地 Python 独立验收；不把 Replay 正确率称为竞赛成绩。

### 边界复查与新增回归

- 核对发现时间戳会让相同 finding/credential/flag 反复成为新事实。`coordination.py` 现在忽略观察时钟生成语义身份，恢复时仍保留旧事实编号；有效验证器回执计为进展，不生成无意义后继 Agent。新增重复事实和验收进展测试。
- 修复取消聚合任务的提前完成通知，以及全局停止时未归并分支证据的问题；增加取消、停止、检查点恢复测试。Commander intelligence 30 项通过。
- 实际 Worker 终点在 Future done 之前发布，防止结果归并时间线倒置；Future 回调只唤醒可回收状态。
- 后台 `follow_up=false` 可显式声明服务无需完成后续接；不会等待或额外唤醒模型。收件箱 9 项通过。
- 修复已有上下文预算错误未显示问题，沿用既有 model.error 展示通道并标记 dispatched=false；没有新增 model.started 或用量。交互回归 51 项通过。
- 工程 Benchmark 的真实 checker 拒绝错误解、检查器篡改和过期回执；只读经验库与完整 Agent 流程集成测试 3 项通过。

### 阶段三恢复补充与阶段四性能定位

- 完成收件箱现在保存已知 Worker 检查点位置。恢复只读取当前项目目录内、同运行和同任务的终态结果；旧执行器不重启。管理器切换任务组时，同名任务不会混入旧完成通知。收件箱 12 项通过。
- 新增时间线后，轻任务出现实测退化：四任务 588.49→709.54 ms，连续任务 1427.90→1588.65 ms（5 次中位数）。保留 `trina-longrun-scheduling-isolated-1.json`，没有隐藏退化样本。
- cProfile 定位到四任务运行的 62 次 SQLite 连接约占 0.417/0.626 秒。增强原有 `EventBus.keep_open`：运行作用域内按线程复用连接、嵌套作用域不重复打开；每条事件独立提交，不跨模型请求持有写事务。Worker 执行线程沿用此作用域。
- 新增嵌套复用、异常关闭、跨线程可见性、事务回滚测试；EventBus 25 项通过，Commander 41 项通过。
- 连接复用后 5 次中位数（旧调度→当前）：单任务 265.89→133.22 ms，两个独立任务 389.14→177.57，四个独立任务 636.24→247.25，依赖图 1068.62→477.57，连续领取 1425.74→631.90，长短混合 1269.44→470.17，同时完成 607.16→241.82，事件突发 2065.79→185.27。原始样本 `trina-longrun-scheduling-pooled-1.json`。各场景模型请求均为 0。

### 阶段六对照与消融

- 新增 `tests/benchmark_competition.py` 和 3 项验收测试。A 单 Agent；B 会话开始时的调度源码；C 当前调度；D 当前调度加只读冻结经验。所有组共用当前 AgentRuntime 和工具，不声称 B 是整份旧版本环境。
- 固定 Replay 响应、65536 上下文、1024 输出上限、每题 6 步/30 秒、4 Worker 上限；6 个本地算法任务有真实输出文件、独立 Python 验收脚本和验收工件。训练夹具与测试算法分开，经验库每轮校验哈希不变。
- 记录实际请求、工具失败比例、排队、有效执行区间、空闲、模型交接和验收时间；另做动态优先级、Worker 补位、事件唤醒、经验检索四项消融。验收拒绝错误解、检查器篡改和过期结果。
- 已发现并修正两项基准配置问题：初轮未隔离 cwd 导致预检进入项目；未声明 Replay 上下文窗口导致一组少一次请求。失败样本保留，不把它们当成本收益。
- 最后独立测量 `trina-longrun-competition-pooled-1/results.json`：各组每次 6/6 本地夹具验收、24 次 Replay 请求、外部请求 0、重复执行 0。Token 未报告，用 null 标记；真实竞赛成功率 null。A/B/C/D 各 3 次，同时保留每次消融样本。
- 该基准主要验证本地工程开销，零模型网络延迟放大存储改进占比，不能推断真实解题成功率或同模型线上耗时。3 次消融的微小差异不作因果结论。

### 剩余时间：工具输出交互

- 按用户后续任务优先级补齐现有工具卡：默认显示 stdout/stderr 首尾摘要；F3 展开/收起保留内容，F4 复制保留内容。完整原始输出仍由事件/工件留存，没有在 UI 线程读文件。
- 沿用 ResponsiveText 与局部块缓存，实时更新继承展开状态；按当前视口选择工具卡，滚动到旧结果不会误复制最新任务。输入草稿和焦点不变。
- 修改 `trina/event_view.py`、`trina/console_ui.py`、`tests/test_ui_v07.py`。真实 prompt_toolkit VT 输出/按键在 80×24、100×30、120×36、160×48 均完成折叠→展开→流式更新→复制→收起；UI 10 项、EventBus 25 项通过。系统剪贴板调用在测试中替换，未声称完成跨终端人工验收。
- 正在运行最终全量回归，后续追加准确结果。

### 交付前复查（05:07 后）

- 经验检索限制每次查询最多读取 12 MB 证明；同次查询共享证明只哈希一次，跨查询重新核验。新增有界 IO/复验测试，经验专项 11 项通过。
- 修复工具卡 ASCII 边框与状态分隔符；用户输出正文不做字形替换。UI 专项 11 项通过。
- 发现并先复现跨任务组手动读取旧结果仍留下自动完成通知的问题。`Subworkers.task_result` 返回原始 stream_id，历史结果保留该归属，显式结果工具按原流确认；收件箱 13、Subworker 9、工作台 8 项通过。该修复不改变基准中的执行路径。
- 本轮源码/测试相对会话开始快照实际改动 28 个文件，新增 40 个测试方法，没有删除测试。详见 `long_run_20261128/session_changes.json`，其中包含修改前后 SHA-256。
- 最近完整回归为 971 项：959 通过、11 跳过、1 失败，244.076 秒；包含经验限额、工具卡 ASCII。随后新增跨组确认测试，正在跑最终回归。

### 最终调度对照（5 次交替，中位数）

| 场景 | 修改前 ms | 修改后 ms | 修改前/后请求 |
|---|---:|---:|---:|
| single | 280.78 | 135.64 | 0/0 |
| two_independent | 374.37 | 163.74 | 0/0 |
| four_independent | 588.18 | 224.04 | 0/0 |
| dependencies | 1043.85 | 466.76 | 0/0 |
| continuous | 1457.92 | 650.38 | 0/0 |
| long_and_short | 1308.42 | 469.71 | 0/0 |
| simultaneous_completion | 584.52 | 231.31 | 0/0 |
| event_burst | 2048.22 | 193.97 | 0/0 |

调度器观测开销、队列等待、Worker 空闲、峰值并发、重复执行、规划轮数及所有样本均保存在 `long_run_20261128/scheduling.json`。所有场景受既有并发上限限制，重复执行均为 0。

### 最终 A/B/C/D 与消融（各 3 次，中位数）

| 组 | 总耗时 s | 首次验收 s | 平均就绪排队 s | Worker 空闲合计 s | 请求 |
|---|---:|---:|---:|---:|---:|
| A_single | 1.744 | 0.261 | 0.683 | 0.011 | 24.000 |
| B_snapshot | 6.282 | 2.759 | 0.994 | 6.939 | 24.000 |
| C_upgraded | 1.131 | 0.531 | 0.248 | 1.496 | 24.000 |
| D_memory | 1.134 | 0.502 | 0.267 | 1.512 | 24.000 |
| D_no_priority | 1.103 | 0.504 | 0.264 | 1.465 | 24.000 |
| D_fixed_workers | 1.142 | 0.499 | 0.273 | 1.711 | 24.000 |
| D_poll_replanning | 1.257 | 0.524 | 0.287 | 2.047 | 24.000 |
| D_no_memory | 1.157 | 0.521 | 0.256 | 1.594 | 24.000 |

各组每轮 6/6 本地夹具通过、重复执行 0、上下文预算错误 0；单 Agent 峰值 1，其余峰值 4。实际外部供应商请求均为 0。Replay Token 未报告，不能推断真实 Token 下降；经验简报最多 3000 字符，真实 Token 增量仍待同模型测量。
动态优先级消融比 D 略快，表明这组很小的固定任务不足以证明优先级收益；经验开关差异也不足以证明解题收益。固定批次/轮询组较慢，仍只代表此工程夹具。
性能原始样本和时间线在 `long_run_20261128/competition.json`；独立执行工件目录在每个 sample.artifact_dir（系统临时目录，包含事件库、原始报告、检查点和真实 checker 输出）。

### 工具投影性能（5 次交替，中位数）

相同当前 viewport/历史缓存、替换会话开始的 event_view，120 次输出与行排版：80×24 为 153.91→84.79 ms；100×30 为 147.74→90.56；120×36 为 159.57→91.51；160×48 为 149.44→98.27。最终工具块从 53～97 行降至 13 行，保留内容均为 6064 字符。该微基准不包含终端传输或真实模型流，不等于实际终端 FPS。
`long_run_20261128/ui_performance.py` 可复现投影基准，结果与源码哈希在相邻 JSON。

### 本轮实际修改文件

- `trina/brain/agent.py`
- `trina/brain/chat.py`
- `trina/completion_inbox.py`
- `trina/config.py`
- `trina/console_ui.py`
- `trina/coordination.py`
- `trina/event_latency.py`
- `trina/event_view.py`
- `trina/events.py`
- `trina/strategy_memory.py`（新增）
- `trina/subworkers.py`
- `trina/swarm.py`
- `trina/terminal.py`
- `trina/tools/coordination.py`
- `trina/tools/external.py`
- `trina/tools/terminal.py`
- `tests/benchmark_commander.py`
- `tests/benchmark_competition.py`（新增）
- `tests/test_chat_resume.py`
- `tests/test_commander_intelligence.py`
- `tests/test_competition_benchmark.py`（新增）
- `tests/test_completion_inbox.py`
- `tests/test_event_latency.py`
- `tests/test_events.py`
- `tests/test_interactive.py`
- `tests/test_runtime_status.py`
- `tests/test_strategy_memory.py`（新增）
- `tests/test_ui_v07.py`
- `docs/development/LONG_RUN_180_PROGRESS.md`（本轮追加研发记录）
- `docs/development/long_run_20261128/*`（基线源码快照、哈希、对照样本、回归日志和投影基准脚本）

### 复现入口与边界

1. 基线压缩包 `long_run_20261128/baseline_source.zip` 保存会话开始的脏工作区源码。把其中 `trina/swarm.py`、`trina/coordination.py`、`trina/intent_planner.py` 放到新建基线目录；原始本机目录为 `C:/Users/WYH/AppData/Local/Temp/trina-longrun-baseline-20261128-yvv6y1et`。
2. 仓库根目录运行 `python tests/benchmark_commander.py --baseline-dir <基线目录> --output <新JSON路径> --repeats 5`。
3. 运行 `python tests/benchmark_competition.py --baseline-dir <基线目录> --output-dir <不存在的新目录> --repeats 3`；各组轮流执行，不与全量测试并行。
4. 运行 `python docs/development/long_run_20261128/ui_performance.py --output <新JSON路径>`。
5. 回归入口 `python -m unittest discover -s tests`。Windows 本轮设置 PYTHONIOENCODING=utf-8、GIT_CONFIG_COUNT=0。

### 尚未完成与下一阶段

- 真实固定供应商、模型版本、思考强度与 Token 预算的竞赛留出集 A/B/C/D；当前只完成离线 Replay 工程对照，竞赛成功率明确为 null。
- 对模糊自然语言复杂目标的拆分质量没有真题证明；沿用明确独立清单/显式依赖的保守确定性拆解，信息不足时单 Agent。
- 既有机器规约失败未擅自修改：版本 0.6.0 对程序/pyproject 0.6.4，用户已删除的 evidence 及缺失测试引用；没有伪造或恢复证据。
- 未引入 Provider Scheduler：没有供应商容量瓶颈证据。没有 MCTS、新常驻工具或新 Skill。
- 系统剪贴板和多个终端宿主的人工体验未验收；自动化已覆盖四种尺寸的真实 VT 渲染/按键，剪贴板系统调用使用替身。
- 下一阶段优先：① 固定预算真实模型留出集与统计消融；② 按项目实际发布版本和重新生成的真实证据修复机器规约；③ 在真实任务上记录经验命中后的验收贡献、Token 与失败路径，决定是否扩大经验检索。

### 最后兼容性回归与工件归档

- 972 项全量发现一个本次新问题：旧 UI 快照缺少 run_id 时，历史 Worker 结果归属字段取值抛 KeyError（test_background_notice_is_visible_without_enqueuing_model_input）。已修为缺省兼容读取，没有删除或放宽测试；保留失败日志 regression_found_compatibility_error.log，正在重跑全量。
- A/B/C/D 的 1586 个真实本地工件（原始事件库、验收、报告、检查点）已压缩到 long_run_20261128/competition_artifacts.zip，避免仅依赖临时目录。ZIP 内路径对应 competition.json 各 artifact_dir 的最后一级目录。
- B→C 的工具结束到下一次模型请求延迟（各轮 p50 的中位数）为 254.281→3.635 ms；同样是固定 Replay 工程路径，不包含真实网络等待。
- 400 任务/40 新事实的增量优先级验证：修改前后均只评估 40 次，350.04→347.78 ms；说明该能力在本轮开始前已经存在，没有将既有能力冒充新增收益。

### 最终验收记录

- 收尾时间：2026-11-28 05:22:40 +08:00；本次实际持续 185 分 23 秒。期间进行代码、测试、性能定位与证据归档，没有用空转填补时长。
- 最终全量：**972 项，960 通过、11 跳过、1 项原有规约失败，0 error，256.242 秒**。本次新增 40 项测试，没有删除旧测试；最后发现的旧快照兼容回归已修复并通过全量。
- 剩余失败只有 test_current_contract_is_machine_verifiable，原因仍为原有版本/删除证据/不存在测试引用。没有为了通过而修改规约或恢复用户删除的文件。
- 六阶段代码与离线工程验收完成；真实固定模型的竞赛成功率、Token 节省仍待验证。具体限制见前述章节，不以 Replay 结果替代竞赛成绩。
- 原有 AgentRuntime、ToolRegistry、workspace 批量读取/并发工具源码哈希与会话开始一致。main 的 HEAD 未变；未提交、推送、重置或恢复用户已有删除。
- 交付入口：session_changes.json（实际文件与测试清单）、verification.json（准确计数与时间）、regression.log（完整最终日志）、scheduling.json、competition.json、ui_performance.json（原始样本）、competition_artifacts.zip（1586 项真实工件）、artifact_hashes.json（归档校验）。
