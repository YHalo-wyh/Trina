# 统一运行事件与工具窗口验证

日期：2026-11-13。范围：Trina 本地运行事件、工具生命周期、Commander 实时事实投影、持久游标消费与终端展示。

## 实现与验证入口

- [事件协议](../../docs/EVENTS.md)与 [ADR-0027](../../design/adr/0027-unified-runtime-events.md)说明事件身份、确认游标、内部状态和恢复边界。
- [事件测试](../../tests/test_events.py)覆盖并发顺序、跨进程消费、重放接续、异常与中断终态、真实 stdout/stderr、工具窗口、实时事实及较新事件检查点恢复。
- [机器检查结果](contract-result.json)保存实际命令、退出码和输出：550 项单测中 540 项通过、10 项按环境条件跳过；课程追踪与差异检查返回 0。机器规约核对六类交付、22 条需求、49 条规则和 115 个去重测试引用。

## 显示与重放演示

[HTML 预览](tool-window-preview.html)和[纯文本预览](tool-window-preview.txt)由实际 `EventTranscript`、`EnhancedConsoleInput` 和终端渲染函数生成。正文为固定演示文本，工具输出来自通过 `ToolRegistry → run_external → scope.execute_streamed` 执行的真实 Python 进程；没有调用在线模型或外部目标。

[事件记录](demo-events.jsonl)保留完整演示顺序，[演示结果](demo-result.json)保存事件库和审计路径。实时与重放得到相同的三个内容块：说明、工具窗口、完成正文。工具窗口含 stdout、stderr、耗时和真实退出码；输入草稿保持，审计链校验通过。

复现演示：

```powershell
.\.venv\Scripts\python.exe -X utf8 evidence/20261113-unified-events/render_demo.py
```

演示会创建独立的 `runs/event-demo-*/events.sqlite3`，并更新本目录的预览与导出记录。数据库和原审计为本地运行产物；仓库中的 JSONL 用于核对展示顺序。

完整检查：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m scripts.verify_contract --execute --json
```

## 结果边界

这些检查验证事件协议、执行与观察链路，不测量真实模型的解题成功率或协作收益。事件重放恢复显示、观察和调度投影；Swarm 继续以任务为单位恢复，不复活旧模型请求、后台进程或历史命令。
