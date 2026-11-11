"""专职 Agent 的隔离推理：只接收快照，只返回建议，不持有执行上下文。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .llm import LLMBackend, LLMResponse
from .tools.registry import ToolSpec


@dataclass(frozen=True)
class SpecialistRole:
    """固定角色与建议工具范围；角色本身没有执行权限。"""

    name: str
    categories: tuple[str, ...]
    instruction: str


ROLES = {
    "recon": SpecialistRole(
        "recon", ("recon", "web"),
        "分析已知端口、服务与 Web 线索。",
    ),
    "web": SpecialistRole(
        "web", ("web", "vuln", "exploit", "creds"),
        "分析页面、路径与参数。",
    ),
    "review": SpecialistRole(
        "review", (),
        "复核已有证据与缺口，只返回 final.summary。",
    ),
}


@dataclass(frozen=True)
class SpecialistRequest:
    role: str
    target: str
    task: str
    context: str


@dataclass
class SpecialistResult:
    proposal: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    response: LLMResponse | None = None


class Specialist:
    """一次调用一个角色；不保存对话，不执行工具，也不递归派生 Agent。"""

    def __init__(self, backend: LLMBackend):
        self.backend = backend

    def consult(
        self,
        request: SpecialistRequest,
        *,
        scope_block: str,
        specs: list[ToolSpec],
        max_tokens: int,
    ) -> SpecialistResult:
        from .brain.prompts import parse_action, render_system_prompt

        role = ROLES.get(request.role)
        if role is None:
            return SpecialistResult(error="未知专职角色")
        allowed = {spec.name: spec for spec in specs
                   if spec.category in role.categories and spec.name != "consult_specialist"}
        system = (
            render_system_prompt()
            + "\n\n# Delegated Assignment\n"
            + f"{role.instruction}\n"
            "根据给定数据返回一个建议 JSON：{\"action\": \"工具名\", \"params\": {}}；"
            "或 {\"final\": {\"summary\": \"结论\"}}。建议不会自动执行。\n"
            "# 配置范围\n" + scope_block
            + "\n# 可建议的工具\n"
            + ("\n".join(spec.render(include_examples=False) for spec in allowed.values()) or "无")
        )
        user = json.dumps({
            "target": request.target, "task": request.task, "context": request.context,
        }, ensure_ascii=False)
        response = self.backend.complete(system, user, max_tokens=max_tokens)
        if not response.ok:
            return SpecialistResult(error=response.error or "专职 Agent 调用失败", response=response)
        proposal = parse_action(response.text)
        if "error" in proposal or ("action" in proposal) == ("final" in proposal):
            return SpecialistResult(error="专职 Agent 必须返回一个动作或复核摘要", response=response)
        if "final" in proposal:
            final = proposal["final"]
            if not isinstance(final, dict) or not isinstance(final.get("summary"), str):
                return SpecialistResult(error="复核摘要必须是 final.summary 字符串", response=response)
            # 不转发模型自评状态、flag 或发现；建议永远不能成为工具事实。
            return SpecialistResult(proposal={"final": {"summary": final["summary"][:1600]}}, response=response)
        name = proposal["action"]
        if not isinstance(name, str) or name not in allowed:
            return SpecialistResult(error="专职 Agent 建议了角色范围外或未注册的工具", response=response)
        if not isinstance(proposal.get("params"), dict):
            return SpecialistResult(error="建议动作缺少 params 对象", response=response)
        # 参数依然是不可信建议；主控下一步调用时再走控制器、审批与注册表校验。
        clean = {"thought": str(proposal.get("thought", ""))[:500],
                 "action": name, "params": proposal["params"]}
        if isinstance(proposal.get("test_id"), str):
            clean["test_id"] = proposal["test_id"]
        return SpecialistResult(proposal=clean, response=response)


__all__ = ["ROLES", "Specialist", "SpecialistRequest", "SpecialistResult", "SpecialistRole"]
