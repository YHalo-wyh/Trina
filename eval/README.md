# eval · 用例与判定

本轮课程检查的[具体规格与验收矩阵](../docs/course/acceptance-matrix.md)及[2026-10-21 原始验证](../evidence/20261021-course-pack/README.md)已独立归档。下文保留既有 A1–A8 和评测运行入口。

## 用例（谁定义、在哪）
| 内容 | 位置 | 说明 |
|---|---|---|
| 评测场景（用例） | `trina/eval/scenarios/web.yaml`、`host.yaml`、`integrated.yaml` | 每个场景 = 目标 + 任务 + ground truth + 期望端口/服务 |
| 注册检查项 | `configs/ranges/*.yaml` 的 `tests:` 段 | 控制器只批准注册项；`expect_finding` 声明"跑它期望发现什么" |
| 真值（ground truth） | `configs/ranges/*.yaml` 的 `ground_truth:` 段 | **真值不喂给 Agent**，只用于判分 |
| 判定口径 | `docs/EVAL.md`、`trina/eval/metrics.py` | precision/recall/FPR/遗漏率/覆盖度，按场景声明范围评分 |

## 验收ID（与 `brief/README.md` 对应）
| 验收ID | 对应评测证据 |
|---|---|
| A1 越界拒绝 | `tests/test_scope.py`；`trina guard check 8.8.8.8` |
| A2 注册表裁决 | `tests/test_policy.py::TestController`；审计 `policy.deny` |
| A3 预算与停止 | `tests/test_policy.py::test_budget_stops_checks`、`test_no_new_evidence_triggers_stop` |
| A4 结论三分类 | `tests/test_policy.py::TestVerdictAndReview` |
| A5 证据可复现 | `trina review --state runs/<id>/state.json`（缺证据会被标记） |
| A6 审计不可篡改 | `tests/test_audit.py`；`trina audit verify` |
| A7 人工可介入 | `tests/test_interactive.py` |
| A8 交接与恢复 | `release/README.md` 的四步复现 |

## 怎么跑
```bash
python3 -m trina play                                   # 内置模拟器（lab）一键开打
python3 -m trina eval run --suite web                    # Web 专项（自动拉起/回收 lab）
python3 -m trina eval run --suite host                   # 主机专项
python3 -m trina eval run --suite integrated             # 蓝队对抗 + TSecBench bundle 回放
# 离线、零 LLM 成本：
python3 -m trina eval run --suite integrated --set llm.backend=replay
```
产物：`reports/eval/<suite>-<ts>.md|.json`（汇总进 `evidence/` 时才提交）。

## 判定时的三条纪律（来自课程要求）
1. **统计单位与分母写清**：按场景/漏洞类聚合，先写统计单位再比指标。
2. **没有完整标签就限制结论**：不把"未发现"写成"不存在"（结论三分类见 ADR-0007）。
3. **一次只改一个主要因素**：改护栏/改提示词/改工具分开跑，否则无法归因。
