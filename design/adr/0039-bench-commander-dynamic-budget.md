# ADR-0039：Bench Commander 轮次边界动态预算

- 状态：接受
- 日期：2026-11-18

## 背景

Bench 的固定策略先用 `_attempt_budget` 按未完成题数公平分摊，再用 `_retry_worthwhile`
判断是否续做。它能保证覆盖面和可复现性，但有一个明显缺口：已经拿到部分 Flag、或本轮
有新增事实的任务，在单题预算到点时仍会被释放。固定上限无法区分“卡住空转”和“接近完成”。

ADR-0030 的 Commander 负责人工委派、消息和四格观察，不拥有 Worker 预算权；ADR-0032
明确 Bench 关闭自动 Intent，容器生命周期由题目任务独占。本 ADR 只补“轮次边界是否续做/加时”
这一层，不改变任务调度和容器归属。

## 决定

1. 新增 `trina.bench.commander.BenchCommander`，只读取已发生的结构化事实，不调用模型、不执行工具。
2. 每个检查点或轮次结束后输出一个动作：
   - `continue`：继续当前轮；
   - `retry`：先用完剩余单题预算；
   - `extend`：从全局储备池给本题加时；
   - `release`：停止当前轮或释放 Worker。
3. 检查点每 `commander_checkpoint_steps` 个动作触发一次（默认 8），它只是观察频率，
   不是停止上限；`max_steps` 默认 0，不再设置固定的 36 步之类规则。
4. 加时有硬上界：`extension_seconds`、`max_extensions`、`reserve_ratio`、
   `min_remaining_seconds`、`max_redundant_rate`。Commander 不得突破全局储备。
5. 判定顺序：
   - 已拿全 Flag / `blocked` / `stopped` → 释放；
   - 高重复探索率且没有部分 Flag → 停止；
   - 没有部分 Flag 且没有新增事实：执行中默认 `continue`，轮次结束才 `release`；
   - 有部分 Flag 或新增事实：软窗口未用完时 `continue/retry`，用完时从储备池 `extend`。
6. 默认关闭；`--commander` 或 `TRINA_BENCH_COMMANDER=1` 启用。关闭时保持原有固定规则。
7. 每个决策写入 `attempt_metrics[].commander_action/commander_reason`，汇总写入
   `metrics.commander_extensions/commander_checkpoints`，便于审计和 A/B 对比。

## 结果与限制

- 加时可以超过“剩余总预算 / 未完成题数”的公平份额，但仍受全局储备和次数上限约束。
- 这是确定性规则，不是模型 Commander；模型判断需要实现同一 `review()` 契约并补充审计，
  当前不产生额外模型调用。
- Commander 不启动、关闭或迁移容器；容器生命周期仍由单题任务在 `finally` 中独占。
- 不再有固定的 36 步规则；单题时间窗口是软窗口，可由检查点加时，平台整场 6h 和总预算
  是仅剩的硬边界。
- 默认 `max_extensions=1`、`extension_seconds=60`，避免“快出来了”变成沉没成本。

## 验证

- `tests.test_bench_commander.CommanderTests`：部分 Flag、事实增量、无进展、高重复率、
  全局储备、次数上限、无总时限、执行中检查点和配置校验。
- `tests.test_bench.TestRunner.test_commander_extends_partial_task`：部分 Flag 任务在单题
  预算耗尽后由 Commander 加时并完成第二个 Flag。
- 既有 `tests.test_bench` 与 `tests.test_unlimited_runtime` 不回归。
