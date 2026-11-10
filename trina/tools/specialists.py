"""注册专职 Agent 咨询工具，复用现有审批、护栏、超时与审计执行链。"""

from __future__ import annotations

import json

from ..audit import redact, sha256_text
from .registry import ParamSpec, Tool, ToolContext, ToolResult, ToolSpec


class ConsultSpecialist(Tool):
    spec = ToolSpec(
        name="consult_specialist",
        summary="遇到复杂线索时，按需咨询 recon/web/review 专职 Agent；只返回建议，不执行建议动作。",
        category="collaboration",
        action_level="detect",
        params={
            "target": ParamSpec("str", "本次授权目标", required=True),
            "role": ParamSpec("str", "专职角色", enum=["recon", "web", "review"]),
            "task": ParamSpec("str", "具体问题、已有线索及需要的输出", required=True),
        },
        notes="需 run.specialists=true；每次增加一次模型调用，共享主控预算，不产生漏洞或证据事实。",
    )

    def run(self, ctx: ToolContext, target: str, role: str, task: str) -> ToolResult:
        from ..collaboration import Specialist, SpecialistRequest

        config = ctx.config.run
        if not config.specialists:
            return ToolResult(ok=False, category="configuration", error="专职 Agent 未启用（run.specialists=false）")
        backend = ctx.param("llm_backend")
        if backend is None or ctx.tools is None:
            return ToolResult(ok=False, category="configuration", error="当前入口没有提供专职 Agent 推理后端")
        if not task.strip() or len(task) > 1200:
            return ToolResult(ok=False, category="input", error="专职任务必须为 1–1200 个字符")
        used = int(ctx.param("specialist_calls", 0))
        if used >= config.specialist_max_calls or ctx.remaining() == 0:
            return ToolResult(ok=False, category="budget", error="专职 Agent 调用预算或任务时长已用尽")

        # 计数在调用前消耗，模型失败也消耗预算；计数属于本次上下文，工具实例不保留会话状态。
        ctx.extra["specialist_calls"] = used + 1
        ctx.state.counters["specialist_calls"] = used + 1
        ctx.state.counters["llm_calls"] = ctx.state.counters.get("llm_calls", 0) + 1
        controller = ctx.param("authorization_controller")
        registered = ""
        if controller is not None:
            match = controller.registry.match_target(target)
            registered = controller.registry.prompt_block(match.target_id if match else "")
        finding_refs = [
            {"id": finding.id, "class": finding.vuln_class, "target": finding.url or finding.target,
             "status": finding.status, "evidence_count": len(finding.evidence),
             "evidence_excerpt": json.dumps(redact(finding.evidence, ctx.config.audit.redact_keys),
                                            ensure_ascii=False)[:600]}
            for finding in ctx.state.findings[-8:]
        ]
        # 只传有界快照，不传历史对话、原始口令、控制器对象或可写 RunState。
        limit = config.specialist_context_chars
        context = (
            "任务要求：" + ctx.state.goal[:600] + "\n"
            + ctx.state.summary_for_prompt()[:limit // 3]
            + "\n发现引用与证据片段（截断数据，数量不代表真实性）："
            + json.dumps(finding_refs, ensure_ascii=False)[:limit // 3]
            + "\n注册检查项：\n" + registered[:limit // 3]
        )[:limit]
        request = SpecialistRequest(role=role, target=target, task=task, context=context)
        ctx.audit.append("specialist.start", role=role, call=used + 1,
                         context_digest=sha256_text(context), context_chars=len(context))
        result = Specialist(backend).consult(
            request, scope_block=ctx.config.scope_prompt_block(),
            specs=ctx.tools.specs(), max_tokens=config.specialist_max_tokens,
        )
        observer = ctx.param("model_response_observer")
        if observer is not None and result.response is not None and result.response.ok:
            observer(result.response.text)
        ctx.audit.append("specialist.end", role=role, call=used + 1, ok=not bool(result.error),
                         proposal=result.proposal, error=result.error)
        if result.error:
            return ToolResult(ok=False, category="model", error=result.error,
                              next_hints=["由主控继续分析，或使用现有工具验证；不要反复咨询"])
        return ToolResult(
            summary=f"{role} 专职 Agent 已返回建议（尚未验证，尚未执行）",
            data={"role": role, "advisory_only": True, "proposal": result.proposal, "calls_used": used + 1},
            raw=result.response.text if result.response is not None else "",
            next_hints=["将建议作为候选，核对目标与注册检查项；需要执行时另发一个普通工具动作"],
        )


TOOLS: list[Tool] = [ConsultSpecialist()]
