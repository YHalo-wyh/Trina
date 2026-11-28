"""把新事实转成有界后继 Intent；只生成任务，不执行工具或修改事实。"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class IntentProposal:
    """与 ``TaskSpec`` 解耦的 Commander 提案。"""

    id: str
    target: str
    goal: str
    depends_on: tuple[str, ...]
    priority: int
    max_steps: int
    max_seconds: int
    capabilities: tuple[str, ...] = ()
    fact_ids: tuple[str, ...] = ()


class FactIntentPlanner:
    """确定性、可审计的最小事实规划器，不额外消耗一次模型调用。"""

    def decompose(self, source: Any) -> list[dict[str, Any]]:
        """只拆显式独立工作列表；含共享顺序但无结构化依赖时保持单任务。"""
        match = re.fullmatch(r"\s*(独立任务|并行任务|independent tasks|任务计划|task plan)\s*[:：]\s*(.+)", source.goal,
                             flags=re.IGNORECASE | re.DOTALL)
        if not match:
            return []
        parts = [part.strip() for part in re.split(r"[；;\n]+", match.group(2)) if part.strip()]
        if not 2 <= len(parts) <= 12:
            return []
        proposals, labels = [], {}
        for index, part in enumerate(parts, 1):
            label = re.match(r"^(\d+)[.、)]\s*(.+)$", part)
            if match.group(1).casefold() in {"任务计划", "task plan"} and not label:
                return []
            number, body = (label.group(1), label.group(2)) if label else (str(index), part)
            if number in labels:
                return []
            prefix = source.id if len(source.id) <= 48 else source.id[:39] + "-" + hashlib.sha256(source.id.encode()).hexdigest()[:8]
            labels[number] = f"{prefix}-part-{index}"
            dependencies = re.search(r"\s*\[(?:依赖|depends on)\s*[:：]\s*([\d,， ]+)\]\s*$", body, re.I)
            refs = re.split(r"[,， ]+", dependencies.group(1).strip()) if dependencies else []
            if match.group(1).casefold() in {"任务计划", "task plan"} and index > 1 and not dependencies:
                return []
            body = body[:dependencies.start()].strip() if dependencies else body
            # 无显式依赖编号时，不能凭连接词猜测能够并行。
            if not body or re.search(r"然后|之后|依赖|after |depends on", body, re.I):
                return []
            goal, _, deliverable = body.partition("=>")
            proposals.append({"id": labels[number], "target": source.target, "goal": goal.strip(),
                              "deliverable": deliverable.strip() or goal.strip(), "depends_on": refs})
        if len({" ".join(item["goal"].split()).casefold() for item in proposals}) != len(parts):
            return []
        for item in proposals:
            if any(ref not in labels for ref in item["depends_on"]):
                return []
            item["depends_on"] = tuple(dict.fromkeys((*source.depends_on, *(labels[ref] for ref in item["depends_on"]))))
        return proposals

    def propose(self, source: Any, facts: Iterable[dict[str, Any]], *, planning_scope: str = "") -> list[IntentProposal]:
        proposals: list[IntentProposal] = []
        origin = planning_scope or str(source.id)
        for fact in facts:
            fact_id = str(fact.get("id") or "")
            kind = str(fact.get("kind") or "")
            payload = fact.get("payload") if isinstance(fact.get("payload"), dict) else {}
            goal = self._goal(kind, payload)
            if not fact_id or not goal:
                continue
            planning_key = self._planning_key(kind, payload)
            suffix = hashlib.sha256(f"{origin}:{planning_key}".encode("utf-8")).hexdigest()[:10]
            proposals.append(IntentProposal(
                id=f"auto-{origin[:24]}-{suffix}"[:64],
                target=str(source.target),
                goal=goal,
                depends_on=(str(source.id),),
                priority=max(-100, int(source.priority) - 1),
                max_steps=int(source.max_steps),
                max_seconds=int(source.max_seconds),
                capabilities=tuple(source.capabilities),
                fact_ids=(fact_id,),
            ))
        return proposals

    @staticmethod
    def _planning_key(kind: str, payload: dict[str, Any]) -> str:
        if kind == "finding":
            return ":".join((kind, str(payload.get("vuln_class") or ""),
                             str(payload.get("url") or payload.get("target") or "")))
        if kind == "credential":
            return ":".join((kind, str(payload.get("service") or ""), str(payload.get("target") or "")))
        return kind

    @staticmethod
    def _goal(kind: str, payload: dict[str, Any]) -> str:
        if kind == "asset":
            ports = ", ".join(sorted(str(item) for item in (payload.get("ports") or {})))
            technologies = ", ".join(str(item) for item in (payload.get("technologies") or [])[:6])
            paths = ", ".join(str(item) for item in (payload.get("web_paths") or [])[-6:])
            observations = [item for item in (
                f"端口 {ports}" if ports else "",
                f"技术栈 {technologies}" if technologies else "",
                f"路径 {paths}" if paths else "",
            ) if item]
            if not observations:
                return ""
            return "基于已验证资产事实深化目标验证：" + "；".join(observations) + "。不要重复基础枚举。"
        if kind == "finding" and str(payload.get("status") or "") in {"verified", "exploited"}:
            title = str(payload.get("title") or payload.get("vuln_class") or "已验证发现")
            target = str(payload.get("url") or payload.get("target") or "")
            return f"复核 {title} 的实际影响并完成任务目标" + (f"：{target}" if target else "")
        if kind == "credential" and payload.get("verified"):
            service = str(payload.get("service") or "服务")
            target = str(payload.get("target") or "当前目标")
            return f"使用已验证凭据继续验证 {service}@{target}，避免重复凭据猜测"
        return ""


__all__ = ["FactIntentPlanner", "IntentProposal"]
