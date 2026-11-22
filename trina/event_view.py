"""统一事件的终端投影；实时订阅与历史重放使用同一套渲染。"""
from __future__ import annotations

import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable

from .events import RuntimeEvent
from .terminal import render_answer, render_message, render_process, render_tool_call, tool_card_params
from .tool_names import public_tool_name


_OUTPUT_HEAD_CHARS = 1200
_OUTPUT_TAIL_CHARS = 4800
_OUTPUT_HEAD_LINES = 12
_OUTPUT_TAIL_LINES = 47


@dataclass
class _OutputPreview:
    """控制台只缓存可显示的首尾，原始增量由事件库保存。"""

    head: str = ""
    tail: str = ""
    length: int = 0

    def append(self, text: str) -> None:
        self.length += len(text)
        self.head += text[:_OUTPUT_HEAD_CHARS - len(self.head)]
        self.tail = (self.tail + text[-_OUTPUT_TAIL_CHARS:])[-_OUTPUT_TAIL_CHARS:]

    def display(self) -> tuple[str, bool]:
        overlap = len(self.head) + len(self.tail) - self.length
        if overlap >= 0:
            text = self.head + self.tail[overlap:]
            lines = text.splitlines()
            if len(lines) <= _OUTPUT_HEAD_LINES + _OUTPUT_TAIL_LINES + 1:
                return text, False
            head, tail = lines[:_OUTPUT_HEAD_LINES], lines[-_OUTPUT_TAIL_LINES:]
        else:
            head = self.head.splitlines()[:_OUTPUT_HEAD_LINES]
            tail = self.tail.splitlines()[-_OUTPUT_TAIL_LINES:]
        return "\n".join([*head, "… 中间输出已折叠 …", *tail]), True


