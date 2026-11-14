# 子 Worker 与同排底栏验证 · 2026-11-14

本次实现 `/subworker` 实时面板、独立模型/供应商/思考配置、后台并行、运行中追加、等待任务改配置、暂停/继续/停止和检查点恢复。主底栏的缓存与 Token 改到模型/轮次右侧同一行。

## 本人实际执行

- `python -X utf8 -m unittest tests.test_subworkers -q`：9 项通过。
- 暂存代码导出为独立验证仓库后执行完整机器门禁：579 项，569 通过、10 跳过，课程追踪/机器规约/差异检查全部通过，见 `index-verification.json`。导出树为 `92ddca421b3a53ae1ad6e15712091e37a9045cde`；最终补充的只有验证结果与文档，业务源码与此树相同。
- 同一独立快照在 WSL 执行 Subworker、底栏、会话、Swarm 和协作测试共 41 项，通过，耗时 18.373 秒。
- `python -X utf8 -m scripts.verify_contract --execute --json`：混合当前工作区的完整回归 583 项，573 通过、10 跳过；课程追踪、54 条机器规则、130 个测试引用与差异检查通过。结果原件为 `working-tree-verification.json`。
- 两个真实本地 HTTP/SSE 请求分别携带 DeepSeek/max 与 GPT/high 参数，消息保持标准 API 角色结构；本地夹具返回的 Token 和缓存分别进入对应任务及子任务总账。
- 离线模型回放驱动两个真实 Python 子进程，分别在独立运行目录写入 `proof.txt`；统一 ToolRegistry、审计和报告实际执行。没有把文件写入成功当成渗透目标达成。
- 验证了跨线程追加、等价去重、等待配置更新、运行配置拒绝、无效档位回滚、收尾拒绝、取消租约回收、超时依赖收敛与模型快照恢复。
- 真实 prompt_toolkit 应用验证 T 键作用于选中任务；主底栏 1–200 列及任务行窄屏均检查不超宽。

## 预览与性能口径

`footer-preview.png` 与 `subworker-preview.png` 来自实际 prompt_toolkit 屏幕缓冲，使用脚本 `render_preview.py` 采集。截图中的名称、任务、模型、Token、命中率及耗时均为 **UI 演示数据**，没有线上模型评测含义。

`render-performance.json` 保存本机实际布局计时：144 列底栏中位约 0.05 ms；10000 条任务、只渲染可见 8 项的布局函数中位约 0.23 ms。每项执行 1000 次。此数字不含模型网络耗时、磁盘访问、终端传输或屏幕绘制，不能解释为实际 FPS。运行环境和负载变化会影响结果。

## 曾出现的问题

第一次完整回归中的旧 `test_tool_success_invalid_exception_and_timeout_have_one_terminal` 出现一次 `failed` 与 `timed_out` 分类差异。该测试单独重跑通过，随后完整回归通过；未改测试断言或放宽超时。原结果保存在 `first-working-tree-verification.json`。这说明 30 ms 级超时检查存在负载敏感性，当前证据没有证明已消除该偶发问题。

实现期间修复了实际发现的 Swarm 旧问题：`stopped` 被汇总为 `completed`、超时依赖无法终结，以及 UI 线程直接修改任务图的风险；都有专项回归断言。

## 结论与未查范围

当前证据支持：子任务的模型与思考档位实际传递、账本独立、任务图单线程变更、取消/超时终态正确、终端工具真实执行、同排布局与面板键盘操作可用。尚未使用付费线上模型验证解题收益，也没有多台真实终端的主观流畅度比较。

模型网络请求的取消仍受既有传输超时约束；整场费用/Token 硬预算、活动 Worker 完整聊天接管与自动文件冲突处理未实现。`cwd=project` 和 `root=project` 仍共享当前仓库，独立运行目录不等于 OS 沙箱。持久模型默认值写在 YAML `subworker` 段，命令修改的是内存配置。
