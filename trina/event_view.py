"""统一事件的终端投影；实时订阅与历史重放使用同一套渲染。"""
from __future__ import annotations

import json
import threading
from collections import OrderedDict
from typing import Any, Callable

from .events import RuntimeEvent
from .terminal import render_answer, render_message, render_process, render_tool_call


class EventTranscript:
    """call_id 定位工具窗口，消息增量保留在主内容区，不解析日志猜测状态。"""

    def __init__(self, output: Callable[[str], None], *, editor: Any = None, color: bool = False):
        self.output, self.editor, self.color = output, editor, color
        self.calls: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.cursor: dict[str, int] = {}
        self._model_errors: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._lock = threading.RLock()

    def consume(self, event: RuntimeEvent) -> None:
        with self._lock:
            self._consume(event)

    def _consume(self, event: RuntimeEvent) -> None:
        if event.seq <= self.cursor.get(event.stream_id, 0):
            return
        payload = event.payload
        if event.kind == "model.error" or (event.kind == "model.finished" and payload.get("ok") is False):
            identity = (event.stream_id, str(payload.get("request_id", event.seq)))
            if identity not in self._model_errors:
                if self.editor is not None:
                    self.editor.update_stream("", final=True)
                from .brain.failure import LLM_FAILURE_CN, classify_llm_response
                from .llm import LLMResponse
                error = str(payload.get("error") or "供应商未返回错误详情")
                kind = classify_llm_response(LLMResponse(ok=False, error=error))
                self.output(render_message(f"{LLM_FAILURE_CN[kind]} · {error}", label="错误", color=self.color))
                self._model_errors[identity] = None
                while len(self._model_errors) > 256:
                    self._model_errors.popitem(last=False)
        elif event.kind == "model.retry":
            self.output(render_process(f"第 {payload.get('attempt')} 次重试 · {payload.get('delay')}s 后重试",
                                       label="模型", color=self.color))
        elif event.kind == "user.message":
            self.output(render_message(str(payload.get("text", "")), label="你", color=self.color))
        elif event.kind == "assistant.message":
            text, final = str(payload.get("text", "")), bool(payload.get("final"))
            if self.editor is not None:
                self.editor.update_stream(text, final=final)
            elif final and text:
                self.output(render_answer(text, color=self.color))
        elif event.kind.startswith(("tool.", "job.")) and event.call_id:
            kind = event.kind.replace("job.", "tool.", 1) if event.kind.startswith("job.") else event.kind
            row = self.calls.setdefault(event.call_id, dict(tool=payload.get("tool", "工具"), params={},
                                                           status="requested", output="", stderr=""))
            if kind in {"tool.requested", "tool.validated", "tool.started"}:
                row.update(tool=payload.get("tool", row["tool"]), params=payload.get("params", row["params"]))
                row["status"] = "running" if kind == "tool.started" else "requested"
            elif kind == "tool.approval_requested":
                row["status"] = "awaiting_approval"
            elif kind == "tool.output":
                field = "stderr" if payload.get("stream") == "stderr" else "output"
                row[field] = (row[field] + str(payload.get("delta", "")))[-16000:]
            elif kind == "tool.finished":
                data = payload.get("data") or {}
                stdout, stderr = data.get("stdout"), data.get("stderr")
                if stdout is not None:
                    row["output"] = str(stdout)
                elif payload.get("raw"):
                    row["output"] = str(payload["raw"])[-16000:]
                elif data:
                    row["output"] = json.dumps(data, ensure_ascii=False, indent=2)[-16000:]
                if not row["output"]:
                    row["output"] = str(payload.get("summary", ""))
                if stderr is not None:
                    row["stderr"] = str(stderr)
                row.update(status=payload.get("status", "failed"), error=payload.get("error", ""),
                           duration=float(payload.get("duration") or 0), command=payload.get("command", ""),
                           exit_code=data.get("exit_code"))
            card = render_tool_call(**row, color=self.color)
            if self.editor is not None:
                self.editor.update_tool(event.call_id, card)
            elif kind == "tool.finished":
                self.output(card)
            while len(self.calls) > 128:
                self.calls.popitem(last=False)
        self.cursor[event.stream_id] = event.seq
