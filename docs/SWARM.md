# Trina Swarm 首版使用说明

`trina swarm` 是 V2 RASC 架构的任务级运行入口。Commander 负责 DAG 调度、相似性发现和定向情报转发，Worker 运行独立的 `PentestAgent` ReAct 循环。每个题目获得独立的事实图、运行目录、状态文件、审计日志和报告；Worker 池只复用模型后端实例，不继承前一个题目的对话或 `RunState`。

## 快速运行

```powershell
.\.venv\Scripts\python.exe -m trina swarm --target http://127.0.0.1:9099 --goal "验证目标并获取 flag" --workers 2
```

默认任务图包含并行的 `recon`（服务识别）与 `web`（Web 面研究），完成后执行 `verify`。各任务默认最多 12 步、600 秒，可用 `--max-steps` 和 `--max-seconds` 修改。`--workers` 受 `scope.max_host_concurrency` 限制；每个 Worker 的请求速率份额按总 Worker 数分配。Worker 默认继承 full 信任模式；显式设置 restricted 时才按目标和动作边界拒绝。

## 自定义任务图

将下列内容保存为 `tasks.yaml`：

```yaml
tasks:
  - id: app
    target: http://127.0.0.1:9099
    goal: 梳理应用结构与入口
    capabilities: [web]
    priority: 20
    max_steps: 8
  - id: api
    target: http://127.0.0.1:9099
    goal: 研究 API 参数与响应
    capabilities: [web]
    priority: 10
    max_steps: 8
  - id: verify
    target: http://127.0.0.1:9099
    goal: 验证前序发现并总结结果
    capabilities: [verify]
    depends_on: [app, api]
    max_steps: 12
```

```powershell
.\.venv\Scripts\python.exe -m trina swarm --manifest tasks.yaml --workers 2
```

能力名：`recon`、`web`、`verify`、`full`。它们按工具类别装配现有 `ToolRegistry`，未知能力、重复研究目标、缺失依赖和依赖环在启动前拒绝。程序接口 `SwarmRuntime.add_task(TaskSpec(...))` 可向尚在运行的图中添加候选研究任务，由 Commander 去重与校验。

## 独立上下文与情报转发

Commander 在 Worker 每次决策前查看已完成任务，按下列信号计算相似性：

- 规范化目标和端口相同：`0.95`；主机相同但端口不同：`0.90`。
- 当前任务显式依赖来源任务：至少 `0.75`。
- 任务目标完全相同：至少 `0.70`；验证方向的有效词项高度重合：至少 `0.65`。
- 低于 `0.60` 不转发。

转发内容只包含已观察的资产、带证据的 `verified/exploited` 发现、已验证凭据、结论和工件位置。Flag 不进入跨题情报。情报在模型输入中标为 `Commander 转发情报`，并携带来源任务、运行编号、目标、相似度和理由。Worker 只能把它当作候选线索，必须在自己的目标上重新调用工具验证。`RunState`、证据关系图、假设台账、计数、结论和报告不会吸收其他题目的内容。

## 恢复与产物

```powershell
.\.venv\Scripts\python.exe -m trina swarm --resume runs\<swarm-id>\swarm.json
```

恢复保留已完成任务，只重试中断时仍为 `running` 的任务。恢复粒度是整个任务，尚不恢复 ReAct 循环的中间一步。已完成结果中的 `intelligence` 供 Commander 重新计算相似性；恢复后仍按阈值定向转发，不共享完整对话，也不恢复另一题的模型上下文。

| 产物 | 路径 |
|---|---|
| Commander 检查点 | `runs/<swarm-id>/swarm.json` |
| Commander 哈希链审计 | `logs/<swarm-id>.jsonl` |
| Commander 汇总报告 | `reports/<swarm-id>_report.{md,html}` |
| Worker 状态 | `runs/<swarm-id>-<task-id>-<attempt>/state.json` |
| Worker 哈希链审计 | `logs/<swarm-id>-<task-id>-<attempt>.jsonl` |
| Worker 报告 | `reports/<swarm-id>-<task-id>-<attempt>_report.{md,html}` |

当前首版支持任务级并行、能力装配、租约、复用、Commander 相似性路由、候选任务 API 和任务级恢复。运行中交互式人工接管、自动从 Worker 结果生成新任务、持久经验检索与 A/B/C/D 消融评测尚待下一阶段实现。
