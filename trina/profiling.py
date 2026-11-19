"""轻量 Agent 运行剖析器。

剖析器只消费运行事件和结构化用量，不读取模型提示词、工具原文、Cookie
或密钥。它可以在单 Agent 和 Swarm 事件流上复用，输出同一份机器可读报告。
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percentile))))
    return round(ordered[index], 6)


class AgentProfiler:
    """事件驱动的运行剖析器，默认只保留聚合指标和少量调用身份。"""

    def __init__(self, run_id: str, *, worker_id: str = "", enabled: bool = True):
        self.run_id = str(run_id)
        self.worker_id = str(worker_id)
        self.enabled = bool(enabled)
        self.started_at = time.time()
        self._started = time.monotonic()
        self._lock = threading.RLock()
        self._open_models: dict[str, dict[str, Any]] = {}
        self._open_tools: dict[str, dict[str, Any]] = {}
        self._open_tasks: dict[str, dict[str, Any]] = {}
        self._llm: list[dict[str, Any]] = []
        self._tools: list[dict[str, Any]] = []
        self._tasks: list[dict[str, Any]] = []
        self._phases: defaultdict[str, list[float]] = defaultdict(list)
        self._counters: defaultdict[str, int] = defaultdict(int)
        self._finished = False

    @classmethod
    def from_env(cls, run_id: str, *, worker_id: str = "", enabled: bool = True) -> "AgentProfiler":
        flag = os.environ.get("TRINA_PROFILE", "")
        if flag:
            enabled = flag.strip().lower() not in {"0", "false", "off", "no"}
        return cls(run_id, worker_id=worker_id, enabled=enabled)

    def consume_event(self, event: Any) -> None:
        """消费 EventBus 事件，忽略所有正文，只提取运行元数据。"""
        if not self.enabled or event is None:
            return
        kind = str(getattr(event, "kind", ""))
        payload = dict(getattr(event, "payload", {}) or {})
        now = time.monotonic()
        identity = str(getattr(event, "call_id", "") or payload.get("request_id", ""))
        task_id = str(getattr(event, "task_id", "") or "")
        worker_id = str(getattr(event, "worker_id", "") or self.worker_id)
        with self._lock:
            if kind == "model.started":
                request_id = str(payload.get("request_id") or identity)
                self._open_models[request_id] = {
                    "request_id": request_id, "started": now, "first_token": None,
                    "task_id": task_id, "worker_id": worker_id,
                }
                task = self._open_tasks.get(task_id)
                if task is not None:
                    task.setdefault("first_activity", now)
                self._counters["llm_requests"] += 1
            elif kind in {"model.text_delta", "model.thinking_delta"}:
                request_id = str(payload.get("request_id") or identity)
                item = self._open_models.get(request_id)
                if item is not None and item["first_token"] is None and payload.get("delta"):
                    item["first_token"] = round(now - item["started"], 6)
            elif kind == "model.finished":
                request_id = str(payload.get("request_id") or identity)
                item = self._open_models.pop(request_id, None)
                if item is not None:
                    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
                    self._llm.append({
                        "request_id": request_id,
                        "task_id": item["task_id"], "worker_id": item["worker_id"],
                        "duration_seconds": round(max(0.0, now - item["started"]), 6),
                        "first_token_seconds": item["first_token"],
                        "ok": bool(payload.get("ok", True)),
                        "input_tokens": usage.get("input_tokens") if usage.get("input_reported") else None,
                        "output_tokens": usage.get("output_tokens") if usage.get("output_reported") else None,
                        "cache_read_tokens": usage.get("cache_read_tokens") if usage.get("cache_read_reported") else None,
                        "cache_write_tokens": usage.get("cache_write_tokens") if usage.get("cache_write_reported") else None,
                    })
            elif kind == "model.retry":
                self._counters["model_retries"] += 1
            elif kind == "tool.started":
                call_id = identity or f"tool-{len(self._open_tools) + len(self._tools) + 1}"
                self._open_tools[call_id] = {
                    "call_id": call_id, "tool": str(payload.get("tool") or ""),
                    "started": now, "task_id": task_id, "worker_id": worker_id,
                }
                self._counters["tool_calls"] += 1
            elif kind == "tool.finished":
                call_id = identity
                item = self._open_tools.pop(call_id, None)
                if item is not None:
                    duration = payload.get("duration")
                    try:
                        duration = float(duration) if duration is not None else now - item["started"]
                    except (TypeError, ValueError):
                        duration = now - item["started"]
                    ok = bool(payload.get("ok", payload.get("status") == "completed"))
                    self._tools.append({
                        "call_id": call_id, "tool": item["tool"], "task_id": item["task_id"],
                        "worker_id": item["worker_id"], "duration_seconds": round(max(0.0, duration), 6),
                        "status": str(payload.get("status") or ("completed" if ok else "failed")),
                        "ok": ok,
                    })
                    if not ok:
                        self._counters["tool_failures"] += 1
            elif kind == "task.started":
                self._open_tasks[task_id] = {"task_id": task_id, "worker_id": worker_id, "started": now}
                self._counters["subworker_starts"] += 1
            elif kind == "task.finished":
                item = self._open_tasks.pop(task_id, None)
                if item is not None:
                    self._tasks.append({
                        "task_id": task_id, "worker_id": item["worker_id"],
                        "duration_seconds": round(max(0.0, now - item["started"]), 6),
                        "startup_seconds": round(max(0.0, (item.get("first_activity") or now) - item["started"]), 6),
                        "status": str(payload.get("status") or "unknown"),
                    })

    def record_phase(self, name: str, duration_seconds: float, **metadata: Any) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._phases[str(name)].append(round(max(0.0, float(duration_seconds)), 6))
            if metadata.get("compacted"):
                self._counters["compactions"] += 1

    def record_loop(self, *, step: int, round_number: int) -> None:
        if self.enabled:
            with self._lock:
                self._counters["agent_rounds"] = max(self._counters["agent_rounds"], int(round_number))
                self._counters["agent_steps"] = max(self._counters["agent_steps"], int(step))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            llm = list(self._llm)
            tools = list(self._tools)
            tasks = list(self._tasks)
            phases = {name: {
                "calls": len(values),
                "total_seconds": round(sum(values), 6),
                "p50_seconds": _percentile(values, .50),
                "p95_seconds": _percentile(values, .95),
            } for name, values in self._phases.items()}
            counters = dict(self._counters)
            wall = max(0.0, time.monotonic() - self._started)
        def aggregate(rows: list[dict[str, Any]], key: str = "duration_seconds") -> dict[str, Any]:
            values = [float(item[key]) for item in rows if isinstance(item.get(key), (int, float))]
            return {"calls": len(rows), "total_seconds": round(sum(values), 6),
                    "p50_seconds": _percentile(values, .50), "p95_seconds": _percentile(values, .95)}
        input_values = [item["input_tokens"] for item in llm if isinstance(item.get("input_tokens"), int)]
        output_values = [item["output_tokens"] for item in llm if isinstance(item.get("output_tokens"), int)]
        cache_values = [item["cache_read_tokens"] for item in llm if isinstance(item.get("cache_read_tokens"), int)]
        ttft_values = [item["first_token_seconds"] for item in llm if isinstance(item.get("first_token_seconds"), (int, float))]
        return {
            "version": 1,
            "status": "enabled" if self.enabled else "disabled",
            "run_id": self.run_id,
            "worker_id": self.worker_id,
            "started_at": self.started_at,
            "wall_time_seconds": round(wall, 6),
            "counters": counters,
            "llm": {**aggregate(llm),
                    "input_tokens": sum(input_values) if input_values else None,
                    "output_tokens": sum(output_values) if output_values else None,
                    "cache_read_tokens": sum(cache_values) if cache_values else None,
                    "first_token_p50_seconds": _percentile(ttft_values, .50),
                    "first_token_p95_seconds": _percentile(ttft_values, .95),
                    "duration_availability": "reported" if llm else "unavailable",
                    "token_usage_availability": "reported" if (input_values or output_values or cache_values) else "unavailable"},
            "tools": {**aggregate(tools), "failed": sum(1 for item in tools if not item["ok"]),
                      "by_tool": {name: aggregate([item for item in tools if item["tool"] == name])
                                  for name in sorted({str(item["tool"]) for item in tools})}},
            "subworkers": {**aggregate(tasks), "completed": len(tasks),
                           "startup_seconds": round(sum(float(item.get("startup_seconds", 0.0)) for item in tasks), 6),
                           "execution_seconds": round(sum(max(0.0, float(item.get("duration_seconds", 0.0)) -
                                                               float(item.get("startup_seconds", 0.0))) for item in tasks), 6),
                           "waiting_seconds": None,
                           "waiting_availability": "unavailable",
                           "availability": "reported" if tasks else "unavailable"},
            "phases": phases,
        }

    def write(self, path: str | Path) -> dict[str, Any]:
        report = self.snapshot()
        if self.enabled:
            destination = Path(path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(destination)
            report["path"] = str(destination)
        return report

    def finish(self, path: str | Path) -> dict[str, Any]:
        with self._lock:
            self._finished = True
        return self.write(path)


__all__ = ["AgentProfiler"]
