# 统一运行事件与断点续流

日期：2026-11-13。

模型请求、工具调用、后台作业、任务调度和协作图变更使用 `trina.events.EventBus`。每次独立运行在 `runs/<run-id>/events.sqlite3` 保存事件；Swarm 的 Worker 共同写入所属 Commander 的事件库，`run_id/task_id/worker_id` 保留具体来源。原有 JSONL 哈希链审计、状态文件和报告继续保存。

## 事件信封

| 字段 | 含义 |
|---|---|
| `version` | 当前协议版本为 1 |
| `stream_id` | 事件库所属运行；重开同一事件库必须匹配 |
| `seq` | SQLite 事务分配的单调序号；在同一事件流中比较 |
| `run_id` | 生产事件的具体运行 ID，包括 Worker 尝试号 |
| `kind` | 事件类型 |
| `time` | 生产时间，Unix 秒 |
| `source` | runtime、worker、commander、executor、background 或 recovery |
| `task_id/worker_id` | Swarm 来源；普通聊天为空 |
| `call_id` | 一次工具或后台作业的关联 ID |
| `payload` | 独立 JSON 快照；敏感字段按配置脱敏 |

事件身份是 `(stream_id, seq)`。不同事件库的数字序号不能互相替代。订阅者获得的对象与生产者原参数相互独立，订阅者修改对象不改变已提交记录。

## 生命周期

```text
model.started → model.connected → model.text_delta / model.thinking_delta → model.tool_call → model.finished
             ↘ model.error → model.finished（ok=false）→ model.retry（仅可恢复故障）
user.message → assistant.message（同一 message_id，text 为当前正文，final 标识确认）

tool.requested
  → tool.approval_requested（显式启用审批时）
  → tool.validated → tool.started → tool.output（stdout/stderr/log）
  → tool.finished
  → state.updated（产出事实或证据时）

job.started → job.output → job.finished
task.started → task.finished
coordination.* → runtime.checkpoint / runtime.finished
```

参数错误、未知工具、不可用、控制器拒绝、人工拒绝和护栏拒绝均产生终态；未获执行许可的调用没有 `tool.started`。终态包含 `completed/failed/denied/invalid/unavailable/timed_out/cancelled/interrupted`。原生模型调用 ID 另存于 `model.tool_call.native_call_id`，工具执行使用本地 `call_id`。

`model.error` 在收到失败时发布，不等待后续重试预算耗尽；不发送流事件的后端在 `model.finished` 前补发错误。终端按 `(stream_id, request_id)` 避免错误与失败终态重复显示。`model.retry` 包含 `attempt/delay/failure_kind`；鉴权和额度错误不产生重试事件。`model.connected` 表示已经收到响应头，不代表开始推理。

工具输出仍受执行租约约束。调用超时后，迟到线程不能追加输出事件或提交隔离状态。前台命令通过原有 `run_external` 放行后，在 `scope.execute_streamed` 读取两个输出管道并处理取消和进程树回收。后台作业有独立生命周期，启动调用返回后仍可继续产生 `job.output`；会话关闭会等待作业终态事件完成。

终端按 `call_id` 原位更新工具输入、Output、stderr、错误、耗时和退出码。普通说明、原生调用前的正文及最终回答直接在主内容区排版。正文流、工具窗口和历史重放都使用相同的事件投影，工具参数不会作为回答正文显示。

## 持久化与消费确认

SQLite 使用 WAL 和 `synchronous=FULL`。生产者先提交事务，再通知订阅者；订阅者异常不会回滚已经执行的动作，错误保存在 `EventBus.errors`。回放与实时消费都从同一份事件表读取，不在两种模式之间切换临时队列。

公开事件及 CLI 输出按配置脱敏；`state.updated` 和 `runtime.checkpoint` 在同一事务的内部状态表保留完整快照，供 Commander 投影与恢复使用。内部状态与原有 `state.json/swarm.json` 保存同等完整的任务数据，避免脱敏后的凭据改变事实身份或失去可用性。事件库按运行工件保管，公开重放不导出内部状态表。

`replay(after=N)` 严格读取 `seq > N`。`subscribe(...).poll()` 维护本地游标；带 `name` 的消费者调用 `ack()` 后，游标才持久化。进程在处理后、确认前退出时，该批次会再次投递，因此消费者应按事件身份去重。命名消费者需要固定筛选条件；更换用途或筛选条件时使用新的名称。

回调订阅在本进程生产事件时顺序投递；跨进程订阅使用 `poll(timeout=...)`。同进程通知使用条件变量，跨进程最多每 100ms 检查新提交。事件表保留全部记录，显示缓冲和协作 trace 的裁剪不删除事件。

```powershell
# 重放成与控制台一致的正文和工具窗口
python -m trina events runs/<run-id>/events.sqlite3

# 从序号 120 之后继续，持续输出 JSONL
python -m trina events runs/<run-id>/events.sqlite3 --after 120 --follow --json

# 命名消费者保存进度，下次使用同一命令接续
python -m trina events runs/<run-id>/events.sqlite3 --consumer inspector --follow --json

# 仅查看一个 Worker 任务的工具事件
python -m trina events runs/<swarm-id>/events.sqlite3 --task main --kind 'tool.*' --json
```

## Commander 调度与恢复

Commander 在自己的线程消费 Worker 的 `tool.finished/state.updated`，验证任务、Worker 和尝试运行 ID 后更新 Trace 和事实图。真实事实在 Worker 返回前即可进入协作平面；新事实触发图修订，下一轮 Planner 读取最新图。依赖仍按任务终态解锁，普通输出和重复事实不增加规划轮。

Future 完成、Intent 变更和事实发布通过同一事件流唤醒 Commander。200ms 超时只用于检查外部暂停、停止等信号。任务 DAG、能力租约和独立 Worker 状态继续遵循原有执行规则。

检查点先作为 `runtime.checkpoint` 事务提交，再原子替换 `swarm.json`。检查点保存 `commander_cursor` 和协作投影。恢复时先读取较新的已提交事件快照，再接收尚未消费的 Worker 事实；原在途 Intent 回收到新尝试。旧工具调用没有完成记录时标记 `interrupted`。

事件重放恢复显示、观察和调度投影，不重新发起历史网络请求或执行历史命令。Swarm 执行恢复仍以任务为单位，旧原生进程及在途模型请求不复活。Planner 保持确定性的 DAG 调度，不从任意模型文本自动生成研究分支。

## 验证入口

`tests/test_events.py` 覆盖并发顺序、深层快照、字段脱敏、确认与未确认游标、跨进程续流、重放和实时衔接、订阅异常重试、工具终态、真实命令增量、迟到线程拒绝、终端去重及实时事实调度。`test_runtime_status.py`、`test_registry.py`、`test_swarm.py` 和 `test_chat_resume.py` 检查既有取消、护栏、租约与恢复规则。
