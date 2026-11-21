# ADR-0028：0.4.0 分段底栏、交互 Worker 与 Git 分支

日期：2026-11-14。状态：已实施。

## 问题与参考

提交 6d794eb 已加入事件驱动工具窗口和底栏信息，但长模型名与用量可能把底部堆成多行，窄屏路径和仪表的预算不一致。用户还需要当前角色标识、Worker 会话切换、当前 main 的代码编辑，以及字符首页动态效果。

阅读 OMP 官方 [presets.ts](https://github.com/can1357/oh-my-pi/blob/main/packages/tui/src/status-line/presets.ts)、[schema.ts](https://github.com/can1357/oh-my-pi/blob/main/packages/tui/src/status-line/schema.ts)、[footer.ts](https://github.com/can1357/oh-my-pi/blob/main/packages/tui/src/status-line/footer.ts) 和 [settings.md](https://github.com/can1357/oh-my-pi/blob/main/docs/settings.md)。采纳分段、密度预设、按宽度收起字段与区分窗口占用/累计用量；未引入其渲染引擎或 Nerd Font 依赖。

## 决定

1. 新增 UIConfig，提供 compact/default/full、dot/pipe/slash、上下文条与动画设置。状态和用量各最多一行；角色优先，窄屏省略次要字段，未知统计不补零。当前分支在会话启动及 Git/终端动作后读取，不在每帧启动进程。
2. `/worker` 复用 ChatStore 建立独立交互会话。每个角色独立消息、状态和用量；当前进程保留角色到历史 ID 的映射，重启用 `/resume` 恢复。快照保存角色，切换关闭旧进程。它不等同于 Swarm 活跃任务接管，后者仍待实现。
3. `workspace_git` 经 ToolRegistry/run_external 提供状态、列举、switch/create；不自动强制覆盖、删除或提交。main 可用同一文件工具直接编辑；冲突由真实 Git 返回。
4. 字符字标保持可复制，首页仅变颜色，输入后冻结。plain、reduced、none 静态显示；版本统一 0.4.0，小字逐字炫彩。产品不依赖图像库，预览脚本只用于生成验证图。
5. 精简七模块 Core，补充代码约定、现有差异、编码验证和跨 Worker 失败经验。工具协议留在任务合同；旧对话保留保存的前缀，采用新 Core 需新建对话。

## 验证与限制

`tests/test_console_v040.py` 使用真实临时 Git 仓库验证 main 编辑与分支切换，并验证角色往返恢复和 1–144 列；`test_console_session` 覆盖持久输入、真实 VT 画面、选项页返回与草稿；`test_banner` 检查动画不改字形。缓存回归继续验证小窗口能创建继承检查点。

未测量真实模型提效，也不承诺所有终端固定延迟。当前分支外部变化在下次相关动作后刷新；角色槽位不做跨进程自动恢复；Swarm 运行中 Worker 视图/接管不属于本接口。
