"""事件驱动的多 Agent 协作状态：共享待办、Intent 队列、双图与执行留痕。"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from typing import Any, Iterable


TERMINAL_INTENTS = {"completed", "failed", "blocked", "skipped", "timed_out"}
FAILED_INTENTS = {"failed", "blocked", "skipped", "timed_out"}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _stable_id(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:16]


def _task_data(task: Any) -> dict[str, Any]:
    return {
        "id": str(task.id),
        "target": str(task.target),
        "goal": str(task.goal),
        "depends_on": list(task.depends_on),
        "priority": int(task.priority),
        "max_seconds": int(task.max_seconds),
        "capabilities": list(task.capabilities),
    }


class CoordinationRuntime:
    """Commander 单写、多 Worker 只读的轻量协作平面。

    Planner 只在 ``emit()`` 产生的新事件后推进一轮；普通工具步骤只写 trace，
    不会唤醒 Planner。事实图只保存工具实际产出的可复用事实，探索图保存尚未
    完成的 Intent、依赖和结果血缘，两者通过 anchors 连接。
    """

    VERSION = 1

    def __init__(self, tasks: Iterable[Any], *, event_bus: Any = None):
        self.event_bus = event_bus
        self.lock = threading.RLock()
        self.todo: dict[str, dict[str, Any]] = {}
        self.intents: dict[str, dict[str, Any]] = {}
        self.facts: dict[str, dict[str, Any]] = {}
        self.exploration: dict[str, Any] = {"nodes": {}, "edges": [], "anchors": []}
        self.traces: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.trace_seq = 0
        self.revision = 0
        self.planned_revision = -1
        self.planner_rounds = 0
        for task in tasks:
            self.add_intent(task, notify=False)
        self.emit("runtime.seeded", count=len(self.intents))

    def add_intent(self, task: Any, *, notify: bool = True) -> bool:
        row = _task_data(task)
        signature = _stable_id("sig-", {
            "target": row["target"].strip().casefold(),
            "goal": row["goal"].strip().casefold(),
            "capabilities": sorted(row["capabilities"]),
        })
        with self.lock:
            if row["id"] in self.intents or any(item["signature"] == signature for item in self.intents.values()):
                return False
            self.intents[row["id"]] = {
                **row,
                "signature": signature,
                "status": "pending",
                "attempt": 0,
                "worker_id": "",
                "lease_id": "",
                "claimed_at": 0.0,
                "deadline": 0.0,
                "error": "",
            }
            self.todo[row["id"]] = {
                "id": row["id"], "summary": row["goal"], "depends_on": list(row["depends_on"]),
                "status": "waiting" if row["depends_on"] else "ready", "evidence_refs": [],
            }
            node_id = f"intent:{row['id']}"
            self.exploration["nodes"][node_id] = {
                "id": node_id, "kind": "intent", "summary": row["goal"], "target": row["target"],
                "status": "pending", "priority": row["priority"],
            }
            for dependency in row["depends_on"]:
                edge = {"src": f"intent:{dependency}", "dst": node_id, "rel": "enables"}
                if edge not in self.exploration["edges"]:
                    self.exploration["edges"].append(edge)
            if notify:
                self.emit("intent.added", intent_id=row["id"])
            return True

    def emit(self, kind: str, *, intent_id: str = "", **payload: Any) -> None:
        with self.lock:
            self.revision += 1
            self.events.append({
                "revision": self.revision, "kind": kind, "intent_id": intent_id,
                "time": time.time(), "payload": payload,
            })
            del self.events[:-2000]
            if self.event_bus is not None:
                self.event_bus.publish("coordination." + kind, source="commander", task_id=intent_id,
                                       revision=self.revision, **payload)

    def plan(self, records: dict[str, Any]) -> list[str]:
        """消费一批图变更事件并返回当前可领取 Intent；无新事件不新增规划轮。"""
        with self.lock:
            if self.planned_revision != self.revision:
                self.planned_revision = self.revision
                self.planner_rounds += 1
                self._trace("planner", "planner", "round", f"处理至事件修订 {self.revision}", "", True)
            ready: list[dict[str, Any]] = []
            for intent_id, intent in self.intents.items():
                record = records.get(intent_id)
                status = str(getattr(record, "status", intent["status"]))
                if status != "pending" or intent["status"] != "pending":
                    if intent_id in self.todo and status in TERMINAL_INTENTS | {"running"}:
                        self.todo[intent_id]["status"] = "in_progress" if status == "running" else status
                    continue
                dependency_states = [str(getattr(records.get(dep), "status", "pending")) for dep in intent["depends_on"]]
                if any(state in FAILED_INTENTS for state in dependency_states):
                    self.todo[intent_id]["status"] = "blocked"
                    continue
                if all(state == "completed" for state in dependency_states):
                    self.todo[intent_id]["status"] = "ready"
                    ready.append(intent)
                else:
                    self.todo[intent_id]["status"] = "waiting"
            return [item["id"] for item in sorted(ready, key=lambda item: (-item["priority"], item["id"]))]

    def claim(self, intent_id: str, worker_id: str, *, lease_id: str = "", ttl: float = 600) -> str:
        with self.lock:
            intent = self.intents[intent_id]
            if intent["status"] != "pending":
                raise RuntimeError(f"Intent {intent_id} 不可领取：{intent['status']}")
            lease = lease_id or uuid.uuid4().hex
            now = time.time()
            intent.update({
                "status": "running", "worker_id": worker_id, "lease_id": lease,
                "claimed_at": now, "deadline": now + max(1.0, float(ttl)),
                "attempt": int(intent["attempt"]) + 1, "error": "",
            })
            self.todo[intent_id]["status"] = "in_progress"
            self.exploration["nodes"][f"intent:{intent_id}"]["status"] = "running"
            self._trace(intent_id, worker_id, "intent.start", f"{worker_id} 领取 Intent", "", True)
            self.emit("intent.claimed", intent_id=intent_id, worker_id=worker_id, lease_id=lease)
            return lease

    def settle(self, intent_id: str, lease_id: str, status: str, *, error: str = "") -> None:
        if status not in TERMINAL_INTENTS:
            raise ValueError(f"非法 Intent 终态: {status}")
        with self.lock:
            intent = self.intents[intent_id]
            if intent["status"] != "running" or intent["lease_id"] != lease_id:
                raise RuntimeError("Intent 租约不匹配")
            worker = intent["worker_id"]
            intent.update({"status": status, "lease_id": "", "deadline": 0.0, "error": error})
            self.todo[intent_id]["status"] = status
            self.exploration["nodes"][f"intent:{intent_id}"]["status"] = status
            self._trace(intent_id, worker, "intent.end", status, error, status == "completed")
            self.emit("intent.settled", intent_id=intent_id, status=status, error=error[:300])

    def block(self, intent_id: str, reason: str) -> None:
        self.terminate_unclaimed(intent_id, "blocked", reason)

    def terminate_unclaimed(self, intent_id: str, status: str, reason: str) -> None:
        if status not in TERMINAL_INTENTS:
            raise ValueError(f"非法 Intent 终态: {status}")
        with self.lock:
            intent = self.intents[intent_id]
            if intent["status"] in TERMINAL_INTENTS:
                return
            if intent["status"] == "running":
                raise RuntimeError("运行中的 Intent 必须使用 settle 并校验租约")
            intent.update({"status": status, "error": reason, "lease_id": "", "deadline": 0.0})
            self.todo[intent_id]["status"] = status
            self.exploration["nodes"][f"intent:{intent_id}"]["status"] = status
            self._trace(intent_id, "commander", f"intent.{status}", reason, reason, False)
            self.emit(f"intent.{status}", intent_id=intent_id, reason=reason[:300])

    def recover_inflight(self, *, reason: str = "运行恢复，回收失效租约") -> list[str]:
        with self.lock:
            recovered = []
            for intent_id, intent in self.intents.items():
                if intent["status"] != "running":
                    continue
                worker = intent["worker_id"]
                intent.update({"status": "pending", "worker_id": "", "lease_id": "", "deadline": 0.0, "error": reason})
                self.todo[intent_id]["status"] = "ready"
                self.exploration["nodes"][f"intent:{intent_id}"]["status"] = "pending"
                self._trace(intent_id, worker, "intent.recovered", reason, reason, False)
                recovered.append(intent_id)
            if recovered:
                self.emit("intent.recovered", intents=recovered)
            return recovered

    def sync_records(self, records: dict[str, Any]) -> None:
        """兼容旧检查点：以 Commander 已落盘终态补齐 Intent/Todo 状态。"""
        with self.lock:
            for intent_id, record in records.items():
                if intent_id not in self.intents:
                    continue
                status = str(getattr(record, "status", "pending"))
                if status not in TERMINAL_INTENTS:
                    continue
                self.intents[intent_id].update({"status": status, "lease_id": "", "deadline": 0.0,
                                                "error": str(getattr(record, "error", ""))})
                self.todo[intent_id]["status"] = status
                self.exploration["nodes"][f"intent:{intent_id}"]["status"] = status

    def expire_leases(self, now: float | None = None) -> list[str]:
        """回收没有活跃执行器的过期租约；实时执行器仍由自身 max_seconds 收尾。"""
        moment = time.time() if now is None else now
        with self.lock:
            expired = [key for key, item in self.intents.items()
                       if item["status"] == "running" and item["deadline"] and item["deadline"] <= moment]
            for intent_id in expired:
                intent = self.intents[intent_id]
                worker = intent["worker_id"]
                intent.update({"status": "pending", "worker_id": "", "lease_id": "", "deadline": 0.0,
                               "error": "Intent lease timeout"})
                self.todo[intent_id]["status"] = "ready"
                self.exploration["nodes"][f"intent:{intent_id}"]["status"] = "pending"
                self._trace(intent_id, worker, "intent.timeout", "租约超时，已退回队列", "Intent lease timeout", False)
            if expired:
                self.emit("intent.timeout", intents=expired)
            return expired

    def publish_state(self, intent_id: str, state: Any, *, evidence_refs: Iterable[str] = (), include_steps: bool = True) -> list[str]:
        """把实际 RunState 投影成可复用事实和可检索步骤，不复制 Worker 对话。"""
        refs = [str(item) for item in evidence_refs]
        added: list[str] = []
        with self.lock:
            for index, step in enumerate(list(getattr(state, "steps", []) or []) if include_steps else [], 1):
                action = str(step.get("action") or "step")
                summary = str(step.get("summary") or step.get("error") or action)[:300]
                detail = _canonical({key: step.get(key) for key in ("phase", "action", "summary", "error", "ok") if key in step})
                self._trace(intent_id, str(self.intents[intent_id].get("worker_id") or "worker"),
                            "tool" if action not in {"final", "final_rejected"} else action,
                            summary, detail, bool(step.get("ok", not step.get("error"))), step_id=index)
            candidates: list[tuple[str, dict[str, Any], list[str]]] = []
            for host, host_state in sorted(getattr(state, "hosts", {}).items()):
                payload = {
                    "host": host, "alive": host_state.alive,
                    "ports": {str(port): value for port, value in sorted(host_state.ports.items())},
                    "technologies": list(host_state.technologies), "web_paths": list(host_state.web_paths),
                }
                if payload["alive"] is not None or payload["ports"] or payload["technologies"] or payload["web_paths"]:
                    candidates.append(("asset", payload, refs))
            for finding in getattr(state, "findings", []) or []:
                if finding.status not in {"verified", "exploited"} or not finding.evidence:
                    continue
                candidates.append(("finding", finding.to_dict(), refs + [str(item) for item in finding.evidence]))
            for credential in getattr(state, "credentials", []) or []:
                if credential.verified:
                    candidates.append(("credential", credential.to_dict(), refs))
            for flag in getattr(state, "flags", []) or []:
                candidates.append(("flag", dict(flag), refs))
            for kind, payload, evidence in candidates:
                fact_id = _stable_id("fact-", {"kind": kind, "payload": payload})
                row = self.facts.get(fact_id)
                if row is None:
                    row = {"id": fact_id, "kind": kind, "payload": payload, "sources": [], "evidence_refs": [], "created_at": time.time()}
                    self.facts[fact_id] = row
                    self.exploration["nodes"][fact_id] = {"id": fact_id, "kind": "fact_ref", "summary": self._fact_summary(row)}
                    added.append(fact_id)
                if intent_id not in row["sources"]:
                    row["sources"].append(intent_id)
                for ref in evidence:
                    if ref and ref not in row["evidence_refs"]:
                        row["evidence_refs"].append(ref)
                anchor = {"intent": f"intent:{intent_id}", "fact": fact_id}
                if anchor not in self.exploration["anchors"]:
                    self.exploration["anchors"].append(anchor)
                edge = {"src": f"intent:{intent_id}", "dst": fact_id, "rel": "yields"}
                if edge not in self.exploration["edges"]:
                    self.exploration["edges"].append(edge)
            self.todo[intent_id]["evidence_refs"] = list(dict.fromkeys(refs + added))
            if added:
                self.emit("facts.published", intent_id=intent_id, facts=added)
            return added

    def observe_tool(self, event: Any) -> None:
        """Commander 线程消费统一事件，实时建立工具观察和失败索引。"""
        with self.lock:
            payload = event.payload
            self._trace(event.task_id, event.worker_id, "tool", str(payload.get("summary") or payload.get("error") or payload.get("tool", "")),
                        _canonical({"call_id": event.call_id, **payload}), bool(payload.get("ok")))

    def _trace(self, intent_id: str, worker_id: str, kind: str, summary: str, detail: str,
               ok: bool, *, step_id: int | None = None) -> None:
        self.trace_seq += 1
        row = {
            "id": self.trace_seq, "intent_id": intent_id, "worker_id": worker_id,
            "step_id": step_id, "kind": kind, "summary": summary[:500], "detail": detail[:4000],
            "ok": bool(ok), "time": time.time(),
        }
        self.traces.append(row)
        del self.traces[:-5000]

    def search_traces(self, query: str, *, exclude_intent: str = "", limit: int = 8) -> list[dict[str, Any]]:
        terms = [item for item in query.casefold().split() if item]
        with self.lock:
            scored = []
            for row in self.traces:
                if exclude_intent and row["intent_id"] == exclude_intent:
                    continue
                haystack = f"{row['summary']} {row['detail']}".casefold()
                score = sum(2 if term in str(row["summary"]).casefold() else 1 for term in terms if term in haystack)
                if terms and not score:
                    continue
                scored.append((score + (0 if row["ok"] else 1), row))
            selected = sorted(scored, key=lambda item: (-item[0], -int(item[1]["id"])))[:max(1, min(int(limit), 20))]
            return [{key: row[key] for key in ("id", "intent_id", "worker_id", "step_id", "kind", "summary", "ok", "time")}
                    for _, row in selected]

    def get_trace(self, intent_id: str, *, step_ids: Iterable[int] = (), limit: int = 20) -> list[dict[str, Any]]:
        wanted = {int(item) for item in step_ids}
        with self.lock:
            rows = [dict(row) for row in self.traces if row["intent_id"] == intent_id
                    and (not wanted or row.get("step_id") in wanted)]
            return rows[-max(1, min(int(limit), 50)):]

    def experience_for(self, intent_id: str, *, limit: int = 8) -> dict[str, Any]:
        with self.lock:
            rows = [row for row in self.traces if row["intent_id"] == intent_id]
            observations = [row["summary"] for row in rows if row["kind"] in {"tool", "intent.end"} and row["ok"]][-limit:]
            failures = [row["summary"] + ((": " + row["detail"][:300]) if row["detail"] else "")
                        for row in rows if not row["ok"]][-limit:]
            facts = [dict(row) for row in self.facts.values() if intent_id in row["sources"] and row["kind"] != "flag"]
            return {
                "observations": observations,
                "failures": failures,
                "facts": [{"id": row["id"], "kind": row["kind"], "payload": row["payload"],
                           "evidence_refs": row["evidence_refs"]} for row in facts[:limit]],
            }

    @staticmethod
    def _fact_summary(row: dict[str, Any]) -> str:
        payload = row.get("payload") or {}
        return str(payload.get("title") or payload.get("host") or payload.get("target") or row.get("kind") or "fact")[:300]

    def overview(self) -> dict[str, Any]:
        with self.lock:
            return {
                "planner_rounds": self.planner_rounds,
                "revision": self.revision,
                "todolist": [dict(item) for item in self.todo.values()],
                "intent_counts": {status: sum(item["status"] == status for item in self.intents.values())
                                  for status in sorted({item["status"] for item in self.intents.values()})},
                "fact_counts": {kind: sum(item["kind"] == kind for item in self.facts.values())
                                for kind in sorted({item["kind"] for item in self.facts.values()})},
            }

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(_canonical({
                "version": self.VERSION, "todo": self.todo, "intents": self.intents,
                "facts": self.facts, "exploration": self.exploration, "traces": self.traces,
                "events": self.events, "revision": self.revision,
                "planned_revision": self.planned_revision, "planner_rounds": self.planner_rounds,
                "trace_seq": self.trace_seq,
            }))

    @classmethod
    def restore(cls, tasks: Iterable[Any], data: dict[str, Any] | None) -> "CoordinationRuntime":
        runtime = cls(tasks)
        if not data:
            return runtime
        if int(data.get("version", 0)) != cls.VERSION:
            raise ValueError("不支持的 coordination 检查点版本")
        with runtime.lock:
            for name in ("todo", "intents", "facts", "exploration", "traces", "events"):
                setattr(runtime, name, json.loads(_canonical(data.get(name, getattr(runtime, name)))))
            runtime.revision = int(data.get("revision", 0))
            runtime.planned_revision = int(data.get("planned_revision", -1))
            runtime.planner_rounds = int(data.get("planner_rounds", 0))
            runtime.trace_seq = int(data.get(
                "trace_seq",
                max((int(item.get("id", 0)) for item in runtime.traces), default=0),
            ))
        return runtime


__all__ = ["CoordinationRuntime", "FAILED_INTENTS", "TERMINAL_INTENTS"]
