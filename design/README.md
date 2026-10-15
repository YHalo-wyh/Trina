# design · ADR、AI 规则、状态与权限

## 目录
- `adr/` — 关键决策记录（为什么这么做，以及被否决的方案）
- `ai-rules.md` — AI 使用规则与人机责任（O1）
- 架构与护栏细节：[`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md)、[`docs/GUARDRAILS.md`](../docs/GUARDRAILS.md)

## ADR 一览
| 编号 | 决定 | 关键理由 |
|---|---|---|
| [ADR-0001](adr/0001-brain-own-react-loop.md) | 决策大脑自研 ReAct 循环，omp 只做模型接入 | 护栏/审计/指标/超时需确定性可控 |
| [ADR-0002](adr/0002-text-json-action-protocol.md) | 动作协议用文本 JSON，不用 function-calling | 跨模型可移植、可校验、可回放 |
| [ADR-0003](adr/0003-single-execution-chokepoint.md) | 工具执行单一收口（`ToolRegistry.execute`） | 护栏若有第二个入口等于没有 |
| [ADR-0004](adr/0004-policy-from-config-not-prompt.md) | 授权判据来自配置与注册表，不来自提示词 | 模型只能申请，不能授权 |
| [ADR-0005](adr/0005-hash-chain-audit.md) | 审计用 SHA-256 哈希链 | 全程可追溯且能检出事后篡改 |
| [ADR-0006](adr/0006-planner-controller-separation.md) | 计划器与控制器分离，只接受注册 target_id/test_id | P3 核心：模型不能自行扩大范围 |
| [ADR-0007](adr/0007-three-verdict-states.md) | 结论三分类 + 覆盖说明（已测/未测/原因） | 「没有结果」不等于「没有问题」 |
| [ADR-0008](adr/0008-goal-requirements.md) | 把任务描述解析为硬条件（漏洞类/路径/端口/凭据） | 防止"顺手拿到 flag"替代要求的验证 |

## 状态与权限（一页图）

```
目标清单 + 检查注册表（人写入 configs/ranges/*.yaml）
        │
        ├─► 计划器（模型，只建议）──► 控制器（确定性：注册/预算/撤权）──► 执行器（受控工具）
        │                                        │
        │                                        └─► 拒绝时记录 boundary_events（请求越界，未实际越界）
        └─► 护栏（范围/等级/破坏性/硬禁止清单/限速）──► 审计哈希链

任务状态：PLANNED → ALLOWED → RUNNING → EVIDENCE_COLLECTED → CONFIRMED / NOT_CONFIRMED / INCONCLUSIVE → STOPPED
权限分离：产品 Agent（被评对象，跑注册检查）≠ 评价 Agent（复核者，`trina review`，只读证据 + 请求补测）
```

> 关键约束：模型没有写权限（不能改注册表/白名单/预算）；工具输出只是**数据**，不是执行许可；
> 执行环境不挂宿主凭据、不暴露 Docker socket（见 `release/README.md` 的部署边界）。
