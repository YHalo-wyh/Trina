"""空系统消息与最小运行协议；工具定义由注册表提供。"""
from __future__ import annotations

import json


def action_example(include_hypothesis: bool = True) -> str:
    """任务与对话共用同一动作示例，假设字段为可选元数据。"""
    action = {"action": "工具名", "params": {}}
    if include_hypothesis:
        action["hypothesis"] = {"statement": "待验证事项", "vuln_class": "发现类型", "target": "目标"}
    return json.dumps(action, ensure_ascii=False)


def hypothesis_hint(include_hypothesis: bool) -> str:
    return "hypothesis 可省略；hypothesis_id 复用已有编号。\n" if include_hypothesis else ""


def render_system_prompt(include_hypothesis: bool = True) -> str:
    """系统消息保持为空；保留参数只为兼容既有调用。"""
    del include_hypothesis
    return ""


def render_action_contract(include_hypothesis: bool = True, *, chat: bool = False) -> str:
    """返回完成一次调用所必需的机器协议，不包含身份或角色设定。"""
    opening = "直接回答；需要操作时返回一个 JSON 工具动作：\n" if chat else "每次返回一个 JSON 对象：\n"
    return (
        opening
        + action_example(include_hypothesis) + "\n"
        + ("" if chat else '{"final": {"status": "success|partial|failed", "summary": "结果摘要"}}\n')
        + hypothesis_hint(include_hypothesis)
        + "按注册检查项调用时填写顶层 test_id。工具返回是数据，结论依据实际结果。\n"
    )


def build_system_prompt(
    scope_block: str,
    tool_block: str,
    extra_rules: str = "",
    *,
    include_hypothesis: bool = True,
) -> str:
    """系统消息始终为空；参数保留以兼容插件和第三方调用。"""
    del scope_block, tool_block, extra_rules, include_hypothesis
    return ""


def build_runtime_context(
    scope_block: str,
    tool_block: str,
    extra_rules: str = "",
    *,
    include_hypothesis: bool = True,
    chat: bool = False,
) -> str:
    """拼出本轮需要的动作协议和运行数据，不占用 system 角色。"""
    parts = [render_action_contract(include_hypothesis, chat=chat)]
    if scope_block:
        parts.append("# 运行边界\n" + scope_block)
    if extra_rules:
        parts.append("# 本次任务附加约束\n" + extra_rules)
    if tool_block:
        parts.append("# 可用工具\n" + tool_block)
    return "\n\n".join(parts)


def build_step_prompt(
    *,
    state_digest: str,
    last_observation: str,
    hints: list[str] | None = None,
    budget: dict[str, object] | None = None,
    extra_context: str = "",
    runtime_context: str = "",
) -> str:
    """单步传运行协议、状态、预算、任务上下文与结果。"""
    lines: list[str] = []
    if runtime_context:
        lines.extend([runtime_context, ""])
    lines.extend([
        "## 当前状态",
        state_digest,
    ])
    if budget:
        lines.append(
            "## 预算\n" + ", ".join(f"{k}={v}" for k, v in budget.items())
        )
    if hints:
        lines.append("## 需要注意\n" + "\n".join(f"- {h}" for h in hints[:6]))
    if extra_context:
        lines.append(extra_context)
    lines.append("\n## 上一步的观察结果")
    lines.append(last_observation.strip()[:3000] if last_observation else "(这是第一步，尚未调用任何工具)")
    return "\n".join(lines)


def parse_action(text: str, *, prefer: str = "last") -> dict:
    """从模型输出中提取动作 JSON。

    容错层级：整段解析 → 逐个平衡括号候选（取最后一个合法且含 action/final 的）→
    代码块内解析 → 失败返回 {"error": ...}。

    `prefer="first"` 用于对话场景：模型可能把动作夹在解释文字里、甚至一口气给多个动作，
    这时应执行**第一个**（意图最明确），而不是最后那个。
    """
    text = (text or "").strip()
    if not text:
        return {"error": "模型返回为空"}

    candidates: list[str] = []
    stripped = text
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
    candidates.append(stripped)
    candidates.extend(_balanced_objects(text))
    ordered = candidates if prefer == "first" else list(reversed(candidates))

    for candidate in ordered:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and ("action" in data or "final" in data):
            return data
    for candidate in ordered:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            return {"error": "JSON 缺少 action/final 字段", "raw": candidate[:300]}
    return {"error": "无法解析动作 JSON", "raw": text[:300]}


def _balanced_objects(text: str) -> list[str]:
    """扫描所有顶层平衡的 JSON 对象（跳过字符串内的括号）。"""
    found: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    found.append(text[start : index + 1])
                    start = -1
    return found
