"""ReAct 决策大脑：自动决策、自动调工具、自动收敛。

一步的循环（观察 → 思考 → 动作）::

    状态摘要 + 阶段任务 + 上一步观察
        → LLM（标准结构化消息；OpenAI 兼容 / replay）
        → {"thought": ..., "action": "工具名", "params": {...}}
        → ToolRegistry.execute（参数校验 → 护栏 → 审计 → 执行）
        → 观察回灌 + 事实并入 RunState → 下一步

直到模型给出 `final`、或步数/时长/停滞触发收敛。全过程写入审计与 RunState.steps。
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..audit import AuditLog
from ..config import TrinaConfig
from ..llm import LLMBackend, LLMMessage, make_backend
from ..scope import ScopeGuard, ScopeViolation
from ..state import VERDICT_CN, RunState
from ..tools import ToolRegistry, build_registry
from ..tools.http import HttpClient
from ..tools.registry import ToolContext
from ..tools.schema import FINISH_TOOL_NAME, parse_arguments, schema_signature, tool_schemas
from .failure import (
    LoopGuard,
    advice_for,
    alternatives_for,
    classify_failure,
    classify_llm_response,
    decide_after_failure,
)
from .goal import GoalRequirements
from .planner import Planner
from .prompts import (
    build_prompt_layers,
    build_runtime_context,
    build_step_prompt,
    build_system_prompt,
    parse_action,
)
from .scheduler import DEFAULT_MIN_PRIORITY, HypothesisScheduler

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
        config: TrinaConfig,
        *,
        registry: ToolRegistry | None = None,
        backend: LLMBackend | None = None,
        logger: Callable[[str], None] | None = None,
        on_step: Callable[[StepRecord], None] | None = None,
        audit: AuditLog | None = None,
        approval: Callable[[dict[str, Any]], bool] | None = None,
        steer_queue: Any | None = None,
        stop_event: Any | None = None,
        pause_event: Any | None = None,
        controller: Any | None = None,
    ):
        self.config = config
        self.registry = registry or build_registry()
        self.backend = backend or make_backend(config.llm)
        self.logger = logger or (lambda message: None)
        self.on_step = on_step
        self._audit = audit
        #: 人工审批回调：入参为待执行动作描述，返回 False 表示人工拒绝
        self.approval = approval
        #: 人工介入队列：运行中从里面取提示词（steer）注入下一步
        self.steer_queue = steer_queue
        #: 人工停止 / 暂停信号（threading.Event 语义）
        self.stop_event = stop_event
        self.pause_event = pause_event
        #: 独立授权控制器（None 时按 config.policy 自动创建）
        self.controller = controller

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
        commander_briefs: list[dict[str, Any]] | None = None,
        commander_brief_provider: Callable[[], list[dict[str, Any]]] | None = None,
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
        usage_before = self.backend.ledger.to_dict()
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
        ctx.extra["llm_backend"] = self.backend
        state.note("run", f"开始对 {target} 的自动化渗透测试", goal=goal)

        # 各题状态保持独立。Commander 只把判断为相关的有效情报作为候选线索转发，
        # 当前 Worker 仍需用自己的工具重新验证，情报不会直接写入 RunState。
        from ..context import brief_digest, normalize_briefs, render_commander_briefs

        commander_briefs = normalize_briefs(commander_briefs)
        seen_briefs: set[str] = set()

        def commander_context_block() -> str:
            dynamic: list[dict[str, Any]] = []
            if commander_brief_provider is not None:
                try:
                    dynamic = commander_brief_provider()
                except Exception as exc:  # noqa: BLE001 - 情报发现故障不应中断本题
                    audit.append("context.commander.error", error=f"{type(exc).__name__}: {exc}"[:300])
            current = normalize_briefs([*commander_briefs, *dynamic])
            for brief in current:
                digest = brief_digest(brief)
                if digest in seen_briefs:
                    continue
                seen_briefs.add(digest)
                audit.append(
                    "context.commander.forward",
                    source_task=brief.get("source_task", ""),
                    score=brief.get("similarity", 0),
                    reasons=brief.get("reasons", []),
                    digest=digest,
                )
            return render_commander_briefs(current)

        planner = Planner(max_steps_per_phase=max(4, max_steps // len(("recon", "enum", "vuln", "exploit", "post", "report"))))
        requirements = GoalRequirements.parse(goal)
        if not requirements.empty:
            state.note("goal", f"目标要求（未满足前不允许收敛）：{requirements.describe()}")
        hints: list[str] = list(seed_hints or []) + requirements.credential_hints()

        # 假设台账 + 循环守卫（文档建议：把"待验证的问题"结构化，并识别无新信息的重复）
        from ..hypotheses import HypothesisLedger

        ledger = HypothesisLedger(risk_aversion=float(getattr(self.config.run, "hypothesis_risk_aversion", 1.0) or 1.0))
        loop_guard = LoopGuard()
        # 洞察层开关（对照实验用）：关闭只影响"模型看到什么"，统计口径不变（redundant_action_rate / fail_* 照记）
        use_ledger = bool(getattr(self.config.run, "hypothesis_ledger", True))
        use_guard = bool(getattr(self.config.run, "loop_guard", True))
        # 确定性调度：把台账优先级翻译成"这一步验证谁"（关闭台账时无从调度）
        scheduler = (
            HypothesisScheduler(
                min_priority=float(getattr(self.config.run, "scheduler_min_priority", DEFAULT_MIN_PRIORITY) or 0.0)
            )
            if (use_ledger and bool(getattr(self.config.run, "hypothesis_scheduler", True)))
            else None
        )

        # 独立控制器：授权来自注册表与预算合同，不来自模型的文字
        controller = self.controller
        if controller is None:
            from ..policy import AuthorizationController, Budget, PolicyRegistry

            controller = AuthorizationController(
                PolicyRegistry.load(self.config, extra_dirs=list(self.config.policy.registry_dirs)),
                mode=self.config.policy.mode,
                budget=Budget(
                    max_checks=self.config.policy.max_checks,
                    logic_seconds=self.config.policy.logic_seconds,
                    stop_on_no_new_evidence=self.config.policy.stop_on_no_new_evidence,
                ),
                audit=audit,
            )
            self.controller = controller
        registered = controller.registry.match_target(target)
        ctx.extra["authorization_controller"] = controller
        target_id = registered.target_id if registered else ""
        if target_id and not getattr(controller, "default_target_id", ""):
            controller.default_target_id = target_id
        policy_block = controller.registry.prompt_block(target_id)
        if not controller.registry.empty:
            state.note(
                "policy",
                f"授权策略：mode={controller.mode}，注册目标 {len(controller.registry.targets)} 个、"
                f"注册检查项 {len(controller.registry.tests)} 个"
                + (f"；当前目标 target_id={target_id}" if target_id else "；当前目标未在注册表中"),
            )
        # ---- 协议：原生 Tool Calling 与 JSON Action 共用同一条执行链 ----
        native_mode = self._native_tools_enabled()
        tool_payload: list[dict[str, Any]] | None = tool_schemas(self.registry) if native_mode else None
        if native_mode:
            audit.append(
                "protocol.native", decision="allow", tools=len(tool_payload or []),
                schema_signature=schema_signature(tool_payload or []),
            )
            state.note("protocol", f"使用原生工具调用（{len(tool_payload or [])} 个 schema，含 finish）")
        # ---- 提示词分层（P0）：Core/Role 进 system，任务契约进首条 user ----
        if bool(getattr(self.config.llm, "prompt_layers", False)):
            layers = build_prompt_layers(
                self.config.scope_prompt_block(),
                self.registry.prompt_block(),
                extra_rules,
                policy_block,
                role_profile=str(getattr(self.config.llm, "role_profile", "") or ""),
                protocol="native" if native_mode else "json",
                include_hypothesis=use_ledger,
            )
            system_prompt, runtime_context = layers.system(), layers.first_user()
            state.note("prompt", "使用分层提示词：Core+Role(system) / Contract(user) / Dynamic(按步)")
            audit.append("prompt.layers", decision="allow", mode="layered", native=native_mode)
        else:
            system_prompt = build_system_prompt(
                self.config.scope_prompt_block(),
                self.registry.prompt_block(),
                extra_rules,
                include_hypothesis=use_ledger,
            )
            runtime_context = build_runtime_context(
                self.config.scope_prompt_block(),
                self.registry.prompt_block(),
                extra_rules,
                include_hypothesis=use_ledger,
            ) + ("\n\n# 已注册检查项\n" + policy_block if policy_block else "")
            audit.append("prompt.layers", decision="allow", mode="legacy", native=native_mode)
        # system 与初始运行合同固定在前缀；每轮状态、观察和模型回复只向后追加。
        # 这样既保持标准 Chat Template，也让供应商缓存看到字节级稳定前缀。
        messages: list[LLMMessage] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": runtime_context},
        ]
        #: 原生协议要求每个 tool_call 都有对应的 tool 消息，缺一条下一轮就会被供应商拒绝
        pending_tool_messages: list[dict[str, Any]] = []
        native_call_id = ""

        def queue_tool_result(text: str) -> None:
            """把观察结果排进 tool 消息队列（JSON 模式为空操作）。"""
            if native_call_id:
                pending_tool_messages.append(
                    {"role": "tool", "tool_call_id": native_call_id, "content": (text or "")[:2000]}
                )

        observation = ""
        last_action_key = ""
        stall = 0
        new_facts = True
        record_handle = Path(record_path).open("w", encoding="utf-8") if record_path else None
        recorded_steps = 0
        record_lock = threading.Lock()
        if record_handle:
            record_handle.write('{"steps": [\n')

        def record_response(text: str, calls: list[Any] | None = None, reasoning: str = "") -> None:
            """保存实际文本与结构化工具调用，保证原生协议也可确定性回放。"""
            nonlocal recorded_steps
            with record_lock:
                if record_handle is None or record_handle.closed:
                    return
                if recorded_steps:
                    record_handle.write(",\n")
                recorded: dict[str, Any] = {"response": text}
                if calls:
                    recorded["tool_calls"] = [
                        {"id": call.id, "name": call.name, "arguments": call.arguments}
                        for call in calls
                    ]
                if reasoning:
                    recorded["reasoning"] = reasoning
                record_handle.write(json.dumps(recorded, ensure_ascii=False))
                record_handle.flush()
                recorded_steps += 1

        ctx.extra["model_response_observer"] = record_response

        def accept_steers() -> bool:
            nonlocal requirements
            from ..session import GoalUpdate
            accepted = False
            if self.steer_queue is not None:
                while True:
                    try:
                        note = self.steer_queue.get_nowait()
                    except Exception:  # noqa: BLE001 - 队列空
                        break
                    if isinstance(note, GoalUpdate):
                        state.goal = str(note)
                        requirements = GoalRequirements.parse(state.goal)
                        audit.append("human.goal", decision="allow", goal=state.goal[:2000])
                        hints.append(f"[目标更新] {state.goal or '已清除'}")
                        messages.append({"role": "user", "content": f"[目标更新] {state.goal or '已清除'}"})
                        accepted = True
                        continue
                    if note:
                        hints.append(f"[人工介入] {note}")
                        messages.append({"role": "user", "content": f"[人工介入] {note}"})
                        state.note("human", f"人工补充：{note}")
                        audit.append("human.steer", decision="allow", message=str(note)[:400])
                        accepted = True
            return accepted

        started = time.monotonic()
        steps = 0
        final: dict[str, Any] | None = None
        final_rejects = 0
        denial_counts: dict[str, int] = {}
        try:
            for index in range(1, max_steps + 1):
                # 人工介入：停止 / 暂停 / 实时提示
                if self.stop_event is not None and self.stop_event.is_set():
                    state.note("human", "收到人工停止指令")
                    state.status = "stopped" if state.status == "running" else state.status
                    break
                if self.pause_event is not None:
                    while self.pause_event.is_set() and not (self.stop_event is not None and self.stop_event.is_set()):
                        time.sleep(0.2)
                accept_steers()
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
                if not missing_goal and not requirements.empty:
                    hints.extend(item for item in requirements.soft_hints(state)[:1] if item not in hints)

                # 确定性调度：挑出本步应验证的最高优先级假设（绑定注册检查项）
                schedule = None
                if scheduler is not None:
                    schedule = scheduler.decide(
                        ledger,
                        controller.registry.tests_for(target_id) if target_id else [],
                        executed_tests=set(controller.executed),
                        goal_classes=requirements.classes,
                    )
                    state.scheduler = scheduler.to_dict()
                    if schedule is not None:
                        state.note(
                            "scheduler",
                            f"调度：验证 {schedule.hypothesis_id}（优先级 {schedule.score:.2f}）→ "
                            + (f"检查项 {schedule.test_id}（{schedule.tool}）" if schedule.test_id else f"自由验证 {schedule.vuln_class}")
                            + ("；强制" if schedule.required else ""),
                        )

                step_prompt = build_step_prompt(
                    state_digest=state.summary_for_prompt(),
                    last_observation=observation,
                    hints=hints,
                    runtime_context="",
                    budget={
                        "阶段步数": f"{planner.phase_step}/{planner.max_steps_per_phase}",
                        "总步数": f"{index}/{max_steps}",
                        "剩余秒": int(ctx.remaining() or 0),
                    },
                    # 任务硬性要求放在显眼位置：否则模型容易被"顺路发现"带偏，把预算花在目标之外
                    extra_context=(
                        (
                            commander_context_block() + "\n\n"
                            if (commander_briefs or commander_brief_provider)
                            else ""
                        )
                    )
                    + (
                        (
                            "## 未完成的任务要求\n"
                            + "\n".join(f"- {item}" for item in missing_goal)
                            + "\n\n"
                        )
                        if missing_goal
                        else ""
                    )
                    + (
                        "## 假设台账（按优先级排序）\n" + ledger.prompt_block()
                        if use_ledger
                        else ""
                    )
                    + ("\n" + schedule.prompt_block() if schedule is not None else ""),
                )
                hints = []

                # 原生协议：先把上一轮的 tool 结果补进历史，再追加本步提示
                # （顺序必须是 assistant(tool_calls) → tool(...) → user，否则供应商直接 400）
                if pending_tool_messages:
                    messages.extend(pending_tool_messages)  # type: ignore[arg-type]
                    pending_tool_messages.clear()
                messages.append({"role": "user", "content": step_prompt})
                native_call_id = ""
                response = self._ask(messages, tool_payload)
                if not response.ok:
                    failure_kind = classify_llm_response(response)
                    state.counters[f"llm_{failure_kind}"] = state.counters.get(f"llm_{failure_kind}", 0) + 1
                    state.counters["errors"] = state.counters.get("errors", 0) + 1
                    state.note("llm_error", f"[{failure_kind}] {response.error}")
                    self.logger(f"[{index}] LLM 失败({failure_kind}): {response.error}")
                    audit.append(
                        "llm.error", model=self.backend.config.model, error=response.error[:300],
                        step=index, kind=failure_kind,
                    )
                    # 协议不支持：降级一次，之后不再发原生请求（重试不会改变供应商结论）
                    if (
                        failure_kind == "protocol_unsupported"
                        and native_mode
                        and bool(getattr(self.config.llm, "tool_protocol_fallback", True))
                    ):
                        native_mode, tool_payload = False, None
                        state.note("protocol", "供应商不支持原生工具调用，已降级为 JSON Action")
                        audit.append("protocol.downgrade", step=index, decision="allow", reason=response.error[:200])
                        hints.append("协议已降级为 JSON 动作")
                        observation = (
                            "供应商不支持原生工具调用，已切换到 JSON 动作协议。"
                            '请只返回一个 JSON 对象：{"action": "工具名", "params": {...}} 或 {"final": {...}}。'
                        )
                        continue
                    decision = decide_after_failure(failure_kind, attempt=state.counters["errors"])
                    if decision == "stop" or state.counters["errors"] >= MAX_LLM_FAILURES:
                        state.status = "failed"
                        break
                    hints.append("上一步推理失败，请直接给出一个工具动作")
                    observation = f"LLM 调用失败（{failure_kind}）：{response.error}"
                    time.sleep(min(2.0 * state.counters["errors"], 8.0))
                    continue
                state.counters["llm_calls"] = state.counters.get("llm_calls", 0) + 1
                if self.stop_event is not None and self.stop_event.is_set():
                    state.note("human", "收到人工停止指令")
                    state.status = "stopped"
                    break
                if accept_steers():
                    audit.append("llm.response_superseded", decision="defer", reason="收到补充，重新决策", step=index)
                    continue
                # assistant 消息必须原样带回 tool_calls 与 reasoning_content：
                # DeepSeek 思考模式要求上一轮思维链出现在同一条消息里，否则下一轮 400。
                assistant_message: dict[str, Any] = {"role": "assistant", "content": response.text}
                native_calls = list(response.tool_calls) if native_mode else []
                if native_calls:
                    assistant_message["tool_calls"] = [call.to_dict() for call in native_calls]
                    native_call_id = native_calls[0].id
                    # 本步只执行一个动作；其余调用也必须回一条 tool 消息，保持协议完整
                    for extra in native_calls[1:]:
                        pending_tool_messages.append({
                            "role": "tool", "tool_call_id": extra.id,
                            "content": "未执行：每步只允许一个动作，请在下一步单独调用。",
                        })
                if response.reasoning:
                    assistant_message["reasoning_content"] = response.reasoning
                messages.append(assistant_message)  # type: ignore[arg-type]
                record_response(response.text, response.tool_calls, response.reasoning)

                action = self._decode_action(response, native_calls)
                if native_calls:
                    state.counters["native_calls"] = state.counters.get("native_calls", 0) + 1
                if "error" in action:
                    state.counters["errors"] = state.counters.get("errors", 0) + 1
                    state.counters["protocol_error"] = state.counters.get("protocol_error", 0) + 1
                    hints.append("输出必须是单个 JSON 对象：{\"thought\":..., \"action\":..., \"params\":{...}}")
                    observation = f"动作解析失败：{action.get('error')}；原始输出片段：{str(action.get('raw', ''))[:200]}"
                    state.log_step(
                        StepRecord(index, phase, summary="解析失败", error=action.get("error", ""), llm_seconds=response.duration).to_dict()
                    )
                    queue_tool_result(observation)
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
                        queue_tool_result(observation)
                        continue
                    final = action.get("final") if isinstance(action.get("final"), dict) else {"summary": str(action.get("final"))}
                    state.note("final", str(final.get("summary", ""))[:400])
                    state.log_step(
                        StepRecord(index, phase, thought=str(action.get("thought", "")), action="final", summary=str(final.get("summary", ""))[:200], ok=True).to_dict()
                    )
                    break

                tool_name = str(action.get("action", ""))
                params = action.get("params") if isinstance(action.get("params"), dict) else {}
                params = dict(params)
                thought = str(action.get("thought", ""))
                # 模型常把 test_id 写进 params（两种写法都接受，执行前必须摘掉，
                # 否则会被参数校验判成"未知参数"而白白浪费一步）
                inline_test_id = params.pop("test_id", "") if isinstance(params.get("test_id"), str) else ""
                test_id = str(action.get("test_id", "") or inline_test_id or "")
                action_target = str(params.get("target") or params.get("url") or target)
                level = self._effective_level(tool_name, params)
                hypothesis = ledger.from_action(action) if use_ledger else None
                if use_ledger and action.get("hypothesis_id") and hypothesis is None:
                    hints.append("hypothesis_id 不存在：请引用台账中的编号或提出新假设")

                # 独立控制器：在执行层逐动作批准（不认模型的自我声明）
                policy_decision = controller.check(
                    {
                        "index": index,
                        "tool": tool_name,
                        "params": params,
                        "level": level,
                        "target": action_target,
                        "test_id": test_id,
                    }
                )
                if not policy_decision.allowed:
                    if hypothesis is not None:
                        ledger.record_outcome(hypothesis.id, new_information=False, blocked=True,
                                              note=policy_decision.reason)
                    state.counters["denied"] = state.counters.get("denied", 0) + 1
                    state.note("policy", f"控制器拒绝：{policy_decision.reason}")
                    state.note_boundary(
                        tool=tool_name,
                        reason=policy_decision.reason,
                        kind=policy_decision.kind,
                        target=action_target,
                        test_id=test_id,
                    )
                    hints.append(
                        policy_decision.hint
                        or f"控制器未批准该动作（{policy_decision.kind}）：{policy_decision.reason}"
                    )
                    observation = (
                        f"独立授权控制器拒绝了该动作（{policy_decision.kind}）：{policy_decision.reason}。"
                        "请改用已注册的检查项，或停止并把未覆盖项写进报告。"
                    )
                    state.log_step(
                        StepRecord(index, phase, thought=thought, action=tool_name, denied=True,
                                   summary=f"控制器拒绝（{policy_decision.kind}）").to_dict()
                    )
                    queue_tool_result(observation)
                    continue

                # guided 模式可能由控制器反查出检查项，审批与调度统计使用同一解析结果。
                test_id = policy_decision.test_id or test_id
                if self.approval is not None:
                    request = {
                        "index": index,
                        "phase": phase,
                        "tool": tool_name,
                        "level": level,
                        "params": params,
                        "thought": thought,
                        "target": action_target,
                        "test_id": test_id,
                    }
                    if not self.approval(request):
                        if hypothesis is not None:
                            ledger.record_outcome(hypothesis.id, new_information=False, blocked=True,
                                                  note="人工审批未通过")
                        state.note("human", f"人工拒绝动作：{tool_name}（{level}）")
                        audit.append("human.reject", step=index, tool=tool_name, level=level, decision="deny")
                        state.note_boundary(tool=tool_name, reason="人工审批未通过", kind="human", target=action_target)
                        hints.append(f"人工拒绝了 {tool_name}：换一个更保守的动作，或先解释为什么需要它")
                        observation = f"人工审批未通过：动作 {tool_name} 被操作者拒绝，请改用其他方式推进。"
                        state.log_step(
                            StepRecord(index, phase, thought=thought, action=tool_name, denied=True,
                                       summary="人工拒绝（未执行）").to_dict()
                        )
                        queue_tool_result(observation)
                        continue

                self.logger(f"[{index}/{max_steps}] {phase} · {tool_name} {_brief_params(params)}")
                audit.append(
                    "react.action", step=index, phase=phase, thought=thought[:300], tool=tool_name, params=params,
                    # 假设进哈希链：state.json 可改，审计链不可改——"为什么做这步"必须可追溯
                    hypothesis=(
                        {
                            "id": hypothesis.id,
                            "statement": hypothesis.statement[:200],
                            "confidence": hypothesis.confidence,
                            "priority": round(hypothesis.priority(), 4),
                        }
                        if hypothesis is not None
                        else None
                    ),
                    reasoning_on_existing=str(action.get("hypothesis_id") or "") or None,
                )
                before_knowledge = state.knowledge_digest()

                result = self.registry.execute(tool_name, params, ctx)
                state.coverage = controller.coverage()
                observation = result.to_observation()
                queue_tool_result(observation)
                new_facts = before_knowledge != state.knowledge_digest()

                # 假设台账：模型可在动作里带 hypothesis；无论成败都用结果更新（确定性规则，不看自我宣告）
                if hypothesis is not None:
                    updated = ledger.record_tool_outcome(
                        hypothesis.id, result=result, state=state, tool=tool_name, new_information=new_facts,
                    )
                    if updated is not None:
                        state.note("hypothesis", f"{updated.id} → {updated.status}：{updated.statement[:80]}")

                # 循环识别（结果指纹）与失败分类：把"重试"变成有依据的升级阶梯
                advice = loop_guard.observe(tool_name, params, result, new_information=new_facts)
                if advice and use_guard:
                    hints.append(advice)
                    state.note("loop", advice)
                elif advice:
                    state.loop["last_advice"] = advice  # 对照臂：照样记录，但不影响模型
                failure_kind = classify_failure(result, new_information=new_facts)
                if failure_kind != "ok":
                    state.counters[f"fail_{failure_kind}"] = state.counters.get(f"fail_{failure_kind}", 0) + 1
                    fallback = advice_for(
                        failure_kind,
                        alternatives=alternatives_for(self.registry, tool_name, tried=loop_guard.tried_tools()),
                        attempt=loop_guard.escalation_step,
                    )
                    if fallback and use_guard:
                        hints.append(fallback)
                if use_guard and loop_guard.needs_human():
                    state.note("human_request", "循环守卫：连续多次无新信息，建议人工介入调整方向")

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

                # 调度遵从性：模型是否按"这一步该验证谁"执行（未遵从只升级提示，不阻断、不代编证据）
                if schedule is not None:
                    followed = scheduler.observe(schedule, test_id=test_id, tool=tool_name)
                    state.scheduler = scheduler.to_dict()
                    if followed is False:
                        state.counters["scheduler_miss"] = state.counters.get("scheduler_miss", 0) + 1
                        state.note(
                            "scheduler",
                            f"未按调度执行：应验证 {schedule.hypothesis_id}"
                            f"（{schedule.test_id or schedule.tool or schedule.vuln_class}），实际 {tool_name}",
                        )
                        hints.append(
                            f"上步检查项：期望 {schedule.test_id or schedule.tool or schedule.vuln_class}，实际 {test_id or tool_name}"
                        )
                    elif followed is True:
                        state.note("scheduler", f"按调度执行：{schedule.hypothesis_id} ← {test_id or tool_name}")
                    audit.append(
                        "react.schedule",
                        step=index,
                        hypothesis_id=schedule.hypothesis_id,
                        expected=schedule.test_id or schedule.tool,
                        actual=test_id or tool_name,
                        followed=followed,
                    )

                if result.denied:
                    state.note_boundary(
                        tool=tool_name,
                        reason=result.error or result.summary,
                        kind=result.category or "guard",
                        target=action_target,
                        test_id=test_id,
                    )
                    denial_key = f"{tool_name}|{result.category}"
                    denial_counts[denial_key] = denial_counts.get(denial_key, 0) + 1
                    if denial_counts[denial_key] >= 2:
                        hints.append(
                            f"{tool_name} 已因「{result.category}」被拒绝 {denial_counts[denial_key]} 次："
                            "禁止再用同样的参数/等级调用它，必须换成更低等级的等价工具或改成合规参数"
                        )
                        planner.force_advance()
                elif not result.ok:
                    # 普通失败（目标不可达、工具报错）单独计数：
                    # 结论判定里它意味着"没形成判断"，而不是"范围内未确认"
                    state.counters["failures"] = state.counters.get("failures", 0) + 1
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

                # 预算与停止合同：连续无新增证据 / 检查数 / 逻辑预算到点就停，并留下覆盖说明
                if controller.note_evidence(new_facts):
                    state.note("budget", f"按停止合同收尾：{controller.stop_reason()}")
                    break
                stop_reason = controller.stop_reason()
                if stop_reason:
                    state.note("budget", f"按停止合同收尾：{stop_reason}")
                    break
                # 周期落盘：让外部（trina watch / 编辑器 / 另一个终端）能实时看到进展
                every = int(getattr(self.config.run, "flush_state_every", 1) or 0)
                if every and index % every == 0:
                    state.hypotheses = ledger.to_dict()
                    state.loop = loop_guard.to_dict()
                    if scheduler is not None:
                        state.scheduler = scheduler.to_dict()
                    self._save_state(state, workspace)
        finally:
            ctx.extra.pop("model_response_observer", None)
            with record_lock:
                if record_handle:
                    record_handle.write("\n]}\n")
                    record_handle.close()

        # ------------------------------------------------------------------ 收尾
        if final is not None and (final.get("status") or final.get("flags")):
            state.note("final_claim", "模型自评与 flag 声明仅作摘要；运行结果以工具事实判定")
        state.ended_at = time.time()
        verified = [f for f in state.findings if f.status in ("verified", "exploited") and f.evidence]
        if state.status == "stopped":
            pass
        elif not requirements.empty:
            # 有明确任务要求时，以「要求是否达成」判定成败：模型自评与 flag 都不能替代验证
            if requirements.satisfied(state):
                state.status = "success"
            elif state.flags or state.findings:
                state.status = "partial"
            else:
                state.status = "failed"
        elif state.status == "running":
            if state.flags or verified:
                state.status = "success"
            elif state.findings or state.hosts or state.counters.get("denied", 0):
                state.status = "partial"
            else:
                state.status = "failed"
        state.ledger = self.backend.ledger.delta(usage_before).to_dict()
        unfinished = ledger.finalize()
        if unfinished:
            state.note(
                "hypotheses",
                f"本轮仍有 {len(unfinished)} 条假设未解决（既未确认也未否定，已列入待验证）："
                + "；".join(f"{item.id} {item.statement[:40]}" for item in unfinished[:3])
                + ("…" if len(unfinished) > 3 else ""),
            )
        state.hypotheses = ledger.to_dict()
        state.loop = loop_guard.to_dict()
        if scheduler is not None:
            state.scheduler = scheduler.to_dict()
            if scheduler.decisions:
                state.note(
                    "scheduler",
                    f"调度小结：决策 {scheduler.decisions} 次，按调度执行 {scheduler.followed} 次、"
                    f"未遵从 {scheduler.missed} 次（遵从率 {scheduler.compliance_rate:.2f}）",
                )
                audit.append("run.scheduler", **scheduler.to_dict())

        # 结论状态与覆盖说明（P3：没有得到证据 ≠ 获得否定证据，检查没跑不能当作通过）
        coverage = controller.coverage()
        state.coverage = coverage
        executed = list(coverage.get("executed") or [])
        error_count = state.counters.get("errors", 0)
        failures = state.counters.get("failures", 0)
        if state.status == "blocked" or not any(step.get("kind") == "tool" and step.get("ok") for step in state.steps):
            verdict = "INCONCLUSIVE"
        elif verified:
            verdict = "CONFIRMED"
        elif error_count or failures or coverage.get("revoked") or state.counters.get("denied", 0):
            # 工具失败/超时/撤权/被拒 → 无法形成判断（不能写成"没问题"）
            verdict = "INCONCLUSIVE"
        else:
            verdict = "NOT_CONFIRMED"
        state.verdict = verdict
        state.note(
            "verdict",
            f"结论状态 {verdict}（{VERDICT_CN.get(verdict, '')}）；已测 {len(executed)} 项，"
            f"未测 {len(coverage.get('untested') or [])} 项，停止原因：{coverage.get('stop_reason') or '正常收敛'}",
        )

        # 证据关系图：让"结论 → 证据 → 检查项/假设"可反向追溯
        try:
            from ..evidence_graph import build as build_graph

            graph = build_graph(state, registry=controller.registry, hypotheses=state.hypotheses)
            state.graph = graph.to_dict()
            state.note("graph", graph.summary())
        except Exception as exc:  # noqa: BLE001 - 绘图失败不影响结论
            state.graph = {"error": f"{type(exc).__name__}: {exc}"}

        ledger_summary = ledger.summary()
        state.note(
            "hypotheses",
            f"假设台账：共 {ledger_summary['total']} 条（待验证 {ledger_summary['open']}、"
            f"已确认 {ledger_summary['confirmed']}、已否定 {ledger_summary['refuted']}、被阻断 {ledger_summary['blocked']}）",
        )
        audit.append(
            "run.verdict",
            verdict=verdict,
            executed=len(executed),
            untested=len(coverage.get("untested") or []),
            checks_used=coverage.get("checks_used"),
            stop_reason=coverage.get("stop_reason", ""),
            boundary_events=len(state.boundary_events),
        )
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
    def _native_tools_enabled(self) -> bool:
        """是否走原生 Tool Calling。

        ``auto`` 看后端能力，``json`` 强制旧协议（对照实验用），``native`` 只在后端支持时生效。
        """
        protocol = str(getattr(self.config.llm, "tool_protocol", "auto") or "auto")
        if protocol == "json":
            return False
        return bool(getattr(self.backend, "supports_native_tools", False))

    @staticmethod
    def _decode_action(response: Any, native_calls: list[Any]) -> dict[str, Any]:
        """把模型响应解码成统一 action 字典；原生与 JSON 两种输出共用下游逻辑。

        ``finish`` 是控制面：它只产生 ``{"final": ...}``，**不会**进入 ToolRegistry。
        """
        if native_calls:
            call = native_calls[0]
            params, error = parse_arguments(call.arguments)
            if error:
                return {"error": f"{call.name} 参数解析失败：{error}"}
            if call.name == FINISH_TOOL_NAME:
                return {"final": params}
            return {"action": call.name, "params": params}
        return parse_action(response.text)

    def _effective_level(self, tool_name: str, params: dict[str, Any]) -> str:
        """取工具在本次参数下的实际动作等级（动态等级优先，异常时回退静态声明）。"""
        spec = self.registry.get(tool_name)
        if spec is None:
            return "unknown"
        try:
            return spec.required_level(params) or spec.spec.action_level
        except Exception:  # noqa: BLE001
            return spec.spec.action_level

    def _ask(self, messages: list[LLMMessage], tools: list[dict[str, Any]] | None = None):
        """带重试的 LLM 调用。

        重试按失败类型给预算（见 :func:`trina.brain.failure.decide_after_failure`）：
        协议不支持立刻返回让调用方降级，权限拒绝直接停，只有运行故障才退避重试。
        """
        last = None
        attempt = 0
        while True:
            # 只用后端**自己声明**的完整消息接口：包装器（如测试用的记录后端）
            # 通过 __getattr__ 转发时不能用内层实现，否则观察不到真实提示词。
            method = self.backend.complete_messages if "complete_messages" in dir(type(self.backend)) else None
            if method is not None:
                if tools is not None:
                    response = method(messages, tools=tools)
                else:
                    response = method(messages)
            else:  # 仅供旧测试替身
                response = self.backend.complete(messages[0]["content"], messages[-1]["content"])
            if response.ok and (response.text.strip() or response.tool_calls):
                return response
            last = response
            kind = classify_llm_response(response)
            action = decide_after_failure(kind, attempt=attempt)
            if action in ("stop", "downgrade"):
                return response
            attempt += 1
            if attempt > MAX_LLM_RETRIES:
                return response
            time.sleep(min(2.0 * attempt, 10.0))

    def _save_state(self, state: RunState, workspace: Path) -> None:
        temporary: Path | None = None
        try:
            path = workspace / "state.json"
            # 同目录临时文件替换，读者只能看到完整旧快照或完整新快照。
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=workspace,
                                             prefix="state-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(state.to_dict(), handle, ensure_ascii=False, indent=1)
            temporary.replace(path)
            state.note("artifact", f"状态已保存: {path}")
        except OSError as exc:  # pragma: no cover - 磁盘异常不应影响结论
            state.note("error", f"状态保存失败: {exc}")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


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
    config: TrinaConfig,
    target: str,
    *,
    goal: str = "",
    max_steps: int | None = None,
    logger: Callable[[str], None] | None = None,
) -> RunState:
    """一次性调用的便捷入口。"""
    return PentestAgent(config, logger=logger).run(target, goal=goal, max_steps=max_steps)


__all__ = ["PentestAgent", "StepRecord", "run_pentest"]
