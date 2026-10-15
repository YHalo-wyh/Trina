# design · ADR、AI 规则、状态与权限

## 目录
- `adr/` — 关键决策记录（为什么这么做，以及被否决的方案）
- `ai-rules.md` — AI 使用规则与人机责任（O1）
- 架构与护栏细节：[`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md)、[`docs/GUARDRAILS.md`](../docs/GUARDRAILS.md)

## ADR 一览

### 架构决策
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

### 实证决策（都有实验卡与逐次数据，对应 O4：实验卡＋逐次数据＋ADR）
| 编号 | 决定 | 实验卡 | 关键数据 |
|---|---|---|---|
| [ADR-0009](adr/0009-evidence-over-status-code.md) | 验收判定以响应证据为准，状态码只是前置条件 | [E1](../review/experiments/E1-verdict-evidence-vs-status.md) | 软 404：误报 1 → **0**；目录爆破 700 请求 0 误报 |
| [ADR-0010](adr/0010-credential-verification-scope.md) | 动作等级按**尝试规模**判定，被拒时回灌等价做法 | [E2](../review/experiments/E2-credential-verification-scope.md) | 弱口令场景 partial(0/1) → **success(1/1)**，等级拒绝 8–10 → **0** |
| [ADR-0011](adr/0011-eval-scoring-scope.md) | 评测口径：按声明范围评分 + 端口感知 AND 匹配 + 基础设施类不计误报 | — | web 套件 precision **57.1% → 100%**、FPR **42.9% → 0%** |
| [ADR-0012](adr/0012-fixed-vs-dynamic-checks.md) | 保留阶段机与注册检查项，动态只在阶段内发生 | [E3](../review/experiments/E3-fixed-vs-dynamic.md) | 修前 GT 命中 0/N → 修后 7/8 场景通过；**固定臂未跑**（缺口如实标注） |
| [ADR-0013](adr/0013-human-approval-gates.md) | 人工介入＝审批闸门 + 引导式介入，超时即拒绝 | — | 审批/介入/停止/撤权均有测试与实测会话 |
| [ADR-0014](adr/0014-fixture-self-healing.md) | 夹具自愈：谁拉起谁回收，评测器自动起靶机 | — | 冷环境一键跑通；靶机被回收不再污染指标 |

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
