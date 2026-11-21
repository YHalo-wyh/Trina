# ADR-0015：洞察层——假设台账、失败分类与证据关系图

- 状态：已采纳
- 相关：ADR-0001（大脑自持 ReAct 循环）、ADR-0009（证据优于状态码）、ADR-0011（评测计分口径）

## 背景
实测暴露了三类"看起来能跑、其实说不清"的问题：

1. **盲动**：模型每一步都在调工具，但"为什么现在做这一件事"只存在于当步的 `thought` 里，
   下一步就丢了；无法回答"你当初怀疑什么、验证到哪一步了"；
2. **打转**：同一工具 + 同一参数 + 同一结果反复调用（实测出现过连续 3 次 `http_fingerprint` 失败重试），
   失败原因不外显，也没有"换路"的硬机制；
3. **结论不可追溯**：报告里只有"漏洞列表"，没有"这条结论由哪些证据、哪次检查、哪条假设支撑"，
   复核者只能逐条读日志反推。

## 决定
在"工具事实库（`RunState`）"之上加一层**洞察层**，四件事各自独立、可单测：

1. **假设台账**（`trina/hypotheses.py`）
   - 模型可在动作里带 `hypothesis`（`statement/confidence/value/info_gain/cost/risk/next_step`），
     等级词（high/medium/low）折算成数值；
   - 优先级 = `置信度 × 价值 × 信息增量 / (成本 + λ·风险 + ε)`，λ 由配置项 `hypothesis_risk_aversion` 控制；
   - 状态机 `open → verifying → confirmed | refuted | blocked`，**由结果确定性驱动**：
     同漏洞类、位置与参数的带证据 verified/exploited 发现 → confirmed；明确反证 → refuted；连续无新信息降优先级并保留未解决；被护栏拒绝 → blocked（等人）；
   - 结束未收敛的假设显式标为"未解决"（不假装验证过、也不当已否定），并写进结论说明；
   - 同义假设按前 24 字前缀去重，避免模型换措辞把台账撑爆。

2. **失败分类与循环守卫**（`trina/brain/failure.py`）
   - 把每次工具结果归成 `ok / boundary_denied / tool_error / environment_error / budget_exhausted / hypothesis_error`；
     计数写入 `state.counters["fail_*"]`；
   - 循环守卫按"工具+参数+结果指纹"识别无新信息重复，给出**递进建议**：
     第 2 次"换参数/换切入点" → 第 3 次"换工具/换阶段" → 第 4 次起"建议人工介入"；
   - `alternatives_for()` 在注册表里挑**同类能力、动作等级不更高**的替代工具，建议是可执行的而不是空话。

3. **证据关系图**（`trina/evidence_graph.py`）
   - 由 `RunState` 单向构建：目标 → 主机 → 端口/技术栈 → 路径 → 发现 → 证据，另有 检查项 → 发现、假设 → 发现、发现/路径 → flag；
   - 输出 mermaid（Markdown 报告用）+ 结构化节点/边（HTML 报告与 JSON 用）；
   - 构建/渲染失败**不影响**报告生成（报告宁可少一节也不能挂）。

4. **工作台视图与内部指标**
   - `trina/board.py`：纯函数 `render_board()`，一屏给出"任务状态 / 研究成果 / 假设台账 / 覆盖 / 最近动作 / 成本"，
     控制台 `/board` 与 `trina watch` 共用同一份渲染（避免两套视图漂移）；
   - 控制台新增 `/hypotheses`、`/graph`；`trina watch [run_id]` 周期刷新；
   - 指标口径补齐（`docs/EVAL.md` §1.2）：`cost_per_success`、`time_to_evidence_avg`、
     `redundant_action_rate`、`recovery_rate`——不只看通过率。

5. **对照开关**（`run.hypothesis_ledger` / `run.loop_guard`，默认 `true`）
   - 关闭只影响"模型看到什么"：提示词里不出现台账、模型给的 `hypothesis` 不登记、循环/失败建议不回灌；
   - **度量口径不变**（`redundant_action_rate`、`fail_*` 照常统计），否则两条臂不可比；
   - 两条臂的提示词由**同一模板**渲染（`render_system_prompt(include_hypothesis)`），不会漂移。
   - 假设内容进**审计哈希链**（`react.action.hypothesis`），而不只在可改的 `state.json` 里。

## 实测验证
- `tests/test_insight.py`（26 项）：优先级公式、等级词折算、去重、状态机、收尾语义、循环递进、
  失败分类、替代工具、图的节点/边/渲染、面板字段、四项新指标、`RunState.from_dict` 往返、
  以及**开关语义**（关臂提示词无 `hypothesis`、台账为空；守卫关闭后度量照记但建议不回灌；假设进审计链）；
- 本地 lab 实跑：一次 7 步任务记录 4 条假设（2 条 confirmed、2 条收尾为未解决）、图 30 节点/36 边，
  报告新增"六、证据链与假设台账"（含 mermaid）；
- 失败注入实跑（靶机不在场）：3 次 `environment_error` 被分类计数、循环守卫给出换路建议、
  `redundant_action_rate=0.1667` 落进 `state.json`；
- **A/B 对照（`review/experiments/E4-insight-layer.md`）**：两臂各 3 次、同一任务交替执行 →
  开关生效可证（开臂每轮 3–5 条假设，关臂 0）；验证发现 4 vs 1（方向性，n=3 不作结论）；
  成本 **+44% token**（9807 vs 6801）、Time to Evidence +3.8s、步数 +6%；
  负对照（靶机全挂）两臂均为 INCONCLUSIVE、0 发现——洞察层不会无中生有。

## 后果
- 正面：每步决策可解释（假设→验证→状态）、失败可归因、结论可反向追溯、
  报告与工作台共享同一事实源；"打转"从不可见变成有刻度的指标。
- 负面：**实测成本 +44% token**（同任务 9807 vs 6801）、Time to Evidence +3.8s、步数 +6%；
  提示词变长（动作协议多一个可选字段）、`state.json` 变大（假设/图）、台账与图的构建多一次遍历（毫秒级）。
  是否值得取决于目标场景：拿到更多**已验证**结论 vs 每目标多花的 token（当前 ≈ 7355 vs 20404 token/个已验证发现，n=3）。
- 已知边界：`redundant_action_rate` 只看"指纹相同"，参数微调但无信息增量的动作不计入（宁可低估）；
  简单任务上守卫可能**一次都不触发**（本轮 A/B 两臂冗余率均为 0，守卫收益未被测到）；
  HTML 报告不内嵌 mermaid（离线自包含优先），改以"起点-关系-终点"表代替。

## 被否决的方案
1. **画在提示词里**（把假设塞进 system prompt 让模型自己维护）：模型会漂移、会漏，无法审计（同 ADR-0004 的口径）。
2. **只记日志不做状态机**：日志不能驱动决策（无法"按优先级决定下一步"），也无法在收尾时说明未答的问题。
3. **HTML 里引 mermaid.js CDN**：报告要求离线自包含、无外部资源（ADR 与报告契约），故否决。
4. **用"关闭台账但仍展示 field"的方式做对照臂**：模型会被字段引导去自我维护假设，臂不干净；
   故改为同模板渲染的 `include_hypothesis=False`（提示词里整段消失）。
