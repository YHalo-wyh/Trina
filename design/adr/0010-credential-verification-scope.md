# ADR-0010：动作等级按「尝试规模」判定，被拒时回灌等价做法

- 状态：已采纳（实验见 [`review/experiments/E2`](../../review/experiments/E2-credential-verification-scope.md)）
- 相关：ADR-0004、ADR-0007

## 背景
默认动作等级上限是 `verify`（安全优先），但"验证已知的一组凭据"与"字典爆破"被同一个工具承载。
按工具级一刀切判 `exploit` 的后果：Agent 在默认配置下**无法完成弱口令验证**，
实测出现 29–38 步里 8–10 次等级拒绝、场景只能判 partial。

## 实验数据（本仓库实测）
| 规则 | 场景 | 结果 | 步数 | 等级拒绝 |
|---|---|---|---:|---:|
| 工具级固定 `exploit`（修前） | `host-ssh-weak-password` | partial（GT 0/1） | 29–38 | 8–10 |
| `username`+`password` → `verify` | `host-ssh-weak-password` | **success（GT 1/1）** | 12 | 0 |
| 同规则（表单分支） | `web-login` | **success（GT 1/1）** | 12 | 0 |
| 字典（>3 组）→ `exploit` | `host-ssh-weak-password` | 被拒（符合预期） | — | 1 |

## 决定
1. 工具可实现 `required_level(params)`：按**尝试规模**而不是工具名判定等级
   （显式凭据 ≤3 组＝`verify`；字典或大规模＝`exploit`）；
2. 工具可实现 `level_hint(params)`：被等级拒绝时回灌"更低等级的等价做法"（例：单次凭据验证 / `http_form_login`）；
3. 工具内部子请求必须使用本次调用被放行的**有效等级**（`ctx.effective_level`），避免"外层放行、内层自拒"。

## 后果
- 正面：默认配置下常见任务可完成，同时批量爆破仍被拦；护栏语义更精确（可解释"为什么这次被拒"）。
- 负面：等级判定逻辑进了工具实现，需要每个高危工具自己写清楚；由 `tests/test_policy.py` 等守住。
- 已知边界：规模阈值（3 组）是**试跑参数**，不是安全标准；班级/场景应按靶机承受力冻结。

## 被否决的方案
- 一律按工具名判 `exploit`：安全但把常见任务也堵死（本 ADR 修的就是这个）。
- 一律按"参数里有没有 username"判：模型换写法（`users: ["trina"]`）就失效——按**规模**判才稳。
