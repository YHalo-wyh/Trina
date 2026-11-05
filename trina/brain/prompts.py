"""任务输入与 JSON 动作协议。

要点
----
* **动作协议是文本 JSON**：不依赖各家模型的 function-calling，跨模型可移植、可审计、可回放。
* **授权边界来自 `config.scope_prompt_block()`**：与护栏同源，杜绝提示词与真实判据漂移。
* **单步单动作**：便于护栏逐条裁决，也便于失败重试与停滞检测。
"""

from __future__ import annotations

import json

SYSTEM_PROMPT_TEMPLATE = """依据当前任务、状态和工具返回选择下一步。每次只输出一个 JSON 对象。

# 执行规则
- 每步最多调用一个工具，收到结果后再决定下一步。
- 参数名称和类型遵循工具清单。遇到拒绝或失败，根据返回原因调整动作。
- 结论必须引用实际工具证据，不编造结果；重复失败时换参数、工具或检查方向。
- 目标完成或没有可继续验证的线索时，以 final 结束。

# 动作格式
{"thought": "选择该动作的简短理由", "action": "工具名", "params": {"参数": "值"}%(hypothesis_field)s}
%(hypothesis_note)s

# 结束格式
{"thought": "简短总结", "final": {"status": "success|partial|failed", "summary": "结论摘要", "findings": ["有证据的发现"], "flags": ["实际获得的 flag"]}}

直接输出 JSON，不使用 Markdown 代码块；解释放在 thought 中。
"""

#: `hypothesis` 字段的两种形态：有台账时展示，无台账时整段消失（对照臂用同一份模板渲染）
HYPOTHESIS_FIELD_BLOCK = """,
 "hypothesis": {"statement": "这条动作要验证的假设", "vuln_class": "sqli", "target": "待验证的完整 URL 或主机", "parameter": "id", "confidence": "low|medium|high", "value": "high|medium|low", "info_gain": "high|medium|low", "cost": "high|medium|low", "risk": "high|medium|low", "next_step": "验证成功后打算做什么"}"""

HYPOTHESIS_NOTE_BLOCK = """
（`hypothesis` 可选但推荐：系统按优先级排序，并会**确定性调度**——把最高优先级的可绑定假设挑出来，指定下一步用哪个注册检查项去验它。自动确认需要填写 vuln_class、target（精确到路径，查询串可省略）及可选 parameter，并取得匹配的带证据 verified/exploited 发现。**vuln_class 必须写发现类型**（如 sqli / lfi / rce / information_disclosure / security_misconfig_headers / weak_password / unrestricted_file_upload），**不要写阶段名**（recon/enum/vuln 都不是发现类型，会导致无法绑定检查项而失效）。继续验证既有假设时，用顶层 "hypothesis_id": "H-001" 代替新增假设。连续无新信息只降低优先级，仍属未解决，不能声称已否定。）"""


def render_system_prompt(include_hypothesis: bool = True) -> str:
    """渲染动作协议；``include_hypothesis=False`` 时省略假设台账字段。"""
    return SYSTEM_PROMPT_TEMPLATE % (
        {"hypothesis_field": HYPOTHESIS_FIELD_BLOCK, "hypothesis_note": HYPOTHESIS_NOTE_BLOCK}
        if include_hypothesis
        else {"hypothesis_field": "", "hypothesis_note": ""}
    )


#: 默认（启用假设台账）的动作协议由 ``render_system_prompt(True)`` 生成；
#: 对照臂用 ``render_system_prompt(False)``，两者同源同模板，不会漂移。

_REMINDER_HEAD = "请输出且仅输出一个 JSON 对象：调用工具用 "
_REMINDER_ACTION = '{"thought": "...", "action": "工具名", "params": {...}'
_REMINDER_HYPOTHESIS = ', "hypothesis": {"statement": "本步要验证的假设", "confidence": "low|medium|high"}'
_REMINDER_TAIL = '}；结束用 {"thought": "...", "final": {...}}。'


