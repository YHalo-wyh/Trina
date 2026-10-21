# evidence · 运行事实（精选样本）

**当前课程提交：[2026-10-21 验证事实](20261021-course-pack/README.md)**，包含完整回归、两轮全新 lab、原始状态与审计、文档追踪检查和隔离失败实验。对应[根目录开题与第一次汇报](../开题报告与第一次汇报.md)。

> 本目录只放**运行事实**：能追到「版本 + 复现命令 + 输入」的证据。
> 对事实的解释与复核放在 `review/`；全量产物（每次运行的 runs/logs/reports）不入库，由 `.gitignore` 排除。

## 本目录内容
```
evidence/20261015-lab-e2e/
├── audit.jsonl            审计哈希链（每条动作含护栏裁决；敏感字段已脱敏）
├── audit-verify.txt       `trina audit verify` 的输出（哈希链完整性结论）
├── state.json             运行状态：发现、覆盖（已测/未测/原因）、结论状态、边界事件
└── report.md              当次生成的渗透测试报告（Markdown）

evidence/20261016-insight-layer/
├── audit.jsonl            审计哈希链（147 条，完整）
├── audit-verify.txt       `trina audit verify` 输出
├── state.json             同上，另含 `hypotheses`（假设台账）/`graph`（证据图）/`loop`（冗余率）
└── report.md              含"六、证据链与假设台账"（mermaid 关系图）的报告
```

## 这份样本是什么

### 样本 1 · `20261015-lab-e2e`（端到端：侦察→验证→夺旗）

| 项 | 值 |
|---|---|
| 场景 | 内置模拟器（lab，故意脆弱靶机，仅回环） |
| 命令 | `python3 -m trina play --target http://127.0.0.1:9099 --goal "拿到 flag 并验证 SQL 注入与命令注入" --max-steps 16` |
| 结果 | 结论 **CONFIRMED**；漏洞 7 条（含 sqli / rce 已验证）；flag 1 个；被拒动作 1 次（`rce_exec` 超动作等级） |
| 覆盖 | 已测 5 / 未测 6 个注册检查项（未测原因见 state.json） |
| 复核 | [`../review/20261015-lab-e2e-review.md`](../review/20261015-lab-e2e-review.md)（评价 Agent 生成初稿，人工签署栏待填） |

### 样本 2 · `20261016-insight-layer`（洞察层：假设台账 + 证据图 + 循环/失败归因）

| 项 | 值 |
|---|---|
| 场景 | 内置模拟器（lab，仅回环）；模型 `deepseek/deepseek-v4-flash`；动作上限 `verify` |
| 命令 | `python3 -m trina play --target http://127.0.0.1:9099 --max-steps 7` |
| 结果 | 结论 **NOT_CONFIRMED**（跑完已调度检查、无已验证漏洞）；漏洞 6 条（含 `.git` 泄露，高危 1）；flag 1 个（`flag{trina_lab_d9176a45}`） |
| 洞察层 | 假设 4 条（2 条 confirmed 由新发现驱动、2 条收尾标为未解决）；证据图 30 节点 / 36 边；`redundant_action_rate = 0.0`；失败 0 |
| 报告 | 新增章节"六、证据链与假设台账"（mermaid + 关系表 + 假设表） |
| 复核 | [`../review/20261016-165055-2598dc-review.md`](../review/20261016-165055-2598dc-review.md)（评价 Agent 初稿） |
| 实验卡 | [`../review/experiments/E4-insight-layer.md`](../review/experiments/E4-insight-layer.md)（含故障注入那轮的失败归因数据） |

## 怎么复现
```bash
git checkout <本目录文件对应的 commit>          # 见 git log
python3 -m trina lab --http --port 9099         # 起模拟器（或直接让 play 自动拉起）
python3 -m trina play --target http://127.0.0.1:9099 --goal "拿到 flag 并验证 SQL 注入与命令注入" --max-steps 16
python3 -m trina audit verify logs/<新 run_id>.jsonl   # 哈希链应完整
python3 -m trina review --state runs/<新 run_id>/state.json
```
> 注意：模型采样与靶机时序会带来差异，**不要求分数完全一致**；
> 要求的是：结论与覆盖能解释、证据可复核、审计链可校验。

## 提交边界（再次强调）
- **可以提交**：本目录这类"小体积、可复核"的关键证据；评测汇总指标（`eval/README.md` 提到的 JSON 摘要）。
- **不要提交**：全量 `runs/`、`logs/`、`reports/`（每次运行都产生，体积迅速膨胀且可重复生成）；
  任何令牌/密钥/真实目标数据；大二进制（模型权重、靶场镜像）。
- 灰区（要写清来源才提交）：裁剪过的 `state.json`、自建靶场的回放录制 `*.replay.yaml`。
