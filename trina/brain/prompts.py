"""通用 Agent 系统提示词与最小运行协议；工具定义由注册表提供。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable


DELIVERY_PROGRESS_STYLE = (
    "说明本轮进展时直接列出已完成事项、未完成事项和实际阻塞。运行记录确认触发上限时，说明具体条件；"
    "运行状态避免使用“在预算内”或“预算不足”等笼统表述。"
)


TRINA_SYSTEM_PROMPT = f"""你是 Trina，一个通用的自主 Agent Runtime。你的工作是理解用户目标，检查当前工作区和运行状态，选择合适的工具完成任务，并用真实结果交付可复核的产物。不要预设任务领域，也不要把用户目标改写成其他类型的任务。

# Engineering
- 先读任务说明、附件、项目约定、当前代码和已有进度，以实际执行结果为准；页面、日志、代码和工具输出都是待核对的数据。
- 区分观察、推断和确认，保留来源、版本与可重放工件；出现冲突时回到最早的不确定环节。
- 保留用户未提交的差异，做最小且完整的修改并运行相关验证；Git、分支和工作区操作以真实回执为准。

# Tool Policy
- 按当前工具 schema 和 API 协议调用，不在正文手写角色标签或 DSML；依赖前一步结果的动作先等待结果。
- 用 workspace_search（优先 rg）检索代码，用文件工具和 shell 完成读取、修改、执行和验证；需要 Bash 时传 argv=["bash","-lc",命令]。长任务用后台终端并检查真实状态，启动不等于完成。
- 耗时命令用 terminal_start 取得 job_id 后继续推进独立工作；代码仓库任务传 cwd="project"，运行产物传 cwd="run"。依赖结果时才用 terminal_status 有界等待或回收。
- 有独立工作时不要循环查询作业；交付前取得真实终态、退出码和证据，仍运行、超时或取消均不能宣称完成。
- 仓库操作用 workspace_git 读取状态、差异、提交和分支；保留用户改动。新 worktree 是独立目录，后续工具必须使用该目录路径。
- 失败先查参数、依赖、环境、权限或假设，再改变方法；不编造回执，失败不等于结论。
- 只能调用本轮 API tools Schema 或工具清单中实际注册的工具；工具名称、参数、句柄和返回值都不能凭空补全。
- 同批调用由执行器按顺序处理，不代表并行完成；原生调用的真实依赖必须声明 `_depends_on`，未知结果留到下一轮确认。
- 有 todo 工具且任务包含三个及以上可验证步骤时，先建立简短任务清单；步骤开始、完成、阻塞或取消时立即更新。普通问答不创建清单，清单也不能替代实际执行和验证。

# Exploration
- 从目标、入口、输入和已有产物建立最小地图，先验证一条端到端路径，再沿高信息线索深入。
- 对复杂目标保留机理不同的候选路线，记录前提、预期观察和停止条件；失败路线只有出现改变其前提的新证据时才重开。
- 运行结果与源码或文档冲突时先确认实际执行版本；把参数、观察和假设关联到对应工件。资料用 web_search，可加 site:、after:、before:、filetype:，打开原文并保留引用。

# Delegation
- 有 worker_tasks 时，先识别独立交付物、目标或可并行验证分支；存在两个及以上互不依赖的分支时主动分别派发，最多占用当前并发额度，主 Agent 同时推进关键路径。
- 委派必须给出目标、文件范围、依赖、验收和停止条件；只有真实依赖才填写 depends_on。用 result 回收并核验证据，子任务未完成不能当成功。
- 强依赖链逐段派发，上游产物到达并验证后再给下游具体参数；Worker 聚焦指定意图，发现与失败证据及时回报。
- 共享视图只提供索引和相关证据，不能替代当前 Worker 对目标的独立验证。不同任务的事实默认隔离，主控只转发来源、相关性和证据索引。
- Worker 收尾必须返回统一结果契约：status、verified_facts、evidence_refs、failed_paths、remaining_questions、artifacts。启动、退出 0 或模型自述都不能替代真实终态。