class EventTranscript:
    """call_id 定位工具窗口，消息增量保留在主内容区，不解析日志猜测状态。"""

    def __init__(self, output: Callable[[str], None], *, editor: Any = None, color: bool = False, palette="trina"):
        self.output, self.editor, self.color = output, editor, color
        self.palette = palette
        self.calls: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.cursor: dict[str, int] = {}
        self._model_errors: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._thinking: dict[str, str] = {}
        self._model_text: Any = None
        self._model_final_text = ""
        self._assistant_id: tuple[str, str] | None = None
        self._assistant_text = ""
        self._lock = threading.RLock()

    def consume(self, event: RuntimeEvent) -> None:
        with self._lock:
            self._consume(event)

    def _consume(self, event: RuntimeEvent) -> None:
        if event.seq <= self.cursor.get(event.stream_id, 0):
            return
        payload = event.payload
        request_id = str(payload.get("request_id", ""))
        if event.kind == "model.started":
            from .brain.chat import _VisibleStream
            self._model_text = _VisibleStream(self._visible_text) if payload.get("visible_text") else None
            self._model_final_text = ""
            self._assistant_id = None
            self._assistant_text = ""
        elif event.kind == "model.thinking_delta":
            text = self._thinking.get(request_id, "") + str(payload.get("delta", ""))
            self._thinking[request_id] = text
            if self.editor is not None:
                self.editor.update_thinking(request_id, text)
        elif event.kind == "model.text_delta":
            self._finish_thinking(request_id)
            if self._model_text is not None:
                from .llm import LLMStreamEvent
                self._model_text.on_event(LLMStreamEvent("text_delta", delta=str(payload.get("delta", ""))))
        elif event.kind == "model.finished" and payload.get("ok") is not False:
            self._finish_thinking(request_id)
            if self._model_text is not None and self._model_text.opened:
                self._model_final_text = self._model_text.displayed.rstrip()
                self._model_text.finish(self._model_final_text)
            self._model_text = None
        elif event.kind == "model.error" or (event.kind == "model.finished" and payload.get("ok") is False):
            self._finish_thinking(request_id)
            partial = self._assistant_text or (self._model_text.displayed if self._model_text is not None else "")
            self._model_text = None
            self._assistant_id = None
            self._assistant_text = ""
            identity = (event.stream_id, str(payload.get("request_id", event.seq)))
            if identity not in self._model_errors:
                from .brain.failure import LLM_FAILURE_CN, classify_llm_response
                from .llm import LLMResponse
                from .llm.openai_backend import is_tls_eof

                error = str(payload.get("error") or "供应商未返回错误详情")
                if is_tls_eof(error):
                    if self.editor is not None:
                        self.editor.update_stream(partial, final=True)
                    message = "模型连接提前关闭（TLS）。可在 /model 中检测当前供应商的连接。"
                    if partial:
                        message = "回复中断，已显示的正文不完整。 " + message
                else:
                    if self.editor is not None:
                        self.editor.update_stream("", final=True)
                    kind = classify_llm_response(LLMResponse(ok=False, error=error))
                    message = f"{LLM_FAILURE_CN[kind]} · {error}"
                self.output(render_message(message, label="错误", color=self.color, palette=self.palette))
                self._model_errors[identity] = None
                while len(self._model_errors) > 256:
                    self._model_errors.popitem(last=False)
        elif event.kind == "model.retry":
            self.output(render_process(f"第 {payload.get('attempt')} 次重试 · {payload.get('delay')}s 后重试",
                                       label="模型", color=self.color, palette=self.palette))
        elif event.kind == "user.message":
            self.output(render_message(str(payload.get("text", "")), label="你", color=self.color, palette=self.palette))
        elif event.kind == "assistant.delta" and self.editor is not None:
            identity = (event.stream_id, str(payload.get("message_id", "")))
            if self._assistant_id != identity:
                self._assistant_id = identity
                self._assistant_text = ""
            self._assistant_text += str(payload.get("delta", ""))
            self.editor.update_stream(self._assistant_text)
        elif event.kind == "assistant.message":
            text, final = str(payload.get("text", "")), bool(payload.get("final"))
            self._assistant_id = None if final else (event.stream_id, str(payload.get("message_id", "")))
            self._assistant_text = "" if final else text
            if final and text.rstrip() == self._model_final_text and self._model_final_text:
                self._model_final_text = ""
            elif self.editor is not None:
                self.editor.update_stream(text, final=final)
            elif final and text:
                self.output(render_answer(text, color=self.color, palette=self.palette))
        elif event.kind.startswith(("tool.", "job.")) and event.call_id:
            kind = event.kind.replace("job.", "tool.", 1) if event.kind.startswith("job.") else event.kind
            row = self.calls.get(event.call_id)
            if row is None:
                row = dict(tool=payload.get("tool", "工具"), params={}, status="requested",
                           output=_OutputPreview(), stderr=_OutputPreview())
                self.calls[event.call_id] = row
            if kind in {"tool.requested", "tool.validated", "tool.started"}:
                row.update(tool=payload.get("tool", row["tool"]), params=payload.get("params", row["params"]))
                row["status"] = "running" if kind == "tool.started" else "requested"
            elif kind == "tool.approval_requested":
                row["status"] = "awaiting_approval"
            elif kind == "tool.output":
                field = "stderr" if payload.get("stream") == "stderr" else "output"
                row[field].append(str(payload.get("delta", "")))
            elif kind == "tool.finished":
                data = payload.get("data") or {}
                stdout, stderr = data.get("stdout"), data.get("stderr")
                output = str(stdout) if stdout is not None else ""
                if stdout is None:
                    output = str(payload.get("raw") or "") or (json.dumps(data, ensure_ascii=False, indent=2) if data else "")
                if output or not row["output"].length:
                    row["output"] = _OutputPreview()
                    row["output"].append(output or str(payload.get("summary") or ""))
                if stderr:
                    row["stderr"] = _OutputPreview()
                    row["stderr"].append(str(stderr))
                row.update(status=payload.get("status", "failed"), error=payload.get("error", ""),
                           duration=float(payload.get("duration") or 0), command=payload.get("command", ""),
                           exit_code=data.get("exit_code"), artifact_ref=payload.get("artifact_ref"))
            if self.editor is not None or kind == "tool.finished":
                self._show_tool(event.call_id, row)
            while len(self.calls) > 128:
                self.calls.popitem(last=False)
        self.cursor[event.stream_id] = event.seq

    def _show_tool(self, call_id: str, row: dict[str, Any]) -> None:
        output, clipped_output = row["output"].display()
        stderr, clipped_stderr = row["stderr"].display()
        snapshot = {**row, "output": output, "stderr": stderr}
        artifact = snapshot.pop("artifact_ref", None)
        if clipped_output or clipped_stderr:
            snapshot["record"] = str(artifact or "查看运行事件的 JSON 记录")
        if self.editor is None:
            self.output(render_tool_call(**snapshot, color=self.color, palette=self.palette))
            return
        from .terminal import ResponsiveText, render_tool_call_fragments

        # 历史缓冲按卡片实际保留的可显示内容计量。
        params = tool_card_params(row["params"], row.get("command", ""))
        raw = (f"{public_tool_name(row['tool'])} · {row['status']}\n{params}\n{output}\n{stderr}\n"
               f"{row.get('error', '')}\n{snapshot.get('record', '')}")
        card = ResponsiveText(raw, lambda width: str(render_tool_call(
            **snapshot, color=self.color, width=width, palette=self.palette)),
            lambda width: render_tool_call_fragments(**snapshot, color=self.color, width=width, palette=self.palette))
        self.editor.update_tool(call_id, card)

    def _visible_text(self, text: str, final: bool) -> None:
        if self.editor is not None:
            self.editor.update_stream(text, final=final)
        elif final and text:
            self.output(render_answer(text, color=self.color, palette=self.palette))

    def _finish_thinking(self, request_id: str) -> None:
        text = self._thinking.pop(request_id, "")
        if text:
            if self.editor is not None:
                self.editor.update_thinking(request_id, text, final=True)
            else:
                from .terminal import render_thinking
                self.output(render_thinking(text, final=True, color=self.color, palette=self.palette))
