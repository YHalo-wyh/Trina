"""控制台的只读任务澄清，不参与任务执行。"""
from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class PlanReview:
    task: str
    answers: tuple[tuple[str, str], ...] = ()
    question: str = ""
    options: tuple[str, ...] = ()
    criteria: tuple[str, ...] = ()

    def request(self, backend) -> PlanReview:
        response = backend.complete_messages([
            {"role": "system", "content": (
                '你只帮助用户澄清普通工作任务的交付物、范围和验收标准。每次最多问一个需要用户决定的问题，'
                '提供二到五个具体选项。若这些信息足够，返回可核对的验收条目。不要设计攻击、执行动作或调用工具。'
                '仅返回 JSON，要么 {"question":"问题","options":["选项一","选项二"]}，'
                '要么 {"criteria":["验收标准"]}。已确认的答案不要重复询问。'
            )},
            {"role": "user", "content": json.dumps({"task": self.task, "answers": self.answers}, ensure_ascii=False)},
        ])
        if not response.ok:
            raise ValueError(response.error or "模型未完成澄清")
        if response.tool_calls:
            raise ValueError("计划澄清仅接收文本")
        text = response.text.strip()
        if text.startswith("```") and text.endswith("```") and "\n" in text:
            text = text.split("\n", 1)[1][:-3].strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("模型没有返回可读的澄清结果") from exc
        if isinstance(data, dict):
            question = data.get("question")
            options = _items(data.get("options"), 2, 5)
            if set(data) == {"question", "options"} and isinstance(question, str) and question.strip() and options:
                return PlanReview(self.task, self.answers, question.strip()[:500], options)
            criteria = _items(data.get("criteria"), 1, 8)
            if set(data) == {"criteria"} and criteria:
                return PlanReview(self.task, self.answers, criteria=criteria)
        raise ValueError("澄清结果缺少问题和选项，或缺少验收标准")

    def followup(self, answer: str) -> PlanReview:
        return PlanReview(self.task, (*self.answers, (self.question, answer)))

    def draft(self) -> str:
        lines = [self.task]
        if self.answers:
            lines.extend(["", "已确认", *(f"- {question} {answer}" for question, answer in self.answers)])
        lines.extend(["", "验收标准", *(f"- {item}" for item in self.criteria)])
        return "\n".join(lines)


def _items(value: object, minimum: int, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        return ()
    if not all(isinstance(item, str) and item.strip() for item in value):
        return ()
    items = tuple(item.strip()[:500] for item in value)
    return items if len(set(items)) == len(items) else ()
