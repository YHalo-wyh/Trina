"""事件驱动的多 Agent 协作状态：共享待办、Intent 队列、双图与执行留痕。"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from typing import Any, Iterable
from urllib.parse import urlsplit


TERMINAL_INTENTS = {"completed", "failed", "blocked", "skipped", "timed_out", "cancelled"}
FAILED_INTENTS = TERMINAL_INTENTS - {"completed"}
SCHEDULING_EVENTS = {
    "runtime.seeded",
    "intent.added",
    "intent.settled",
    "intent.retry",
    "intent.blocked",
    "intent.failed",
    "intent.skipped",
    "intent.timed_out",
    "intent.cancelled",
    "intent.completed",
    "intent.recovered",
    "intent.timeout",
    "facts.published",
    "worker.idle",
    "direction.invalid",
    "intent.stalled",
    "intent.progress",
}
_EVENT_COALESCE_SECONDS = 0.05


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _stable_id(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:16]


def _fact_id(kind: str, payload: dict[str, Any]) -> str:
    """观察时钟变化不是新事实；保留证据正文、执行条件及凭据的真实差异。"""
    stable = {key: value for key, value in payload.items() if key not in {"ts", "first_seen", "last_seen"}}
    if kind == "finding":
        stable["evidence"] = [{key: value for key, value in item.items() if key not in {"ts", "clock"}}
                              if isinstance(item, dict) else item for item in payload.get("evidence", [])]
    return _stable_id("fact-", {"kind": kind, "payload": stable})


def _task_data(task: Any) -> dict[str, Any]:
    return {
        "id": str(task.id),
        "target": str(task.target),
        "goal": str(task.goal),
        "depends_on": list(task.depends_on),
        "priority": int(task.priority),
        "max_steps": int(task.max_steps),
        "max_seconds": int(task.max_seconds),
        "capabilities": list(task.capabilities),
        "max_attempts": int(getattr(task, "max_attempts", 1)),
        "context_key": str(getattr(task, "context_key", "")),
        "deliverable": str(getattr(task, "deliverable", "")),
    }


def task_signature(task: Any) -> str:
    """执行身份保留路径大小写、依赖输入、交付约束和显式模型条件。"""
    row = _task_data(task)
    return _stable_id("sig-", {"target": row["target"].strip(),
        "goal": " ".join(row["goal"].split()).casefold(),
        **{key: sorted(row[key]) for key in ("capabilities", "depends_on")},
        **{key: str(getattr(task, key, "")) for key in
           ("context_key", "deliverable", "model", "profile", "reasoning_effort")}})


def _terms(value: str) -> set[str]:
    """提取轻量语义词，供事实相关性和重复工作判断使用。"""
    text = str(value or "").casefold()
    terms = set(re.findall(r"[a-z0-9_.+-]{2,}", text))
    stop = {"目标", "验证", "检查", "分析", "发现", "结果", "任务", "前序"}
    for segment in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        terms.update(segment[index:index + 2] for index in range(len(segment) - 1))
    return {item for item in terms if item not in stop}


def _target_host(value: str) -> str:
    text = str(value or "").strip()
    parsed = urlsplit(text if "://" in text else "//" + text)
    return str(parsed.hostname or text).casefold()


def _work_descriptor(intent: dict[str, Any]) -> tuple[str, frozenset[str], frozenset[str]]:
    return (
        _target_host(intent.get("target", "")),
        frozenset(intent.get("capabilities") or ()),
        frozenset(_terms(intent.get("goal", ""))),
    )


def _similar_work(
    source: tuple[str, frozenset[str], frozenset[str]],
    destination: tuple[str, frozenset[str], frozenset[str]],
) -> bool:
    source_host, source_capabilities, left = source
    destination_host, destination_capabilities, right = destination
    if source_host != destination_host:
        return False
    if source_capabilities and destination_capabilities and not source_capabilities & destination_capabilities:
        return False
    return bool(left and right and len(left & right) / max(1, min(len(left), len(right))) >= 0.75)


class CoordinationRuntime:
    """Commander 单写、多 Worker 只读的轻量协作平面。

    Planner 只在 ``emit()`` 产生的新事件后推进一轮；普通工具步骤只写 trace，
    不会唤醒 Planner。事实图只保存工具实际产出的可复用事实，探索图保存尚未
    完成的 Intent、依赖和结果血缘，两者通过 anchors 连接。
    """

    VERSION = 2

    def __init__(self, tasks: Iterable[Any], *, event_bus: Any = None):
        self.event_bus = event_bus
        self.lock = threading.RLock()
        self.todo: dict[str, dict[str, Any]] = {}
        self.intents: dict[str, dict[str, Any]] = {}
        self.facts: dict[str, dict[str, Any]] = {}
        self._fact_ids: dict[str, str] = {}
        self.exploration: dict[str, Any] = {"nodes": {}, "edges": [], "anchors": []}
        self.traces: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.planned_facts: set[str] = set()
        self.trace_seq = 0
        self.revision = 0
        self.planned_revision = -1
        self.planner_rounds = 0
        self.schedule_revision = 0
        self.planned_schedule_revision = -1
        self.priority_snapshot: dict[str, dict[str, Any]] = {}
        self._ready_order: list[str] = []
        self._planned_state_signature = ""
        self._direction_revision = 0
        self._recent_schedule_events: dict[str, float] = {}
        self.coalesced_events = 0
        self.duplicate_tasks = 0
        self._dirty_tasks: set[str] = set()
        self._fact_counts: dict[str, int] = {}
        self._fact_revision = 0
        for task in tasks:
            self.add_intent(task, notify=False)
        self.emit("runtime.seeded", count=len(self.intents))

    def add_intent(self, task: Any, *, notify: bool = True) -> bool:
        row = _task_data(task)
        signature = task_signature(task)
        with self.lock:
            if row["id"] in self.intents or any(item["signature"] == signature for item in self.intents.values()):
                self.duplicate_tasks += 1
                return False
            self.intents[row["id"]] = {
                **row,
                "signature": signature,
                "queue_index": len(self.intents),
                "status": "pending",
                "attempt": 0,
                "worker_id": "",
                "lease_id": "",
                "claimed_at": 0.0,
                "deadline": 0.0,
                "error": "",
                "queued_at": time.time(), "effective_progress": 0, "last_progress_at": 0.0,
                "no_progress_actions": 0, "lifecycle": "BLOCKED" if row["depends_on"] else "QUEUED",
                "model_requests": 0, "tokens": 0, "execution_seconds": 0.0,
            }
            self._dirty_tasks.add(row["id"])
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
                self.emit("intent.added", intent_id=row["id"], reason="接收明确目标和执行条件", deliverable=row["deliverable"] or row["goal"])
            return True

    def emit(self, kind: str, *, intent_id: str = "", **payload: Any) -> None:
        with self.lock:
            now = time.monotonic()
            self.revision += 1
            self.events.append({
                "revision": self.revision, "kind": kind, "intent_id": intent_id,
                "time": time.time(), "payload": payload,
            })
            del self.events[:-2000]
            if kind in SCHEDULING_EVENTS:
                affected = {intent_id} if intent_id else set()
                affected.update(payload.get("intents", ()))
                if kind == "runtime.seeded":
                    affected.update(self.intents)
                if kind == "facts.published":
                    for key, item in self.intents.items():
                        if any(self._fact_relevant(item, self.facts[fid]) for fid in payload.get("facts", ()) if fid in self.facts):
                            affected.add(key)
                            self._fact_counts.pop(key, None)
                elif intent_id in self.intents:
                    source = self.intents[intent_id]
                    affected.update(key for key, item in self.intents.items()
                                    if intent_id in item["depends_on"] or key in source["depends_on"]
                                    or (_target_host(item["target"]) == _target_host(source["target"]) and item.get("context_key") == source.get("context_key")))
                self._dirty_tasks.update(affected)
                fingerprint = _stable_id("event-", {"kind": kind, "intent_id": intent_id, "payload": payload})
                previous = self._recent_schedule_events.get(fingerprint, 0.0)
                if now - previous >= _EVENT_COALESCE_SECONDS:
                    self.schedule_revision += 1
                    if kind == "direction.invalid":
                        self._direction_revision += 1
                    self._recent_schedule_events[fingerprint] = now
                else:
                    self.coalesced_events += 1
                self._recent_schedule_events = {
                    key: moment for key, moment in self._recent_schedule_events.items()
                    if now - moment < _EVENT_COALESCE_SECONDS
                }
            if self.event_bus is not None:
                self.event_bus.publish("coordination." + kind, source="commander", task_id=intent_id,
                                       revision=self.revision, **payload)

    def _relevant_fact_count(self, intent: dict[str, Any]) -> int:
        key = intent["id"]
        if key not in self._fact_counts:
            self._fact_counts[key] = sum(self._fact_relevant(intent, fact) for fact in self.facts.values())
        return self._fact_counts[key]

    @staticmethod
    def _fact_relevant(intent: dict[str, Any], fact: dict[str, Any]) -> bool:
        target_host = _target_host(str(intent.get("target") or ""))
        goal_terms = _terms(str(intent.get("goal") or ""))
        dependencies = set(intent.get("depends_on") or ())
        sources = set(fact.get("sources") or ())
        payload = fact.get("payload") if isinstance(fact.get("payload"), dict) else {}
        encoded = _canonical(payload).casefold()
        return bool(dependencies & sources or (target_host and target_host in encoded) or goal_terms & _terms(encoded))

    def _schedule_state_signature(self) -> str:
        """只描述会改变调度结论的状态，忽略重复唤醒和展示事件。"""
        return _stable_id("schedule-", {
            "intents": [
                {
                    "id": item["id"], "status": item["status"], "attempt": item.get("attempt", 0),
                    "priority": item.get("priority", 0), "depends_on": item.get("depends_on", ()),
                    "max_steps": item.get("max_steps", 0), "max_seconds": item.get("max_seconds", 0),
                    "progress": item.get("effective_progress", 0), "stalled": item.get("lifecycle") == "STALLED",
                    "invalid": item.get("direction_invalid", False),
                }
                for item in self.intents.values()
            ],
            "facts": sorted(self.facts),
            "fact_revision": self._fact_revision,
        })

    def _dynamic_priority(
        self,
        intent: dict[str, Any],
        downstream_counts: dict[str, int],
        duplicate_penalties: dict[str, int],
    ) -> dict[str, Any]:
        """用现有任务、事实和依赖字段计算确定性调度分数。"""
        importance = int(intent.get("priority") or 0) * 10_000
        progress = min(int(intent.get("effective_progress") or 0), 5) * 20
        likelihood = -min(max(0, int(intent.get("attempt") or 0) - 1), 5) * 40
        stalled = -150 if intent.get("stalled_at_progress", -1) == intent.get("effective_progress", 0) else 0
        downstream = min(4, downstream_counts.get(intent["id"], 0)) * 40
        evidence = min(self._relevant_fact_count(intent), 8) * 50
        max_steps = max(0, int(intent.get("max_steps") or 0))
        max_seconds = max(0, int(intent.get("max_seconds") or 0))
        cost_units = max_steps + ((max_seconds + 59) // 60)
        cost = min(cost_units if cost_units else 8, 100)
        duplicate = duplicate_penalties.get(intent["id"], 0)
        score = importance + progress + downstream + evidence + likelihood + stalled - cost - duplicate
        return {
            "score": score,
            "base_priority": int(intent.get("priority") or 0),
            "progress": progress,
            "completion_likelihood": likelihood, "stalled_penalty": stalled,
            "dependency_value": downstream,
            "new_information": evidence,
            "estimated_cost": cost,
            "duplicate_work": -duplicate,
            "queue_index": int(intent.get("queue_index") or 0),
        }

    def plan(self) -> list[str]:
        """仅在关键调度事件后重算顺序，并返回当前可领取 Intent。"""
        with self.lock:
            event_dirty = self.planned_schedule_revision != self.schedule_revision or bool(self._dirty_tasks)
            state_signature = self._schedule_state_signature() if event_dirty else self._planned_state_signature
            replan = event_dirty and state_signature != self._planned_state_signature
            if event_dirty:
                self.planned_schedule_revision = self.schedule_revision
                if not replan:
                    self._dirty_tasks.clear()
            if replan:
                self._planned_state_signature = state_signature
                self.planned_revision = self.revision
                self.planner_rounds += 1
                self._trace("planner", "planner", "round", f"处理至调度修订 {self.schedule_revision}", "", True)
            ready: list[dict[str, Any]] = []
            for intent_id, intent in self.intents.items():
                status = str(intent["status"])
                if status != "pending":
                    if intent_id in self.todo and status in TERMINAL_INTENTS | {"running"}:
                        self.todo[intent_id]["status"] = "in_progress" if status == "running" else status
                    continue
                dependency_states = [str(self.intents[dep]["status"]) for dep in intent["depends_on"]]
                if any(state in FAILED_INTENTS for state in dependency_states):
                    self.todo[intent_id]["status"] = "blocked"
                    continue
                if all(state == "completed" for state in dependency_states):
                    self.todo[intent_id]["status"] = "ready"
                    intent["lifecycle"] = "QUEUED"
                    ready.append(intent)
                else:
                    self.todo[intent_id]["status"] = "waiting"
                    intent["lifecycle"] = "BLOCKED"
            if replan:
                ready_keys = {item["id"] for item in ready}
                self.priority_snapshot = {key: value for key, value in self.priority_snapshot.items() if key in ready_keys}
                downstream_counts = {intent_id: 0 for intent_id in self.intents}
                for other in self.intents.values():
                    if other.get("status") != "pending":
                        continue
                    for dependency in other.get("depends_on", ()):
                        downstream_counts[dependency] = downstream_counts.get(dependency, 0) + 1
                duplicate_penalties: dict[str, int] = {}
                descriptors = {intent_id: _work_descriptor(item) for intent_id, item in self.intents.items()}
                intents_by_host: dict[str, list[dict[str, Any]]] = {}
                for other in self.intents.values():
                    intents_by_host.setdefault(descriptors[other["id"]][0], []).append(other)
                previous_by_host: dict[str, list[dict[str, Any]]] = {}
                for item in ready:
                    descriptor = descriptors[item["id"]]
                    host = descriptor[0]
                    if any(
                        item.get("context_key") == previous.get("context_key") and _similar_work(descriptor, descriptors[previous["id"]])
                        for previous in previous_by_host.get(host, ())
                    ):
                        duplicate_penalties[item["id"]] = 100
                    for other in intents_by_host.get(host, ()):
                        if (other["id"] == item["id"] or item.get("context_key") != other.get("context_key")
                                or not _similar_work(descriptor, descriptors[other["id"]])):
                            continue
                        if other.get("status") == "running":
                            duplicate_penalties[item["id"]] = max(300, duplicate_penalties.get(item["id"], 0))
                        elif other.get("status") == "completed":
                            duplicate_penalties[item["id"]] = max(200, duplicate_penalties.get(item["id"], 0))
                    previous_by_host.setdefault(host, []).append(item)
                changes = []
                for item in ready:
                    key = item["id"]
                    if key in self._dirty_tasks or key not in self.priority_snapshot:
                        previous = self.priority_snapshot.get(key)
                        self.priority_snapshot[key] = self._dynamic_priority(item, downstream_counts, duplicate_penalties)
                        if previous is not None and previous != self.priority_snapshot[key]:
                            changes.append({"intent_id": key, "previous": previous, "components": self.priority_snapshot[key]})
                if changes:
                    # 初次评分已随领取事件记录；实际调整按批次审计，避免每个候选单独写盘。
                    self.emit("priority.updated", changes=changes, reason="任务状态或相关证据变化")
                self._dirty_tasks.clear()
                self._ready_order = [
                    item["id"] for item in sorted(
                        ready,
                        key=lambda item: (
                            -int(self.priority_snapshot[item["id"]]["score"]),
                            int(item.get("queue_index") or 0),
                        ),
                    )
                ]
            ready_ids = {item["id"] for item in ready}
            return [intent_id for intent_id in self._ready_order if intent_id in ready_ids]

    def claim(self, intent_id: str, worker_id: str, *, lease_id: str = "", ttl: float = 600) -> str:
        if ttl < 0:
            raise ValueError("ttl 必须非负，0 随任务释放")
        with self.lock:
            intent = self.intents[intent_id]
            if intent["status"] != "pending":
                raise RuntimeError(f"Intent {intent_id} 不可领取：{intent['status']}")
            if any(self.intents[dep]["status"] != "completed" for dep in intent["depends_on"]):
                raise RuntimeError("Intent 前置依赖尚未完成")
            lease = lease_id or uuid.uuid4().hex
            now = time.time()
            intent.update({
                "status": "running", "worker_id": worker_id, "lease_id": lease,
                "claimed_at": now, "deadline": now + float(ttl) if ttl else 0.0,
                "attempt": int(intent["attempt"]) + 1, "error": "",
                "lifecycle": "RUNNING", "last_progress_at": now, "no_progress_actions": 0, "stop_loss_requested": False,
                "queue_seconds": float(intent.get("queue_seconds", 0)) + max(0, now - intent.get("queued_at", now)),
            })
            self.todo[intent_id]["status"] = "in_progress"
            self.exploration["nodes"][f"intent:{intent_id}"]["status"] = "running"
            self._trace(intent_id, worker_id, "intent.start", f"{worker_id} 领取 Intent", "", True)
            self.emit("intent.claimed", intent_id=intent_id, worker_id=worker_id, lease_id=lease,
                      reason="依赖已完成，按动态优先级和可用槽位领取", priority=self.priority_snapshot.get(intent_id, {}))
            return lease

    def settle(self, intent_id: str, lease_id: str, status: str, *, error: str = "") -> None:
        if status not in TERMINAL_INTENTS:
            raise ValueError(f"非法 Intent 终态: {status}")
        with self.lock:
            intent = self.intents[intent_id]
            if intent["status"] != "running" or intent["lease_id"] != lease_id:
                raise RuntimeError("Intent 租约不匹配")
            worker = intent["worker_id"]
            intent.update({"status": status, "lifecycle": status.upper(), "lease_id": "", "deadline": 0.0, "error": error})
            self.todo[intent_id]["status"] = status
            self.exploration["nodes"][f"intent:{intent_id}"]["status"] = status
            self._trace(intent_id, worker, "intent.end", status, error, status == "completed")
            self.emit("intent.settled", intent_id=intent_id, status=status, error=error[:300])

    def retry(self, intent_id: str, lease_id: str, reason: str) -> None:
        """回收异常 Worker 的租约并把 Intent 重新放回 ready 队列。"""
        with self.lock:
            intent = self.intents[intent_id]
            if intent["status"] != "running" or intent["lease_id"] != lease_id:
                raise RuntimeError("Intent 租约不匹配")
            worker = intent["worker_id"]
            intent.update({
                "status": "pending", "worker_id": "", "lease_id": "", "deadline": 0.0,
                "error": reason[:500], "lifecycle": "QUEUED", "queued_at": time.time(),
            })
            self.todo[intent_id]["status"] = "ready"
            self.exploration["nodes"][f"intent:{intent_id}"]["status"] = "pending"
            self._trace(intent_id, worker, "intent.retry", "Worker 异常，重新排队", reason, False)
            self.emit("intent.retry", intent_id=intent_id, worker_id=worker, reason=reason[:300])

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
            intent.update({"status": status, "lifecycle": status.upper(), "error": reason, "lease_id": "", "deadline": 0.0})
            self.todo[intent_id]["status"] = status
            self.exploration["nodes"][f"intent:{intent_id}"]["status"] = status
            self._trace(intent_id, "commander", f"intent.{status}", reason, "" if status == "completed" else reason, status == "completed")
            self.emit(f"intent.{status}", intent_id=intent_id, reason=reason[:300])

    def recover_inflight(self, *, reason: str = "运行恢复，回收失效租约") -> list[str]:
        with self.lock:
            recovered = []
            for intent_id, intent in self.intents.items():
                if intent["status"] != "running":
                    continue
                worker = intent["worker_id"]
                intent.update({"status": "pending", "lifecycle": "QUEUED", "queued_at": time.time(),
                               "worker_id": "", "lease_id": "", "deadline": 0.0, "error": reason})
                self.todo[intent_id]["status"] = "ready"
                self.exploration["nodes"][f"intent:{intent_id}"]["status"] = "pending"
                self._trace(intent_id, worker, "intent.recovered", reason, reason, False)
                recovered.append(intent_id)
            if recovered:
                self.emit("intent.recovered", intents=recovered)
            return recovered

    def import_legacy_records(self, records: dict[str, Any]) -> None:
        """只在恢复 v1-v3 检查点时把旧任务副本迁入权威 Intent。"""
        with self.lock:
            for intent_id, record in records.items():
                if intent_id not in self.intents:
                    continue
                source = record if isinstance(record, dict) else vars(record)
                for key in (
                    "status", "attempt", "worker_id", "lease_id", "started_at", "ended_at",
                    "result", "error", "capability_lease_id", "capability_ids",
                    "capability_snapshot", "evidence_refs", "model", "reasoning_effort", "profile",
                ):
                    if key in source:
                        self.intents[intent_id][key] = source[key]
                status = str(self.intents[intent_id].get("status") or "pending")
                self.todo[intent_id]["status"] = "in_progress" if status == "running" else status
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
                intent.update({"status": "pending", "lifecycle": "QUEUED", "queued_at": moment,
                               "worker_id": "", "lease_id": "", "deadline": 0.0,
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
        changed: list[str] = []
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
            for verification in getattr(state, "casebook", {}).get("goal_verifications", []):
                if verification.get("status") == "passed" and verification.get("evidence_id"):
                    candidates.append(("verification", {"target": state.target, "status": "passed",
                        "check_digest": hashlib.sha256(str(verification.get("verifier", "")).encode()).hexdigest()},
                        [str(verification["evidence_id"])]))
            for kind, payload, evidence in candidates:
                identity = _fact_id(kind, payload)
                fact_id = self._fact_ids.get(identity, identity)
                self._fact_ids[identity] = fact_id
                row = self.facts.get(fact_id)
                if row is None:
                    row = {"id": fact_id, "kind": kind, "payload": payload, "sources": [], "evidence_refs": [], "created_at": time.time()}
                    self.facts[fact_id] = row
                    self.exploration["nodes"][fact_id] = {"id": fact_id, "kind": "fact_ref", "summary": self._fact_summary(row)}
                    added.append(fact_id)
                if intent_id not in row["sources"]:
                    row["sources"].append(intent_id)
                    changed.append(fact_id)
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
                self.record_progress(intent_id, "获得新的可验证事实", count=len(added))
            if changed:
                self._fact_revision += 1
                self.emit("facts.published", intent_id=intent_id, facts=changed)
            return added

    def record_progress(self, intent_id: str, reason: str, *, count: int = 1) -> None:
        """只有新增事实才计有效进展，工具心跳和模型文本不重置进展时钟。"""
        with self.lock:
            item = self.intents[intent_id]
            item["effective_progress"] = int(item.get("effective_progress", 0)) + count
            item["last_progress_at"] = time.time()
            item["no_progress_actions"] = 0
            if item.get("lifecycle") == "STALLED":
                item["lifecycle"] = "RUNNING"
            item["direction_invalid"] = False
            self.emit("intent.progress", intent_id=intent_id, reason=reason, count=item["effective_progress"])

    def observe_usage(self, event: Any) -> None:
        """按事件序号计费一次；重放、恢复和重复通知不会重复累加。"""
        with self.lock:
            item = self.intents[event.task_id]
            if event.seq <= item.get("usage_seq", 0):
                return
            item["usage_seq"] = event.seq
            if event.kind == "model.started":
                item["model_requests"] = int(item.get("model_requests", 0)) + 1
            elif event.kind == "model.finished":
                usage = event.payload.get("usage") or {}
                item["tokens"] = int(item.get("tokens", 0)) + int(usage.get("total_tokens") or
                    (int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))))

    def invalidate_direction(self, intent_id: str, reason: str, evidence: list[str]) -> bool:
        """必须携带已知证据，才允许将方向标记为失效。"""
        with self.lock:
            item = self.intents[intent_id]
            if item.get("direction_invalid") or not reason or not evidence:
                return False
            known = {str(row["id"]) for row in self.traces if row["intent_id"] == intent_id and not row["ok"]}
            known.update(key for key, fact in self.facts.items() if intent_id in fact["sources"])
            if not set(evidence) <= known:
                return False
            item.update(direction_invalid=True, invalid_reason=reason, invalid_evidence=list(evidence))
            self.emit("direction.invalid", intent_id=intent_id, reason=reason, evidence=evidence)
            return True

    def assess_progress(self, *, now: float | None = None, stale_seconds: float = 300.0,
                        min_actions: int = 3) -> list[str]:
        """长时间无事实且多次无效动作才评估；静默长调用不能据此判失败。"""
        moment = time.time() if now is None else now
        stalled = []
        with self.lock:
            for key, item in self.intents.items():
                if item["status"] != "running" or item.get("lifecycle") == "STALLED":
                    continue
                last = item.get("last_progress_at") or item.get("claimed_at") or moment
                if moment - last < stale_seconds or item.get("no_progress_actions", 0) < min_actions:
                    continue
                item["lifecycle"] = "STALLED"
                item["stalled_at_progress"] = item.get("effective_progress", 0)
                stalled.append(key)
                self.emit("intent.stalled", intent_id=key, reason="持续无有效进展且重复或失败动作达到阈值",
                          seconds_without_progress=moment - last, actions=item["no_progress_actions"],
                          evidence=item.get("recent_failures", []))
        return stalled

    def observe_tool(self, event: Any) -> None:
        """Commander 线程消费统一事件，实时建立工具观察和失败索引。"""
        with self.lock:
            payload = event.payload
            self._trace(event.task_id, event.worker_id, "tool", str(payload.get("summary") or payload.get("error") or payload.get("tool", "")),
                        _canonical({"call_id": event.call_id, **payload}), bool(payload.get("ok")))
            item = self.intents[event.task_id]
            if event.seq <= item.get("tool_seq", 0):
                return
            item["tool_seq"] = event.seq
            outcome = _stable_id("outcome-", {key: payload.get(key) for key in ("tool", "summary", "error", "ok")})
            seen = item.setdefault("tool_outcomes", [])
            if not payload.get("ok") or outcome in seen:
                item["no_progress_actions"] = item.get("no_progress_actions", 0) + 1
            if not payload.get("ok"):
                failures = item.setdefault("recent_failures", [])
                failures.append(str(self.trace_seq))
                del failures[:-8]
            if outcome not in seen:
                seen.append(outcome)
                del seen[:-128]
            # 同一动作被执行护栏持续拒绝，是确定性的无效执行条件。
            if payload.get("status") == "denied":
                item["denied_streak"] = item.get("denied_streak", 0) + 1
                if item["denied_streak"] >= 3:
                    self.invalidate_direction(event.task_id, "连续动作被执行护栏拒绝，需要改变方向", item["recent_failures"][-3:])
            else:
                item["denied_streak"] = 0

    def unplanned_facts(self, fact_ids: Iterable[str]) -> list[dict[str, Any]]:
        """返回尚未驱动过规划的事实；调用方成功处理后再显式确认。"""
        with self.lock:
            return [dict(self.facts[fact_id]) for fact_id in fact_ids
                    if fact_id in self.facts and fact_id not in self.planned_facts]

    def mark_facts_planned(self, fact_ids: Iterable[str]) -> None:
        with self.lock:
            selected = {str(item) for item in fact_ids if str(item) in self.facts}
            new = selected - self.planned_facts
            if not new:
                return
            self.planned_facts.update(new)
            self.emit("facts.planned", facts=sorted(new))

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

    def worker_view(self, *, limit: int = 24, include_facts: bool = True) -> dict[str, Any]:
        """返回 Worker 按需读取的共享视图，不把别的任务事实写入本题 RunState。

        视图只包含事实索引、待办前沿和数量摘要。原始工具输出仍留在各自
        Worker 工件中，需要细节时通过 ``shared_trace_get`` 按 Intent 读取。
        """
        capped = max(1, min(int(limit), 100))
        with self.lock:
            ready = [
                {"id": item["id"], "summary": item["summary"],
                 "status": item["status"], "depends_on": list(item.get("depends_on", []))}
                for item in self.todo.values() if item.get("status") == "ready"
            ][:capped]
            running = [
                {"id": item["id"], "summary": item["summary"],
                 "status": item["status"], "worker_id": self.intents[item["id"]].get("worker_id", "")}
                for item in self.todo.values() if item.get("status") == "in_progress"
            ][:capped]
            facts: list[dict[str, Any]] = []
            if include_facts:
                for row in list(self.facts.values())[-capped:]:
                    facts.append({
                        "id": row["id"], "kind": row["kind"],
                        "summary": self._fact_summary(row),
                        "payload": row.get("payload", {}),
                        "sources": list(row.get("sources", [])),
                        "evidence_refs": list(row.get("evidence_refs", [])),
                    })
            return {
                "revision": self.revision,
                "planner_rounds": self.planner_rounds,
                "scheduler_model_requests": 0,
                "coalesced_events": self.coalesced_events,
                "priority_snapshot": json.loads(_canonical(self.priority_snapshot)),
                "ready": ready,
                "running": running,
                "facts": facts,
                "fact_counts": {kind: sum(item["kind"] == kind for item in self.facts.values())
                                for kind in sorted({item["kind"] for item in self.facts.values()})},
            }

    def overview(self) -> dict[str, Any]:
        with self.lock:
            return {
                "planner_rounds": self.planner_rounds,
                "scheduler_model_requests": 0,
                "coalesced_events": self.coalesced_events,
                "duplicate_tasks": self.duplicate_tasks,
                "resources": self.resource_stats(),
                "priority_snapshot": json.loads(_canonical(self.priority_snapshot)),
                "revision": self.revision,
                "todolist": [dict(item) for item in self.todo.values()],
                "intent_counts": {status: sum(item["status"] == status for item in self.intents.values())
                                  for status in sorted({item["status"] for item in self.intents.values()})},
                "fact_counts": {kind: sum(item["kind"] == kind for item in self.facts.values())
                                for kind in sorted({item["kind"] for item in self.facts.values()})},
            }

    def resource_stats(self) -> dict[str, Any]:
        with self.lock:
            rows = list(self.intents.values())
            now = time.time()
            tasks = {item["id"]: {key: item.get(key, 0) for key in
                     ("model_requests", "tokens", "effective_progress", "queue_seconds", "execution_seconds", "last_progress_at")}
                     for item in rows}
            for item in rows:
                if item["status"] == "running":
                    tasks[item["id"]]["execution_seconds"] += max(0.0, now - (item.get("started_at") or item.get("claimed_at") or now))
            return {"active_workers": sum(item["status"] == "running" for item in rows),
                    "waiting_tasks": sum(item["status"] == "pending" for item in rows),
                    "model_requests": sum(item.get("model_requests", 0) for item in rows),
                    "tokens": sum(item.get("tokens", 0) for item in rows),
                    "effective_progress": sum(item.get("effective_progress", 0) for item in rows),
                    "duplicate_tasks": self.duplicate_tasks,
                    "tasks": tasks}

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(_canonical({
                "version": self.VERSION, "todo": self.todo, "intents": self.intents,
                "facts": self.facts, "exploration": self.exploration, "traces": self.traces,
                "events": self.events, "revision": self.revision,
                "planned_revision": self.planned_revision, "planner_rounds": self.planner_rounds,
                "schedule_revision": self.schedule_revision,
                "planned_schedule_revision": self.planned_schedule_revision,
                "priority_snapshot": self.priority_snapshot, "ready_order": self._ready_order,
                "planned_state_signature": self._planned_state_signature,
                "direction_revision": self._direction_revision,
                "fact_revision": self._fact_revision,
                "coalesced_events": self.coalesced_events,
                "duplicate_tasks": self.duplicate_tasks,
                "trace_seq": self.trace_seq, "planned_facts": sorted(self.planned_facts),
            }))

    @classmethod
    def restore(cls, tasks: Iterable[Any], data: dict[str, Any] | None) -> "CoordinationRuntime":
        tasks = list(tasks)
        runtime = cls(tasks)
        if not data:
            return runtime
        if int(data.get("version", 0)) not in (1, cls.VERSION):
            raise ValueError("不支持的 coordination 检查点版本")
        with runtime.lock:
            for name in ("todo", "intents", "facts", "exploration", "traces", "events"):
                setattr(runtime, name, json.loads(_canonical(data.get(name, getattr(runtime, name)))))
            # 旧检查点继续使用原有事实编号和图边，新增发布通过语义索引查重。
            runtime._fact_ids = {_fact_id(row["kind"], row["payload"]): key for key, row in runtime.facts.items()}
            runtime.revision = int(data.get("revision", 0))
            runtime.planned_revision = int(data.get("planned_revision", -1))
            runtime.planner_rounds = int(data.get("planner_rounds", 0))
            runtime.schedule_revision = int(data.get("schedule_revision", runtime.revision))
            runtime.planned_schedule_revision = int(data.get("planned_schedule_revision", -1))
            runtime.priority_snapshot = json.loads(_canonical(data.get("priority_snapshot", {})))
            runtime._ready_order = [str(item) for item in data.get("ready_order", [])]
            runtime._planned_state_signature = str(data.get("planned_state_signature", ""))
            runtime._direction_revision = int(data.get("direction_revision", 0))
            runtime._fact_revision = int(data.get("fact_revision", 0))
            runtime.coalesced_events = int(data.get("coalesced_events", 0))
            runtime.duplicate_tasks = int(data.get("duplicate_tasks", 0))
            specs = {task.id: task for task in tasks}
            for index, intent in enumerate(runtime.intents.values()):
                intent.setdefault("queue_index", index)
                intent.setdefault("max_steps", 0)
                intent.setdefault("context_key", "")
                intent.setdefault("deliverable", "")
                intent.setdefault("lifecycle", "QUEUED" if intent["status"] == "pending" else intent["status"].upper())
                intent["signature"] = task_signature(specs[intent["id"]])
            runtime._dirty_tasks = set(runtime.intents)
            runtime.planned_schedule_revision = -1
            runtime._planned_state_signature = ""
            runtime.trace_seq = int(data.get(
                "trace_seq",
                max((int(item.get("id", 0)) for item in runtime.traces), default=0),
            ))
            runtime.planned_facts = {str(item) for item in data.get("planned_facts", [])}
        return runtime


__all__ = ["CoordinationRuntime", "FAILED_INTENTS", "TERMINAL_INTENTS"]