def protocol_reminder(include_hypothesis: bool = True) -> str:
    """单步协议提醒；与系统提示同源，同样支持"没有假设台账"的基线口径。"""
    return (
        _REMINDER_HEAD
        + _REMINDER_ACTION
        + (_REMINDER_HYPOTHESIS if include_hypothesis else "")
        + _REMINDER_TAIL
    )


#: 各阶段的作业指引。阶段由 Planner 依据事实推进，不由模型自称。
PHASE_DIRECTIVES: dict[str, str] = {
    "recon": (
        "阶段目标：摸清目标生存状态。先用 port_scan 拿到开放端口（先 quick 再按需扩大），"
        "必要时用 http_fingerprint 判断是否为 Web 目标。不要在没有端口信息时就去做注入。"
    ),
    "enum": (
        "阶段目标：把「端口」变成「可攻击面」。对开放服务做 service_probe / tls_info，"
        "Web 目标做 http_fingerprint、dir_bruteforce、web_surface，把表单、参数、路径、技术栈摸清楚。"
    ),
    "vuln": (
        "阶段目标：发现并**验证**漏洞。用 poc_scan / nuclei_scan 做批量体检，"
        "再针对有参数的接口做 sqli_test、cmd_inject_test、lfi_test、param_reflect_scan。"
        "每条漏洞都要有请求与响应证据；不确定时提高验证强度重测一次。"
    ),
    "exploit": (
        "阶段目标：把已验证的漏洞变成实际控制权或数据读取能力。可用 rce_exec（命令白名单）、lfi_test 读文件、"
        "file_upload_test 验证上传、weak_password_brute 验证弱口令。只做无害验证与取证，禁止破坏性动作。"
        "注意：验证**已知的一组**用户名/口令请用 http_form_login（或 weak_password_brute 同时给出 username 与 password），"
        "这类单凭据验证属于 verify 级；只有字典爆破才需要 exploit 级。"
        "若某个工具因动作等级被拒，先判断是否有更低等级的等价工具（例如用 http_form_login 代替爆破）。"
    ),
    "post": (
        "阶段目标：取证与收尾。用 flag_hunt 或 rce_exec 读取 flag；整理已获得的凭据与漏洞；"
        "确认关键结论都有证据，然后给出 final。"
    ),
    "report": "阶段目标：已经具备足够材料，请立即用 final 结束并给出结论摘要。",
}

#: 阶段 → 推荐工具类别（用于精简提示词并引导模型用对工具）
PHASE_TOOL_CATEGORIES: dict[str, tuple[str, ...]] = {
    "recon": ("recon", "utility"),
    "enum": ("recon", "web", "utility"),
    "vuln": ("vuln", "web", "exploit", "utility"),
    "exploit": ("exploit", "creds", "vuln", "utility"),
    "post": ("creds", "exploit", "web", "utility"),
    "report": ("utility",),
}


def build_system_prompt(
    scope_block: str,
    tool_block: str,
    extra_rules: str = "",
    *,
    include_hypothesis: bool = True,
) -> str:
    """拼出动作协议、配置范围和工具清单。"""
    parts = [render_system_prompt(include_hypothesis), "\n# 授权边界（硬约束，来自配置文件）\n" + scope_block]
    if extra_rules:
        parts.append("\n# 本次任务附加约束\n" + extra_rules)
    parts.append("\n# 可用工具\n" + tool_block)
    return "\n".join(parts)


def build_step_prompt(
    *,
    state_digest: str,
    phase: str,
    directive: str,
    last_observation: str,
    hints: list[str] | None = None,
    budget: dict[str, object] | None = None,
    extra_context: str = "",
    include_hypothesis: bool = True,
) -> str:
    """拼出单步的 user 提示：状态 → 阶段任务 → 上一步观察 → 提醒。"""
    lines = [
        "## 当前状态",
        state_digest,
        "",
        f"## 当前阶段：{phase}",
        directive,
    ]
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
    lines.append("\n" + protocol_reminder(include_hypothesis))
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
