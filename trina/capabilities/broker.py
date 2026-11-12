"""Capability Fabric：能力授权、租约、调用路由与证据索引。"""
from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..config import ACTION_LEVELS
from ..tools import ToolContext, ToolRegistry, ToolResult
from .catalog import Capability, CapabilityCatalog
from .runtime import CapabilityRuntime


class CapabilityLeaseError(RuntimeError):
    pass


@dataclass
class CapabilityLease:
    """Worker 在单个任务内持有的有界能力集合。"""

    id: str
    worker_id: str
    task_id: str
    capabilities: tuple[Capability, ...]
    expires_at: float
    registry: ToolRegistry = field(default_factory=ToolRegistry)
    tool_names: dict[str, str] = field(default_factory=dict)
    guidance: str = ""
    active: bool = True

    @property
    def snapshot(self) -> list[dict[str, str]]:
        return [item.snapshot() for item in self.capabilities]

    def check(self) -> None:
        if not self.active:
            raise CapabilityLeaseError(f"Capability 租约已释放: {self.id}")
        if time.monotonic() >= self.expires_at:
            self.active = False
            raise CapabilityLeaseError(f"Capability 租约已过期: {self.id}")


class CapabilityFabric:
    """Commander 与 Worker 之间唯一的能力平面。"""

    def __init__(self, catalog: CapabilityCatalog, runtime: CapabilityRuntime) -> None:
        self.catalog = catalog
        self.runtime = runtime
        self._leases: dict[str, CapabilityLease] = {}
        self._worker_leases: dict[tuple[str, str], str] = {}
        self._memory: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def resolve(self, intent: str, constraints: dict[str, Any] | None = None) -> list[Capability]:
        """发现候选，并过滤依赖、运行时与显式动作等级约束。"""
        constraints = dict(constraints or {})
        maximum_level = str(constraints.get("max_action_level") or "")
        candidates = self.catalog.resolve(intent, constraints)
        selected: list[Capability] = []
        for capability in candidates:
            if capability.policy.get("enabled") is False:
                continue
            level = str(capability.policy.get("action_level") or "detect")
            if maximum_level in ACTION_LEVELS and level in ACTION_LEVELS:
                if ACTION_LEVELS.index(level) > ACTION_LEVELS.index(maximum_level):
                    continue
            required_tools = capability.requires.get("tools") or ()
            if not isinstance(required_tools, (list, tuple)):
                raise ValueError(f"Capability {capability.id} requires.tools 必须是列表")
            if any(shutil.which(str(tool)) is None for tool in required_tools):
                continue
            available, _reason = self.runtime.available(capability)
            if available:
                selected.append(capability)
        return selected

    def lease(
        self,
        worker_id: str,
        task_id: str,
        intent: str,
        *,
        constraints: dict[str, Any] | None = None,
        ttl: float = 900,
        pinned: Iterable[dict[str, str]] | None = None,
    ) -> CapabilityLease:
        if not worker_id or not task_id or ttl <= 0:
            raise ValueError("Capability 租约需要 worker_id/task_id 和正数 ttl")
        with self._lock:
            key = (worker_id, task_id)
            previous = self._worker_leases.get(key)
            if previous and self._leases[previous].active:
                raise CapabilityLeaseError(f"Worker {worker_id} 已持有任务 {task_id} 的能力租约")
            if pinned:
                capabilities = self.catalog.exact(pinned)
                unavailable = []
                for item in capabilities:
                    available, reason = self.runtime.available(item)
                    if not available:
                        unavailable.append(f"{item.id}: {reason}")
                if unavailable:
                    raise CapabilityLeaseError("固定能力当前不可用: " + "；".join(unavailable))
            else:
                capabilities = self.resolve(intent, constraints)
            if not capabilities:
                raise CapabilityLeaseError(f"任务 {task_id} 没有可用能力")
            lease = CapabilityLease(
                id=uuid.uuid4().hex,
                worker_id=worker_id,
                task_id=task_id,
                capabilities=tuple(capabilities),
                expires_at=time.monotonic() + ttl,
            )

            def publish(capability: Capability, result: ToolResult, arguments: dict[str, Any]) -> None:
                self.publish(lease, capability, result, arguments)

            registry, mapping = self.runtime.bind(
                capabilities,
                check_lease=lease.check,
                publish=publish,
            )
            guidance_parts = []
            for capability in capabilities:
                guidance = capability.load_guidance()
                if guidance:
                    guidance_parts.append(f"## {capability.id}@{capability.version}\n{guidance}")
            lease.registry = registry
            lease.tool_names = mapping
            lease.guidance = "\n\n".join(guidance_parts)
            # Agent 只接收本次租约选中的 Guidance 和固定版本快照。
            registry.capability_guidance = lease.guidance  # type: ignore[attr-defined]
            registry.capability_snapshot = lease.snapshot  # type: ignore[attr-defined]
            registry.capability_lease_id = lease.id  # type: ignore[attr-defined]
            self._leases[lease.id] = lease
            self._worker_leases[key] = lease.id
            return lease

    def invoke(self, capability_id: str, arguments: dict[str, Any], worker_context: dict[str, Any]) -> ToolResult:
        """对外的统一调用入口；实际执行仍落到租约 registry.execute。"""
        lease = self._lease_from_context(worker_context)
        lease.check()
        tool_name = lease.tool_names.get(capability_id)
        if not tool_name:
            return ToolResult(ok=False, denied=True, summary=f"能力未绑定: {capability_id}", error="capability not leased")
        ctx = worker_context.get("ctx")
        if not isinstance(ctx, ToolContext):
            raise ValueError("worker_context.ctx 必须是 ToolContext")
        return lease.registry.execute(tool_name, arguments, ctx)

    def release(self, lease_id: str) -> None:
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                raise CapabilityLeaseError(f"未知 Capability 租约: {lease_id}")
            if not lease.active:
                return
            lease.active = False
            self._worker_leases.pop((lease.worker_id, lease.task_id), None)

    def get_lease(self, lease_id: str) -> CapabilityLease:
        with self._lock:
            lease = self._leases.get(lease_id)
        if lease is None:
            raise CapabilityLeaseError(f"未知 Capability 租约: {lease_id}")
        return lease

    def publish(
        self,
        lease: CapabilityLease,
        capability: Capability,
        result: ToolResult,
        arguments: dict[str, Any] | None = None,
    ) -> str:
        """只发布事实类型、证据摘要和引用，不同步完整对话或直接合并状态。"""
        lease.check()
        evidence = []
        for item in result.evidence or ():
            if not isinstance(item, dict):
                continue
            compact = {
                key: item.get(key)
                for key in ("kind", "target", "method", "status", "note", "path")
                if item.get(key) not in (None, "")
            }
            compact["digest"] = _digest(item)
            evidence.append(compact)
        body = {
            "capability": capability.id,
            "version": capability.version,
            "worker": lease.worker_id,
            "task": lease.task_id,
            "ok": result.ok,
            "summary": result.summary[:500],
            "fact_kinds": sorted((result.facts or {}).keys()),
            "facts_digest": _digest(result.facts or {}),
            "evidence": evidence,
            "argument_keys": sorted((arguments or {}).keys()),
        }
        reference = _digest(body)
        entry = {"id": reference, **body, "published_at": time.time()}
        with self._lock:
            self._memory.setdefault(reference, entry)
        return reference

    def publish_task(self, lease: CapabilityLease, result: dict[str, Any]) -> str:
        """任务收尾索引，供 Commander 交接工件和证据引用。"""
        lease.check()
        body = {
            "capability": "task.result",
            "versions": lease.snapshot,
            "worker": lease.worker_id,
            "task": lease.task_id,
            "status": result.get("status"),
            "verdict": result.get("verdict"),
            "artifacts": {
                key: value for key, value in (result.get("artifacts") or {}).items()
                if key in {"state_file", "audit_path", "report_paths", "workspace"}
            },
            "facts_digest": _digest(result.get("facts") or {}),
        }
        reference = _digest(body)
        with self._lock:
            self._memory.setdefault(reference, {"id": reference, **body, "published_at": time.time()})
        return reference

    def refs_for_task(self, task_id: str) -> list[str]:
        with self._lock:
            return sorted(entry["id"] for entry in self._memory.values() if entry.get("task") == task_id)

    def memory_snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(self._memory[key]) for key in sorted(self._memory)]

    def _lease_from_context(self, context: dict[str, Any]) -> CapabilityLease:
        lease_id = str(context.get("lease_id") or "")
        with self._lock:
            if not lease_id:
                lease_id = self._worker_leases.get((str(context.get("worker_id") or ""), str(context.get("task_id") or "")), "")
            lease = self._leases.get(lease_id)
        if lease is None:
            raise CapabilityLeaseError("worker_context 未关联有效 Capability 租约")
        return lease


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8", "replace")).hexdigest()


__all__ = ["CapabilityFabric", "CapabilityLease", "CapabilityLeaseError"]
