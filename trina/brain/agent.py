"""ReAct 决策大脑：自动决策、自动调工具、自动收敛。

一步的循环（观察 → 思考 → 动作）::

    状态摘要 + 阶段任务 + 上一步观察
        → LLM（omp / openai / replay）
        → {"thought": ..., "action": "工具名", "params": {...}}
        → ToolRegistry.execute（参数校验 → 护栏 → 审计 → 执行）
        → 观察回灌 + 事实并入 RunState → 下一步

直到模型给出 `final`、或步数/时长/停滞触发收敛。全过程写入审计与 RunState.steps。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..audit import AuditLog
from ..config import PAIConfig
from ..llm import LLMBackend, make_backend
from ..scope import ScopeGuard, ScopeViolation
from ..state import RunState
from ..tools import ToolRegistry, build_registry
from ..tools.http import HttpClient
from ..tools.registry import ToolContext
from .goal import GoalRequirements
from .planner import Planner
from .prompts import build_step_prompt, build_system_prompt, parse_action

MAX_LLM_RETRIES = 4
#: 目标未达成时最多拒绝几次「提前结束」，超过则尊重模型判断（避免死循环）
MAX_FINAL_REJECTS = 2
#: 连续多少次 LLM 失败后放弃本次任务（后端抖动不应立刻毁掉一个场景）
MAX_LLM_FAILURES = 6


@dataclass
class StepRecord:
    index: int
    phase: str
    thought: str = ""
    action: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    ok: bool = False
    denied: bool = False
    summary: str = ""
    duration: float = 0.0
    llm_seconds: float = 0.0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "phase": self.phase,
            "thought": self.thought,
            "action": self.action,
            "params": self.params,
            "ok": self.ok,
            "denied": self.denied,
            "summary": self.summary,
            "duration": round(self.duration, 3),
            "llm_seconds": round(self.llm_seconds, 2),
            "error": self.error,
        }


class PentestAgent:
    """全自动渗透测试 Agent。"""

    def __init__(
        self,
        config: PAIConfig,
        *,
        registry: ToolRegistry | None = None,
        backend: LLMBackend | None = None,
        logger: Callable[[str], None] | None = None,
        on_step: Callable[[StepRecord], None] | None = None,
        audit: AuditLog | None = None,
    ):
        self.config = config
        self.registry = registry or build_registry()
        self.backend = backend or make_backend(config.llm)
        self.logger = logger or (lambda message: None)
        self.on_step = on_step
        self._audit = audit

    # ------------------------------------------------------------------ 入口
    def run(
        self,
        target: str,
        *,
        goal: str = "",
        max_steps: int | None = None,
        max_seconds: int | None = None,
        extra_rules: str = "",
        seed_hints: list[str] | None = None,
        record_path: str | Path | None = None,
        run_id: str | None = None,
    ) -> RunState:
        """对单目标执行一次全自动渗透，返回最终状态。"""
        run_id = run_id or time.strftime("%Y%m%d-%H%M%S") + "-" + _short_hash(target)
        max_steps = max_steps or self.config.llm.max_steps
        max_seconds = max_seconds or self.config.llm.max_seconds

        audit = self._audit or AuditLog.open(
            self.config.audit_dir,
            run_id=run_id,
            redact_keys=self.config.audit.redact_keys,
            record_output_digest=self.config.audit.record_output_digest,
            resume=False,
        )
        guard = ScopeGuard(self.config.scope, audit=audit)
        state = RunState(run_id=run_id, target=target, goal=goal, max_evidence=self.config.run.max_evidence)
        workspace = self.config.workspace_dir / run_id
        workspace.mkdir(parents=True, exist_ok=True)

        audit.append(
            "run.start",
            target=target,
            goal=goal,
            model=self.config.llm.model,
            backend=self.config.llm.backend,
            max_steps=max_steps,
            max_seconds=max_seconds,
            scope=guard.summary(),
        )

        # 预检：目标越界直接拒绝，不进入 LLM（模型无权授权）
        decision = guard.check_target(target, action="preflight")
        if decision.denied:
            state.status = "blocked"
            state.note("denied", f"目标 {target} 不在授权范围：{decision.reason}")
            audit.append("run.blocked", target=target, decision="deny", reason=decision.reason)
            state.ended_at = time.time()
            state.artifacts = {"workspace": str(workspace), "audit_path": str(audit.path), "state_file": str(workspace / "state.json")}
            self._save_state(state, workspace)
            return state

        ctx = ToolContext(
            config=self.config,
            guard=guard,
            audit=audit,
            state=state,
            http=HttpClient(guard, timeout=15.0),
            workspace=workspace,
            tools=self.registry,
            deadline=time.monotonic() + max_seconds,
            logger=self.logger,
        )
        self._ctx = ctx
        state.note("run", f"开始对 {target} 的自动化渗透测试", goal=goal)

        planner = Planner(max_steps_per_phase=max(4, max_steps // len(("recon", "enum", "vuln", "exploit", "post", "report"))))
        requirements = GoalRequirements.parse(goal)
        if not requirements.empty:
            state.note("goal", f"目标要求（未满足前不允许收敛）：{requirements.describe()}")
        hints: list[str] = list(seed_hints or []) + requirements.credential_hints()
        system_prompt = build_system_prompt(self.config.scope_prompt_block(), self.registry.prompt_block(), extra_rules)
        observation = ""
        last_action_key = ""
        stall = 0
        new_facts = True
        record_handle = Path(record_path).open("w", encoding="utf-8") if record_path else None
        recorded_steps = 0
        if record_handle:
            record_handle.write('{"steps": [\n')

        started = time.monotonic()
        steps = 0
        final: dict[str, Any] | None = None
        final_rejects = 0
        denial_counts: dict[str, int] = {}
        try:
            for index in range(1, max_steps + 1):
                if ctx.remaining() is not None and ctx.remaining() <= 0:
                    state.note("budget", f"时长预算用尽（{max_seconds}s）")
                    break
                steps = index
                missing_goal = requirements.unsatisfied(state) if not requirements.empty else []
                phase, transition = planner.tick(state, new_facts=new_facts)
                if transition:
                    state.set_phase(phase, transition)
                    hints.append(transition)
                if planner.should_finish(state) and phase in ("post", "report"):
                    if not missing_goal:
                        state.note("budget", "已无更多可执行动作，提前收敛")
                        break
                    # 拿到 flag 不等于完成目标：任务要求的验证还没做，必须继续
                    hints.extend(missing_goal[:3])
                    hints.append("不要用取巧路径（如直接读 /flag）结束任务：请完成目标要求的验证并给出证据")
                if missing_goal:
                    hints.extend(item for item in missing_goal[:2] if item not in hints)
                elif not requirements.empty:
                    hints.extend(item for item in requirements.soft_hints(state)[:1] if item not in hints)

                step_prompt = build_step_prompt(
                    state_digest=state.summary_for_prompt(),
                    phase=phase,
                    directive=planner.directive(state),
                    last_observation=observation,
                    hints=hints,
                    budget={
                        "阶段步数": f"{planner.phase_step}/{planner.max_steps_per_phase}",
                        "总步数": f"{index}/{max_steps}",
                        "剩余秒": int(ctx.remaining() or 0),
                    },
                    # 任务硬性要求放在显眼位置：否则模型容易被"顺路发现"带偏，把预算花在目标之外
                    extra_context=(
                        "## 本次任务的硬性要求（未完成前不允许结束，请优先处理）\n"
                        + "\n".join(f"- {item}" for item in missing_goal)
                        if missing_goal
                        else ""
                    ),
                )
                hints = []

                response = self._ask(system_prompt, step_prompt)
                if not response.ok:
                    state.counters["errors"] = state.counters.get("errors", 0) + 1
                    state.note("llm_error", response.error)
                    self.logger(f"[{index}] LLM 失败: {response.error}")
                    audit.append("llm.error", model=self.backend.config.model, error=response.error[:300], step=index)
                    if state.counters["errors"] >= MAX_LLM_FAILURES:
                        state.status = "failed"
                        break
                    hints.append("上一步推理失败，请直接给出一个工具动作")
                    observation = f"LLM 调用失败：{response.error}"
                    time.sleep(min(2.0 * state.counters["errors"], 8.0))
                    continue
                state.counters["llm_calls"] = state.counters.get("llm_calls", 0) + 1
                if record_handle:
                    if recorded_steps:
                        record_handle.write(",\n")
                    record_handle.write(json.dumps({"response": response.text}, ensure_ascii=False))
                    record_handle.flush()
                    recorded_steps += 1

                action = parse_action(response.text)
                if "error" in action:
                    state.counters["errors"] = state.counters.get("errors", 0) + 1
                    hints.append("输出必须是单个 JSON 对象：{\"thought\":..., \"action\":..., \"params\":{...}}")
                    observation = f"动作解析失败：{action.get('error')}；原始输出片段：{str(action.get('raw', ''))[:200]}"
                    state.log_step(
                        StepRecord(index, phase, summary="解析失败", error=action.get("error", ""), llm_seconds=response.duration).to_dict()
                    )
                    continue

                if "final" in action:
                    missing = requirements.unsatisfied(state) if not requirements.empty else []
                    if missing and final_rejects < MAX_FINAL_REJECTS:
                        final_rejects += 1
                        hints.extend(missing[:3])
                        hints.append("系统拒绝了你的结束请求：先完成上述目标要求（要有工具产出的证据）")
                        observation = (
                            "结束请求被拒绝（目标尚未达成）："
                            + "；".join(missing)
                            + "。请继续调用工具完成验证，然后再给出 final。"
                        )
                        state.note("goal", "拒绝提前结束：" + "；".join(missing))
                        state.log_step(
                            StepRecord(index, phase, thought=str(action.get("thought", "")), action="final_rejected",
                                       summary="目标未达成，拒绝结束").to_dict()
                        )
                        continue
                    final = action.get("final") if isinstance(action.get("final"), dict) else {"summary": str(action.get("final"))}
                    state.note("final", str(final.get("summary", ""))[:400])
                    state.log_step(
                        StepRecord(index, phase, thought=str(action.get("thought", "")), action="final", summary=str(final.get("summary", ""))[:200], ok=True).to_dict()
                    )
                    break

                tool_name = str(action.get("action", ""))
                params = action.get("params") if isinstance(action.get("params"), dict) else {}
                thought = str(action.get("thought", ""))
                self.logger(f"[{index}/{max_steps}] {phase} · {tool_name} {_brief_params(params)}")
                audit.append("react.action", step=index, phase=phase, thought=thought[:300], tool=tool_name, params=params)

                result = self.registry.execute(tool_name, params, ctx)
                observation = result.to_observation()
                new_facts = bool(result.facts) and any(
                    bool(value) for value in (result.facts or {}).values() if isinstance(value, (list, dict, tuple, set))
                )

                step = StepRecord(
                    index=index,
                    phase=phase,
                    thought=thought,
                    action=tool_name,
                    params=params,
                    ok=result.ok,
                    denied=result.denied,
                    summary=result.summary or result.error,
                    duration=result.duration,
                    llm_seconds=response.duration,
                    error=result.error,
                )
                state.log_step(step.to_dict())
                if self.on_step:
                    self.on_step(step)

                if result.denied:
                    hints.extend(result.next_hints[:3] or ["该动作被护栏拒绝，请换合规做法"])
                    denial_key = f"{tool_name}|{result.category}"
                    denial_counts[denial_key] = denial_counts.get(denial_key, 0) + 1
                    if denial_counts[denial_key] >= 2:
                        hints.append(
                            f"{tool_name} 已因「{result.category}」被拒绝 {denial_counts[denial_key]} 次："
                            "禁止再用同样的参数/等级调用它，必须换成更低等级的等价工具或改成合规参数"
                        )
                        planner.force_advance()
                elif not result.ok:
                    hints.append(f"工具失败（{result.error[:80]}），换工具或换参数，不要重试同一动作")
                elif requirements.credentials and not state.credentials:
                    hints.extend(requirements.credential_hints()[:1])

                # 停滞检测：同一动作重复 / 连续无新增事实
                key = f"{tool_name}:{json.dumps(params, sort_keys=True, ensure_ascii=False)}"
                stall = stall + 1 if key == last_action_key else 0
                last_action_key = key
                if stall >= self.config.llm.stall_limit:
                    hints.append(f"同一动作已连续执行 {stall + 1} 次，必须改变策略（换工具/换参数/换切入点）")
                    planner.force_advance()
                    stall = 0
                if not result.ok and not result.denied and key in observation:
                    planner.force_advance()
                for hint in hints:
                    state.note("hint", hint)
        finally:
            if record_handle:
                record_handle.write("\n]}\n")
                record_handle.close()

        # ------------------------------------------------------------------ 收尾
        if final is not None:
            raw_status = str(final.get("status", "")).lower()
            state.status = raw_status if raw_status in ("success", "partial", "failed") else state.status
            for flag in final.get("flags") or []:
                state.absorb({"flags": [{"value": str(flag), "source": "agent_final"}]})
        state.ended_at = time.time()
        if not requirements.empty:
            # 有明确任务要求时，以「要求是否达成」判定成败：模型自评与 flag 都不能替代验证
            if requirements.satisfied(state):
                state.status = "success"
            elif state.flags or state.findings:
                state.status = "partial"
            else:
                state.status = "failed"
        elif state.status == "running":
            if state.flags or any(f.status in ("verified", "exploited") for f in state.findings):
                state.status = "success"
            elif state.findings:
                state.status = "partial"
            else:
                state.status = "failed"
        state.ledger = self.backend.ledger.to_dict()
        state.note(
            "run.end",
            f"任务结束：状态={state.status}，步数={steps}，漏洞={len(state.findings)}，flag={len(state.flags)}",
        )
        if not requirements.empty:
            state.note("goal", f"结束时的目标要求缺口：{requirements.unsatisfied(state) or '无'}")
        audit.append(
            "run.end",
            target=target,
            status=state.status,
            steps=steps,
            findings=len(state.findings),
            flags=len(state.flags),
            credentials=len(state.credentials),
            denied=state.counters.get("denied", 0),
            duration=round(state.duration, 1),
            ledger=state.ledger,
            http=ctx.http.stats(),
        )
        state.artifacts = {"workspace": str(workspace), "audit_path": str(audit.path), "state_file": str(workspace / "state.json")}
        self._save_state(state, workspace)
        return state

    # ------------------------------------------------------------------ 内部
    def _ask(self, system: str, user: str):
        """带重试的 LLM 调用（后端抖动时退避重试，避免直接毁掉整个场景）。"""
        last = None
        for attempt in range(MAX_LLM_RETRIES + 1):
            response = self.backend.complete(system, user)
            if response.ok and response.text.strip():
                return response
            last = response
            if attempt < MAX_LLM_RETRIES:
                time.sleep(min(2.0 * (attempt + 1), 10.0))
        return last

    def _save_state(self, state: RunState, workspace: Path) -> None:
        try:
            path = workspace / "state.json"
            path.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
            state.note("artifact", f"状态已保存: {path}")
        except OSError as exc:  # pragma: no cover - 磁盘异常不应影响结论
            state.note("error", f"状态保存失败: {exc}")


def _short_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:6]


def _brief_params(params: dict[str, Any], limit: int = 80) -> str:
    try:
        text = json.dumps(params, ensure_ascii=False)
    except TypeError:
        text = str(params)
    return text if len(text) <= limit else text[:limit] + "…"


def run_pentest(
    config: PAIConfig,
    target: str,
    *,
    goal: str = "",
    max_steps: int | None = None,
    logger: Callable[[str], None] | None = None,
) -> RunState:
    """一次性调用的便捷入口。"""
    return PentestAgent(config, logger=logger).run(target, goal=goal, max_steps=max_steps)


__all__ = ["PentestAgent", "StepRecord", "run_pentest"]
