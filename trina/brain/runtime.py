"""所有 Agent 共用的决策循环：一轮模型响应执行一批动作，执行仍走原注册表。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..llm.base import LLMResponse
from ..tools.schema import FINISH_TOOL_NAME, parse_arguments


RETRY = object()


@dataclass
class ActionOutcome:
    observation: str = ""
    ok: bool = True
    stop: bool = False
    answer: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeAction:
    position: int
    action: dict[str, Any]
    call_id: str = ""
    dependencies: tuple[int, ...] = ()


def decode_batch(response: LLMResponse, native: bool, decode_text: Callable) -> tuple[list[RuntimeAction], str]:
    """保留 API 调用身份；依赖仅为本批次前序编号，不能伪造额外权限。"""
    actions: list[RuntimeAction] = []
    calls = list(response.tool_calls) if native else []
    note = response.text.strip() if calls else ""
    if calls:
        for position, call in enumerate(calls, 1):
            params, error = parse_arguments(call.arguments)
            action = {"error": f"{call.name} 参数解析失败：{error}"} if error else (
                {"final": params} if call.name == FINISH_TOOL_NAME else {"action": call.name, "params": params})
            actions.append(RuntimeAction(position, action, call.id))
    else:
        decoded = decode_text(response.text)
        if isinstance(decoded, tuple):
            action, note = decoded
        else:
            action = decoded
        if action is None:
            return [], response.text.strip()
        actions.append(RuntimeAction(1, action))
    mixed_finish = len(actions) > 1 and any("final" in item.action for item in actions)
    for item in actions:
        params = dict(item.action.get("params") or {})
        dependencies = params.pop("_depends_on", [])
        if not isinstance(dependencies, list) or any(type(index) is not int or index < 1 or index >= item.position for index in dependencies):
            item.action = {"error": "_depends_on 必须是本批次前序调用的1基编号列表"}
        elif "params" in item.action:
            item.action = {**item.action, "params": params}
            item.dependencies = tuple(dict.fromkeys(dependencies))
        if mixed_finish:
            item.action = {"error": "finish 不能与工具动作混用；先读取实际工具结果再结束"}
    return actions, note


class AgentRuntime:
    """唯一模型—动作循环；任务和对话只提供请求、执行、展示与收尾适配。"""

    def __init__(self, *, max_steps: int, messages: list, request: Callable,
                 execute: Callable, decode_text: Callable, native: Callable[[], bool],
                 record_response: Callable | None = None, observe: Callable | None = None,
                 say: Callable | None = None, interrupted: Callable | None = None,
                 start_execute: Callable | None = None, can_parallel: Callable | None = None,
                 parallel_limit: int = 1, resume: Callable | None = None, emit: Callable | None = None):
        if type(max_steps) is not int or max_steps < 0:
            raise ValueError("max_steps 必须为非负整数，0 不限制")
        if type(parallel_limit) is not int or not 1 <= parallel_limit <= 16:
            raise ValueError("parallel_limit 必须为 [1,16] 内的整数")
        self.max_steps, self.messages = max_steps, messages
        self.request, self.execute, self.decode_text, self.native = request, execute, decode_text, native
        self.record_response, self.observe, self.say = record_response, observe, say
        self.interrupted = interrupted or (lambda: "")
        self.start_execute, self.can_parallel = start_execute, can_parallel
        self.parallel_limit = parallel_limit
        self.resume = resume or (lambda **kwargs: False)
        self.emit = emit or (lambda *args, **kwargs: None)
        self.steps = self.rounds = 0

    def run(self) -> ActionOutcome:
        last = ActionOutcome()
        while not self.max_steps or (self.steps < self.max_steps and self.rounds < self.max_steps):
            self.resume(wait=False)
            self.rounds += 1
            response = self.request(self.steps + 1, self.rounds)
            if response is None:
                return ActionOutcome(stop=True)
            if response is RETRY:
                continue
            actions, note = decode_batch(response, self.native(), self.decode_text)
            calls = list(response.tool_calls) if self.native() else []
            message: dict[str, Any] = {"role": "assistant", "content": response.text}
            if calls:
                message["tool_calls"] = [call.to_dict() for call in calls]
            if response.reasoning:
                message["reasoning_content"] = response.reasoning
            self.messages.append(message)
            if self.record_response:
                self.record_response(response)
            if not actions:
                # 对话正文与任务协议恢复分别由适配器判断。
                outcome = self.say(note, response) if self.say else ActionOutcome(answer=note, stop=True)
                if outcome.stop:
                    if self.resume(wait=True):
                        continue
                    return outcome
                continue
            if note and self.say:
                self.say(note, response, interim=True)
            outcomes: dict[int, ActionOutcome] = {}
            stopping = False
            cancellation = ""
            def record(item: RuntimeAction, outcome: ActionOutcome) -> None:
                nonlocal last, stopping
                outcomes[item.position] = outcome
                if outcome.stop or not stopping:
                    last = outcome
                stopping = stopping or outcome.stop
                if item.call_id:
                    tool_message = {
                        "role": "tool", "tool_call_id": item.call_id,
                        "content": outcome.observation[:4000],
                    }
                    self.messages.append(tool_message)
                else:
                    tool_message = {"role": "user", "content": "[工具结果] " + outcome.observation}
                    if not outcome.stop:
                        self.messages.append(tool_message)
                if self.observe:
                    from ..execution_timeline import phase
                    with phase(self.emit, "result_delivery", call_id=item.call_id):
                        self.observe(item, outcome, tool_message)

            cursor = 0
            while cursor < len(actions):
                item = actions[cursor]
                cancellation = cancellation or self.interrupted()
                reason = cancellation
                if stopping or reason or (self.max_steps and self.steps >= self.max_steps):
                    outcome = ActionOutcome(observation=reason or "未执行：本轮已停止或达到执行上限", ok=False)
                elif any(not outcomes[index].ok for index in item.dependencies):
                    outcome = ActionOutcome(observation="未执行：依赖调用失败或未执行", ok=False)
                elif self._parallel_candidate(item, outcomes):
                    pending: list[tuple[RuntimeAction, Callable[[], ActionOutcome]]] = []
                    while cursor < len(actions) and len(pending) < self.parallel_limit:
                        candidate = actions[cursor]
                        if not self._parallel_candidate(candidate, outcomes):
                            break
                        if self.max_steps and self.steps >= self.max_steps:
                            break
                        self.steps += 1
                        try:
                            completion = self.start_execute(
                                candidate.action, self.steps, response,
                                llm_seconds=response.duration if candidate.position == 1 else 0.0,
                                call_id=candidate.call_id,
                            )
                        except Exception as exc:  # noqa: BLE001 - 单个提交失败不能阻塞同批独立动作
                            failure = ActionOutcome(
                                observation=f"工具提交失败：{type(exc).__name__}: {exc}", ok=False,
                            )
                            completion = lambda failure=failure: failure
                        pending.append((candidate, completion))
                        cursor += 1
                    for candidate, completion in pending:
                        try:
                            outcome = completion()
                        except Exception as exc:  # noqa: BLE001 - 单个结果失败不能吞掉其余回执
                            outcome = ActionOutcome(
                                observation=f"工具结果归并失败：{type(exc).__name__}: {exc}", ok=False,
                            )
                        record(candidate, outcome)
                    continue
                else:
                    self.steps += 1
                    outcome = self.execute(item.action, self.steps, response,
                                           llm_seconds=response.duration if item.position == 1 else 0.0,
                                           call_id=item.call_id)
                record(item, outcome)
                cursor += 1
            if stopping:
                if not cancellation and self.resume(wait=True):
                    continue
                return last
        return last

    def _parallel_candidate(self, item: RuntimeAction, outcomes: dict[int, ActionOutcome]) -> bool:
        """只并发适配器明确标记且所有声明依赖已经成功完成的动作。"""
        return bool(
            self.parallel_limit > 1
            and self.start_execute is not None
            and self.can_parallel is not None
            and all(index in outcomes and outcomes[index].ok for index in item.dependencies)
            and self.can_parallel(item.action)
        )