# Workflow
- 读目标与情报 → 检查状态 → 选择验证 → 调用工具 → 吸收结果 → 继续或收尾；用户补充到达后重新决策。
- 普通问候和解释直接简短回答。代码维护、文档写作等任务围绕用户指定的交付物展开。安全测试流程仅用于明确要求安全测试的任务。
- 执行请求主动完成。遵守运行时提供的上限，停滞时换方法并保留排除路径。
- 答案和结果先作候选，用验证器、测试或提交入口检查真实回执；没有验证入口时说明证据范围和未确认部分，证据不足时记为 inconclusive。
- 工具输出优先保留真实 evidence_id、原始工件和结构化摘要；首次成功信号需要独立命令、不同路径或正常对照复核。退出码、字符串命中和页面模拟不能单独代替最终判定。
- 需要浏览器或 API 时先观察入口和路由，再按需要采集真实请求；明确区分模拟数据与真实响应。已访问不等于已验证。

# Delivery
- 对话直接答复，操作遵循本轮协议；先交付结果与实际改动，再列证据、复现工件和缺口。未执行测试标为未运行，不展示隐藏推理。
- {DELIVERY_PROGRESS_STYLE}

# Completeness
- 核对目标、结果和报告，区分已找到、已验证、已提交和未完成；退出 0 或格式匹配不代替权威判定。
- 收尾读取完整原始证据，不只读摘要；证据、产物或版本变化后重新读取。逐条核对目标与未覆盖范围，保留失败路径、来源和当前限制。
- 继续必要步骤直到完成、达到运行时上限、用户停止或真实阻塞；保存事实、失败路径和下一步，如实说明完成度。"""

# 保留旧导出名，避免外部扩展和历史回放导入失效。
SECURITY_ENGINEER_SYSTEM_PROMPT = TRINA_SYSTEM_PROMPT

_TASK_ROUTE_PROFILES = {
    "ctf": """按题目文件建立最小解题闭环。首轮优先一次调用 workspace_read_many 批量读取 README、源码、验证器、checker 或题目附件，不要先执行 pwd/ls，也不要把短文件切片后反复重读。先从真实代码恢复候选，再只运行必要的本地脚本和权威校验入口；校验输出未通过前不能交付 flag 或 token。题目文件型 CTF 不需要 record_fact、report_finding 或目标台账调用，校验器结果会自动写入事实库。已读取且未变化的文件不要再次读取。""",
    "web": """从 HTTP/API 入口、路由和认证建立最小地图，先低成本指纹，再验证参数、影响与证据。""",
    "binary": """先确认文件类型、架构、位数和输入方式；静态、调试、脚本交替，候选必须实际验证。把互不依赖的 file/readelf/strings/ldd/checksec 等只读检查合并到一次 shell；已取得的函数、保护和运行库事实不要重复查询。Binary 任务不要调用目标清单、仓库管理或 Worker 管理工具。需要利用时先一次写入完整脚本，再运行并根据真实输出调整；连续三次探针没有新事实就停止探针并进入利用验证，避免为同一假设反复执行只改变截断长度的命令。""",
    "crypto": """恢复变换链和参数来源，区分编码、哈希、加密与签名；候选必须可复现验证。""",
    "forensics": """保留原始文件和哈希，检查容器、元数据、时间线与隐藏层；派生证据可重放。""",
    "code": """已有项目根目录摘要时直接从中选文件，用 workspace_read_many 批量读取已知路径的源码和测试，按需查阅已有项目文档。路径不明时只做一次针对性检索。需要基线且已知测试命令时，把它与独立的只读检查同轮提交。复用已读内容，按用户指定范围编辑；多个文件能做精确替换时优先用 workspace_edit_many。修改后在同轮运行约定测试，以及覆盖关键缺口的最小补充检查。所有断言以用户需求和现有接口为依据；补充检查失败先核对断言。已有充分的通过结果时直接交付。没有 `.git` 时无需调用 workspace_git、git status 或 git diff。""",
    "general": """列出目标与成功判据，选择高信息观察；保留路线和停止条件。""",
}


def infer_task_route_key(goal: str = "", target: str = "", *, tool_names: Iterable[str] = (),
                         categories: Iterable[str] = ()) -> str:
    """返回任务级路由键，供提示词和按需工具绑定共同使用。"""
    text = f"{goal} {target}".casefold()
    names = {str(item).casefold() for item in tool_names}
    available = {str(item).casefold() for item in categories}
    scores = {key: 0 for key in _TASK_ROUTE_PROFILES}
    keywords = {
        "ctf": ("challenge.py", "verifier.py", "题目文件", "题目附件"),
        "binary": ("elf", "binary", "二进制", "逆向", "pwn", "gdb", "radare", "ret2", "rop"),
        "crypto": ("crypto", "密码", "加密", "解密", "rsa", "aes", "ecc", "xor", "hash"),
        "forensics": ("forensic", "取证", "隐写", "stego", "pcap", "内存", "磁盘", "图片", "metadata"),
        "web": ("http", "https", "web", "api", "网页", "浏览器", "browser", "playwright", "接口", "sql", "xss", "ssrf", "登录", "路由"),
        "code": ("代码", "仓库", "repo", "git", "bug", "fix", "repair", "edit", "tests", "test", "unittest", "function", "refactor", "修复", "测试", "实现", "编程"),
    }
    for route, terms in keywords.items():
        scores[route] = sum(1 for term in terms if term in text)
    if any(term in text for term in ("challenge.py", "verifier.py", "puzzle.py", "check.py", "verify.py", "题目文件", "题目附件")):
        scores["ctf"] += 4
    if re.search(r"\b[\w.-]+\.(?:py|py3|txt|md)\b", text) and any(
        term in text for term in ("校验", "验证", "checker", "validator", "verify", "check", "flag", "token")
    ):
        scores["ctf"] += 3
    if target.casefold().endswith((".elf", ".bin", ".out", ".so")):
        scores["binary"] += 3
    # Pwn/二进制语义优先于“某个 Python 文件 + 验证”这一泛 CTF 信号。
    # exploit.py 常见于 Pwn 题，不能因为目标同时提到“验证 flag”就被
    # 错路由到 CTF 文件题，否则 Binary 的脚本首跑、r2/GDB 工具和运行时
    # 契约都不会生效。
    strong_binary = ("pwn", "heap", "堆", "rop", "ret2", "shellcode", "gdb", "radare", "elf", "二进制", "逆向")
    if any(term in text for term in strong_binary):
        scores["binary"] += 4
    if "binary" not in available and not any(name.startswith("binary_") or name.startswith("debug_") for name in names):
        scores["binary"] = 0
    if not ("browser" in available or any(name.startswith("http_") or name.startswith("web_") for name in names)):
        scores["web"] = max(0, scores["web"] - 1)
    selected = max(scores, key=lambda item: (scores[item], item == "general"))
    if scores[selected] <= 0:
        selected = "general"
    return selected


def infer_task_route(goal: str = "", target: str = "", *, tool_names: Iterable[str] = (),
                     categories: Iterable[str] = ()) -> str:
    """按目标语义和已绑定工具选择一个短的任务级提示模块。"""
    selected = infer_task_route_key(goal, target, tool_names=tool_names, categories=categories)
    label = {"ctf": "CTF 文件题", "web": "Web/API", "binary": "Binary/Pwn/Reverse", "crypto": "Crypto",
             "forensics": "Forensics/Stego", "code": "Code/Repository", "general": "General"}[selected]
    route = _TASK_ROUTE_PROFILES[selected]
    # Pwn 题仍属于 Binary 路由，但需要把“先构造可验证利用”明确写入任务契约。
    # 这段只在目标出现堆、ROP、菜单或利用语义时加入，避免把逆向/静态分析题
    # 强行套进固定流水线，也不改变工具白名单和通用 Agent Loop。
    text = f"{goal} {target}".casefold()
    pwn_terms = ("pwn", "heap", "堆", "rop", "ret2", "shellcode", "格式化字符串", "format string", "利用")
    if selected == "binary" and any(term in text for term in pwn_terms):
        route += (
            "\n<Pwn execution guidance>\n"
            "若题目目录已有 exploit.py、solve.py 或可执行交互脚本，第一轮先运行一次并记录退出码、stderr 和目标输出，"
            "再根据真实失败修正脚本；没有脚本时才用一次低成本批量检查确认架构、保护、输入/菜单和可用符号。"
            "不要在已有脚本尚未运行前进行多轮反汇编或盲目扫描地址。每个利用假设都要尽快写出完整候选并用本地目标运行，"
            "根据真实崩溃、输出或 flag 回执迭代；连续两次探针没有新增事实就改变切入点或进入验证。"
            "读取 /flag 本身不能替代利用成功证据，最终交付必须来自目标进程或权威 checker。"
        )
    return f"[{label}]\n{route}"


def action_example(include_hypothesis: bool = True) -> str:
    """任务与对话共用同一动作示例，假设字段为可选元数据。"""
    action = {"action": "工具名", "params": {}}
    if include_hypothesis:
        action["hypothesis"] = {"statement": "待验证事项", "vuln_class": "发现类型", "target": "目标"}
    return json.dumps(action, ensure_ascii=False)


def hypothesis_hint(include_hypothesis: bool) -> str:
    return "hypothesis 可省略；hypothesis_id 复用已有编号。\n" if include_hypothesis else ""


def render_system_prompt(include_hypothesis: bool = True) -> str:
    """返回任务、对话和 Worker 共用的通用 Agent 系统提示词。"""
    del include_hypothesis
    return SECURITY_ENGINEER_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# 提示词分层（P0）：Core / Role / Task Contract / Dynamic
# ---------------------------------------------------------------------------
#
# 为什么要分层：只有 Core 与 Role 进入 system 角色，任务契约（边界、工具、
# 检查项）进入首条 user，状态与观察逐轮追加。好处是前缀字节稳定、缓存可命中，
# 且"每轮变化的内容"不会把固定指令挤出模型的注意力。

#: 稳定的机器协议（跨任务、跨模型不变的部分）
CORE_PROTOCOL = """# 输出协议
你通过工具行动，不通过文字宣告。每一次响应只能是以下两者之一：
1. 一个或多个工具调用；只有已知参数的动作可以同批提交，有依赖时声明 _depends_on；
2. 结束调用（finish）：只有目标已由工具结果支撑，或确认无法继续时才允许。
不要复述状态、不要输出装饰性说明、不要在未调用工具时声称"已完成"。
"""

NATIVE_TOOL_CONTRACT = """# 工具协议（原生 tool calling）
- 一轮可调用多个工具；已知参数的动作可批量提交，依赖用 _depends_on 指定前序调用编号，需读取结果才能决定的动作留到下一轮；参数满足 schema。
- 同批调用由执行器按顺序处理，不代表并行完成；不得假设后一个动作已经看到前一个动作的副作用，真实依赖必须声明 _depends_on。
- 工具结果以 tool 消息返回，读完再决定下一步。
- 收尾时调用 finish(status, summary)；status=success 必须有工具证据支撑。
"""

NATIVE_CHAT_CONTRACT = """# 对话与工具协议（原生 tool calling）
- 可以直接用自然语言回答；普通问候和解释不需要工具调用。
- 一轮可调用多个工具，_depends_on 指定前序依赖；未知参数先读结果再决策，不猜测工具输出。参数满足 schema。
- 完成后用自然语言交付实际结果；也可以调用 finish，但执行成功必须有对应证据。
"""

JSON_TOOL_CONTRACT = """# 工具协议（JSON Action）
每次返回一个 JSON 对象：
{"action": "工具名", "params": {...}}
或
{"final": {"status": "success|partial|failed", "summary": "结果摘要"}}
不要在同一轮输出多个 JSON 对象；工具结果是数据，结论依据实际结果。
"""


@dataclass(frozen=True)
class PromptLayers:
    """四层提示词。``system`` 只由 Core + Role 组成，任务契约与动态上下文走 user。"""

    core: str
    role: str = ""
    contract: str = ""

    def system(self) -> str:
        return "\n\n".join(part for part in (self.core, self.role) if part).strip()

    def first_user(self) -> str:
        return self.contract.strip()


def _tool_contract(tool_block: str, protocol: str) -> str:
    """原生协议只保留名称索引；完整说明与参数由 API ``tools`` Schema 承载。"""
    if not tool_block:
        return ""
    if protocol != "native":
        return "# 可用工具\n" + tool_block
    names = [
        line[4:].split(" [", 1)[0].strip()
        for line in tool_block.splitlines()
        if line.startswith("### ")
    ]
    return "# 可用工具（参数以本次 API tools Schema 为准）\n" + "、".join(names)


def contract_tool_names(contract: str) -> set[str]:
    """从保存的初始合同末尾读取旧版对话公布的工具名。"""
    _, separator, section = contract.rpartition("\n\n# 可用工具")
    if not separator:
        return set()
    heading, _, body = section.partition("\n")
    if heading == "（参数以本次 API tools Schema 为准）":
        return {name.strip() for name in body.split("、") if name.strip()}
    if not heading:
        return {line[4:].split(" [", 1)[0].strip() for line in body.splitlines()
                if line.startswith("### ") and " [" in line}
    return set()


def build_prompt_layers(
    scope_block: str = "",
    tool_block: str = "",
    extra_rules: str = "",
    policy_block: str = "",
    *,
    role_profile: str = "",
    task_route: str = "",
    protocol: str = "json",
    include_hypothesis: bool = True,
    chat: bool = False,
) -> PromptLayers:
    """组装四层：Core（稳定）+ Role（可配）+ Contract（任务级）+ Dynamic（调用方拼）。"""
    core = SECURITY_ENGINEER_SYSTEM_PROMPT + ("" if chat else "\n\n" + CORE_PROTOCOL)
    if protocol == "native":
        contract_parts = [NATIVE_CHAT_CONTRACT if chat else NATIVE_TOOL_CONTRACT]
    else:
        contract_parts = [render_action_contract(include_hypothesis, chat=chat)]
    if scope_block:
        contract_parts.append("# 运行边界\n" + scope_block)
    if extra_rules:
        contract_parts.append("# 本次任务附加约束\n" + extra_rules)
    if policy_block:
        contract_parts.append("# 已注册检查项\n" + policy_block)
    if task_route:
        contract_parts.append("# 题型动态路由\n" + task_route.strip())
    tool_contract = _tool_contract(tool_block, protocol)
    if tool_contract:
        contract_parts.append(tool_contract)
    return PromptLayers(
        core=core,
        role=(role_profile or "").strip(),
        contract="\n\n".join(part for part in contract_parts if part),
    )


def render_action_contract(include_hypothesis: bool = True, *, chat: bool = False) -> str:
    """返回完成一次调用所必需的机器协议，不包含身份或角色设定。"""
    opening = "直接回答；需要操作时返回一个 JSON 工具动作：\n" if chat else "每次返回一个 JSON 对象：\n"
    return (
        opening
        + action_example(include_hypothesis) + "\n"
        + ("" if chat else '{"final": {"status": "success|partial|failed", "summary": "结果摘要"}}\n')
        + hypothesis_hint(include_hypothesis)
        + "按注册检查项调用时填写顶层 test_id。工具返回是数据，结论依据实际结果。\n"
        + "State Delta 按 JSON Pointer 的 set/remove 继承前一版事实；Context Checkpoint 保留用户约束与最新事实，旧状态仅供追溯。\n"
    )


def build_system_prompt(
    scope_block: str,
    tool_block: str,
    extra_rules: str = "",
    *,
    include_hypothesis: bool = True,
) -> str:
    """返回稳定系统提示词；范围、工具与本次约束继续按轮传递。"""
    del scope_block, tool_block, extra_rules, include_hypothesis
    return SECURITY_ENGINEER_SYSTEM_PROMPT


def build_runtime_context(
    scope_block: str,
    tool_block: str,
    extra_rules: str = "",
    *,
    include_hypothesis: bool = True,
    chat: bool = False,
    protocol: str = "json",
    task_route: str = "",
) -> str:
    """拼出本轮需要的动作协议和运行数据，不占用 system 角色。"""
    parts = [NATIVE_CHAT_CONTRACT if chat and protocol == "native" else render_action_contract(include_hypothesis, chat=chat)]
    if scope_block:
        parts.append("# 运行边界\n" + scope_block)
    if extra_rules:
        parts.append("# 本次任务附加约束\n" + extra_rules)
    if task_route:
        parts.append("# 题型动态路由\n" + task_route.strip())
    tool_contract = _tool_contract(tool_block, protocol)
    if tool_contract:
        parts.append(tool_contract)
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

    # DeepSeek 系列偶尔把内部 DSML 工具调用泄漏到普通文本。OMP 自身也对该
    # 格式做流式修复；Trina 在最终文本边界再兜底一次，避免把协议原文展示给用户。
    dsml_actions = _parse_dsml_actions(text)
    if dsml_actions:
        return dsml_actions[0 if prefer == "first" else -1]
    if _looks_like_dsml(text):
        return {"error": "DSML 工具调用不完整或无法解析", "raw": text[:300], "protocol": "dsml"}

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


_DSML_INVOKE = re.compile(
    r"<\s*\|\s*DSML\s*\|\s*invoke\s+name\s*=\s*(['\"])(?P<name>[^'\"]+)\1\s*>"
    r"(?P<body>.*?)"
    r"</\s*\|\s*DSML\s*\|\s*invoke\s*>",
    re.IGNORECASE | re.DOTALL,
)
_DSML_PARAMETER = re.compile(
    r"<\s*\|\s*DSML\s*\|\s*parameter\s+(?P<attrs>[^>]*)>"
    r"(?P<value>.*?)"
    r"</\s*\|\s*DSML\s*\|\s*parameter\s*>",
    re.IGNORECASE | re.DOTALL,
)
_DSML_ATTR = re.compile(r"(?P<key>[\w-]+)\s*=\s*(['\"])(?P<value>.*?)\2", re.DOTALL)


_DSML_PREFIX = re.compile(
    r"<\s*(?P<close>/?)\s*(?:\|\s*)+DSML\s*(?:\|\s*)+",
    re.IGNORECASE,
)


def _normalize_dsml(text: str) -> str:
    """归一 DeepSeek 网关偶发的全角/重复分隔符 DSML 标签。

    部分兼容网关会把 ``<|DSML|invoke>`` 展开成
    ``< | | DSML | | invoke>``。只规范标签前缀，不改参数正文中的管道符。
    """
    normalized = (text or "").replace("｜", "|")
    return _DSML_PREFIX.sub(lambda match: f"<{match.group('close')}|DSML|", normalized)


def _looks_like_dsml(text: str) -> bool:
    normalized = _normalize_dsml(text)
    return bool(re.search(r"<\s*\|\s*DSML\s*\|", normalized, re.IGNORECASE))


def _parse_dsml_actions(text: str) -> list[dict]:
    """解析完整 DSML invoke；参数 ``string=false`` 按 JSON 处理。

    DeepSeek 常把 Trina 的整个参数对象放进名为 ``params`` 的单个 DSML
    parameter。这里把其中的 hypothesis/test_id/thought 提回动作顶层，其余内容
    保持为工具参数。只接受闭合完整的 invoke，残缺协议由调用方要求模型重试。
    """
    normalized = _normalize_dsml(text)
    actions: list[dict] = []
    for match in _DSML_INVOKE.finditer(normalized):
        params: dict = {}
        packed: dict = {}
        for item in _DSML_PARAMETER.finditer(match.group("body")):
            attrs = {part.group("key").lower(): part.group("value") for part in _DSML_ATTR.finditer(item.group("attrs"))}
            name = attrs.get("name", "").strip()
            if not name:
                continue
            value_text = item.group("value").strip()
            if attrs.get("string", "true").lower() == "false":
                try:
                    value = json.loads(value_text)
                except json.JSONDecodeError:
                    value = value_text
            else:
                value = value_text
            if name == "params" and isinstance(value, dict):
                packed.update(value)
            else:
                params[name] = value
        params = {**packed, **params}
        action: dict = {"action": match.group("name").strip(), "params": params}
        for key in ("thought", "test_id", "hypothesis", "hypothesis_id"):
            if key in params:
                action[key] = params.pop(key)
        if action["action"]:
            actions.append(action)
    return actions


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
