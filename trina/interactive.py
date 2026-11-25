"""交互式控制台：人工可介入的全自动渗透。

    python3 -m trina console        # 或直接 `python3 -m trina`

三条交互路径
------------
1. **一条命令开打**：`play`（自动起 lab 靶机 → 跑 → 出报告）
2. **运行中实时介入**：`hint <文本>` 注入提示、`pause`/`unpause`、`stop`、`status`
3. **可选审批闸门**：默认关闭；显式指定 `--approve` 后，超时未答复按拒绝处理。

命令都只是薄封装：能委托给 CLI 的（tools/guard/ranges/report/audit）直接复用同一实现，避免两套行为。
"""

from __future__ import annotations

import difflib
import json
from collections import deque
from pathlib import Path
import shlex
import sys
import threading
import time
from typing import Any, Callable

from .background_view import ACTIVE, STATUS_LABELS, BackgroundRow, BackgroundWait
from .config import TrinaConfig
from .console_state import ConsoleContext, ConsoleState, TrackedBackend
from .model_names import display_model as _display_model
from .console_ui import (
    ChoiceSpec, EnhancedConsoleInput, command_detail_sections, command_help_sections,
    command_names, enhanced_input_available, find_command,
)
from .events import RuntimeEvent
from .plan_review import PlanReview
from .session import APPROVAL_MODES, Session
from .shutdown import Shutdown
from .tasks import TaskStore, TaskScheduler
from .terminal import (
    PHASES, clean_text, render_approval, render_message,
    render_panel, render_step, render_answer, render_process, show_home, terminal_width, use_color,
)

#: 已知命令名（用于区分"命令"与"对 Agent 说话"）
COMMAND_NAMES = command_names() | {"y", "yes", "n", "no", "deny"}

_TOOL_ACTIVITY = {
    "terminal_exec": "运行终端命令",
    "terminal_start": "启动后台终端",
    "terminal_status": "等待或读取后台作业",
    "terminal_stop": "停止后台作业",
    "workspace_read": "读取文件",
    "workspace_read_many": "批量读取文件",
    "workspace_write": "写入文件",
    "workspace_edit": "编辑文件",
    "workspace_edit_many": "批量编辑文件",
    "workspace_search": "搜索工作区",
    "workspace_git": "执行 Git 操作",
    "web_search": "搜索资料",
    "worker_tasks": "处理子 Worker 任务",
    "todo": "更新任务清单",
}


