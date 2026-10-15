# brief · 问题、范围、验收ID、责任

## 问题（要解决什么）
授权靶场内的**证据驱动安全测试**：给定一个已授权的靶场，自动完成侦察 → 枚举 → 漏洞验证 → 取证 → 报告，
并对每一步给出**可复核的证据**；在证据不足、超预算或授权被撤销时**如实停止**，而不是宣称"全部安全"。

## 范围（明确边界，来自配置而非提示词）
| 维度 | 约定 | 来源 |
|---|---|---|
| 目标范围 | 仅 `configs/trina.yaml#scope.authorized_cidrs` / `authorized_hosts`；公网默认拒绝 | `trina/scope.py` |
| 动作等级 | `detect` < `verify` < `exploit`，默认上限 `verify`；破坏性动作另需显式开关 + 主机白名单 | `configs/trina.yaml#scope` |
| 授权策略 | `free` / `guided`（默认）/ `strict`；strict 只接受注册 `target_id` + `test_id` | `configs/trina.yaml#policy` |
| 预算与停止 | 检查数上限、逻辑预算秒数、连续无新增证据即停 | 同上 |
| 检查项来源 | 靶场 yaml 的 `targets:` / `tests:` 段（人可读、可审查） | `configs/ranges/*.yaml` |
| 模拟器优先 | 先在 `trina lab`（内置故意脆弱靶机）验证，再接入授权端点 | `trina/lab/`、`release/README.md` |

**不做**：真实目标、未授权扫描、DoS/删库/写 webshell/拖库导出（硬禁止清单，配置无法开启）、真实凭据与业务数据。

## 验收ID（评测与复核都以此为准）
| 验收ID | 内容 | 判定方式 | 证据位置 |
|---|---|---|---|
| A1 | 目标越界必须被拒绝，且不产生网络动作 | `trina guard check 8.8.8.8` 拒绝 + 审计 `guard.deny` | `evidence/`、`tests/test_scope.py` |
| A2 | 只接受注册检查项（strict） | 未注册 `test_id`/目标被控制器拒绝，记录 `boundary_events` | `tests/test_policy.py`、审计 `policy.deny` |
| A3 | 预算与停止合同生效 | 超检查数/逻辑预算/连续无新证据即停，并给出未测项 | `state.coverage`、报告「结论与覆盖」章 |
| A4 | 结论三分类正确 | 有证据 → CONFIRMED；跑完无发现 → NOT_CONFIRMED；有缺口/失败/受限 → INCONCLUSIVE | `state.verdict`、`docs/EVAL.md` |
| A5 | 每条漏洞有证据、可复现 | finding 必带 evidence（请求/响应/命令），报告含复现命令 | `evidence/`、报告「漏洞详情」章 |
| A6 | 审计不可篡改 | `trina audit verify` 通过；改一条即报错 | `evidence/` 审计样本 |
| A7 | 人工可介入 | 审批闸门、`hint`、`pause/stop`、撤权 `revoke` 均生效 | `tests/test_interactive.py`、`docs/RUNBOOK.md` |
| A8 | 可交接与恢复 | 干净环境按 `release/README.md` 跑通；能重置靶机与证据 | `release/README.md` |

## 责任（谁决定、谁执行、谁复核）
| 角色 | 职责 | 说明 |
|---|---|---|
| 操作者（人） | 批准/拒绝高危动作、决定是否撤权、对结论签署 | 交互控制台 `trina console`；审批超时按拒绝处理 |
| 计划器（模型） | 在注册检查项里**提议**下一项检查与参数 | 只能建议，不能扩大范围、不能把工具输出当许可 |
| 控制器（确定性代码） | 逐动作批准：目标/检查项注册 + 预算 + 撤权 | `trina/policy.py`；不读模型的自我声明 |
| 护栏（确定性代码） | 范围、动作等级、破坏性、硬禁止清单、限速 | `trina/scope.py`；执行链单点收口 |
| 复核者（结对组/教师） | 复核证据与结论边界，必要时按注册项补测 | `review/README.md` 模板 |

> 详细计划与里程碑见 [`docs/PLAN.md`](../docs/PLAN.md)。
