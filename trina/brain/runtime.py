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
                 say: Callable | None = None, interrupted: Callable | None = None):
        self.max_steps, self.messages = max_steps, messages
        self.request, self.execute, self.decode_text, self.native = request, execute, decode_text, native
        self.record_response, self.observe, self.say = record_response, observe, say
        self.interrupted = interrupted or (lambda: "")
        self.steps = self.rounds = 0

    def run(self) -> ActionOutcome:
        last = ActionOutcome()
        while self.steps < self.max_steps and self.rounds < self.max_steps:
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
                    return outcome
                continue
            if note and self.say:
                self.say(note, response, interim=True)
            outcomes: dict[int, ActionOutcome] = {}
            stopping = False
            cancellation = ""
            for item in actions:
                cancellation = cancellation or self.interrupted()
                reason = cancellation
                if stopping or reason or self.steps >= self.max_steps:
                    outcome = ActionOutcome(observation=reason or "未执行：本次任务已停止或动作预算已用尽", ok=False)
                elif any(not outcomes[index].ok for index in item.dependencies):
                    outcome = ActionOutcome(observation="未执行：依赖调用失败或未执行", ok=False)
                else:
                    self.steps += 1
                    outcome = self.execute(item.action, self.steps, response,
                                           llm_seconds=response.duration if item.position == 1 else 0.0)
                    stopping = outcome.stop
                    last = outcome
                outcomes[item.position] = outcome
                if item.call_id:
                    tool_message = {"role": "tool", "tool_call_id": item.call_id, "content": outcome.observation[:4000]}
                    self.messages.append(tool_message)
                else:
                    tool_message = {"role": "user", "content": "[工具结果] " + outcome.observation}
                    if not outcome.stop:
                        self.messages.append(tool_message)
                if self.observe:
                    self.observe(item, outcome, tool_message)
            if stopping:
                return last
        return last
