# Trina v0.6.5 发布验证

- 日期：2026-11-28；发布基线：595a01ccb00b037e714707a720f64147f54cdd2a。
- GitHub 与 Gitee 正式标签最高均为 v0.6.4，本次递增到 v0.6.5。
- pyproject.toml、trina/__init__.py、README 和更新日志同步；`python -m trina --version` 返回 `trina 0.6.5`。
- 机器规约同步产品版本，并将失效的滚动测试引用改为现有真实 VT 验收测试；未改变规则或降低验收要求。
- 从暂存区导出独立发布快照，保留 Git 已有历史证据。本地 173 项 evidence/eval 删除及无关临时脚本保留在用户工作区，不进入本次发布。
- 受测源码、测试和配置与暂存区一致。验证时暂存树：e06046e5cb3ad5722574eba608e10a46b622a8b3；本记录和回归日志在验证后加入，不改变执行代码。

## 验证结果

| 检查 | 结果 |
|---|---|
| `python -m scripts.verify_contract` | 通过：6 类交付物、24 项需求、70 条规则、185 个测试引用 |
| `python -m scripts.verify_course_pack` | 通过：32 份文档、20 项要求、8 个任务、433 个本地链接 |
| `python -m unittest discover -s tests` | 972 项，961 通过、11 跳过，359.303 秒 |
| `git diff --cached --check` | 通过 |
| 原始验证工件 SHA-256 | 14 项暂存字节均匹配；`.gitattributes` 保留原始换行字节 |

完整回归日志：[release_0.6.5_tests.log](release_0.6.5_tests.log)。历史研发记录的失败日志保留原样；发布快照的通过结果以本记录为准。真实模型竞赛成绩及 Token 收益仍待独立评测。
