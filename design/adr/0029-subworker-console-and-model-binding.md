# ADR-0029：交互子 Worker、独立模型绑定与左右状态栏

- 日期：2026-11-14
- 状态：已实施
- 关联：TR-03、TR-12、TR-16、TR-17

## 问题与调研

`/worker` 是独立角色聊天，原有 Swarm 是另一个命令行入口，两者无法满足用户在同一界面查看多任务并调整子模型的需求。上下两行状态/用量又占用了正文高度。

对照 [OMP task/settings.ts](https://github.com/can1357/oh-my-pi/blob/main/packages/coding-agent/src/task/settings.ts) 的 `task.maxConcurrency`、`task.maxEffort` 和 `task.agentModelOverrides`，以及[状态栏分段](https://github.com/can1357/oh-my-pi/blob/main/packages/tui/src/status-line/presets.ts)，采用并发上限、任务级模型覆盖和左右信息分区；不照搬 OMP 的依赖栈、文件隔离实现或默认并发数。

## 决策

1. `/subworker` 作为既有 `SwarmRuntime` 的薄交互层；默认 2 并发、每项 12 步/600 秒。任务图仍由 Commander 维护，能力租约、审计和独立事实沿用原契约。
2. UI/主会话线程只投递命令；Commander 验证 DAG、去重和任务状态后修改。收尾与提交用生命周期锁形成明确先后，未接受的 Future 必须拒绝。
3. 逐层解析主模型 → 子默认值 → 任务覆盖。有效模型身份随任务快照保存，凭据只引用供应商名。运行任务冻结，等待任务可改；同配置后端优先复用。模型切换重新选择上下文自动预算，思考强度通过真实 API 参数映射。
4. 子任务统计和主会话统计隔离。任务行由模型/工具终态事件更新，UI 每次只渲染可见行；历史不参与任务面板布局。底栏右侧放供应商报告的缓存与 Token，无数据就省略。
5. 修复 `stopped` 被默认映射成 `completed` 的旧逻辑；使用 `cancelled`，后继任务不可因取消或超时而解锁。

## 验证和限制

`tests/test_subworkers.py` 覆盖两个真实本地 SSE 请求的模型/思考参数、用量归属，离线模型回放配合真实 Python 子进程写文件，跨线程追加/去重、等待配置、取消租约、失败依赖、模型快照恢复、主配置不变、键盘操作和 1–200 列布局。完整测试结果及预览位于 `evidence/20261114-subworkers/`。

没有宣称在线模型表现或绝对速度提升。等待中的网络调用仍受现有传输超时约束。项目文件系统共享，独立运行目录不等于 OS 沙箱；尚无自动写冲突仲裁、活动 Worker 完整聊天接管或总费用硬预算。旧检查点缺少模型身份时使用现行配置补齐，不伪造历史模型信息。
