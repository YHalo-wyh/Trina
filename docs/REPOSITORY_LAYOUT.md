# 仓库结构与清理记录

2026-11-04 清理了项目内的临时构建目录，并合并重复文档。

| 内容 | 处理 |
|---|---|
| `artifacts/` 中的旧镜像、浏览器扩展备份、课程文件生成中间件等约 1.6 GiB | 移至仓库旁的 `Trina-local-archive-20261104/artifacts/`；保留原文件供恢复，项目目录不再包含这些产物 |
| `docs/TSECBENCH_OPENCODE_GO.md` | 删除；接入事实、两轮结果、截图和质量结论集中在 `评测/2026-11-04_TSecBench_OpenCode_Go/评测报告.md` |
| 后续 `artifacts/` 和仓库根 `providers.json` | 加入 Git 忽略规则，避免中间件和本机密钥配置进入提交 |

`trina/`、`configs/`、`tests/`、`scripts/` 分别保存运行代码、配置、回归测试和启动 / 验证工具。`docs/` 与根目录的需求、规约、设计决策提供当前契约。`评测/` 保存按日期归档的实际平台结果。

`evidence/`、`review/`、`design/` 以及课程交付目录保留原有验证依据。它们与用户要求的课程材料、评测证据和设计审查相关，不能按临时构建文件处理。`runs/`、`logs/`、`reports/` 是本地运行交付物，继续按原规则忽略；`.venv/` 是本机依赖环境。

2026-11-05 收尾时，自动审批以 `blocked by policy` 拒绝了 Python 字节码缓存删除。缓存目录保留，仍由 `__pycache__/` 和 `*.py[cod]` 忽略规则排除。
