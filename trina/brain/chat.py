"""对话式大脑：自然语言进来 → 大模型回答；需要动手时它自己调工具（仍走同一套护栏/控制器/审计）。

与 `PentestAgent` 的区别
------------------------
* `PentestAgent` 是**任务执行**：给定目标与目标要求，按 PTES 阶段机跑到底，产出结论与覆盖；
* `ChatAgent` 是**对话**：用户随口说什么就答什么，想做就调工具，做完继续对话（多轮上下文）。

共用部分（这是关键）：工具执行、控制器授权、护栏、审计、审批、人工介入全部复用同一实现，
所以"聊天里让模型干活"和"一条命令开打"的安全级别完全一致——模型依旧不能扩大范围。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import fields
from typing import Any, Callable

from ..audit import AuditLog
from ..config import TrinaConfig
from ..llm import LLMBackend, LLMMessage, LLMStreamEvent, make_backend
from ..scope import ScopeGuard
from ..state import RunState
from ..tools import ToolRegistry, build_registry
from ..tools.http import HttpClient
from ..tools.registry import ToolContext
from .failure import LoopGuard, advice_for, classify_failure

def render_chat_prompt(include_hypothesis: bool = True) -> str:
    """对话 Assistant 与任务 Agent 共用安全工程师系统提示词。"""
    from .prompts import render_system_prompt

    return render_system_prompt(include_hypothesis)


class ChatAgent:
    """把"对话"和"调用工具"合在一个循环里。"""

    MAX_TOOL_STEPS = 12

    def __del__(self) -> None:
        # 异常退出或旧调用方未显式 close 时，仅释放占用，不执行或写入数据。
        stream = getattr(self, "_file_lock", None)
        if stream is not None:
            stream.close()

    def __init__(
        self,
        config: TrinaConfig,
        *,
        registry: ToolRegistry | None = None,
        backend: LLMBackend | None = None,
        audit: AuditLog | None = None,
        approval: Callable[[dict[str, Any]], bool] | None = None,
        steer_queue: Any | None = None,
        stop_event: Any | None = None,
        pause_event: Any | None = None,
        controller: Any | None = None,
        logger: Callable[[str], None] | None = None,
        on_tool: Callable[[Any], None] | None = None,
        on_stream: Callable[[str, bool], None] | None = None,
        run_id: str | None = None,
        resume_saved: bool = False,
    ):
        self.config = config
        self.registry = registry or build_registry()
        self.backend = backend or make_backend(config.llm)
        self.logger = logger or (lambda message: None)
        self.on_tool = on_tool
        self.on_stream = on_stream
        self.approval = approval
        self.steer_queue = steer_queue
        self.stop_event = stop_event
        self.pause_event = pause_event
        self._controller = controller

        from ..chat_store import ChatStore
        self.store = ChatStore(config.workspace_dir)
        self.run_id = run_id or f"chat-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self._file_lock = self.store.acquire(self.run_id)
        self._closed = False
        self.in_flight = False
        self.title = ""
        try:
            # 先取得独占锁再读取，避免读取到另一实例关闭保存前的旧快照。
            resume_data = self.store.load(self.run_id) if resume_saved else None
            self.audit = audit or AuditLog.open(
                config.audit_dir, run_id=self.run_id, redact_keys=config.audit.redact_keys,
                record_output_digest=config.audit.record_output_digest, resume=resume_saved)
        except Exception:
            self._file_lock.close()
            raise
        self.guard = ScopeGuard(config.scope, audit=self.audit)
        self.state = RunState(run_id=self.run_id, target="", goal="交互对话", status="chat")
        self.workspace = config.workspace_dir / self.run_id
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.ctx = ToolContext(
            config=config,
            guard=self.guard,
            audit=self.audit,
            state=self.state,
            http=HttpClient(self.guard, timeout=15.0),
            workspace=self.workspace,
            tools=self.registry,
            deadline=None,
            logger=self.logger,
        )
        self.history: list[dict[str, str]] = []
        self._messages: list[LLMMessage] = []
        self._messages_ready = False
        self._pending_stream: _VisibleStream | None = None
        self.last_reply_streamed = False
        self.current_request = ""
        self.ctx.extra["llm_backend"] = self.backend
        from ..conversation import ConversationContext
        self.context = ConversationContext(self.ctx)
        self.turns = 0
        #: 假设台账与循环守卫：对话里同样"提假设 → 按优先级验证 → 无新信息就换路"
        from ..hypotheses import HypothesisLedger

        self.ledger = HypothesisLedger()
        self.loop_guard = LoopGuard()
        # 洞察层开关（与任务模式同源配置）：关闭只影响"模型看到什么"，统计口径不变
        self.use_ledger = bool(getattr(config.run, "hypothesis_ledger", True))
        self.use_guard = bool(getattr(config.run, "loop_guard", True))
        from ..llm import LLMUsage
        self.usage = LLMUsage(calls=0)
        self.usage_gaps = {"missing_" + name: 0 for name in
                           ("tokens", "input", "output", "cache", "cache_read", "cache_write", "cost")}
        if resume_data is not None:
            try:
                self._restore(resume_data)
            except Exception:
                self.ctx.extra["background_jobs"].close()
                self._file_lock.close()
                raise

    # ------------------------------------------------------------------ 控制器
    @property
    def controller(self) -> Any:
        if self._controller is None:
            from ..policy import AuthorizationController, Budget, PolicyRegistry

            policy = self.config.policy
            self._controller = AuthorizationController(
                PolicyRegistry.load(self.config, extra_dirs=list(policy.registry_dirs)),
                mode=policy.mode,
                budget=Budget(policy.max_checks, policy.logic_seconds, policy.stop_on_no_new_evidence),
                audit=self.audit,
            )
        self.ctx.extra["authorization_controller"] = self._controller
        return self._controller

    # ------------------------------------------------------------------ 主循环
    def ask(self, message: str) -> str:
        if self._closed:
            raise ValueError("对话已关闭，请使用 /resume 恢复")
        self.in_flight = True
        try:
            return self._ask_turn(message)
        finally:
            self.in_flight = False
            self.save()

    def _ask_turn(self, message: str) -> str:
        """处理一条用户消息：回答，或边执行工具边推进；返回最终自然语言回复。"""
        text = (message or "").strip()
        if not text:
            return ""
        self.turns += 1
        self.current_request = text
        if not self.title:
            self.title = " ".join(text.split())[:80]
        self.context.remember_user(text)
        self.last_reply_streamed = False
        self._initialize_messages()
        self.audit.append("chat.user", decision="allow", message=text[:400])
        self.logger(f"[用户] {text}")
        self.history.append({"role": "user", "content": text})
        self._messages.append({"role": "user", "content": text})
        self.save()

        for step in range(1, self.MAX_TOOL_STEPS + 1):
            if self.stop_event is not None and self.stop_event.is_set():
                return "（已按你的要求停止）"
            if self.pause_event is not None:
                while self.pause_event.is_set() and not (self.stop_event is not None and self.stop_event.is_set()):
                    time.sleep(0.2)
            self._accept_steer()

            response = self._ask_model(step)
            if self.stop_event is not None and self.stop_event.is_set():
                self._cancel_stream()
                return "（已按你的要求停止）"
            if self._accept_steer():
                self.audit.append("chat.response_superseded", decision="defer", reason="收到补充，重新决策")
                self._cancel_stream()
                continue
            if not response.ok:
                self._cancel_stream()
                return f"（模型调用失败：{response.error}）"
            # 结构化 API 历史保留模型实际返回；展示层可以只显示 final 摘要。
            self._messages.append({"role": "assistant", "content": response.text})
            action, note = _parse_action_lenient(response.text)
            if action is None:
                from .prompts import _looks_like_dsml

                if _looks_like_dsml(response.text):
                    # 不把模型内部协议残片当正式答复。保留一次简短的结构化错误，
                    # 让下一轮按 Trina JSON 合同重发；原始残片只留摘要审计。
                    self.audit.append(
                        "chat.protocol_recovery", decision="retry", protocol="dsml",
                        chars=len(response.text), preview=response.text[:160],
                    )
                    self.history.append({"role": "assistant", "content": "[DSML 工具调用未完成，未执行]"})
                    recovery = {
                        "role": "user",
                        "content": "[协议错误] 不要输出 DSML/XML。需要操作时仅按本轮合同返回一个 JSON action；请重新决定。",
                    }
                    self.history.append(recovery)
                    self._messages.append(recovery)
                    self._cancel_stream()
                    self.logger("[协议恢复] 已拦截 DSML 工具调用残片，要求模型按 JSON 重发")
                    continue
                reply = response.text.strip()
                self.history.append({"role": "assistant", "content": reply})
                self.audit.append("chat.answer", decision="allow", chars=len(reply))
                self._finish_stream(reply)
                return reply
            if note:
                self.logger(f"说明：{note[:200]}")

            if "final" in action:
                final = action.get("final")
                reply = str(final.get("summary") if isinstance(final, dict) else final) or "（完成）"
                self.history.append({"role": "assistant", "content": reply})
                self._finish_stream(reply)
                return reply

            # 保留动作与观察的因果关系；工具结果不能成为孤立的用户指令。
            self.history.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
            self._cancel_stream()
            self.save()
            observation = self._run_action(action, step)
            tool_message = {"role": "user", "content": f"[工具结果] {observation}"}
            self.history.append(tool_message)
            self._messages.append(tool_message)
            self.save()

        self.logger(f"[提示] 单条消息内已执行 {self.MAX_TOOL_STEPS} 个动作，先给你阶段性结果")
        return (
            f"（本次已连续执行 {self.MAX_TOOL_STEPS} 个动作，先停一下避免失控。"
            "接着回复「继续」我就从当前进度往下做——已发现的结果我都记在会话里了。）"
        )

    # ------------------------------------------------------------------ 内部
    def _initialize_messages(self) -> None:
        """固定 system 与初始运行合同只写一次，后续严格追加，稳定缓存前缀。"""
        if self._messages_ready:
            return
        tool_block = self.registry.prompt_block()
        from .prompts import build_runtime_context

        if self.config.llm.prompt_layers:
            from .prompts import build_prompt_layers
            layers = build_prompt_layers(self.config.scope_prompt_block(), tool_block,
                                         include_hypothesis=self.use_ledger, chat=True,
                                         role_profile=self.config.llm.role_profile)
            system, contract = layers.system(), layers.first_user()
        else:
            system = render_chat_prompt(self.use_ledger)
            contract = build_runtime_context(self.config.scope_prompt_block(), tool_block,
                                             include_hypothesis=self.use_ledger, chat=True)
        self._messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": contract,
                "cache_scope": "stable",
            },
        ]
        self._messages_ready = True

    def _ask_model(self, step: int = 0):
        registered = self.controller.registry.prompt_block(
            self.controller.registry.match_target(self.state.target).target_id  # type: ignore[union-attr]
            if self.state.target and self.controller.registry.match_target(self.state.target)
            else ""
        )
        from ..conversation import ContextBudgetError, state_projection
        runtime_parts = [self.context.state_update(state_projection(
            self.state, hypotheses=self.ledger.to_dict() if self.use_ledger else None, registered=registered))]
        from ..runtime_status import render_agent_status
        last_observation = self.history[-1]["content"] if self.history and self.history[-1]["content"].startswith("[工具结果]") else ""
        runtime_parts.append(render_agent_status(self.ctx, step=step, max_steps=self.MAX_TOOL_STEPS,
                                               turn=self.turns, observation=last_observation))
        # 动态状态作为新的 user 消息追加；绝不重写 system 或把历史压成角色标签字符串。
        self._messages.append({"role": "user", "content": "\n\n".join(runtime_parts)})
        try:
            self.context.prepare(self._messages)
        except ContextBudgetError as exc:
            from ..llm import LLMResponse
            self.state.context_stats = {**self.context.stats, "budget_error": str(exc)}
            self.audit.append("context.budget_error", error=str(exc))
            return LLMResponse(ok=False, error=str(exc))
        diagnostic = self.context.before_request(self._messages)
        self.state.counters["llm_calls"] = self.state.counters.get("llm_calls", 0) + 1
        self._pending_stream = _VisibleStream(self.on_stream)
        method = getattr(self.backend, "complete_messages", None)
        if method is None:  # 仅供旧测试替身
            response = self.backend.complete(self._messages[0]["content"], self._messages[-1]["content"])
        else:
            response = method(self._messages, on_event=self._pending_stream.on_event)
        self.context.after_response(diagnostic, response)
        self.usage = self.usage.merge(response.usage)
        for name in self.usage_gaps:
            self.usage_gaps[name] += int(not getattr(response.usage, name[8:] + "_reported"))
        return response

    def _finish_stream(self, reply: str) -> None:
        if self._pending_stream is not None and self.on_stream is not None:
            self._pending_stream.finish(reply)
            self.last_reply_streamed = True
        self._pending_stream = None

    def _cancel_stream(self) -> None:
        if self._pending_stream is not None:
            self._pending_stream.cancel()
        self._pending_stream = None

    def close(self) -> None:
        """关闭会话时回收后台作业，并记录最终快照。"""
        if self._closed:
            return
        from ..runtime_status import status_snapshot
        try:
            self.ctx.extra["background_jobs"].close()
            status_snapshot(self.ctx, turn=self.turns)
            self.save()
        finally:
            self._closed = True
            self._file_lock.close()

    def save(self) -> None:
        """只保存对话运行数据；标准消息和最近显示记录分别保留。"""
        if not self._messages_ready or not self.history or self._closed:
            return
        context = self.context
        self.store.save({"version": 1, "run_id": self.run_id, "project": self.store.project,
                         "title": self.title, "turns": self.turns, "in_flight": self.in_flight,
                         "history": self.history, "messages": self._messages, "state": self.state.to_dict(),
                         "hypotheses": self.ledger.to_dict(), "usage": self.usage.to_dict(),
                         "usage_gaps": self.usage_gaps,
                         "controller": self._controller.coverage() if self._controller is not None else {},
                         "controller_elapsed": self._controller.elapsed if self._controller is not None else 0,
                         "stale_steps": self._controller.stale_steps if self._controller is not None else 0,
                         "context": {"revision": context.revision, "epoch": context.epoch,
                                     "snapshot": context.snapshot, "instructions": context.instructions,
                                     "stats": context.stats, "events": context.events,
                                     "last_request": context.last_request.decode("utf-8"),
                                     "last_signature": context.last_signature},
                         "model": self.config.llm.model})

    def _restore(self, data: dict) -> None:
        from ..hypotheses import Hypothesis
        from ..llm import LLMUsage
        if data["run_id"] != self.run_id or data["project"] != self.store.project:
            raise ValueError("对话不属于当前项目")
        self.state = RunState.from_dict(data["state"])
        if self.state.run_id != self.run_id:
            raise ValueError("对话运行状态与 ID 不一致")
        self.ctx.state = self.state
        self.history = data["history"]
        self._messages, self._messages_ready = data["messages"], True
        self.turns, self.title = data["turns"], data["title"]
        allowed = {item.name for item in fields(Hypothesis)}
        for row in data.get("hypotheses", []):
            item = Hypothesis(**{key: value for key, value in row.items() if key in allowed})
            self.ledger.items[item.id] = item
        self.ledger._seq = max((int(key[2:]) for key in self.ledger.items if key.startswith("H-") and key[2:].isdigit()), default=0)
        usage_fields = {item.name for item in fields(LLMUsage)}
        self.usage = LLMUsage(**{key: value for key, value in data.get("usage", {}).items() if key in usage_fields})
        self.usage_gaps.update({key: int(value) for key, value in data.get("usage_gaps", {}).items() if key in self.usage_gaps})
        saved = data.get("context", {})
        for name in ("revision", "epoch", "snapshot", "instructions", "stats", "events", "last_signature"):
            if name in saved:
                setattr(self.context, name, saved[name])
        self.context.last_request = saved.get("last_request", "").encode("utf-8")
        # 不继承旧权限判据或进程，只按当前配置创建控制器。
        controller = self.controller
        coverage = data.get("controller", {})
        controller.checks_used = int(coverage.get("checks_used", self.state.counters.get("tool_calls", 0)))
        controller.started_at = time.monotonic() - float(data.get("controller_elapsed", 0))
        controller.stale_steps = int(data.get("stale_steps", 0))
        controller.executed = [key for key in coverage.get("executed", []) if key in controller.registry.tests]
        controller.revoked = set(coverage.get("revoked", []))
        if data.get("in_flight") or any(job.get("status") == "running" for job in self.state.runtime_status.get("background_jobs", [])):
            self._messages.append({"role": "user", "content": "[会话恢复] 上次执行已中断，旧后台进程未恢复。根据已保存事实和审计核对结果，不自动重放旧工具动作。"})
        self.state.runtime_status = {}
        self.audit.append("chat.resumed", turns=self.turns)
        self.save()

    @classmethod
    def resume(cls, config: TrinaConfig, run_id: str, **kwargs) -> ChatAgent:
        return cls(config, run_id=run_id, resume_saved=True, **kwargs)

    def _drain_steer(self) -> str:
        if self.steer_queue is None:
            return ""
        notes: list[str] = []
        while True:
            try:
                note = self.steer_queue.get_nowait()
            except Exception:  # noqa: BLE001 - 队列空
                break
            from ..session import GoalUpdate
            if isinstance(note, GoalUpdate):
                self.state.goal = str(note)
                notes.append("任务目标更新：" + (str(note) or "已清除"))
            elif note:
                notes.append(str(note))
        return "；".join(notes)

    def _accept_steer(self) -> bool:
        """在请求前与回复后接收补充，避免最终回复遗漏在途输入。"""
        steer = self._drain_steer()
        if not steer:
            return False
        message = {"role": "user", "content": f"[人工介入提示] {steer}"}
        self.context.remember_user(steer)
        self.history.append(message)
        self._messages.append(message)
        self.audit.append("chat.steer", decision="allow", message=steer[:400])
        self.logger(f"[补充] 已接收：{steer}")
        return True

    def _run_action(self, action: dict[str, Any], step: int) -> str:
        tool_name = str(action.get("action", ""))
        params = action.get("params") if isinstance(action.get("params"), dict) else {}
        params = dict(params)
        inline_test_id = params.pop("test_id", "") if isinstance(params.get("test_id"), str) else ""
        test_id = str(action.get("test_id", "") or inline_test_id or "")
        thought = str(action.get("thought", ""))
        target = str(params.get("target") or params.get("url") or self.state.target or "")
        if target and not self.state.target:
            self.state.target = target
        level = self._effective_level(tool_name, params)
        hypothesis = self.ledger.from_action(action) if self.use_ledger else None

        self.audit.append(
            "chat.action", step=step, decision="allow", tool=tool_name, level=level, params=params, thought=thought[:200],
            hypothesis=(
                {"id": hypothesis.id, "statement": hypothesis.statement[:200], "confidence": hypothesis.confidence}
                if hypothesis is not None
                else None
            ),
        )
        self.logger(f"[动作 {step}] {tool_name} {json.dumps(params, ensure_ascii=False)[:120]}")
        if thought:
            self.logger(f"          想法：{thought[:160]}")

        # 控制器（授权：注册/预算/撤权）
        decision = self.controller.check(
            {"step": step, "tool": tool_name, "params": params, "level": level, "target": target, "test_id": test_id}
        )
        if not decision.allowed:
            if hypothesis is not None:
                self.ledger.record_outcome(hypothesis.id, new_information=False, blocked=True, note=decision.reason)
            self.state.note_boundary(tool=tool_name, reason=decision.reason, kind=decision.kind, target=target)
            self.logger(f"[控制器拒绝] {decision.reason}")
            return f"被独立授权控制器拒绝（{decision.kind}）：{decision.reason}。" + (decision.hint or "")

        # 人工审批
        test_id = decision.test_id or test_id
        if self.approval is not None and not self.approval(
            {"step": step, "tool": tool_name, "level": level, "params": params, "thought": thought, "target": target, "test_id": test_id}
        ):
            if hypothesis is not None:
                self.ledger.record_outcome(hypothesis.id, new_information=False, blocked=True, note="人工审批未通过")
            self.state.note_boundary(tool=tool_name, reason="人工审批未通过", kind="human", target=target)
            self.audit.append("chat.reject", step=step, tool=tool_name, decision="deny")
            self.logger("[人工拒绝] 该动作未执行")
            return f"操作者拒绝了 {tool_name}（未执行）。换更保守的做法，或先说明为什么需要它。"

        before_knowledge = self.state.knowledge_digest()
        result = self.registry.execute(tool_name, params, self.ctx)
        if self.on_tool is not None:
            self.on_tool(result)
        new_information = before_knowledge != self.state.knowledge_digest()

        # 假设台账：模型可带 hypothesis；用结果确定性更新状态
        if hypothesis is not None:
            updated = self.ledger.record_tool_outcome(
                hypothesis.id, result=result, state=self.state, tool=tool_name, new_information=new_information,
            )
            if updated is not None:
                self.logger(f"[假设] {updated.id} → {updated.status}（P={updated.confidence:.2f}）：{updated.statement[:60]}")

        # 循环识别 + 失败分类（关闭 loop_guard 时照样统计，只是不把建议回灌给模型）
        advice = self.loop_guard.observe(tool_name, params, result, new_information=new_information)
        feedback: list[str] = []
        if advice and self.use_guard:
            self.logger(f"[循环守卫] {advice}")
            feedback.append(advice)
        elif advice:
            self.state.loop["last_advice"] = advice
        kind = classify_failure(result, new_information=new_information)
        if kind != "ok":
            self.state.counters[f"fail_{kind}"] = self.state.counters.get(f"fail_{kind}", 0) + 1
            hint = advice_for(kind, attempt=self.loop_guard.escalation_step)
            if hint and self.use_guard:
                self.logger(f"[失败分类] {kind}：{hint}")
                feedback.append(hint)
        # 等待仍在运行的终端作业不属于重复漏洞验证，也不记作新增证据。
        if not any(job["status"] == "running" for job in self.ctx.extra["background_jobs"].snapshot()):
            self.controller.note_evidence(new_information)
        self.state.hypotheses = self.ledger.to_dict()
        self.state.loop = self.loop_guard.to_dict()
        observation = result.to_observation(max_chars=1800)
        if feedback:
            observation += "\n[控制器反馈] " + "；".join(feedback)
        self.audit.append(
            "chat.result", step=step, tool=tool_name, ok=result.ok, denied=result.denied,
            summary=result.summary[:200], duration=round(result.duration, 3),
        )
        self.logger(f"          → {'OK' if result.ok else '失败'}: {str(result.summary)[:140]}")
        return observation

    def _effective_level(self, tool_name: str, params: dict[str, Any]) -> str:
        spec = self.registry.get(tool_name)
        if spec is None:
            return "unknown"
        try:
            return spec.required_level(params) or spec.spec.action_level
        except Exception:  # noqa: BLE001
            return spec.spec.action_level

    # ------------------------------------------------------------------ 视图
    def snapshot(self) -> dict[str, Any]:
        coverage = self.controller.coverage() if self._controller is not None else {}
        ledger = self.ledger.summary()
        return {
            "run_id": self.run_id,
            "turns": self.turns,
            "target": self.state.target,
            "findings": len(self.state.findings),
            "flags": len(self.state.flags),
            "checks_used": coverage.get("checks_used", 0),
            "executed": coverage.get("executed", []),
            "hypotheses": ledger,
            "redundant_rate": self.loop_guard.redundant_rate,
            "audit": str(self.audit.path),
        }


def _parse_action_lenient(text: str) -> tuple[dict[str, Any] | None, str]:
    """返回 (动作, 其余自然语言说明)。

    对话里模型常写成「先解释一句 + JSON 动作」甚至一口气给多个动作：
    - 只要文本里存在合法的 action/final 对象就算动作（用 prefer="first" 取意图最明确的那个）；
    - 其余文字作为"说明"回显给用户，不丢信息。
    """
    from .prompts import parse_action

    raw = (text or "").strip()
    if not raw:
        return None, ""
    action = parse_action(raw, prefer="first")
    if "error" in action or ("action" not in action and "final" not in action):
        return None, raw
    note = _strip_json_objects(raw)
    return action, note


def _strip_json_objects(text: str) -> str:
    """去掉文本里的顶层 JSON 对象，留下自然语言部分（用于回显模型的说明）。"""
    from .prompts import _balanced_objects

    remainder = text
    for block in _balanced_objects(text):
        remainder = remainder.replace(block, "")
    return "\n".join(line.strip() for line in remainder.splitlines() if line.strip())


__all__ = ["ChatAgent", "render_chat_prompt"]


class _VisibleStream:
    """只把明确的自然语言正文送往 UI；JSON/DSML 动作在解析前保持隐藏。"""

    def __init__(self, callback: Callable[[str, bool], None] | None):
        self.callback = callback
        self.text = ""
        self.mode = "pending"  # pending | visible | hidden
        self.opened = False
        self.displayed = ""

    def on_event(self, event: LLMStreamEvent) -> None:
        if event.type != "text_delta" or not event.delta:
            return
        self.text += event.delta
        if self.mode == "pending":
            stripped = self.text.lstrip()
            if not stripped:
                return
            lowered = stripped.lower()
            if stripped[0] in "{[<" or lowered.startswith(("```json", "```xml", "```dsml")):
                self.mode = "hidden"
                return
            if stripped.startswith("```") and len(stripped) < 8:
                return
            self.mode = "visible"
        if self.mode == "visible" and self.callback is not None:
            # 模型可能先解释再输出动作。遇到 JSON/XML 起始字节先缓存后缀，
            # 防止在完整解析前把工具协议的一半显示出来；普通代码在最终答复时补齐。
            boundary = min((index for marker in ("{", "<", "```")
                            if (index := self.text.find(marker)) >= 0), default=len(self.text))
            visible = self.text[:boundary]
            if not visible.strip() or visible == self.displayed:
                return
            self.opened = True
            self.displayed = visible
            self.callback(visible, False)

    def finish(self, text: str) -> None:
        if self.callback is not None:
            self.opened = True
            self.callback(text, True)

    def cancel(self) -> None:
        if self.callback is not None and self.opened:
            self.callback("", True)
