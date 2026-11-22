# 项目验证工作单

离线规约和课程文档检查可以用工作单交接。一个工作单绑定一个仓库路径、一个工作单 ID 和一组显式指定的输入文件。`runs/project-verification/<ID>/STATE.json` 保存输入哈希、已登记轮次及带文件引用的结论。结果明细写入同目录下的 `checks/`。这些文件保存在本地运行产物目录。

## 建立工作单并执行检查

在仓库根目录执行以下命令，按任务选择配置和样例文件。每个 `--input` 都绑定文件的 SHA-256。

```text
python -m scripts.project_verification init docs-refs --input pyproject.toml --input docs/course/traceability.json
```

把下面的计划存到 `runs/docs-refs-plan.json`。`checks` 列出本轮检查及各自预计耗时，费用按预计秒数求和。`expected_signal` 写明预期观察和依据，`failure_criterion` 写明怎样判断失败，`alternative` 写明失败后改查哪项工程前提。

```json
{
  "premise": "index-matches-checkout",
  "checks": [
    {"id": "V-CONTRACT", "estimated_s": 3},
    {"id": "V-COURSE", "estimated_s": 3}
  ],
  "budget_s": 10,
  "expected_signal": "索引来自当前仓库，规约声明和课程包引用均能在本地找到",
  "failure_criterion": "任一检查非零退出，定位首个失效引用",
  "alternative": "核对文档迁移记录，更新追踪索引后以新的前提复验"
}
```

```text
python -m scripts.project_verification run docs-refs --plan runs/docs-refs-plan.json
python -m scripts.project_verification show docs-refs
python -m scripts.project_verification handoff docs-refs
```

当前入口复用 `V-CONTRACT` 和 `V-COURSE` 两项固定仓库检查。计划总耗时超出预算、缺少判据、输入变化或前提已有轮次时，启动前直接返回错误。实际执行给每条检查传递剩余时间，首个失败或超时结束本轮。检查期间发生输入漂移时保留输出及漂移原因。`started` 表示轮次已登记且结果尚未落盘。意外终止后保留这条记录，复盘时能看到已经消耗过一次机会。

## 写回证据再交接

用实际检查输出或工作区内的复核记录提交结论。文件引用带 SHA-256。若本轮复核证否了示例前提，可按下例记录。

```text
python -m scripts.project_verification record docs-refs index-matches-checkout refuted --evidence runs/project-verification/docs-refs/checks/index-matches-checkout.json
```

同一前提的结论只能按相同证据幂等提交。新计划如需替代旧前提，在 JSON 增加 `"replaces": "index-matches-checkout"`，并为新前提使用新 ID。工作单校验旧反证的文件哈希。程序按前提 ID 去重，维护者负责判断改名后的两个前提是否相同。超时、运行错误和中断会保留为独立状态，交接者应结合证据判定结论。

交接时发 `handoff` 的输出即可。它给出 `STATE.json` 路径、最多八个已证否的前提 ID 和总数。接手方用 `show` 查看完整清单及证据引用。检查结束后先落盘再交接，后续验证由命令退出和结果文件确认。输入内容变更后，按新的输入创建工作单。
