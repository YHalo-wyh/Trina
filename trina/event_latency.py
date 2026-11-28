"""从持久事件库读取时戳并汇总延迟。"""
from __future__ import annotations

import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from statistics import median


METRIC_LABELS = {
    "task_queue": "任务入队 → Worker 实际执行",
    "worker_to_request": "Worker 实际执行 → 首次模型请求",
    "worker_result_merge": "Worker 完成 → 结果可读",
    "user_to_request": "任务入队 → 模型请求起点",
    "request_to_connected": "请求起点 → 响应头",
    "request_to_first_output": "请求起点 → 首段模型输出",
    "connected_to_first_output": "响应头 → 首段模型输出",
    "request_to_first_text": "请求起点 → 首段正文",
    "request_total": "模型请求总时长",
    "model_to_tool_start": "模型结束 → 首个工具启动",
    "tool_queue": "工具请求 → 工具启动",
    "tool_runtime": "工具启动 → 工具终态",
    "last_tool_to_request": "同轮最后一条工具终态 → 下一请求起点",
    "state_prepare": "状态整理",
    "context_prepare": "上下文整理",
    "context_diagnostic": "上下文诊断",
    "result_delivery": "工具结果归并",
    "background_wait": "后台任务事件等待",
    "completion_delivery": "后台完成 → Agent 消费",
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
               json_extract(body, '$.call_id'),
               json_extract(body, '$.payload.request_id'),
               coalesce(length(json_extract(body, '$.payload.delta')), 0) > 0,
               json_extract(body, '$.payload.phase'),
               json_extract(body, '$.payload.duration'),
               json_extract(body, '$.payload.status'),
               json_extract(body, '$.payload.transport.connection_reused'),
               json_extract(body, '$.payload.transport.connect_seconds'),
               json_extract(body, '$.payload.transport.headers_seconds'),
               json_extract(body, '$.payload.transport.first_event_seconds'),
               json_extract(body, '$.payload.queued_at')
        FROM events
        WHERE seq > ? AND (? IS NULL OR task_id = ?) AND kind IN (
            'model.started', 'model.connected', 'model.text_delta', 'model.thinking_delta',
            'model.finished', 'tool.requested', 'tool.started', 'tool.finished', 'user.message',
            'execution.phase', 'worker.executing', 'worker.finished', 'task.result_ready',
            'chat.turn_finished', 'task.finished', 'runtime.finished'
        )
        ORDER BY seq
    """
    requests: dict[tuple[str, str, str, str], _Request] = {}
    last_tools: dict[tuple[str, str, str], float] = {}
    queued_users: dict[tuple[str, str, str], float] = {}
    last_models: dict[tuple[str, str, str], float] = {}
    requested_tools: dict[tuple[str, str, str, str], float] = {}
    started_tools: dict[tuple[str, str, str, str], float] = {}
    started_workers: dict[tuple[str, str, str], float] = {}
    finished_workers: dict[tuple[str, str], float] = {}
    phase_metrics = {
        "state_prepare", "context_prepare", "context_diagnostic", "result_delivery",
        "background_wait", "completion_delivery",
    }

    def record(metric: str, start: float, end: float) -> None:
        if end >= start and math.isfinite(end - start):
            samples[metric].append(end - start)

    with closing(sqlite3.connect(uri, uri=True)) as db:
        for row in db.execute(query, (after, task, task)):
            (kind, ts, run_id, task_id, worker_id, call_id, request_id, has_delta,
             phase_name, phase_duration, phase_status, reused, connect, headers, first_event, queued_at) = row
            if not isinstance(ts, (int, float)) or not math.isfinite(ts):
                continue
            scope = (run_id, task_id, worker_id)
            tool_key = (*scope, str(call_id or ""))
            if kind == "worker.executing":
                started_workers[scope] = ts
                finished_workers.pop((task_id, worker_id), None)
                if isinstance(queued_at, (int, float)):
                    record("task_queue", queued_at, ts)
            elif kind == "worker.finished":
                finished_workers[(task_id, worker_id)] = ts
            elif kind == "task.result_ready":
                finished = finished_workers.pop((task_id, worker_id), None)
                if finished is not None:
                    record("worker_result_merge", finished, ts)
            elif kind == "execution.phase":
                if (phase_name in phase_metrics and phase_status == "completed"
                        and isinstance(phase_duration, (int, float))):
                    record(phase_name, 0, phase_duration)
            elif kind == "user.message":
                queued_users[scope] = ts
                last_tools.pop(scope, None)
            elif kind == "tool.requested":
                requested_tools[tool_key] = ts
                last_tools.pop(scope, None)
            elif kind == "tool.started":
                started_tools[tool_key] = ts
                requested = requested_tools.pop(tool_key, None)
                if requested is not None:
                    record("tool_queue", requested, ts)
                model_finished = last_models.pop(scope, None)
                if model_finished is not None:
                    record("model_to_tool_start", model_finished, ts)
            elif kind == "tool.finished":
                started = started_tools.pop(tool_key, None)
                if started is not None:
                    record("tool_runtime", started, ts)
                last_tools[scope] = ts
            elif kind in {"chat.turn_finished", "task.finished", "runtime.finished"}:
                last_tools.pop(scope, None)
            elif request_id:
                key = (*scope, request_id)
                if kind == "model.started":
                    requests[key] = _Request(ts)
                    executing = started_workers.pop(scope, None)
                    if executing is not None:
                        record("worker_to_request", executing, ts)
                    queued = queued_users.pop(scope, None)
                    if queued is not None:
                        record("user_to_request", queued, ts)
                    previous_tool = last_tools.pop(scope, None)
                    if previous_tool is not None:
                        record("last_tool_to_request", previous_tool, ts)
                elif kind == "model.finished":
                    request = requests.pop(key, None)
                    if request is not None:
                        record("request_total", request.started, ts)
                        last_models[scope] = ts
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