class Console:
    """对话式控制台：自然语言对话 + 命令两种入口，共用同一套护栏/控制器/审计。"""

    def __init__(
        self,
        config: TrinaConfig | None = None,
        *,
        input_fn: Callable[[str], str] | None = None,
        output: Callable[[str], None] | None = None,
        backend: Any = None,
        registry: Any = None,
        approval_mode: str = "off",
        autostart_lab: bool = True,
        plain: bool = False,
    ):
        self.config = config or TrinaConfig.load()
        self._output_palette = lambda: self.config.ui.output_palette
        self._input = input_fn
        self._enhanced_input: EnhancedConsoleInput | None = None
        self._plain = plain
        self.output = output or (lambda line: print(line, flush=True))
        self.color = use_color(plain=plain or output is not None)
        self._live_step: Any = None
        self._live_action_hint = ""
        self._requested_action: tuple[str, int | None] | None = None
        self._active_call = ""
        self._plan_review: PlanReview | None = None
        self._planning = False
        self._drafts: dict[int, tuple[str, str, str]] = {}
        self._draft_number = 0
        self._enter_draft: tuple[int, threading.Thread | None] | None = None
        self._job_notices: deque[tuple[str, str]] = deque(maxlen=128)
        #: 注入的后端（测试用）或按当前会话配置构建的缓存
        self._backend_obj = backend
        self._tracked_backend = None
        self.view = ConsoleState()
        self._custom_output = output is not None
        #: 测试可注入的后端工厂（形如 make_backend(llm_config) -> LLMBackend）
        self._backend_factory = None
        self._model_catalog = None
        self._recent_models: list[tuple[str, str]] = []
        self.registry = registry
        self.autostart_lab = autostart_lab
        self._lock = threading.RLock()
        self._last_state: Any = None
        self._last_turn_seconds: float | None = None
        self._plan: Any = None
        self._controller: Any = None
        self._controller_config: TrinaConfig | None = None
        self.chat: Any = None
        self.session = Session(approval_mode=approval_mode, emit=self._session_emit)
        self.exit_requested = False
        self._cleanup_done = False
        self.task_store = TaskStore()
        self._goal_text = self.task_store.goal()
        self._launch_lock = threading.RLock()
        self._scheduler = None
        self._scheduler_thread = None
        self._task_config = None
        self._event_transcript = None
        self._task_title = ""
        self._task_max_steps = 0
        self._todo_snapshot: dict[str, Any] = {}
        self._active_worker = "commander"
        self._worker_sessions: dict[str, str] = {}
        self._job_wait = BackgroundWait(self._background_rows, self._report_job_wait)
        self._branch = self._read_branch()
        self._git_branches = [self._branch] if self._branch else []
        self._git_files: list[str] = []
        from .subworkers import Subworkers
        self.subworkers = Subworkers(self._subworker_summary, on_finished=self._background_notice)

    # ------------------------------------------------------------------ 输出
    def _display(self, text: str) -> None:
        with self._lock:
            self.output(text)

    def _emit(self, message: str, prefix: str = "[trina]") -> None:
        self._display(render_message(message, label=prefix, color=self.color, palette=self._output_palette) if message else "")

    def _session_emit(self, message: str) -> None:
        pending = self.session.pending
        if pending is not None and message.startswith("[审批] 请求执行"):
            self._display(render_approval(pending, self.session.approval_timeout, color=self.color))
        else:
            self._emit(message)

    def _background_notice(self, name: str, status: str) -> None:
        self._job_wait.changed()
        if self._cleanup_done:
            return
        message = f"{name} · {STATUS_LABELS.get(status, '已结束')}"
        self._emit(message, prefix="后台")
        self._notify_agent(message, error=status in {"failed", "timed_out", "blocked"})

    def _subworker_summary(self, text: str) -> None:
        self._job_wait.changed()
        if not self._cleanup_done:
            self._emit(text, prefix="Subworker")

    def _background_rows(self) -> list[BackgroundRow]:
        rows = []
        chat = self.chat
        jobs = chat.ctx.extra.get("background_jobs") if chat is not None else None
        if jobs is not None:
            for item in jobs.snapshot(tail=0, include_all=True):
                status = item["status"]
                if status == "cancelled" and not item["finished"] and item["exit_code"] is None:
                    status = "stopping"
                detail = item["error"]
                if item["exit_code"] is not None:
                    detail = f"退出码 {item['exit_code']}" + (f" · {detail}" if detail else "")
                rows.append(BackgroundRow("terminal:" + item["id"], chat.run_id, status, item["elapsed_seconds"], detail))
        run_id, workers = self.subworkers.monitor_rows()
        for item in workers:
            started = item.get("started_at") or 0
            elapsed = (item.get("ended_at") or time.time()) - started if started else 0
            rows.append(BackgroundRow("worker:" + item["id"], run_id, item["status"], elapsed,
                                      item.get("error") or item["goal"]))
        return rows

    def _show_background(self, title: str, rows: list[BackgroundRow]) -> None:
        if not rows:
            self._emit(f"{title} · 当前没有可显示的后台任务", prefix="后台")
            return
        values = []
        for row in rows:
            kind = "终端" if row.id.startswith("terminal:") else "Worker"
            details = f"{row.id} · {STATUS_LABELS.get(row.status, row.status)} · {max(0, row.elapsed):.1f}s"
            if row.detail:
                details += " · " + clean_text(row.detail).replace("\n", " ")[:240]
            values.append((kind, details))
        self._display(render_panel(title, rows=values, color=self.color))

    def _report_job_wait(self, reason: str, rows: list[BackgroundRow]) -> None:
        if not self._cleanup_done:
            self._show_background(reason, rows)

    def _cancel_job_wait(self) -> bool:
        if not self._job_wait.cancel():
            return False
        self._emit("已退出等待，后台任务继续运行", prefix="后台")
        return True

    def _cancel_console_wait(self) -> bool:
        if (self.session.running and self.chat is not None and self.session.state is self.chat.state
                and self.view.activity()[0] == "retry"):
            self.session.request_stop()
            return True
        return self._cancel_job_wait()

    def _cmd_jobs(self, args: list[str]) -> None:
        action, *values = args or ["list"]
        if action == "unwait" and not values:
            if not self._cancel_job_wait():
                self._emit("当前没有等待", prefix="后台")
            return
        rows = self._background_rows()
        if action == "list" and not values:
            running = sum(row.status in ACTIVE for row in rows)
            self._show_background(f"后台任务 · {running} 项运行或排队", rows)
            return
        by_id = {row.id: row for row in rows}
        if action == "wait" and values and values[0] in {"any", "all"}:
            mode, *ids = values
            timeout = float(ids.pop(0)) if ids and ":" not in ids[0] else 30.0
            if any(key not in by_id for key in ids):
                raise ValueError("存在已失效或不属于当前视图的任务 ID；用 /jobs list 查看")
            selected = [by_id[key] for key in dict.fromkeys(ids)] if ids else [row for row in rows if row.status in ACTIVE]
            if selected:
                self._job_wait.start(mode, selected, timeout)
                if self._enhanced_input is not None:
                    self._enhanced_input.app.invalidate()
                elif self._job_wait.activity():
                    self._emit(f"等待 {len(selected)} 项后台任务 · /jobs unwait 退出等待", prefix="后台")
            else:
                self._emit("当前没有任务需要等待", prefix="后台")
            return
        if action in {"logs", "stop", "message"} and values:
            if (action != "message" and len(values) != 1) or (action == "message" and len(values) < 2):
                raise ValueError("使用 /help jobs 查看命令参数")
            row = by_id.get(values[0])
            if row is None:
                raise ValueError("任务不存在或不属于当前视图；用 /jobs list 查看")
            kind, task_id = row.id.split(":", 1)
            if action == "logs":
                self._show_background_log(row, kind, task_id)
            elif row.status not in ACTIVE:
                self._show_background("任务已结束", [row])
            elif kind == "worker":
                self.subworkers.task_control(task_id, "cancel" if action == "stop" else "message",
                                             " ".join(values[1:]), run_id=row.owner)
                self._emit(f"{row.id} · {'已请求停止' if action == 'stop' else '已发送补充'}", prefix="后台")
            elif action == "stop":
                ctx = self.chat.ctx
                jobs = ctx.extra["background_jobs"]
                jobs.stop(task_id)
                ctx.audit.append("external.background.stop", job_id=task_id, source="operator")
                self._show_background("停止请求", [item for item in self._background_rows() if item.key == row.key])
            else:
                raise ValueError("终端作业只支持查看日志和停止")
            return
        raise ValueError("使用 /help jobs 查看命令参数")

    def _show_background_log(self, row: BackgroundRow, kind: str, task_id: str) -> None:
        if kind == "terminal":
            jobs = self.chat.ctx.extra["background_jobs"]
            item = jobs.snapshot(task_id, tail=16_000)[0]
            sections = [(label, item[key]) for key, label in (("stdout_tail", "标准输出"), ("stderr_tail", "标准错误"))
                        if item[key]]
            event_path = self.chat.events.path
        else:
            lines, event_path = self.subworkers.monitor_log(row.owner, task_id, max(20, terminal_width() - 8))
            sections = [("近期记录", "\n".join(line for _, line in lines))] if lines else []
        self._display(render_panel(row.id, sections=sections or [("日志", "暂无输出")],
                                   footer=f"事件记录 {event_path}", color=self.color))

    def _agent_log(self, message: str) -> None:
        import re
        if message.startswith(("[用户] ", "[动作 ", "[失败分类]")) or re.match(r"^\[\d+/(?:\d+|不限)\]", message) or message.lstrip().startswith("→"):
            return  # 正文由结构化事件渲染，内部诊断不占一条消息。
        if "想法：" in message:
            self._display(render_process(message.split("想法：", 1)[1].strip(), label="思考摘要", color=self.color, palette=self._output_palette))
        else:
            self._display(render_process(message.strip(), label="过程", color=self.color, palette=self._output_palette))

    def _on_step(self, step: Any) -> None:
        self._live_step = step

    def _set_live_action_hint(self, value: str) -> None:
        """兼容控制台扩展的显式状态更新入口。"""
        self._live_action_hint = value

    def _clear_action(self) -> None:
        self._active_call = ""
        self._requested_action = None
        self._live_action_hint = ""

    def _on_runtime_event(self, event: RuntimeEvent, *, replayed: bool = False) -> None:
        from .event_view import EventTranscript
        if self._event_transcript is None:
            self._event_transcript = EventTranscript(self._display, editor=self._enhanced_input, color=self.color, palette=self._output_palette)
        if replayed:
            self._event_transcript.consume(event)
            return
        if event.kind == "tool.requested":
            step = event.payload.get("step")
            self._requested_action = (event.call_id, step if isinstance(step, int) and step > 0 else None)
        elif event.kind == "tool.started":
            tool = str(event.payload.get("tool") or "")
            request = self._requested_action
            step = request[1] if request and request[0] == event.call_id else None
            caption = _TOOL_ACTIVITY.get(tool, f"调用 {tool}" if tool else "处理工具")
            self._live_action_hint = f"第 {step} 步 · {caption}" if step is not None else caption
            self._active_call = event.call_id
            self.view.set_activity("tool", "执行中")
        elif event.kind == "tool.finished":
            if self._active_call and event.call_id == self._active_call:
                self._clear_action()
                self.view.set_activity("processing", "整理结果")
            if event.payload.get("tool") in {"terminal_exec", "workspace_git"}:
                self._branch = self._read_branch()
        elif event.kind == "model.started":
            self._clear_action()
            self.view.set_activity("waiting", "请求模型")
            if self._enhanced_input is not None:
                self._enhanced_input.app.invalidate()
        elif event.kind == "model.connected" and self.view.activity()[0] == "retry":
            self.view.set_activity("waiting", "等待输出")
        elif event.kind == "model.retry":
            attempt = event.payload.get("attempt")
            limit = event.payload.get("max_attempts")
            label = f"第 {attempt} 次"
            if limit and self.chat is not None and self.session.state is self.chat.state:
                label = f"{attempt}/{limit} · Esc 取消"
            self.view.set_activity("retry", f"等待重试 · {label}")
        elif event.kind == "model.error":
            self.view.set_activity("error", "模型请求失败")
        elif event.kind == "chat.title" and self._enhanced_input is not None:
            self._enhanced_input.app.invalidate()
        elif event.kind == "todo.updated":
            snapshot = event.payload.get("snapshot")
            if isinstance(snapshot, dict):
                self._todo_snapshot = snapshot
            if self._enhanced_input is not None:
                self._enhanced_input.app.invalidate()
        elif event.kind == "job.finished":
            job_id = str(event.payload.get("job_id") or event.call_id)
            identity = (event.stream_id, job_id)
            with self._lock:
                unseen = job_id and identity not in self._job_notices
                if unseen:
                    self._job_notices.append(identity)
            if unseen:
                self._background_notice(job_id, str(event.payload.get("status") or ""))
        self._event_transcript.consume(event)

    def _todo_view(self) -> dict[str, Any]:
        """给固定任务卡返回同一份会话状态，不从 transcript 反向推断。"""
        if self.chat is not None:
            snapshot = self.chat.todo.view()
            self._todo_snapshot = snapshot
            return snapshot
        return self._todo_snapshot

    def _cmd_tasks(self, args: list[str]) -> None:
        action = (args or ["show"])[0].casefold()
        if action not in {"show", "list", "clear"}:
            raise ValueError("用法：/tasks [show|clear]")
        if action == "clear":
            chat = self._ensure_chat()
            snapshot = chat.todo.clear()
            self._todo_snapshot = snapshot
            chat.ctx.emit("todo.updated", snapshot=snapshot, action="clear", source="operator")
            chat.save()
            self._emit("任务清单已清空", prefix="任务")
            return
        snapshot = self._todo_view()
        rows = snapshot.get("tasks", []) if isinstance(snapshot, dict) else []
        if not rows:
            self._emit("当前没有任务清单；复杂多步任务开始后会自动出现。", prefix="任务")
            return
        counts = snapshot.get("counts", {})
        summary = (f"{int(counts.get('in_progress', 0) or 0)} 进行中 · "
                   f"{int(counts.get('pending', 0) or 0)} 待处理 · "
                   f"{int(counts.get('blocked', 0) or 0)} 阻塞 · "
                   f"{int(counts.get('completed', 0) or 0)} 已完成")
        markers = {"in_progress": "◉", "pending": "○", "blocked": "!",
                   "completed": "✓", "abandoned": "–"}
        labels = {"in_progress": "进行中", "pending": "待处理", "blocked": "阻塞",
                  "completed": "已完成", "abandoned": "已取消"}
        lines = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "pending")
            phase = str(row.get("phase") or "").strip()
            prefix = f"{phase} · " if phase else ""
            note = f" — {row.get('note')}" if row.get("note") else ""
            lines.append(f"{markers.get(status, '○')} {row.get('id')}  {prefix}{row.get('text')}  [{labels.get(status, status)}]{note}")
        self._display(render_panel("任务清单", sections=[("进度", summary), ("任务", "\n".join(lines))],
                                   footer="清单随会话保存 · /tasks clear 清空", color=self.color))

    def _show_help(self, args: list[str] | None = None) -> None:
        name = (args or [""])[0].lstrip("/")
        if name:
            spec = find_command(name)
            if spec is None:
                self._emit(f"没有找到命令 /{name}；输入 /help 查看完整索引。")
                return
            self._display(render_panel(
                f"命令 /{spec.name}", sections=command_detail_sections(spec),
                footer="在输入框键入 / 可实时筛选；Tab 补全，↑↓ 选择。", color=self.color,
            ))
            return
        self._display(render_panel(
            "命令索引", sections=command_help_sections(),
            footer="输入 / 打开实时命令索引 · 继续输入可筛选 · Tab 补全 · /help <命令> 查看选项",
            color=self.color,
        ))

    # ------------------------------------------------------------------ 主循环
    def run(self) -> int:
        if self._input is None and not self._custom_output and enhanced_input_available():
            self._enhanced_input = EnhancedConsoleInput(
                status=self._prompt_status, running=lambda: self.session.running,
                dynamic_values=self._completion_values, plain=self._plain or not self.color,
                usage=self._footer_usage, activity=self._activity, motion=self.config.ui.motion,
                context=self._footer_context, activity_detail=lambda: self._live_action_hint,
                output_palette=self._output_palette, cancel_wait=self._cancel_console_wait,
                command_icons=lambda: self.config.ui.command_icons,
                tasks=self._todo_view,
                pending_draft=self._pending_draft_number,
            )
            original_output = self.output
            self.output = self._enhanced_input.write
            try:
                self._start_scheduler()
                self._enhanced_input.show_home()
                self._enhanced_input.app.pre_run_callables.append(self._start_update_check)
                self._enhanced_input.run(self.handle)
            except (EOFError, KeyboardInterrupt):
                pass
            finally:
                try:
                    self._cleanup()
                finally:
                    self.output = original_output
            return 0
        try:
            show_home(output=self._display, color=self.color)
            interrupt_exit_armed = False
            while not self.exit_requested:
                try:
                    raw = self._read_input()
                except KeyboardInterrupt:
                    if interrupt_exit_armed:
                        break
                    interrupt_exit_armed = True
                    if self.session.running:
                        self.handle("/stop")
                    self._emit("再按 Ctrl+C 退出 Trina")
                    continue
                interrupt_exit_armed = False
                if not self.handle(raw):
                    break
        except (EOFError, KeyboardInterrupt, StopIteration):
            pass
        finally:
            self._cleanup()
        return 0

    def _start_update_check(self) -> None:
        """在界面运行后查询一次正式版本。"""
        editor = self._enhanced_input
        if editor is None:
            return

        def check() -> None:
            from .updater import UpdateError, check_release

            try:
                notice = check_release()
            except UpdateError:
                return
            if notice and not self._cleanup_done:
                editor.show_update_notice(*notice)

        threading.Thread(target=check, name="trina-update-check", daemon=True).start()

    def _read_input(self) -> str:
        """测试和重定向使用普通输入；真实终端由持久界面接管。"""
        if self._input is not None:
            return self._input(self._prompt())
        return input(self._prompt())

    def _activity(self) -> tuple[str, str, float]:
        if self.session.pending:
            return "approval", "等待审批 · y 允许 / n 拒绝", 0
        if self.session.stop_event.is_set() and self.session.running:
            return "stopping", "停止中", 0
        if self.session.pause_event.is_set() and self.session.running:
            return "paused", "已暂停", 0
        if (waiting := self._job_wait.activity()) is not None:
            count, elapsed = waiting
            return "background_wait", f"等待 {count} 项后台任务 · Esc 退出等待", elapsed
        if not self.session.running:
            worker_badge = self.subworkers.badge if self.subworkers.running else ""
            if self.chat is not None:
                jobs = self.chat.ctx.extra.get("background_jobs")
                if jobs is not None:
                    count, elapsed = jobs.running_activity()
                    if count:
                        label = f"终端 {count} 项运行中"
                        if worker_badge:
                            label += f" · Worker {worker_badge}"
                        return "background", label, elapsed
            if worker_badge:
                return "background", f"后台 Worker · {worker_badge}", 0
            return "idle", "就绪", 0
        return self.view.activity()

    def _footer_context(self) -> ConsoleContext:
        """直接读取当前任务配置，模型切换、恢复和新会话在下一帧生效。"""
        llm = (self._task_config or self.config).llm
        status = "就绪"
        if self.session.running:
            status = "待审批 y/n" if self.session.pending else (
                "暂停" if self.session.pause_event.is_set() else (
                    "停止中" if self.session.stop_event.is_set() else "运行中"
                )
            )
        pending = self.session.steers.qsize()
        queued = f" · 补充 {pending} 条待处理" if pending else ""
        if self._drafts:
            queued += f" · 草稿 {len(self._drafts)}"
        state = self.session.state or self._last_state
        is_chat = self.chat is not None and state is self.chat.state
        context_tokens = self.view.context_tokens
        progress = ""
        if is_chat:
            estimated = self.chat.context.stats.get("estimated_input_tokens")
            if isinstance(estimated, int):
                context_tokens = estimated
        else:
            stats = getattr(state, "context_stats", {}) if state is not None else {}
            estimated = stats.get("estimated_input_tokens") if isinstance(stats, dict) else None
            if isinstance(estimated, int):
                context_tokens = estimated
            step = int(getattr(self._live_step, "index", 0) or
                       (getattr(state, "counters", {}) or {}).get("steps", 0))
            if self._task_max_steps:
                progress = f"步骤 {step}/{self._task_max_steps}"
        from .worker_roles import WORKER_ROLES
        ui = self.config.ui
        role = WORKER_ROLES[self._active_worker][0] if is_chat or not self.session.running else "Task Agent"
        duration_seconds = None
        if is_chat:
            if self.session.running:
                duration_seconds = max(0.0, time.monotonic() - self.session.started_at)
            else:
                duration_seconds = self._last_turn_seconds
        elif state is not None:
            try:
                started = float(getattr(state, "started_at", 0) or 0)
                duration_seconds = max(0.0, float(state.duration)) if started else None
            except (TypeError, ValueError):
                duration_seconds = None
        elif self.session.running:
            duration_seconds = max(0.0, time.monotonic() - self.session.started_at)
        title = self.chat.title if is_chat and self.chat.title != "新对话" else self._task_title
        return ConsoleContext(status=status + queued, model=_display_model(llm.model, llm.reasoning_effort),
                              directory=str(Path.cwd()), task=title or self._task_title or "新对话",
                              progress=progress, context_tokens=context_tokens,
                              context_window=llm.effective_context_window, duration_seconds=duration_seconds,
                              role=role, branch=self._branch,
                              preset=ui.status_line_preset, separator=ui.status_line_separator, context_bar=ui.context_bar,
                              metrics=self.view.metrics(), workers=self.subworkers.badge)

    def _footer_usage(self, width: int, compact: bool = False) -> str:
        return self.view.footer(width, compact, preset=self.config.ui.status_line_preset,
                                separator=self.config.ui.status_line_separator)

    @staticmethod
    def _read_branch() -> str:
        """只读当前项目 Git HEAD，渲染帧不启动子进程；兼容 linked worktree。"""
        directory = Path.cwd()
        for parent in (directory, *directory.parents):
            git = parent / ".git"
            try:
                if git.is_file():
                    git = (parent / git.read_text(encoding="utf-8").strip().removeprefix("gitdir: ")).resolve()
                if git.is_dir():
                    head = (git / "HEAD").read_text(encoding="utf-8").strip()
                    return head.removeprefix("ref: refs/heads/") if head.startswith("ref: ") else head[:8]
            except OSError:
                return ""
        return ""

    def _prompt_status(self) -> str:
        context = self._footer_context()
        return f"{context.status} · {context.model} · 目录 {context.directory} · 任务 {context.task}"

    def _prompt(self) -> str:
        short = clean_text(_display_model(self.config.llm.model, self.config.llm.reasoning_effort) or "?").replace("\n", " ")[:28]
        status = self._prompt_status().split(" · ", 1)[0]
        # 状态在下一次读取输入时刷新；后台进展单独输出，不清空输入缓冲。
        return f"trina · {status} · {short} > " if terminal_width() >= 60 else f"trina · {status} > "

    def _completion_values(self, command: str, completed_args: list[str]) -> list[ChoiceSpec]:
        if command == "setup" and len(completed_args) == 1:
            from .ui_preferences import PREFERENCES
            preference = PREFERENCES.get(completed_args[0])
            return [ChoiceSpec(value, label) for value, label in preference.choices] if preference else []
        if command == "jobs" and completed_args:
            action = completed_args[0]
            if completed_args == ["wait"]:
                return [ChoiceSpec("any", "任意一项结束"), ChoiceSpec("all", "所有选中任务结束")]
            if action in {"logs", "stop", "message", "wait"} and (action == "wait" or len(completed_args) == 1):
                return [ChoiceSpec(row.id, STATUS_LABELS.get(row.status, row.status))
                        for row in self._background_rows()
                        if row.id not in completed_args and (action != "message" or row.id.startswith("worker:"))]
        if command == "plan" and completed_args == ["choose"] and self._plan_review:
            return [ChoiceSpec(str(index), option) for index, option in enumerate(self._plan_review.options, 1)]
        if command == "queue" and completed_args in (["send"], ["drop"]):
            return [ChoiceSpec(str(number), f"{task or '主会话'} · {text.splitlines()[0][:50]}")
                    for number, (text, task, _) in self._drafts.items()]
        """补全运行时才能知道的历史会话、模型、工具、靶场和产物路径。"""
        if command == "branch" and completed_args in ([], ["switch"], ["delete"]):
            return [ChoiceSpec(name, "本地分支") for name in self._git_branches]
        if command == "repo" and completed_args in (["stage"], ["unstage"], ["diff"]):
            return [ChoiceSpec(name, "最近状态中的文件") for name in self._git_files]
        if command == "subworker":
            if completed_args == ["inspect"]:
                return [ChoiceSpec(item["id"], item["goal"]) for item in self.subworkers.rows()]
            if completed_args == ["settings"]:
                return [ChoiceSpec(key, label) for key, label in (("workers", "并发数"), ("max_steps", "每任务步数"), ("max_seconds", "每任务秒数"))]
            if completed_args == ["thinking"]:
                from .llm.reasoning import reasoning_support
                from .swarm import task_llm
                support = reasoning_support(task_llm(self.config))
                return [ChoiceSpec(level, support.label(level)) for level in support.levels]
            if completed_args == ["provider"]:
                from .providers import ProfileStore
                return [ChoiceSpec(profile.id, profile.model) for profile in ProfileStore().profiles.values()]
        if command == "resume" and not completed_args:
            from .chat_store import ChatStore
            try:
                return [ChoiceSpec(row["id"], row["title"]) for row in ChatStore(self.config.workspace_dir).list()[:40]]
            except Exception:
                return []
        from pathlib import Path

        if command == "model" and not completed_args:
            try:
                from .providers import PRESETS, ProfileStore

                store = ProfileStore()
                return ([ChoiceSpec(p.id, f"已保存 · {p.model}") for p in store.profiles.values()]
                        + [ChoiceSpec(p.id, f"{p.name} · 添加连接") for p in PRESETS])
            except Exception:  # noqa: BLE001 - 补全失败不能影响输入
                return []
        if command == "model" and completed_args in (["use"], ["edit"], ["remove"], ["test"], ["keys"]):
            from .providers import ProfileStore
            return [ChoiceSpec(p.id, p.model) for p in ProfileStore().profiles.values()]
        if command == "model" and completed_args == ["add"]:
            from .providers import PRESETS
            return [ChoiceSpec(p.id, p.name) for p in PRESETS]
        if command == "model" and completed_args == ["context"]:
            return [ChoiceSpec("auto", "按当前模型自动选择"), ChoiceSpec("500K", "500,000 Token"),
                    ChoiceSpec("1M", "1,000,000 Token")]
        if command == "tool" and not completed_args:
            try:
                registry = self.registry
                if registry is None:
                    from .tools import build_registry

                    registry = build_registry()
                return [ChoiceSpec(item["name"], item["summary"]) for item in registry.describe()]
            except Exception:  # noqa: BLE001
                return []
        range_names = []
        try:
            from .adapters import available_range_names
            range_names = available_range_names(self.config)
        except OSError:
            pass
        if command in ("play",) and completed_args[-1:] in (["--range"], ["-r"]):
            return [ChoiceSpec(name, "已登记靶场") for name in range_names]
        if command == "ranges" and completed_args == ["status"]:
            return [ChoiceSpec(name, "已登记靶场") for name in range_names]

        state_flags = {"--state"}
        if command in ("report", "review") and completed_args[-1:] and completed_args[-1] in state_flags:
            paths = sorted(self.config.workspace_dir.glob("*/state.json"), key=lambda item: item.stat().st_mtime, reverse=True)
            return [ChoiceSpec(str(path), "运行状态") for path in paths[:12]]
        if command == "audit" and completed_args and completed_args[0] in ("verify", "show", "tail"):
            if len(completed_args) == 1:
                paths = sorted(self.config.audit_dir.glob("*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True)
                return [ChoiceSpec(str(path), "审计日志") for path in paths[:12]]
        return []

    # ------------------------------------------------------------------ 命令
    def handle(self, raw: str) -> bool:
        line = (raw or "").strip()
        armed, self._enter_draft = self._enter_draft, None
        if not line:
            if armed is not None:
                number, owner = armed
                if number in self._drafts:
                    if self.session.running and self.session.thread is owner:
                        self._cmd_queue(["send", str(number)], running_only=True)
                    else:
                        self._emit(f"原任务已结束，草稿 #{number} 仍可用 /queue send {number} 发送", prefix="队列")
                    return True
            self._show_status()
            return True
        if line.lstrip("/").strip() in ("quit", "exit", "q"):
            if self.session.running:
                self._emit("任务仍在运行：先 stop，或再次输入 quit 强制退出。")
                if not getattr(self, "_quit_warned", False):
                    self._quit_warned = True
                    return True
            return False
        self._quit_warned = False

        # 以 / 开头的一律当命令；否则先按命令名匹配，匹配不到就交给对话
        forced = line.startswith("/")
        if forced:
            line = line[1:].strip()
        if not line:
            self._show_help()
            return True
        raw_parts = line.split(maxsplit=2)
        if raw_parts[:2] == ["queue", "add"] and len(raw_parts) == 3:
            self._enqueue_draft(raw_parts[2])
            return True
        if self.session.running and line.startswith("!"):
            self._send_live_note(line[1:].strip())
            return True
        try:
            argv = shlex.split(line)
        except ValueError:
            if not forced and self.session.running:
                self._enqueue_draft(line, on_enter=True)
                return True
            if forced:
                self._emit("命令中的引号未闭合；补齐引号后重试，或输入 /help 查看示例。", prefix="错误")
                return True
            self._emit(f"命令解析失败，按对话处理: {line[:60]}")
            self._handle_chat_input(line)
            return True
        spec = find_command(argv[0])
        command, args = (spec.name if spec is not None else argv[0]), argv[1:]

        if command in {"subworker", "queue", "plan", "jobs", "resume", "tasks", "todo"}:
            try:
                {"subworker": self._cmd_subworker, "queue": self._cmd_queue,
                 "plan": self._cmd_plan, "jobs": self._cmd_jobs, "resume": self._cmd_resume,
                 "tasks": self._cmd_tasks, "todo": self._cmd_tasks}[command](args)
            except (KeyboardInterrupt, EOFError):
                self._emit("已返回主会话")
            except Exception as exc:
                self._emit(str(exc), prefix="错误")
            return True

        if self.session.running:
            return self._handle_running(command, args, line, forced=forced)

        if command in ("quit", "exit", "q"):
            self._emit("\n再见。")
            self.running = False
            self.exit_requested = True
            return True

        handler = {
            "help": lambda: self._show_help(args),
            "?": lambda: self._show_help(args),
            "play": lambda: self._cmd_play(args),
            "run": lambda: self._cmd_run(args),
            "status": self._show_status,
            "approve": lambda: self._cmd_approve(args),
            "hint": lambda: self._emit("当前没有运行中的任务"),
            "pause": lambda: self._emit("当前没有运行中的任务"),
            "unpause": lambda: self._emit("当前没有运行中的任务"),
            "stop": lambda: self._emit("当前没有运行中的任务"),
            "revoke": lambda: self._cmd_revoke(args, True),
            "grant": lambda: self._cmd_revoke(args, False),
            "y": lambda: self._emit("当前没有待审批的动作"),
            "n": lambda: self._emit("当前没有待审批的动作"),
            "tools": lambda: self._cmd_tools(args),
            "tool": lambda: self._cmd_tool(args),
            "guard": lambda: self._delegate(["guard", *args]),
            "ranges": lambda: self._delegate(["ranges", *args]),
            "report": lambda: self._delegate(["report", *args]),
            "review": lambda: self._delegate(["review", *args]),
            "audit": lambda: self._delegate(["audit", *args]),
            "lab": lambda: self._delegate(["lab", *args]),
            "model": lambda: self._cmd_model(args),
            "setup": lambda: self._cmd_setup(args),
            "worker": lambda: self._cmd_worker(args),
            "branch": lambda: self._cmd_branch(args),
            "repo": lambda: self._cmd_repo(args),
            "worktree": lambda: self._cmd_worktree(args),
            "wt": lambda: self._cmd_worktree(args),
            "theme": lambda: self._cmd_theme(args),
            "colors": lambda: self._cmd_theme(args),
            "notify": lambda: self._cmd_notify(args),
            "footer": lambda: self._cmd_footer(args),
            "history": lambda: self._cmd_history(args),
            "new": self._cmd_new,
            "clear": self._cmd_new,
            "usage": self._cmd_usage,
            "context": self._cmd_context,
            "compact": self._cmd_compact,
            "save": self._cmd_save,
            "goal": lambda: self._cmd_goal(args),
            "schedule": lambda: self._cmd_schedule(args),
            "scheduled": lambda: self._cmd_schedule(args),
            "schedualed": lambda: self._cmd_schedule(args),
            "board": self._cmd_board,
            "hypotheses": self._cmd_hypotheses,
            "graph": self._cmd_graph,
        }.get(command)
        if handler is None:
            if forced:
                matches = difflib.get_close_matches(command, sorted(COMMAND_NAMES), n=1, cutoff=0.6)
                suggestion = f"；是否想输入 /{matches[0]}" if matches else ""
                self._emit(f"未知命令 {command!r}{suggestion}。输入 /help 查看命令。")
                return True
            self._handle_chat_input(line)
            return True
        try:
            handler()
        except (KeyboardInterrupt, EOFError):
            self._emit("已取消当前命令")
        except Exception as exc:  # noqa: BLE001 - 控制台不能因为单条命令崩掉
            self._emit(f"命令执行失败: {type(exc).__name__}: {exc}")
        return True

    def _handle_running(self, command: str, args: list[str], line: str, *, forced: bool = False) -> bool:
        if command == "goal":
            self._cmd_goal(args)
            return True
        if command in ("schedule", "scheduled", "schedualed"):
            self._cmd_schedule(args)
            return True
        if command in ("y", "yes", "approve") and (command != "approve" or not args or forced):
            if not self.session.answer(True):
                self._emit("当前没有待审批的动作")
            return True
        if command in ("n", "no", "deny"):
            if not self.session.answer(False):
                self._emit("当前没有待审批的动作")
            return True
        if command == "hint":
            self._send_live_note(" ".join(args))
            return True
        if command == "pause":
            self.session.toggle_pause()
            return True
        if command == "unpause":
            if self.session.pause_event.is_set():
                self.session.toggle_pause()
            else:
                self._emit("任务未处于暂停状态")
            return True
        if command == "stop":
            self.session.request_stop()
            if self.chat is not None and self.session.state is self.chat.state:
                self.chat.ctx.extra["background_jobs"].cancel_running()
            return True
        if command == "status":
            self._show_status()
            return True
        if command == "board":
            self._cmd_board()
            return True
        if command == "context":
            self._cmd_context()
            return True
        if command == "compact":
            self._cmd_compact()
            return True
        if command == "quit":
            self._emit("任务运行中：先 stop 再退出。")
            return True
        if command in ("help", "?"):
            self._show_help(args)
            return True
        if not forced and command not in COMMAND_NAMES:
            self._enqueue_draft(line, on_enter=True)
            return True
        self._emit("任务运行中 · /queue 查看草稿 · /hint 立即补充 · /status 查看 · /stop 停止")
        return True

    def _handle_chat_input(self, text: str) -> None:
        if self._plan_review and self._plan_review.question:
            try:
                self._cmd_plan(["reply", text])
            except Exception as exc:
                self._emit(str(exc), prefix="错误")
        else:
            self._start_chat_turn(text)

    def _pending_draft_number(self) -> int | None:
        armed = self._enter_draft
        if armed is not None:
            number, owner = armed
            if self.session.running and self.session.thread is owner and number in self._drafts:
                return number
        return None

    def _enqueue_draft(self, text: str, *, task_id: str = "", on_enter: bool = False) -> None:
        if not text.strip():
            return
        runtime = self.subworkers.runtime if task_id else None
        self._draft_number += 1
        self._drafts[self._draft_number] = (text, task_id, runtime.run_id if runtime else "")
        if on_enter:
            self._enter_draft = self._draft_number, self.session.thread
        hint = "再按一次 Enter 立即发送" if on_enter else f"/queue send {self._draft_number} 发送"
        self._emit(f"草稿 #{self._draft_number} 已排队 · {hint}", prefix="队列")

    def _send_live_note(self, text: str) -> bool:
        if self._planning:
            self._emit("正在生成澄清问题，请在结果出现后选择或补充。")
            return False
        if not self.session.running or self.session.stop_event.is_set():
            self._emit("当前没有可接收实时补充的任务")
            return False
        self.session.steer(text)
        return bool(text.strip())

    def _cmd_queue(self, args: list[str], *, running_only: bool = False) -> None:
        action = args[0] if args else "list"
        if action == "list":
            rows = [(f"#{number} · {task or '主会话'}", text) for number, (text, task, _) in self._drafts.items()]
            self._display(render_panel("补充队列", rows=rows or [("队列", "暂无草稿")],
                                       footer="/queue add <文本> · /queue send [编号] · /queue drop <编号>", color=self.color))
            return
        if action not in {"send", "drop"} or len(args) > 2 or (action == "drop" and len(args) != 2):
            raise ValueError("/queue list | add <文本> | send [编号] | drop <编号>")
        number = int(args[1].lstrip("#")) if len(args) == 2 else next(iter(self._drafts), 0)
        if number not in self._drafts:
            raise ValueError("未找到草稿")
        if action == "send":
            text, task_id, run_id = self._drafts[number]
            if task_id:
                runtime = self.subworkers.runtime
                if runtime is None or runtime.run_id != run_id:
                    raise ValueError("原 Worker 已结束，可从 /queue 查看并复制草稿")
                self.subworkers.task_control(task_id, "message", text)
            elif running_only or self.session.running:
                if not self._send_live_note(text):
                    return
            else:
                if self._plan_review and self._plan_review.question:
                    raise ValueError("请先完成或取消当前计划澄清")
                with self._launch_lock:
                    if not self._start_chat_turn(text):
                        return
        del self._drafts[number]
        self._emit(f"草稿 #{number} 已{'发送' if action == 'send' else '移除'}", prefix="队列")

    def _show_plan_review(self) -> None:
        review = self._plan_review
        if self._planning:
            self._emit("正在澄清任务", prefix="计划")
        elif review and review.question:
            self._display(render_panel("请选择", sections=[(review.question, "\n".join(
                f"{index}. {option}" for index, option in enumerate(review.options, 1)
            ))], footer="/plan choose [编号] · /plan reply <其他答案> · /plan cancel", color=self.color))
        elif review and review.layers:
            self._display(render_panel("分工预览", sections=review.split_sections(),
                                       footer="/plan show 查看 · /plan cancel 关闭", color=self.color))
        elif review and review.criteria:
            self._display(render_panel("计划核对", sections=[("", review.draft())],
                                       footer="/plan queue 加入待发草稿 · /plan cancel 关闭", color=self.color))
        else:
            self._emit("/plan <任务> 澄清 · /plan split <任务> 预览工程分工", prefix="计划")

    def _cmd_plan(self, args: list[str]) -> None:
        action = args[0] if args else "show"
        if action == "show":
            self._show_plan_review()
            return
        if action == "cancel":
            self._plan_review = None
            if self._planning:
                self.session.request_stop()
            self._emit("已取消计划澄清", prefix="计划")
            return
        if self.session.running:
            raise ValueError("当前会话正在运行")
        review = self._plan_review
        if action == "queue":
            if review is not None and review.split_preview:
                raise ValueError("分工预览仅供查看")
            if review is None or not review.criteria:
                raise ValueError("请先完成计划澄清")
            self._enqueue_draft(review.draft())
            self._plan_review = None
            return
        if action in {"choose", "reply"}:
            if review is None or not review.question:
                raise ValueError("当前没有待回答的澄清问题")
            if action == "choose":
                if len(args) == 1 and self._enhanced_input is not None:
                    from .console_ui import select_option
                    selected = select_option(review.question, [
                        ChoiceSpec(str(index), option) for index, option in enumerate(review.options, 1)
                    ])
                    if selected is None:
                        return
                    args = [action, selected]
                if len(args) != 2 or not args[1].isascii() or not args[1].isdigit():
                    raise ValueError("/plan choose <编号>")
                number = int(args[1])
                if not 1 <= number <= len(review.options):
                    raise ValueError("选项编号超出范围")
                answer = review.options[number - 1]
            else:
                answer = " ".join(args[1:]).strip()
                if not answer:
                    raise ValueError("/plan reply <答案>")
            draft = review.followup(answer)
        elif action == "split":
            task = " ".join(args[1:]).strip()
            if not task:
                raise ValueError("用法 /plan split <工程任务>")
            draft = PlanReview(task, split_preview=True)
        else:
            draft = PlanReview(" ".join(args))
        with self._launch_lock:
            if self.session.running:
                raise ValueError("当前会话正在运行")
            self.session.reset_for_run()
            self.session.state = None
            self.session.started_at = time.monotonic()
            backend = self._backend()
            if action == "split":
                self._plan_review = draft
            self._planning = True

            def review_task() -> None:
                try:
                    result = draft.request(backend)
                    if not self.session.stop_event.is_set():
                        self._plan_review = result
                except Exception as exc:
                    if not self.session.stop_event.is_set():
                        self._emit(f"澄清出错: {exc}", prefix="计划")
                finally:
                    self._planning = False
                    self.view.set_activity("idle", "就绪")
                if not self.session.stop_event.is_set():
                    self._show_plan_review()

            self.session.thread = threading.Thread(target=review_task, name="trina-plan-review", daemon=True)
            self.session.thread.start()

    # ------------------------------------------------------------------ 工作台视图
    def _current_state(self) -> Any:
        """当前可展示的状态：运行中的 → 本会话最近一轮 → 磁盘上最近一次运行。"""
        if self.session.state is not None:
            return self.session.state
        if self._last_state is not None:
            return self._last_state
        if self.chat is not None and self.chat.state is not None:
            return self.chat.state
        # 会话里还没有状态：读磁盘上最近一次运行，让 /board /graph 开箱可用
        try:
            from .board import latest_state_file, load_run_state

            config = getattr(self, "config", None)
            workspace = getattr(config, "workspace_dir", None) or "runs"
            path = latest_state_file(workspace)
            if path is not None:
                state = load_run_state(path)
                if state is not None:
                    self.output(f"（已载入最近一次运行 {getattr(state, 'run_id', '') or path.parent.name}；用 /play 开始新一轮）")
                    return state
        except Exception:  # noqa: BLE001 - 载入失败不影响后续命令
            return None
        return None

    def _cmd_board(self) -> None:
        from .board import render_board

        if self.session.running and self.session.state is None:
            self._show_status()
            return
        state = self._current_state()
        if state is None:
            self._display(render_panel("工作台", sections=[("尚无运行记录", "直接输入问题开始对话，或使用 /play 启动任务。")], color=self.color))
            return
        hypotheses = self.chat.ledger.to_dict() if self.chat is not None and state is self.chat.state else None
        self._display(render_board(state, hypotheses=hypotheses, color=self.color))

    def _cmd_hypotheses(self) -> None:
        if self.chat is not None:
            ledger = self.chat.ledger
            items = ledger.to_dict()
            summary = ledger.summary()
        else:
            state = self._current_state()
            items = (getattr(state, "hypotheses", None) or []) if state is not None else []
            summary = {
                "total": len(items),
                "open": len([h for h in items if h.get("status") in ("open", "verifying")]),
                "confirmed": len([h for h in items if h.get("status") == "confirmed"]),
                "refuted": len([h for h in items if h.get("status") == "refuted"]),
                "blocked": len([h for h in items if h.get("status") == "blocked"]),
            }
        if not items:
            self._emit("假设台账为空（模型可以在动作里带 hypothesis 字段提出假设，我会按优先级排序）")
            return
        self._emit(
            f"假设台账：共 {summary['total']} 条（待验证 {summary['open']}、已确认 {summary['confirmed']}、"
            f"已否定 {summary['refuted']}、被阻断 {summary['blocked']}）"
        )
        ordered = sorted(items, key=lambda h: h.get("priority", 0), reverse=True)
        for item in ordered[:15]:
            self.output(
                f"    {item.get('id')} [{item.get('status_cn', item.get('status'))}] "
                f"优先级={item.get('priority')} P={item.get('confidence')} — {str(item.get('statement', ''))[:70]}"
            )

    def _cmd_graph(self) -> None:
        """证据关系图（mermaid）：结论 → 证据 → 检查项/假设 可反向追溯。"""
        state = self._current_state()
        if state is None:
            self._emit("还没有可绘制的证据：先 /play 跑一轮，或说句话让我开工")
            return
        from .evidence_graph import build as build_graph

        registry = self._controller.registry if self._controller is not None else None
        try:
            graph = build_graph(state, registry=registry, hypotheses=getattr(state, "hypotheses", None))
        except Exception as exc:  # noqa: BLE001
            self._emit(f"绘图失败：{type(exc).__name__}: {exc}")
            return
        self._emit(graph.summary())
        self.output(graph.to_mermaid())

    # ------------------------------------------------------------------ 会话/模型
    def _backend(self) -> Any:
        """按当前（会话级）配置取后端；切换模型/后端后会被重建。"""
        if self._backend_obj is None:
            from .llm import make_backend

            self._backend_obj = self._backend_factory(self.config.llm) if self._backend_factory else make_backend(self.config.llm)
        if self._tracked_backend is None or self._tracked_backend.backend is not self._backend_obj:
            self._tracked_backend = TrackedBackend(self._backend_obj, self.view)
        return self._tracked_backend

    def _reload_backend(self) -> None:
        self._backend_obj = self._tracked_backend = None
        if self.chat is not None:
            self.chat.backend = self._backend()
            self.chat.ctx.extra["llm_backend"] = self.chat.backend

    def _cmd_model(self, args: list[str]) -> None:
        llm = self.config.llm
        if not args:
            if self._input is not None:
                self._model_manager().run()
            elif enhanced_input_available():
                manager = self._model_manager()
                choice = manager.pick_quick(plain=self._plain or not self.color)
                if choice == "settings":
                    manager.run()
                elif choice is not None:
                    if not self._launch_lock.acquire(blocking=False):
                        raise ValueError("任务正在启动，空闲时可切换模型")
                    try:
                        if self.session.running:
                            raise ValueError("任务运行中，空闲时可切换模型")
                        changed = manager.switch_model(choice)
                    finally:
                        self._launch_lock.release()
                    if changed:
                        self._emit(f"已切换模型 · {_display_model(llm.model, llm.reasoning_effort)} · {llm.profile or '本会话'}")
            else:
                self._emit(f"当前 {llm.profile or llm.backend} / {_display_model(llm.model, llm.reasoning_effort)} · /model settings 管理连接 · /model list 查看预设")
            return
        if args == ["settings"]:
            self._model_manager().run()
            return
        if args[0] == "context":
            if len(args) != 2:
                self._emit(f"当前上下文 {llm.effective_context_window:,} · /model context auto|500K|1M|Token数")
            else:
                self._model_manager().set_context_window(args[1])
                self._emit(f"上下文窗口已设为 {llm.effective_context_window:,}")
            return
        if args[0] in ("add", "edit", "remove", "test", "keys", "use"):
            manager = self._model_manager()
            if args[0] == "add":
                manager.add(args[1] if len(args) > 1 else "")
            elif args[0] in ("keys", "edit"):
                manager.edit(args[1] if len(args) > 1 else "")
            elif args[0] == "remove":
                manager.remove(args[1] if len(args) > 1 else "")
            elif args[0] == "test":
                if len(args) > 1 and args[1] not in manager.store.profiles:
                    raise ValueError("供应商不存在")
                item = manager.store.profiles[args[1]] if len(args) > 1 else manager.current()
                self._emit(manager.test_connection(item))
            elif len(args) > 1:
                manager.use(args[1])
            else:
                manager.run()
            return
        from .providers import PRESETS, PRESET_BY_ID, ProfileStore
        store = ProfileStore()
        if args[0] in store.profiles:
            self._model_manager().use(args[0])
            return
        if args[0] in PRESET_BY_ID:
            self._model_manager().add(args[0])
            return
        if args[0] in ("list", "ls", "?"):
            self._emit(f"当前后端 {llm.backend} · 模型 {_display_model(llm.model, llm.reasoning_effort)}" + (f" · base_url {llm.base_url}" if llm.backend == "openai" else ""))
            import asyncio
            manager = self._model_manager()

            async def catalog():
                async for rows, notice in manager.discover():
                    pass
                return rows, notice

            rows, notice = asyncio.run(catalog())
            self._emit(f"模型目录（{len(rows)} 项，可用于聊天 {sum(row.meta.kind == 'chat' for row in rows)} 项）")
            for row in rows:
                self.output(f"    {'●' if row.current else ' '} {row.connection_label}/{row.model} · {row.meta.kind} · {row.source}")
            if notice:
                self._emit(notice, prefix="提示")
            return
        spec = args[0]
        from .models import normalize_spec

        provider = (store.profiles[llm.profile].provider if llm.profile in store.profiles else
                    next((p.id for p in PRESETS if p.base_url == llm.base_url), ""))
        llm.model = normalize_spec(spec, llm.backend, provider=provider)
        from .llm.reasoning import reasoning_support
        if llm.reasoning_effort not in reasoning_support(llm).levels:
            llm.reasoning_effort = "default"
        if llm.profile:
            profile = store.profiles[llm.profile]
            profile.model = llm.model
            profile.reasoning_effort = llm.reasoning_effort
            store.save()
        self._reload_backend()
        self._emit(f"已切换模型{'并保存' if llm.profile else '（本会话）'}：{_display_model(llm.model, llm.reasoning_effort)}（后端 {llm.backend}）"
                   + ("" if llm.profile else " · /save 持久化"))

    def _model_manager(self):
        from .model_ui import ModelManager
        from .console_ui import select_option

        def ask(label, default="", *, secret=False):
            if self._input is not None:
                return self._input(label + " › ").strip() or default
            if secret and not enhanced_input_available():
                raise ValueError("密钥录入需要交互终端；也可通过供应商环境变量配置")
            return ModelManager._ask(label, default, secret=secret)

        def choose(title, choices, *, subtitle=""):
            if self._input is None and enhanced_input_available():
                return select_option(title, choices, subtitle=subtitle)
            self._emit(title + (" · " + subtitle if subtitle else ""))
            for i, item in enumerate(choices, 1):
                self.output(f"  {i:02d} {item.description or item.value}")
            value = ask("输入编号或名称，留空返回")
            if value.isdigit() and 1 <= int(value) <= len(choices):
                return choices[int(value) - 1].value
            return next((c.value for c in choices if c.value == value), None)

        manager = ModelManager(self.config, emit=self._emit, reload=self._reload_backend, ask=ask, choose=choose,
                               catalog=self._model_catalog, recent_models=self._recent_models)
        self._model_catalog = manager.catalog
        return manager

    def _cmd_history(self, args: list[str]) -> None:
        if self.chat is None or not self.chat.history:
            self._emit("还没有对话历史（先说句话，或 /play 跑一轮任务）")
            return
        limit = 8
        if args:
            try:
                limit = max(1, int(args[0]))
            except ValueError:
                pass
        items = self.chat.history[-limit:]
        self._emit(f"最近 {len(items)} 条对话（共 {len(self.chat.history)} 条）：")
        for item in items:
            role = {"user": "你", "assistant": "Trina"}.get(item.get("role", ""), item.get("role", "?"))
            text = str(item.get("content", "")).replace("\n", " ")
            if text.startswith("[工具结果]"):
                role = "工具"
                text = text[len("[工具结果]"):].strip()
            self.output(f"    {role}: {text[:220]}")

    def _cmd_new(self) -> None:
        if self.session.running:
            self._emit("有任务在跑：先 /stop 再开新会话")
            return
        self._job_wait.cancel()
        if self.chat is not None:
            self.chat.close()
        self.chat = None
        self._todo_snapshot = {}
        self._task_title = ""
        self.session.state = self._last_state = None
        self.view.reset()
        self._worker_sessions.pop(self._active_worker, None)
        self._emit("已开启新对话会话；上一次对话已保存，可用 /resume 切换。")

    def _cmd_setup(self, args: list[str]) -> None:
        """统一设置入口，个人显示偏好保存到本机。"""
        from .config import _user_data_root
        from .console_ui import SETUP_SECTIONS, select_option
        from .ui_preferences import PREFERENCES, save_preference

        interactive = self._input is None and not self._custom_output and enhanced_input_available()

        def current(name: str) -> str:
            preference = PREFERENCES[name]
            section = getattr(self.config, preference.section)
            value = preference.encode(getattr(section, preference.field))
            return dict(preference.choices).get(value, f"{value} 次")

        def edit(section: str, value: str | None = None) -> None:
            if section == "model" and value is None:
                if not self._launch_lock.acquire(blocking=False):
                    raise ValueError("任务正在启动，空闲时可修改模型")
                try:
                    if self.session.running:
                        raise ValueError("任务运行中，空闲时可修改模型")
                    self._cmd_model(["settings"])
                finally:
                    self._launch_lock.release()
                return
            if section not in PREFERENCES:
                raise ValueError("用法 /setup [项目 [值]]")
            preference = PREFERENCES[section]
            settings = getattr(self.config, preference.section)
            if value is None and not interactive:
                choices = " / ".join(key for key, _ in preference.choices)
                self._emit(f"{preference.label} · {current(section)} · /setup {section} {choices}", prefix="设置")
                return
            if value is None:
                active = preference.encode(getattr(settings, preference.field))
                value = select_option(preference.label,
                                      [ChoiceSpec(key, ("● " if key == active else "") + label)
                                       for key, label in preference.choices], plain=self._plain or not self.color)
            if value is None:
                return
            setting = preference.decode(value)
            save_preference(_user_data_root(), section, value)
            setattr(settings, preference.field, setting)
            if section == "theme":
                self._refresh_output_palette()
            self._emit(f"{preference.label} · {current(section)} · 已保存", prefix="设置")

        if args:
            if len(args) > 2:
                raise ValueError("用法 /setup [项目 [值]]")
            edit(*args)
            return
        if not interactive:
            self._display(render_panel("设置", rows=[(item.description, f"/setup {item.value}") for item in SETUP_SECTIONS],
                                       color=self.color))
            return
        while True:
            options = [ChoiceSpec(item.value, item.description if item.value == "model"
                                  else f"{item.description}  ·  {current(item.value)}") for item in SETUP_SECTIONS]
            selected = select_option("设置", options, plain=self._plain or not self.color)
            if selected is None:
                return
            edit(selected)

    def _cmd_footer(self, args: list[str]) -> None:
        if args:
            if len(args) != 1 or args[0] not in {"default", "compact", "full"}:
                raise ValueError("用法：/footer default|compact|full")
            self.config.ui.status_line_preset = args[0]
        self._emit(f"底栏 {self.config.ui.status_line_preset} · 持久配置：configs/trina.yaml 的 ui 段")

    def _cmd_worker(self, args: list[str]) -> None:
        from .worker_roles import WORKER_ROLES
        from .console_ui import select_option
        choices = [ChoiceSpec(key, f"{label} · {'当前' if key == self._active_worker else '独立上下文'}")
                   for key, (label, _) in WORKER_ROLES.items()]
        if not args:
            if self._input is None and not self._custom_output and enhanced_input_available():
                selected = select_option("切换 Worker", choices, subtitle="每个角色保留独立会话 · Esc 返回")
                if selected is None:
                    return
                args = [selected]
            else:
                self._display(render_panel("Worker 会话", rows=[(item.value, item.description) for item in choices], color=self.color))
                return
        if len(args) != 1 or args[0] not in WORKER_ROLES:
            raise ValueError("用法：/worker " + "|".join(WORKER_ROLES))
        selected = args[0]
        if selected == self._active_worker:
            self._emit(f"当前角色：{WORKER_ROLES[selected][0]}")
            return
        if self.chat is not None and self.chat.history:
            self._worker_sessions[self._active_worker] = self.chat.run_id
        target = self._worker_sessions.get(selected)
        if target:
            self._cmd_resume([target])
            return
        self._job_wait.cancel()
        if self.chat is not None:
            self.chat.close()
        self.chat = None
        self._todo_snapshot = {}
        self._active_worker = selected
        self._task_config = None
        self._task_title = ""
        self.session.reset_for_run()
        self.session.state = self._last_state = None
        self._live_step = None
        self.view.reset()
        self._tracked_backend = None
        self._event_transcript = None
        if self._enhanced_input is not None:
            self._enhanced_input.clear_transcript()
        self._emit(f"已切换到 {WORKER_ROLES[selected][0]} · 独立会话")

    def _cmd_subworker(self, args: list[str]) -> None:
        """交互面板和命令共用现有 Swarm，不触碰主会话或供应商的已保存配置。"""
        from .swarm import TaskSpec, load_manifest, task_llm
        from .model_ui import ModelManager
        from .console_ui import select_option
        interactive = self._input is None and not self._custom_output and enhanced_input_available()
        if interactive and (not args or args[0] in {"grid", "list"}):
            from .subworker_ui import show_subworkers
            notice = ""
            view = args[0] if args else "grid"
            while True:
                selected = show_subworkers(self.subworkers, self.config, notice=notice, view=view)
                notice = ""
                if selected is None:
                    return
                action, task_id = selected
                try:
                    if action in {"model", "thinking", "provider"}:
                        self._edit_subworker_model(action, task_id)
                    elif action == "new":
                        name = ModelManager._ask("Worker 名称", f"task-{len(self.subworkers.rows()) + 1}")
                        target = ModelManager._ask("目标或附件路径")
                        goal = ModelManager._ask("任务目标")
                        self._cmd_subworker(["add" if self.subworkers.running else "start", name, target, goal])
                    elif action == "message" and task_id:
                        text = ModelManager._ask("给 " + task_id + " 的补充")
                        if text:
                            choice = select_option("发送补充", [
                                ChoiceSpec("wait", "加入草稿队列"), ChoiceSpec("now", "立即发送"),
                            ])
                            if choice == "wait":
                                self._enqueue_draft(text, task_id=task_id)
                            elif choice == "now":
                                self.subworkers.task_control(task_id, "message", text)
                    elif action == "cancel" and task_id:
                        self.subworkers.task_control(task_id, "cancel")
                    else:
                        self._cmd_subworker([action, task_id] if task_id and action == "inspect" else [action])
                        if action == "inspect":
                            return
                except ValueError as exc:
                    notice = str(exc)
            return
        action = args[0] if args else "list"
        values = args[1:]
        if action in {"list", "grid"}:
            rows = self.subworkers.rows()
            self._display(render_panel("Subworkers", rows=[
                (item["id"], f"{item['status']} · {item['model']} · {item['reasoning_effort']}\n{item['goal']}") for item in rows
            ] or [("队列", "还没有任务")], footer="/subworker start <名称> <目标> <任务> · /subworker run <清单.yaml>", color=self.color))
        elif action in {"start", "add"}:
            # 可选字段只从明确参数读取；目标文本不拼入命令或 system。
            options = {}
            words = []
            index = 0
            flags = {"--model": "model", "--thinking": "reasoning_effort", "--provider": "profile"}
            while index < len(values):
                word = values[index]
                if word in flags:
                    if index + 1 >= len(values):
                        raise ValueError(f"{word} 缺少值")
                    options[flags[word]] = values[index + 1]
                    index += 2
                else:
                    words.append(word)
                    index += 1
            if len(words) < 3:
                raise ValueError(f"/subworker {action} <名称> <目标> <任务> [--model ID --thinking high --provider 名称]")
            defaults = self.config.subworker
            task = TaskSpec(words[0], words[1], " ".join(words[2:]), max_steps=defaults.max_steps,
                            max_seconds=defaults.max_seconds, **options)
            from dataclasses import replace
            llm = task_llm(self.config, task)
            task = replace(task, model=llm.model, reasoning_effort=llm.reasoning_effort, profile=llm.profile)
            if action == "add":
                self.subworkers.add(task)
            else:
                self._launch_subworkers([task])
        elif action in {"run", "resume"}:
            if len(values) != 1:
                raise ValueError(f"/subworker {action} <文件路径>")
            self._launch_subworkers(load_manifest(values[0]) if action == "run" else None,
                                    checkpoint=values[0] if action == "resume" else "")
        elif action in {"stop", "pause", "continue"}:
            self.subworkers.control(action)
            self._emit({"stop": "正在停止并回收执行资源", "pause": "已请求暂停", "continue": "已继续"}[action], prefix="Subworker")
        elif action in {"message", "queue", "cancel"}:
            if not values or (action != "cancel" and len(values) < 2):
                raise ValueError("/subworker message|queue <名称> <补充> · /subworker cancel <名称>")
            if action == "queue":
                if not self.subworkers.running or values[0] not in {row["id"] for row in self.subworkers.rows()}:
                    raise ValueError("没有找到当前批次中的 Worker")
                self._enqueue_draft(" ".join(values[1:]), task_id=values[0])
                return
            self.subworkers.task_control(values[0], action, " ".join(values[1:]))
            self._emit("补充已发送" if action == "message" else "已请求取消指定任务", prefix="Subworker")
        elif action == "inspect":
            if len(values) != 1:
                raise ValueError("/subworker inspect <任务名称>")
            snapshot = self.subworkers.snapshot()
            item = snapshot["tasks"].get(values[0])
            if item is None:
                raise ValueError("未找到任务")
            result = item.get("result", {})
            self._display(render_panel(values[0], rows=[("任务", item["goal"]), ("状态", item["status"]),
                ("模型", f"{item['model']} · {item['reasoning_effort']}"), ("结论", result.get("verdict") or item.get("error") or "执行中"),
                ("检查点", snapshot["checkpoint"])], sections=[("产物", json.dumps(result.get("artifacts", {}), ensure_ascii=False, indent=2))], color=self.color))
        elif action == "settings":
            if not values and interactive:
                key = select_option("Subworker 资源", [ChoiceSpec(k, f"{label} · {getattr(self.config.subworker, k)}") for k, label in
                    (("workers", "并发"), ("max_steps", "步数"), ("max_seconds", "秒数"))], subtitle="下次启动生效 · 当前队列保持原配置")
                if key:
                    values = [key, ModelManager._ask(key, str(getattr(self.config.subworker, key)))]
            if values:
                if len(values) != 2 or values[0] not in {"workers", "max_steps", "max_seconds"}:
                    raise ValueError("/subworker settings <workers|max_steps|max_seconds> <整数>")
                candidate = self.config.with_overrides(**{"subworker__" + values[0]: int(values[1])})
                if candidate.subworker.workers > candidate.scope.max_host_concurrency:
                    raise ValueError("并发数不能超过 scope.max_host_concurrency")
                self.config.subworker = candidate.subworker
            self._emit(f"并发 {self.config.subworker.workers} · 步数 {self.config.subworker.max_steps or '不限'} · 时长 {str(self.config.subworker.max_seconds) + 's' if self.config.subworker.max_seconds else '不限'} · 持久默认值在 YAML subworker 段", prefix="Subworker")
        elif action in {"model", "thinking", "provider"}:
            if not values and interactive:
                self._edit_subworker_model(action, "")
                return
            if not 1 <= len(values) <= 2:
                raise ValueError(f"/subworker {action} <值> [等待中的任务名称]")
            key = {"model": "model", "thinking": "reasoning_effort", "provider": "profile"}[action]
            overrides = {key: "" if values[0] == "inherit" else values[0]}
            if action in {"model", "provider"}:
                overrides["reasoning_effort"] = "default"
            if action == "provider":
                overrides["model"] = ""
            if len(values) == 2:
                self.subworkers.configure(values[1], **overrides)
            else:
                candidate = self.config.with_overrides(**{"subworker__" + k: v for k, v in overrides.items()})
                llm = task_llm(candidate)
                self.config.subworker = candidate.subworker
                self._emit(f"子 Worker 默认模型 {llm.model} · {llm.reasoning_effort}；主会话保持原配置", prefix="Subworker")
        else:
            raise ValueError("用法：/subworker [start|run|add|list|inspect|model|thinking|provider|settings|pause|continue|stop|resume]")

    def _edit_subworker_model(self, action: str, task_id: str) -> None:
        from .console_ui import select_option
        from .model_ui import ModelManager
        from .swarm import task_llm
        from .llm.reasoning import reasoning_support
        llm = task_llm(self.config)
        if task_id:
            runtime = self.subworkers.runtime
            if runtime is None or runtime.records[task_id].status != "pending":
                raise ValueError("选中任务已启动或结束，不能中途换模型；D 调整新任务默认配置")
            llm = runtime._task_llms[task_id]
        if action == "thinking":
            support = reasoning_support(llm)
            value = select_option("思考强度", [ChoiceSpec(level, ("● " if level == llm.reasoning_effort else "  ") + support.label(level)) for level in support.levels], subtitle=llm.model)
        elif action == "provider":
            from .providers import ProfileStore
            value = select_option("子 Worker 供应商", [ChoiceSpec(p.id, f"{p.id} · {p.model}") for p in ProfileStore().profiles.values()], subtitle="使用 /model 中已保存的连接")
        else:
            value = ModelManager._ask("子 Worker 模型 ID", llm.model)
        if value:
            self._cmd_subworker([action, value, *([task_id] if task_id else [])])

    def _launch_subworkers(self, tasks, *, checkpoint: str = "") -> None:
        from .llm import make_backend
        self.subworkers.start(self.config, tasks, checkpoint=checkpoint, backend_factory=self._backend_factory or make_backend)
        self._emit(f"{self.subworkers.runtime.run_id} 已启动 · /subworker 查看 · 主会话可继续输入", prefix="Subworker")

    def _cmd_branch(self, args: list[str]) -> None:
        from .console_ui import edit_field, select_option
        if not args or args == ["list"]:
            result = self._execute_repo({"action": "branches"})
            branches = result.data.get("branches", [])
            if result.ok and not args and self._input is None and not self._custom_output and enhanced_input_available():
                selected = select_option("本地分支", [ChoiceSpec(name, ("● " if name == self._branch else "  ") + name) for name in branches]
                                         + [ChoiceSpec("action:new", "+ 新建分支")], subtitle="保留本地改动；冲突时 Git 拒绝切换")
                if selected == "action:new":
                    try:
                        name = edit_field("新分支名")
                    except KeyboardInterrupt:
                        return
                    return self._cmd_branch(["new", name]) if name else None
                if selected:
                    return self._cmd_branch([selected])
            return
        action = {"new": "create", "switch": "switch", "delete": "delete"}.get(args[0])
        if action:
            if len(args) not in ({2, 3} if action == "create" else {2}):
                raise ValueError("/branch new <名称> [起点]；/branch switch|delete <名称>")
            self._execute_repo({"action": action, "branch": args[1], **({"base": args[2]} if len(args) == 3 else {})})
        elif len(args) == 1:
            self._execute_repo({"action": "switch", "branch": args[0]})
        else:
            raise ValueError("/branch [名称|list|new <名称>|switch <名称>|delete <名称>]")

    def _execute_repo(self, params: dict) -> Any:
        from .tools.git import READ_ACTIONS
        if params.get("action") not in READ_ACTIONS and (self.session.running or self.subworkers.running):
            raise ValueError("任务运行中不能修改仓库；等待当前任务与子 Worker 结束")
        chat = self._ensure_chat()
        chat.ctx.extra["repository_busy"] = lambda: self.subworkers.running
        result = chat.registry.execute("workspace_git", params, chat.ctx)
        self._branch = self._read_branch()
        if result.ok and params.get("action") == "branches" and params.get("repository", ".") == ".":
            self._git_branches = result.data.get("branches", [])
        elif result.ok and params.get("action") == "status" and params.get("repository", ".") == ".":
            self._git_files = [item["path"] for item in result.data.get("changes", [])]
        if not result.ok:
            self._emit(result.error or result.summary, prefix="错误")
        return result

    def _cmd_repo(self, args: list[str]) -> None:
        from .repo_ui import parse_repo_args, show_repository, show_worktrees
        if not args and self._input is None and not self._custom_output and enhanced_input_available():
            show_repository(self._execute_repo, branches=self._cmd_branch, worktrees=lambda: show_worktrees(self._execute_repo))
        else:
            self._execute_repo(parse_repo_args(args))

    def _cmd_worktree(self, args: list[str]) -> None:
        from .repo_ui import parse_repo_args, show_worktrees
        if not args and self._input is None and not self._custom_output and enhanced_input_available():
            show_worktrees(self._execute_repo)
        else:
            self._execute_repo(parse_repo_args(args, "worktree"))

    def _refresh_output_palette(self) -> None:
        if self._enhanced_input is not None:
            control = self._enhanced_input.transcript_control
            control._blocks.clear()
            control.invalidate_from(0)
            self._enhanced_input.app.invalidate()

    def _cmd_theme(self, args: list[str]) -> None:
        from .console_ui import select_option
        from .output_styles import PALETTES
        value = args[0] if len(args) == 1 else None
        if len(args) > 1:
            raise ValueError("/theme trina|contrast|mono")
        if not args and self._input is None and not self._custom_output and enhanced_input_available():
            value = select_option("输出配色", [ChoiceSpec(name, {"trina": "粉紫 · 语义配色", "contrast": "高对比", "mono": "单色"}[name]) for name in PALETTES],
                                  subtitle="当前 " + self.config.ui.output_palette)
        if value is not None:
            if value not in PALETTES:
                raise ValueError("/theme trina|contrast|mono")
            self.config.ui.output_palette = value
            self._refresh_output_palette()
        self._display(render_answer("## 输出配色 · " + self.config.ui.output_palette + "\n\n"
                                    "正文 **强调** 与 `代码`\n\n```diff\n@@ 示例 @@\n- removed\n+ added\n```",
                                    color=self.color, palette=self._output_palette))

    def _notify_agent(self, body: str, *, error: bool = False) -> None:
        enabled = self.config.ui.error_notify if error else self.config.ui.completion_notify
        if not enabled:
            return
        if self._enhanced_input is not None:
            self._enhanced_input.show_notification("Trina", body, level="error" if error else "success")
        else:
            from .notifications import send_terminal_notification
            send_terminal_notification("Trina", body)

    def _cmd_notify(self, args: list[str]) -> None:
        if len(args) > 1 or (args and args[0] not in {"on", "off", "test"}):
            raise ValueError("/notify on|off|test")
        if args[:1] == ["on"]:
            self.config.ui.completion_notify = True
        elif args[:1] == ["off"]:
            self.config.ui.completion_notify = False
        elif args[:1] == ["test"]:
            self._notify_agent("提醒测试成功")
        state = "开启" if self.config.ui.completion_notify else "关闭"
        self._emit(f"完成提醒已{state}；配置项 ui.completion_notify", prefix="通知")

    def _tool_registry(self):
        """返回控制台使用的真实注册表；目录与执行始终共享同一对象。"""
        if self.chat is not None:
            self.registry = self.chat.registry
        if self.registry is None:
            from .tools import build_registry

            self.registry = build_registry()
        return self.registry

    def _tool_catalog(self):
        from .tool_catalog import build_tool_catalog
        from .tools.profile import visible_tool_names

        registry = self._tool_registry()
        chat = self.chat
        route = "general"
        browser = terminal_session = False
        state = self.session.state or self._last_state
        if chat is not None:
            context = chat.ctx.extra
            route = str(context.get("tool_route") or "general")
            browser = bool(context.get("tool_route_browser"))
            terminal_session = bool(context.get("tool_route_terminal_session"))
            state = chat.state
            visible = context.get("tool_allowlist")
        else:
            visible = None
        if visible is None:
            visible = visible_tool_names(registry, route, browser=browser,
                                         terminal_session=terminal_session)
        visible = set(visible)
        if not self.subworkers.running:
            visible.discard("worker_tasks")
        return build_tool_catalog(
            registry,
            visible=visible,
            route=route,
            role=self._active_worker,
            browser=browser,
            terminal_session=terminal_session,
            subworkers=bool(self.subworkers.running),
            state=state,
        )

    def _show_tool_detail(self, entry) -> None:
        rows = [
            ("公开名称", entry.name),
            ("内部标识", entry.internal_name),
            ("类别 / 动作", f"{entry.spec.category} / {entry.spec.action_level}"),
            ("来源", f"{entry.source_kind} · {entry.source}"),
            ("当前状态", "模型可见" if entry.visible else "按需隐藏"),
        ]
        params = []
        for name, spec in entry.spec.params.items():
            flags = [spec.type, "必填" if spec.required else f"可选 · 默认 {spec.default!r}"]
            if spec.enum:
                flags.append("取值 " + " | ".join(map(str, spec.enum)))
            if spec.description:
                flags.append(spec.description)
            params.append(f"{name}: " + " · ".join(flags))
        p50 = "—" if entry.stats.p50_seconds is None else f"{entry.stats.p50_seconds:.3f}s"
        p95 = "—" if entry.stats.p95_seconds is None else f"{entry.stats.p95_seconds:.3f}s"
        self._display(render_panel(
            f"工具 · {entry.name}",
            rows=rows,
            sections=[
                ("用途", entry.spec.summary),
                ("参数", "\n".join(params) or "无"),
                ("可见性解析", " → ".join(entry.resolution) + "\n" + entry.visibility_reason),
                ("当前会话统计", f"{entry.stats.calls} 次调用 · {entry.stats.failed} 次失败 · p50 {p50} · p95 {p95}"),
            ],
            footer="只读目录；实际调用仍经过 ToolRegistry、护栏、审计与超时",
            color=self.color,
        ))

    def _cmd_tools(self, args: list[str]) -> None:
        """空参数打开工具控制中心；显式 CLI 选项保持原兼容行为。"""
        if args or self._enhanced_input is None:
            self._delegate(["tools", *args])
            return
        from .tool_picker import select_tool

        selected = select_tool(self._tool_catalog(), plain=self._plain or not self.color)
        if selected is not None:
            self._show_tool_detail(selected)

    def _cmd_tool(self, args: list[str]) -> None:
        """持久二进制/浏览器手工调用与当前聊天共用生命周期。"""
        registry = self._tool_registry()
        tool = registry.get(args[0]) if args else None
        if tool is None or tool.spec.category not in {"binary", "browser"}:
            self._delegate(["tool", *args])
            return
        from .cli import build_parser, _literal
        parsed = build_parser().parse_args(["tool", *args])
        params = json.loads(parsed.params or "{}")
        if not isinstance(params, dict):
            raise ValueError("--params 必须是 JSON 对象")
        for item in parsed.param or []:
            key, _, value = item.partition("=")
            params[key] = _literal(value)
        if parsed.target and "path" in tool.spec.params:
            params.setdefault("path", parsed.target)
        if parsed.target and "url" in tool.spec.params:
            params.setdefault("url", parsed.target)
        chat = self._ensure_chat()
        chat._initialize_messages()
        chat.title = chat.title or f"手动工具 {tool.spec.name}"
        self._task_title = chat.title
        instruction = {"role": "user", "content": "[手动工具] " + json.dumps({"action": tool.spec.name, "params": params}, ensure_ascii=False)}
        action = {"action": tool.spec.name, "params": params}
        assistant = {"role": "assistant", "content": json.dumps(action, ensure_ascii=False)}
        chat.history.extend([instruction, assistant])
        chat._messages.extend([instruction, assistant])
        chat.save()
        try:
            observation = chat._run_action(action, 0)
            message = {"role": "user", "content": "[工具结果] " + observation}
            chat.history.append(message)
            chat._messages.append(message)
        finally:
            chat.save()
        self.session.state = chat.state
        self._display(render_process(observation, label="工具结果", color=self.color, palette=self._output_palette))

    def _cmd_resume(self, args: list[str]) -> None:
        from .chat_store import ChatStore
        from .console_ui import select_option
        if len(args) > 1:
            raise ValueError("用法：/resume [list|last|编号|ID]")
        preview_only = self.session.running
        store = ChatStore(self.config.workspace_dir)
        rows = store.list()
        if not rows:
            self._emit("暂无已保存的历史对话；新对话会自动保存。旧审计日志不含完整回复，无法还原未保存的会话。")
            return
        current = self.chat.run_id if self.chat is not None else ""
        selector = args[0] if args else ""
        if selector in {"", "list"}:
            choices = [ChoiceSpec(row["id"],
                f"{'● ' if row['id'] == current else ''}{row['title']} · {time.strftime('%m-%d %H:%M', time.localtime(row['updated']))} · {row['turns']} 轮 · {row['id']}") for row in rows]
            if not args and self._input is None and not self._custom_output and enhanced_input_available():
                editor = self._enhanced_input
                pick = editor.pick_option if editor is not None and editor.app.is_running else select_option
                action = "预览" if preview_only else "切换"
                selected = pick("历史对话", choices, subtitle=f"搜索标题或 ID · Enter {action} · Esc 返回")
                if selected is None:
                    return
                selector = selected
            else:
                action = "预览" if preview_only else "切换"
                self._display(render_panel("历史对话", sections=[("", "\n".join(f"{i:02d}  {item.description}" for i, item in enumerate(choices, 1)))],
                                           footer=f"/resume <编号或ID> {action} · /resume last 最近一条", color=self.color))
                return
        if selector == "last":
            selector = next((row["id"] for row in rows if row["id"] != current), rows[0]["id"])
        elif selector.isdigit():
            index = int(selector) - 1
            if not 0 <= index < len(rows):
                raise ValueError("对话编号超出范围")
            selector = rows[index]["id"]
        matches = [row["id"] for row in rows if row["id"] == selector] or [row["id"] for row in rows if row["id"].startswith(selector)]
        if len(matches) != 1:
            raise ValueError("对话 ID 不存在或前缀不唯一；使用 /resume list 查看")
        run_id = matches[0]
        if run_id == current:
            self._emit("已经是当前对话")
            return
        if not preview_only and not self.session.running and self._launch_lock.acquire(blocking=False):
            try:
                if not self.session.running:
                    self._switch_chat(run_id)
                    return
            finally:
                self._launch_lock.release()

        saved = store.load(run_id)
        lines = []
        for item in reversed(saved.get("text_messages") or saved["history"]):
            role = {"user": "你", "assistant": "Trina"}.get(item.get("role"))
            text = clean_text(item.get("content", "")).strip()
            if role and text and not text.startswith(("[工具结果]", "[手动工具]", "{")):
                lines.append((role, text if len(text) <= 600 else text[:599] + "…"))
                if len(lines) == 8:
                    break
        self._display(render_panel(f"历史预览 · {saved.get('title') or run_id}",
                                  sections=list(reversed(lines)) or [("历史", "暂无消息")],
                                  footer="空闲时可用 /resume 切换", color=self.color))

    def _switch_chat(self, run_id: str) -> None:
        from .brain.chat import ChatAgent

        restored = ChatAgent.resume(self.config, run_id, registry=self.registry, backend=self._backend(),
            approval=self.session.request_approval, steer_queue=self.session.steers, stop_event=self.session.stop_event,
            pause_event=self.session.pause_event, logger=self._agent_log, on_tool=self._on_tool_result,
            on_event=self._on_runtime_event)
        try:
            if self.chat is not None:
                if self.chat.history:
                    self._worker_sessions[self._active_worker] = self.chat.run_id
                self.chat.close()
        except Exception:
            restored.close()
            raise
        self._job_wait.cancel()
        self.chat = restored
        self._todo_snapshot = restored.todo.view()
        self._active_worker = restored.worker_role
        self._worker_sessions[self._active_worker] = restored.run_id
        self._task_config = None
        self._live_step = None
        self._task_title = restored.title
        self._branch = self._read_branch()
        restored.save()
        self.session.reset_for_run()
        self.session.state = self._last_state = restored.state
        self.view.reset()
        self.view.usage = restored.usage
        for name, value in restored.usage_gaps.items():
            setattr(self.view, name, value)
        if self._enhanced_input is not None:
            self._enhanced_input.clear_transcript()
        self._event_transcript = None
        self._emit(f"已恢复：{restored.title} · {restored.turns} 轮 · {restored.run_id}")
        restored.on_event = self._on_runtime_event
        history_actions = 0
        for item in restored.history:
            if item.get("role") == "assistant":
                try:
                    value = json.loads(item.get("content", ""))
                    history_actions += int(isinstance(value, dict) and "action" in value)
                except (ValueError, TypeError):
                    pass
        recorded_actions = sum(1 for _ in restored.events.replay(kinds=("tool.requested",), limit=history_actions + 1))
        if restored.events.last_seq and recorded_actions >= history_actions:
            after = 0
            while batch := restored.events.replay(after):
                for event in batch:
                    self._on_runtime_event(event, replayed=True)
                after = batch[-1].seq
            restored._event_subscription = restored.events.subscribe(self._on_runtime_event, after=after)
            return
        restored._event_subscription = restored.events.subscribe(self._on_runtime_event, after=restored.events.last_seq)
        for item in restored.history:
            role, text = item.get("role"), item.get("content", "")
            if role == "assistant":
                try:
                    action = json.loads(text)
                except (ValueError, TypeError):
                    action = None
                if isinstance(action, dict) and "action" in action:
                    self._display(render_process(f"{action['action']} {json.dumps(action.get('params', {}), ensure_ascii=False)}", label="执行记录", color=self.color))
                else:
                    self._display(render_answer(text, color=self.color, palette=self._output_palette))
            else:
                self._display(render_message(text, label="工具" if text.startswith("[工具结果]") else "你", color=self.color))

    def _cmd_usage(self) -> None:
        data = self.view.usage_data()
        rate = data["cache_hit_rate"]
        rows: list[tuple[str, Any]] = [("模型调用", data["calls"])] if data["calls"] else []
        if data["tokens_reported"]:
            rows.append(("会话 Token", f"{'≥' if data['missing_tokens'] else ''}{data['total_tokens']}"))
        if data["input_reported"]:
            rows.append(("输入 Token", f"{'≥' if data['missing_input'] else ''}{data['input_tokens']}"))
        if data["output_reported"]:
            rows.append(("输出 Token", f"{'≥' if data['missing_output'] else ''}{data['output_tokens']}"))
        if rate is not None:
            rows.append(("缓存命中率", f"{rate:.1%}" + ("（部分请求）" if data["missing_cache"] else "")))
        if data["cache_read_reported"]:
            rows.append(("缓存读取", f"{'≥' if data['missing_cache_read'] else ''}{data['cache_read_tokens']}"))
        if data["cache_write_reported"]:
            rows.append(("缓存写入", f"{'≥' if data['missing_cache_write'] else ''}{data['cache_write_tokens']}"))
        if data["cost_reported"]:
            rows.append(("费用", f"{'≥' if data['missing_cost'] else ''}${data['cost_usd']:.6f}"))
        self._display(render_panel("会话用量", rows=rows, sections=[] if rows else [("", "暂无可显示的用量数据")], color=self.color))

    def _cmd_context(self) -> None:
        state = self._current_state()
        data = getattr(state, "context_stats", {})
        if not data:
            self._emit("当前会话尚无上下文诊断")
            return
        last = data.get("last_request", {})
        rows = [("输入估算 / 上限", f"{data.get('estimated_input_tokens', 0)} / {data.get('input_budget', 0)}"),
                ("继承检查点", data.get("compactions", 0)), ("前缀变化", last.get("reason", "")),
                ("公共前缀字节", last.get("visible_prefix_bytes", 0)),
                ("新增输入字节" if last.get("reason") == "append" else "变化后缀字节", last.get("suffix_visible_bytes", 0))]
        if last.get("uncached_input_tokens") is not None:
            rows.append(("供应商未命中输入 Token", last["uncached_input_tokens"]))
        transport = last.get("transport") or {}
        if "connect_seconds" in transport:
            label = "复用" if transport.get("connection_reused") else f"{transport['connect_seconds']:.3f}s"
            rows.append(("网络连接与握手", label))
        if "headers_seconds" in transport:
            rows.append(("到 HTTP 响应头", f"{transport['headers_seconds']:.3f}s"))
        if "first_event_seconds" in transport:
            rows.append(("到首段模型输出", f"{transport['first_event_seconds']:.3f}s"))
        policy = last.get("cache_policy") or {}
        if policy:
            rows.extend([("缓存策略", f"{policy.get('mode')} / {policy.get('source')}"),
                         ("前缀标识", policy.get("stable_prefix_id", ""))])
        if data.get("last_archive"):
            rows.append(("完整历史归档", data["last_archive"]))
        self._display(render_panel("上下文与缓存", rows=rows, sections=[("计量说明", "本地字节与 Token 估算不能代替服务端缓存判定。")], color=self.color))

    def _cmd_compact(self) -> None:
        if self.session.running:
            self._emit("任务运行中由运行时在请求边界自动创建检查点")
            return
        if self.chat is None:
            self._emit("当前没有可压缩的对话")
            return
        from .conversation import ContextBudgetError
        before = self.chat.context.epoch
        try:
            self.chat.context.prepare(self.chat._messages, force=True)
            self.chat.save()
        except ContextBudgetError as exc:
            self._emit(str(exc))
            return
        self._emit("已创建继承检查点，完整历史已落盘" if self.chat.context.epoch > before else "当前上下文无需压缩")

    def _cmd_save(self) -> None:
        """把当前供应商与模型写入唯一的 v2 供应商配置。"""
        if self.config.llm.profile:
            from .providers import ProfileStore
            store = ProfileStore()
            profile = store.profiles[self.config.llm.profile]
            profile.model = self.config.llm.model
            profile.reasoning_effort = self.config.llm.reasoning_effort
            profile.context_window = self.config.llm.context_window
            store.active = profile.id
            store.save()
            self._emit(f"已保存到 {store.path}：连接 {profile.id} · 模型 {profile.model}")
            return
        from .providers import ProfileStore
        store = ProfileStore()
        if self.config.llm.backend == "openai":
            from .providers import PRESETS, ProviderProfile
            llm = self.config.llm
            name = next((p.id for p in PRESETS if p.base_url == llm.base_url), "custom")
            base_name, suffix = name, 2
            while name in store.profiles:
                name, suffix = f"{base_name}-{suffix}", suffix + 1
            profile = ProviderProfile(name, base_name, llm.base_url, llm.model, llm.api_key_env, llm.api)
            profile.reasoning_effort = llm.reasoning_effort
            profile.cache_mode, profile.cache_ttl = llm.cache_mode, llm.cache_ttl
            profile.cache_features = list(llm.cache_features)
            profile.context_window = llm.context_window
            profile.validate()
            store.profiles[name], store.active = profile, name
            store.save()
            store.apply(llm)
            self._emit(f"已保存到 {store.path}：连接 {name} · 模型 {profile.model}")
            return
        if store.active:
            store.active = ""
            store.save()

        self._emit(f"已保存到 {store.path}：模型 {self.config.llm.model}")

    def _cmd_goal(self, args: list[str]) -> None:
        if args and args[0] not in ("show",):
            value = "" if args[0] == "clear" else " ".join(args[1:] if args[0] == "set" else args).strip()
            if args[0] != "clear" and not value:
                raise ValueError("goal set 需要目标文本")
            self.task_store.set_goal(value)
            self._goal_text = value
            if self.session.running:
                self.session.update_goal(value)
                if self._task_config is not None:
                    self._task_title = value or f"扫描 {self._plan.target}"
        self._display(render_panel("Goal", sections=[("当前目标", self._goal_text or "未设置")], color=self.color))

    def _cmd_schedule(self, args: list[str]) -> None:
        command = args[0] if args else "list"
        if command == "add":
            import argparse
            class ScheduleParser(argparse.ArgumentParser):
                def error(self, message):
                    raise ValueError(f"/schedule add 参数错误: {message}")
            parser = ScheduleParser(prog="/schedule add", exit_on_error=False)
            parser.add_argument("--at", required=True)
            parser.add_argument("--target", required=True)
            parser.add_argument("--goal")
            parser.add_argument("--max-steps", type=int, default=0)
            parser.add_argument("--approve", choices=APPROVAL_MODES, default=self.session.approval_mode)
            values = parser.parse_args(args[1:])
            task = self.task_store.add(values.target, values.at, goal=values.goal, max_steps=values.max_steps,
                                       approve=values.approve, config=self.config)
            self._display(render_panel("定时任务已创建", rows=[("ID", task.id), ("开始时间", task.run_at),
                          ("目标", task.target), ("Goal", task.goal or "未设置")], color=self.color))
        elif command == "cancel" and len(args) == 2:
            self.task_store.cancel(args[1])
            self._emit(f"已取消定时任务 {args[1]}")
        elif command == "list":
            tasks = self.task_store.list()
            self._display(render_panel("定时任务", sections=[("", "\n".join(
                f"{task.id} · {task.run_at} · {task.status} · {task.target}" + (f" · {task.outcome}" if task.outcome else "")
                for task in tasks) or "暂无定时任务")], color=self.color))
        else:
            raise ValueError("用法: /schedule add --at +10m --target <地址>；/schedule list；/schedule cancel <ID>")

    def _start_scheduler(self) -> None:
        self._scheduler = TaskScheduler(self.task_store, execute=self._execute_scheduled,
                                        ready=lambda: not self.session.running, log=self._agent_log)
        def loop():
            while not self._scheduler.stop_event.is_set():
                try:
                    with self._launch_lock:
                        self._scheduler.tick()
                except Exception as exc:
                    self._emit(f"定时队列读取失败: {exc}", prefix="错误")
                self._scheduler.stop_event.wait(1)
        self._scheduler_thread = threading.Thread(target=loop, name="trina-scheduler", daemon=True)
        self._scheduler_thread.start()

    def _execute_scheduled(self, task):
        from .scope import ScopeGuard
        config = TrinaConfig.load(task.config_path or None)
        decision = ScopeGuard(config.scope).check_target(task.target)
        if decision.denied:
            raise ValueError(decision.reason)
        if not self._start_task(target=task.target, goal=task.goal, max_steps=task.max_steps,
                                approval=task.approve, config=config):
            raise ValueError("定时任务未能启动")
        self.session.thread.join()
        if self._last_state is None:
            raise ValueError("任务未产生运行结果")
        return self._last_state

    # ------------------------------------------------------------------ 子命令
    # ------------------------------------------------------------------ 对话
    def _ensure_chat(self) -> Any:
        if self.chat is None:
            from .brain.chat import ChatAgent

            self.chat = ChatAgent(
                self.config,
                registry=self.registry,
                backend=self._backend(),
                approval=self.session.request_approval,
                steer_queue=self.session.steers,
                stop_event=self.session.stop_event,
                pause_event=self.session.pause_event,
                logger=self._agent_log,
                on_tool=self._on_tool_result,
                on_event=self._on_runtime_event,
            )
            self.chat.worker_role = self._active_worker
        self.chat.ctx.extra["subworkers"] = self.subworkers
        self.chat.ctx.extra["subworker_backend_factory"] = self._backend_factory
        return self.chat

    def _on_chat_stream(self, text: str, final: bool) -> None:
        """流式正文只更新增强终端中的同一张答复卡片。"""
        if self._enhanced_input is not None:
            self._enhanced_input.update_stream(text, final=final)

    def _on_tool_result(self, result: Any) -> None:
        """把工具产出的关键事实回显（发现/凭据/flag/路径），让用户看得见"它在干什么"。"""
        facts = result.facts or {}
        shown: set[tuple[str, str, str]] = set()
        for finding in facts.get("findings", []) or []:
            sev = finding.get("severity", "?")
            target = str(finding.get("url") or finding.get("target") or "")
            title = str(finding.get("title") or finding.get("vuln_class") or "发现")
            parameter = str(finding.get("parameter") or "")
            key = (title, target, parameter)
            if key in shown:
                continue
            shown.add(key)
            detail = f" · {parameter}" if parameter and parameter not in title else ""
            self._emit(f"[发现] [{sev}] {title}{detail} · {target}".rstrip(" ·"), prefix="[Trina]")
        for flag in facts.get("flags", []) or []:
            self._emit(f"[flag] {str(flag.get('value'))[:60]}", prefix="[Trina]")
        for cred in facts.get("credentials", []) or []:
            self._emit(f"[凭据] {cred.get('service')}@{cred.get('target')} 用户 {cred.get('username')}", prefix="[Trina]")

    def _start_chat_turn(self, text: str) -> bool:
        if self.session.running:
            self._emit("有任务在跑 · /queue add 保留草稿 · /hint 立即补充")
            return False
        try:
            chat = self._ensure_chat()
        except Exception as exc:  # noqa: BLE001 - 工作目录不可写等启动失败必须回显，不能把控制台打崩
            self._emit(f"对话无法启动：{type(exc).__name__}: {exc}", prefix="错误")
            return False
        if not isinstance(chat.backend, TrackedBackend):
            chat.backend = TrackedBackend(chat.backend, self.view)
            chat.ctx.extra["llm_backend"] = chat.backend
        self.session.reset_for_run()
        self.view.set_activity("processing", "准备回复")
        self._task_title = chat.title or " ".join(text.split())[:80]
        self._live_step = None
        self._clear_action()
        chat.state.goal = self._goal_text
        self.session.state = chat.state
        self.session.started_at = time.monotonic()

        def _worker() -> None:
            from .scope import interactive_model_retries

            try:
                with interactive_model_retries(self.config.llm.connection_retries, self.session.stop_event):
                    reply = chat.ask(text)
                self._task_title = chat.title or self._task_title
                if not chat.last_reply_streamed:
                    self._display(render_answer(reply or "(空回复)", color=self.color, palette=self._output_palette))
                self._last_state = chat.state
                if not self.session.stop_event.is_set():
                    self._notify_agent("本轮回复已完成")
            except Exception as exc:  # noqa: BLE001 - 对话异常不能杀掉控制台
                chat._cancel_stream()
                self._emit(f"对话出错: {type(exc).__name__}: {exc}", prefix="[错误]")
                self._notify_agent("本轮回复异常", error=True)
            finally:
                self._last_turn_seconds = max(0.0, time.monotonic() - self.session.started_at)
                self.view.set_activity("idle", "就绪")

        self.session.thread = threading.Thread(target=_worker, name="trina-chat", daemon=True)
        if self._enhanced_input is None:
            self._emit("思考中…", prefix="过程")
        self.session.thread.start()
        return True

    def _show_status(self) -> None:
        from .board import render_board

        if self.session.running:
            step = self._live_step
            phase = PHASES.get(getattr(step, "phase", ""), "准备 / 等待模型")
            state = self.session.state
            if state is not None:
                phase = PHASES.get(state.phase, state.phase)
            control = "待审批" if self.session.pending else ("已暂停" if self.session.pause_event.is_set() else ("停止中" if self.session.stop_event.is_set() else "运行中"))
            rows = [("状态", control), ("阶段", phase),
                    ("目标", (getattr(state, "target", "") if state is not None else getattr(self._plan, "target", "")) or "未指定"),
                    ("耗时", f"{max(0, time.monotonic() - self.session.started_at):.0f}s")]
            if step is not None:
                rows.append(("最近一步", f"第 {step.index} 步 · {step.action} · {step.summary}"))
            self._display(render_panel("当前任务", rows=rows, footer="/board 查看 · /queue 草稿 · /hint 立即补充 · /stop 停止", color=self.color))
            if self.session.pending:
                self._display(render_approval(self.session.pending, self.session.approval_timeout, color=self.color))
        elif self._last_state is not None:
            self._display(render_board(self._last_state, color=self.color))
        else:
            self._display(render_panel("就绪", rows=[("状态", "空闲"), ("模型", _display_model(self.config.llm.model, self.config.llm.reasoning_effort))],
                                       footer="输入问题开始对话 · /play 开始任务 · /help 查看命令", color=self.color))

    def _cmd_approve(self, args: list[str]) -> None:
        mode = (args[0] if args else "").strip().lower()
        if mode not in APPROVAL_MODES:
            self._emit(f"用法: approve {'|'.join(APPROVAL_MODES)}（当前 {self.session.approval_mode}）")
            return
        self.session.approval_mode = mode
        self._emit(f"审批模式已切换为 {mode}")

    def _cmd_revoke(self, args: list[str], revoke: bool) -> None:
        if not args:
            self._emit("用法: revoke <target_id> / grant <target_id>")
            return
        target_id = args[0]
        self._ensure_controller()
        if revoke:
            self._controller.revoke(target_id, reason="控制台人工撤销")
            self._emit(f"已撤销 {target_id} 的授权：后续对该目标的检查会被控制器拒绝（减权，不绕路）")
        else:
            self._controller.grant(target_id)
            self._emit(f"已恢复 {target_id} 的授权")

    def _ensure_controller(self, config: TrinaConfig | None = None) -> Any:
        owner_config = config or self.config
        if self._controller is None or self._controller_config is not owner_config:
            from .policy import AuthorizationController, Budget, PolicyRegistry

            policy = owner_config.policy
            self._controller = AuthorizationController(
                PolicyRegistry.load(owner_config, extra_dirs=list(policy.registry_dirs)),
                mode=policy.mode,
                budget=Budget(policy.stop_on_no_new_evidence),
            )
            self._controller_config = owner_config
        return self._controller

    def _cmd_play(self, args: list[str]) -> None:
        parsed = _parse_play_args(args)
        self._start_task(target=parsed.get("target"), range_name=parsed.get("range"), goal=parsed.get("goal", self._goal_text),
                         max_steps=parsed.get("max_steps"), approval=parsed.get("approve"))

    def _cmd_run(self, args: list[str]) -> None:
        if not args or args[0].startswith("-"):
            self._emit("用法: run <target> [--goal G] [--max-steps N] [--approve MODE]")
            return
        target = args[0]
        parsed = _parse_play_args(args[1:])
        self._start_task(target=target, range_name=None, goal=parsed.get("goal", self._goal_text),
                         max_steps=parsed.get("max_steps"), approval=parsed.get("approve"))

    def _delegate(self, argv: list[str]) -> None:
        """把无状态命令交给 CLI 同一实现（保证行为一致）。"""
        from . import cli

        if self._enhanced_input is not None:
            import contextlib
            import io
            stream = io.StringIO()
            try:
                with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                    code = cli.main(argv)
            finally:
                self._display(stream.getvalue())
        else:
            code = cli.main(argv)
        if code not in (0, None):
            self._emit(f"（命令退出码 {code}）")

    # ------------------------------------------------------------------ 任务
    def _start_task(self, **kwargs) -> bool:
        with self._launch_lock:
            return self._start_task_locked(**kwargs)

    def _start_task_locked(
        self,
        *,
        target: str | None,
        range_name: str | None,
        goal: str,
        config: TrinaConfig | None = None,
        max_steps: int | None,
        approval: str | None,
    ) -> bool:
        task_config = config or self.config
        if self.session.running:
            self._emit("已有任务在运行：先 stop 或等它结束")
            return False
        if approval:
            if approval.lower() in APPROVAL_MODES:
                self.session.approval_mode = approval.lower()
            else:
                self._emit(f"未知审批模式 {approval}，沿用 {self.session.approval_mode}")
        self.session.reset_for_run()
        self._live_step = None
        # 调度任务会重新载入入队时记录的配置；控制器也必须跟随该配置，
        # 不能复用交互会话里可能已经过期的范围、模式或预算。
        self._ensure_controller(task_config)
        self._controller.checks_used = 0
        self._controller.started_at = time.monotonic()
        try:
            from .play import build_plan
        except ImportError as exc:  # pragma: no cover
            self._emit(f"无法加载 play 模块: {exc}")
            return False
        try:
            plan = build_plan(
                task_config,
                target=target,
                range_name=range_name,
                goal=goal,
                autostart_lab=self.autostart_lab,
                log=lambda msg: self._emit(msg, prefix="[靶场]"),
            )
        except Exception as exc:  # noqa: BLE001 - 靶场准备失败要给出可读原因
            self._emit(f"靶场准备失败: {type(exc).__name__}: {exc}")
            return False
        self._task_config = task_config
        self._last_state = None
        self._plan = plan
        self._task_title = plan.goal or f"扫描 {plan.target}"
        self._task_max_steps = task_config.llm.max_steps if max_steps is None else max_steps
        self._clear_action()
        self.session.state = None
        self.session.started_at = time.monotonic()
        teardown_after = bool(plan.started_fixtures)
        self.view.set_activity("processing", "准备任务")

        def _worker() -> None:
            from .play import run_plan
            from .llm import make_backend

            self._emit(
                f"开始任务：{plan.target}"
                + (f" · 目标要求：{goal}" if goal else ""),
                prefix="[任务]",
            )
            notice: tuple[str, bool] | None = None
            try:
                state = run_plan(
                    task_config,
                    plan,
                    backend=(TrackedBackend(make_backend(task_config.llm), self.view) if config is not None else self._backend()),
                    registry=self.registry,
                    approval=self.session.request_approval,
                    steer_queue=self.session.steers,
                    stop_event=self.session.stop_event,
                    pause_event=self.session.pause_event,
                    controller=self._controller,
                    max_steps=max_steps,
                    log=self._agent_log,
                    on_step=self._on_step,
                    on_event=self._on_runtime_event,
                    shared_context={"subworkers": self.subworkers, "subworker_backend_factory": self._backend_factory},
                )
                self._last_state = state
                self.session.state = state
                self._report_summary(state)
                if state.status != "stopped":
                    notice = (f"任务结束 · {state.status}", state.status in {"failed", "blocked"})
            except Exception as exc:  # noqa: BLE001 - 任务异常必须回显而不是杀掉控制台
                self._emit(f"任务异常: {type(exc).__name__}: {exc}", prefix="[错误]")
                notice = ("任务异常", True)
            finally:
                self._task_config = None
                self.view.set_activity("idle", "就绪")
                if teardown_after:
                    plan.teardown()
                    self._emit("已回收本次自动拉起的靶机", prefix="[靶场]")
                if notice is not None:
                    self._notify_agent(notice[0], error=notice[1])

        self.session.thread = threading.Thread(target=_worker, name="trina-task", daemon=True)
        self.session.thread.start()
        self._emit("任务已在后台运行：直接输入可补充 · status 查看 · stop 停止")

        return True

    def _report_summary(self, state: Any) -> None:
        from .board import render_board
        self._display(render_board(state, color=self.color))
        for finding in state.findings[:8]:
            self._emit(f"[{finding.severity}] {finding.vuln_class} {finding.url or finding.target} ({finding.status})", prefix="发现")

    def _cleanup(self) -> None:
        if self._cleanup_done:
            return
        with Shutdown(self._emit) as shutdown:
            self._cleanup_done = True
            job_wait = getattr(self, "_job_wait", None)
            if job_wait is not None:
                job_wait.close()
            model_catalog = getattr(self, "_model_catalog", None)
            if model_catalog is not None:
                model_catalog.close()
            # 先通知所有生产者停止，再等待；再次 Ctrl+C 只跳过线程等待。
            if self._scheduler is not None:
                shutdown.call("调度器", self._scheduler.stop_event.set)
            shutdown.call("当前任务", self.session.request_stop)
            shutdown.call("Subworker", lambda: self.subworkers.close(timeout=0))
            shutdown.join(self.session.thread)
            shutdown.join(self.subworkers.thread)
            shutdown.join(self._scheduler_thread)
            if self.chat is not None:
                shutdown.call("会话", self.chat.close)
            if self._plan is not None and self._plan.started_fixtures:
                shutdown.call("靶机", self._plan.teardown)


def _parse_play_args(args: list[str]) -> dict[str, Any]:
    """极简参数解析（play/run 通用）：--target/--range/--goal/--max-steps/--approve。

    `--goal` 会把后续非 `--` 开头的词全部吃进去：控制台里用户习惯不加引号（`play --goal 拿到 flag`）。
    """
    parsed: dict[str, Any] = {}
    index = 0
    value_flags = {"--target": "target", "--range": "range", "--approve": "approve"}
    while index < len(args):
        token = args[index]
        if token == "--goal":
            words: list[str] = []
            index += 1
            while index < len(args) and not args[index].startswith("--"):
                words.append(args[index])
                index += 1
            parsed["goal"] = " ".join(words)
            continue
        if token in value_flags:
            parsed[value_flags[token]] = args[index + 1] if index + 1 < len(args) else ""
            index += 2
            continue
        if token == "--max-steps":
            try:
                parsed["max_steps"] = int(args[index + 1])
            except (IndexError, ValueError):
                pass
            index += 2
            continue
        index += 1
    return parsed


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：`python3 -m trina console`。"""
    import argparse

    parser = argparse.ArgumentParser(prog="trina console", description="Trina 交互控制台（人工可介入）")
    parser.add_argument("--approve", choices=APPROVAL_MODES, default="off",
                        help="可选审批模式：off 全自动 / exploit / verify / all（默认 off）")
    parser.add_argument("--config", help="配置文件路径")
    parser.add_argument("--set", action="append", metavar="section.field=value", help="临时覆盖配置，可重复")
    parser.add_argument("--plain", action="store_true", help="关闭终端颜色，适合日志和兼容终端")
    parser.add_argument("--no-lab-autostart", action="store_true", help="play 时不自动起本地 lab 靶机")
    args = parser.parse_args(argv)

    from .cli import _literal  # 复用 CLI 的字面量解析

    overrides = {}
    for item in args.set or []:
        key, _, value = item.partition("=")
        overrides[key] = _literal(value)
    config = TrinaConfig.load(args.config, overrides)
    console = Console(config, approval_mode=args.approve, autostart_lab=not args.no_lab_autostart, plain=args.plain)
    return console.run()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
