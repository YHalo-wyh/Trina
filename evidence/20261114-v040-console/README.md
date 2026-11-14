# 0.4.0 控制台验证 · 2026-11-14

检查对象：单行底栏、Worker 角色切换、Git 分支、字符首页与上下文兼容。

`render_preview.py` 从真实 prompt_toolkit 屏幕缓冲生成 [预览图](console-preview.png) 和 [文本](console-preview.txt)。其中角色、模型和用量为显示演示数据，不代表真实模型成绩；产品字标仍是终端字符。预览脚本使用 Pillow，Trina 运行不依赖它。

实测入口：

- `tests.test_console_v040`：真实临时 Git 仓库中编辑 main、创建/切换分支、验证脏文件冲突不丢内容；Worker 独立历史/角色/用量恢复；1–144 列单行布局；首页动画在提交后冻结。
- `tests.test_console_session`：真实 VT 屏幕、固定底栏、草稿、菜单返回、键盘输入。
- `tests.test_banner`：0.4.0、无色字符与动画字形一致。
- `tests.test_cache_runtime`：小窗口继承检查点与稳定前缀。

执行状态：本人通过自动化工具实际执行；回放模型不等于真实供应商测试。

Windows 工作区完整关卡：574 项，564 通过、10 跳过。为排除未提交的其他 CTFY 改动，又将待提交代码单独检出为快照 `261c822d15f28111b40eddebc8e9ca45f8d6e82a`（树 `ddf59fc9e76ce023339f175efb66556367a8bfbb`）执行机器总关卡。隔离版最终 570 项，560 通过、10 跳过；22 项需求、50 条规则、121 个测试引用及课程材料全部通过。最终提交仅在该代码快照上补充本验收记录与 README 数量说明。

隔离首轮曾有 `test_upload_not_reported_on_soft404` 失败，Windows 回环 HTTP 返回 ConnectionAbortedError(10053)。该用例加新增用例单独重跑 6 项通过，随后完整关卡再次执行 570 项通过；未修改旧测试或放宽判据。

WSL 已实际执行 `python3 -X utf8 -m unittest tests.test_console_v040 tests.test_console_session tests.test_banner tests.test_cache_runtime -q`：44 项通过。

限制：没有测量所有终端的交互延迟，也未验证新提示词的竞赛收益。Worker 角色切换不等同于 Swarm 运行中接管；角色槽位跨进程不自动恢复，历史角色由 `/resume` 恢复。
