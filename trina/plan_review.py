"""控制台的只读任务澄清，不参与任务执行。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PlanBranch:
    """一个待核对的工程分支及其交付物。"""

    id: str
    title: str
    depends_on: tuple[str, ...]
    owner: str
    deliverable: str
    reason: str


_CLARIFY = (
    '你只帮助用户澄清普通工作任务的交付物、范围和验收标准。每次最多问一个需要用户决定的问题，'
    '提供二到五个具体选项。若这些信息足够，返回可核对的验收条目。不要设计攻击、执行动作或调用工具。'
    '仅返回 JSON，要么 {"question":"问题","options":["选项一","选项二"]}，'
    '要么 {"criteria":["验收标准"]}。已确认的答案不要重复询问。'
)
_SPLIT = (
    '为普通软件工程任务起草只读的分工预览，适用于代码维护、文档、离线测试规划。'
    '若任务要求扫描目标、复现或利用漏洞，或超出这些用途，只返回 {"unsupported":true}。'
    '只输出 JSON，不执行工作或调用工具。'
    '按可独立验收的交付物决定分支数量，单步工作只保留一支，不问用户要几个 Agent。'
    '仅在某分支确实需要另一分支的交付物，或两者会改动同一处内容时，才列前置依赖。'
    '独立且交接清晰、收益超过协调成本的分支建议 worker，其余建议 commander，并解释理由。'
    '缺少会影响依赖或交付验收的用户决策时，返回 {"question":"问题","options":["选项一","选项二"]}。'
    '已确认的答案不要重复询问。信息充分时返回 {"branches":[{"id":"docs","title":"更新使用说明",'
    '"depends_on":[],"owner":"commander","deliverable":"新版使用说明","reason":"单项修改无需交接"}]}。'
    '最多十二个分支。ID 用小写字母开头，且只包含小写字母、数字和连字符。'
)


@dataclass(frozen=True)
class PlanReview:
    task: str
    answers: tuple[tuple[str, str], ...] = ()
    question: str = ""
    options: tuple[str, ...] = ()
    criteria: tuple[str, ...] = ()
    split_preview: bool = False
    layers: tuple[tuple[PlanBranch, ...], ...] = ()

    def request(self, backend) -> PlanReview:
        response = backend.complete_messages([
            {"role": "system", "content": _SPLIT if self.split_preview else _CLARIFY},
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
                return PlanReview(self.task, self.answers, question.strip()[:500], options,
                                  split_preview=self.split_preview)
            if self.split_preview:
                if data.get("unsupported") is True and set(data) == {"unsupported"}:
                    raise ValueError("分工预览适用于代码维护、文档和离线测试规划")
                if set(data) == {"branches"}:
                    return PlanReview(self.task, self.answers, split_preview=True,
                                      layers=_branch_layers(data["branches"]))
                raise ValueError("模型没有返回可读的分工方案")
            criteria = _items(data.get("criteria"), 1, 8)
            if set(data) == {"criteria"} and criteria:
                return PlanReview(self.task, self.answers, criteria=criteria)
        raise ValueError("澄清结果缺少问题和选项，或缺少验收标准")

    def followup(self, answer: str) -> PlanReview:
        return PlanReview(self.task, (*self.answers, (self.question, answer)), split_preview=self.split_preview)

    def split_sections(self) -> list[tuple[str, str]]:
        sections = [("任务", self.task)]
        for depth, layer in enumerate(self.layers, 1):
            branches = []
            for branch in layer:
                owner = "建议委派" if branch.owner == "worker" else "主会话负责"
                before = "、".join(branch.depends_on) or "无"
                branches.append(
                    f"{branch.id} · {branch.title} · {owner}\n"
                    f"  前置 {before}\n  交付 {branch.deliverable}\n  理由 {branch.reason}"
                )
            heading = "可并行开始" if depth == 1 and len(layer) > 1 else "可开始" if depth == 1 else f"依赖层 {depth}"
            sections.append((heading, "\n\n".join(branches)))
        return sections

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


def _branch_layers(value: object) -> tuple[tuple[PlanBranch, ...], ...]:
    """校验全部依赖后按拓扑层级展示，顺序相同时保留模型原顺序。"""
    fields = {"id", "title", "depends_on", "owner", "deliverable", "reason"}
    if not isinstance(value, list) or not 1 <= len(value) <= 12:
        raise ValueError("分工方案需要一至十二个分支")
    branches: dict[str, PlanBranch] = {}
    for item in value:
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError("分支缺少必要字段")
        branch_id = item["id"]
        if not isinstance(branch_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,23}", branch_id):
            raise ValueError("分支 ID 格式无效")
        if branch_id in branches:
            raise ValueError(f"分支 ID 重复 · {branch_id}")
        owner = item["owner"]
        if owner not in ("commander", "worker"):
            raise ValueError(f"分支负责人类型无效 · {branch_id}")
        dependencies = item["depends_on"]
        if (not isinstance(dependencies, list) or len(dependencies) >= len(value)
                or not all(isinstance(dep, str) for dep in dependencies)
                or len(set(dependencies)) != len(dependencies)):
            raise ValueError(f"前置依赖格式无效 · {branch_id}")
        text_fields = [item[key] for key in ("title", "deliverable", "reason")]
        if not all(isinstance(text, str) and text.strip() for text in text_fields):
            raise ValueError(f"分支缺少标题、交付物或理由 · {branch_id}")
        title, deliverable, reason = (" ".join(text.split())[:320] for text in text_fields)
        branches[branch_id] = PlanBranch(branch_id, title, tuple(dependencies), owner, deliverable, reason)

    for branch in branches.values():
        if any(dependency not in branches for dependency in branch.depends_on):
            raise ValueError(f"分支引用了不存在的前置项 · {branch.id}")
    remaining = list(branches.values())
    placed: set[str] = set()
    layers = []
    while remaining:
        layer = tuple(branch for branch in remaining if placed.issuperset(branch.depends_on))
        if not layer:
            raise ValueError("分支之间存在循环依赖")
        layers.append(layer)
        placed.update(branch.id for branch in layer)
        remaining = [branch for branch in remaining if branch.id not in placed]
    return tuple(layers)
