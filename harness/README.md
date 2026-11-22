# 项目级 Harness

从这里查找 Trina 的工程规则、验收和恢复流程。

| 事项 | 入口 |
|---|---|
| 开发规则与模块契约 | [AGENTS.md](../AGENTS.md) · [架构](../docs/ARCHITECTURE.md) |
| 需求、规约与决策 | [需求](../需求/README.md) · [规约](../规约/README.md) · [ADR](../ADR/README.md) |
| 测试入口、验收与回滚 | [Harness 清单](harness-manifest.md) · [课程验收矩阵](../docs/course/acceptance-matrix.md) · [连续性包](../docs/course/continuity.md) |
| 离线检查的状态、交接与预算 | [项目验证工作单](project-verification.md) |

从仓库根目录运行 `python -m scripts.verify_contract` 和 `python -m scripts.verify_course_pack` 检查规约、测试引用与文档链接。
