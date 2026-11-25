"""从持久事件库读取时戳并汇总延迟。"""
from __future__ import annotations

import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from statistics import median


METRIC_LABELS = {
    "request_to_connected": "请求起点 → 响应头",
    "request_to_first_output": "请求起点 → 首段模型输出",
    "connected_to_first_output": "响应头 → 首段模型输出",
    "request_to_first_text": "请求起点 → 首段正文",
    "last_tool_to_request": "同轮最后一条工具终态 → 下一请求起点",
    "new_connection": "新建连接与握手",
    "transport_to_headers": "HTTP 调用 → 响应头",
    "transport_to_first_event": "HTTP 调用 → 首个模型数据事件",
}


@dataclass
class _Request:
    started: float
    connected: float | None = None
    saw_output: bool = False
    saw_text: bool = False


def summarize_latency(path: str | Path, *, after: int = 0, task: str | None = None) -> dict:
    """按运行、任务与 Worker 配对时戳，返回秒数及样本量。"""
    if after < 0:
        raise ValueError("--after 必须非负")
    uri = f"{Path(path).resolve().as_uri()}?mode=ro"
    samples: dict[str, list[float]] = {name: [] for name in METRIC_LABELS}
    query = """
        SELECT json_extract(body, '$.kind') AS kind,
               json_extract(body, '$.time'),
               json_extract(body, '$.run_id'),
               json_extract(body, '$.task_id') AS task_id,
               json_extract(body, '$.worker_id'),
               json_extract(body, '$.payload.request_id'),
               coalesce(length(json_extract(body, '$.payload.delta')), 0) > 0,
               json_extract(body, '$.payload.transport.connection_reused'),
               json_extract(body, '$.payload.transport.connect_seconds'),
               json_extract(body, '$.payload.transport.headers_seconds'),
               json_extract(body, '$.payload.transport.first_event_seconds')
        FROM events
        WHERE seq > ? AND (? IS NULL OR task_id = ?) AND kind IN (
            'model.started', 'model.connected', 'model.text_delta', 'model.thinking_delta',
            'model.finished', 'tool.requested', 'tool.finished', 'user.message',
            'chat.turn_finished', 'task.finished', 'runtime.finished'
        )
        ORDER BY seq
    """
    requests: dict[tuple[str, str, str, str], _Request] = {}
    last_tools: dict[tuple[str, str, str], float] = {}

    def record(metric: str, start: float, end: float) -> None:
        if end >= start and math.isfinite(end - start):
            samples[metric].append(end - start)

    with closing(sqlite3.connect(uri, uri=True)) as db:
        for row in db.execute(query, (after, task, task)):
            kind, ts, run_id, task_id, worker_id, request_id, has_delta, reused, connect, headers, first_event = row
            if not isinstance(ts, (int, float)) or not math.isfinite(ts):
                continue
            scope = (run_id, task_id, worker_id)
            if kind == "tool.finished":
                last_tools[scope] = ts
            elif kind in {"tool.requested", "user.message", "chat.turn_finished", "task.finished", "runtime.finished"}:
                last_tools.pop(scope, None)
            elif request_id:
                key = (*scope, request_id)
                if kind == "model.started":
                    requests[key] = _Request(ts)
                    previous_tool = last_tools.pop(scope, None)
                    if previous_tool is not None:
                        record("last_tool_to_request", previous_tool, ts)
                elif kind == "model.finished":
                    if requests.pop(key, None) is not None:
                        for metric, seconds in (("new_connection", connect if reused == 0 else None),
                                                ("transport_to_headers", headers),
                                                ("transport_to_first_event", first_event)):
                            if isinstance(seconds, (int, float)):
                                record(metric, 0, seconds)
                elif (request := requests.get(key)) is not None:
                    if kind == "model.connected" and request.connected is None and ts >= request.started:
                        request.connected = ts
                        record("request_to_connected", request.started, ts)
                    elif has_delta and kind in {"model.text_delta", "model.thinking_delta"}:
                        if not request.saw_output:
                            request.saw_output = True
                            record("request_to_first_output", request.started, ts)
                            if request.connected is not None:
                                record("connected_to_first_output", request.connected, ts)
                        if kind == "model.text_delta" and not request.saw_text:
                            request.saw_text = True
                            record("request_to_first_text", request.started, ts)

    def stats(values: list[float]) -> dict[str, int | float | None]:
        if not values:
            return {"samples": 0, "p50_seconds": None, "p95_seconds": None}
        values.sort()
        return {"samples": len(values), "p50_seconds": round(median(values), 6),
                "p95_seconds": round(values[math.ceil(.95 * len(values)) - 1], 6)}

    return {name: stats(values) for name, values in samples.items()}
