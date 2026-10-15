# review · 实验、Change、Finding、互评与签署

> 本目录放**对事实的解释与复核**，不放运行事实（运行事实在 `evidence/`）。
> 目标组自己写的"已通过"属于自述，必须标注复核状态。

## 现成工具：评价 Agent（只读）
```bash
# 生成复核初稿（核对声明与证据、覆盖缺口、边界事件、审计链），再由人签署
python3 -m trina review --state runs/<run_id>/state.json --save review/
```
评价 Agent 的权限边界（与产品 Agent 分离）：
- **只读** `state.json` / 审计 JSONL / 评测结果；
- **不**接管扫描器、**不**新增工具、**不**放宽目标清单；
- 只能给出「建议补测的注册检查项」清单，实际补测仍要走控制器批准。

## 实验卡索引（O4：实验卡 + 逐次数据 + ADR）

| 实验 | 问题 | 结论（含负结果） | 对应 ADR |
|---|---|---|---|
| [E1](experiments/E1-verdict-evidence-vs-status.md) | 验收判定该看状态码还是响应证据？ | 软 404 靶机上误报 1 → 0，真实发现未减少；**不能**推广为"所有站点零误报" | [ADR-0009](../design/adr/0009-evidence-over-status-code.md) |
| [E2](experiments/E2-credential-verification-scope.md) | 弱口令验证该按工具级还是按尝试规模定等级？ | 显式凭据验证 0/1 → **1/1**、等级拒绝 8–10 → 0；字典仍被拦；**主机场景仍波动**（2/3 ↔ 1/3） | [ADR-0010](../design/adr/0010-credential-verification-scope.md) |
| [E3](experiments/E3-fixed-vs-dynamic.md) | 固定全跑 vs 动态选择检查项，谁更划算？ | **部分完成**：只有动态臂数据（覆盖 5/11、CONFIRMED、53s）；固定臂未跑，因此**不能**宣称动态更优 | [ADR-0012](../design/adr/0012-fixed-vs-dynamic-checks.md) |

> 每张实验卡都必须写清：**统计单位、数据集/版本、无法控制的变量、结论边界**。
> 「没有优势/负结果」同样是有效结论，不允许用含糊总分解释所有变化。

## 模板

### 1) 实验卡（对应 O4 工程取舍）
```markdown
# 实验卡 E1 · <一句话问题>
- Claim：<要证明/证伪的一句话>
- 固定条件：数据/窗口/目标/预算/工具条件
- 唯一主要自变量：<只写一个>
- 无法控制的变量：<诚实列出>
- 结果（含负结果）：<逐次数据 + 汇总，写清统计单位与分母>
- 结论边界：<能说什么、不能说什么>
- 关联证据：evidence/... ；版本：<commit>
```

### 2) Change 记录（对应 O5 变化与对抗）
```markdown
# Change CH-01 · <变更一句话>
- 触发：<Finding / 需求 / 授权收缩>
- 影响分析：<受影响的状态、接口、证据>
- 新验收：<≥2 条>      历史回归：<≥2 条>
- 对抗/边界用例：<越权、撤权、坏参数、软 404、假 flag…>
- 结果：<通过/失败 + 证据索引>
```

### 3) Finding（对应 O3/O5）
```markdown
# Finding F-01 · <标题>
- 目标/版本：<target_id> / <commit>
- 证据：<请求/响应/命令回放 + 审计序号>
- 复现：<最小步骤>
- 结论状态：CONFIRMED / NOT_CONFIRMED / INCONCLUSIVE
- 修复建议：<具体到文件/配置/响应头>
```

### 4) 互评与被否决的 AI 建议（对应 O1/O7）
```markdown
# 互评记录 · <日期>
- 被复核版本：<commit / run_id>
- 复核者：<结对组/教师>
- 采信结论：<采信 / 部分采信 / 退回补测>
- 指出问题：<给最小复现，不泛称"不安全">
- 被否决的 AI 建议（≥1 条）：<建议内容> → 否决理由 → 改用什么
- 签署：<人>  <日期>
```

## 索引（每阶段 1—2 页说明 + 3—5 个关键链接）
| 事件 | 说明 | 关键链接 |
|---|---|---|
| E1 方案与证据挑战 | 见 `docs/EVAL.md` 与 `evidence/` 中的评测摘要 | `eval/README.md`、`evidence/README.md` |
| E2 Change 接力 | 撤权与预算收紧的演练：`revoke` / `max_checks` | `tests/test_policy.py`、`design/adr/0006-*.md` |
| E3 受控对抗 | 蓝队对抗代理（WAF/封禁/限速/篡改/蜜罐）+ 日志/工具输出注入 | `docs/EVAL.md §3`、`design/ai-rules.md §2` |
| 复核签署 | 由 `trina review` 生成初稿，人工填写结论 | 本目录 `*-review.md` |
