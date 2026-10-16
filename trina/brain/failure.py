"""失败分类与循环识别：把"重试"变成有依据的升级阶梯。

文档建议（本模块的实现依据）
--------------------------
* 失败分四类：**工具错误 / 环境错误 / 假设错误 / 预算耗尽**（本模块额外拆出"边界拒绝"一类，
  因为护栏拒绝既不是工具错也不是环境错，它意味着"路走错了"）；
* **重复行为识别比在提示词里写"不要重复尝试"可靠**：对"规范化工具调用 + 结果摘要"生成指纹，
  识别"没有新信息"的循环；
* 升级阶梯：换参数 → 换等价工具 → 换阶段/切入点 → 请求人工。

本模块只做判断与建议，不改状态、不发网络请求——便于单测。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

FAILURE_KINDS = ("ok", "tool_error", "environment_error", "hypothesis_error", "budget_exhausted", "boundary_denied")
FAILURE_CN = {
    "ok": "正常",
    "tool_error": "工具调用错误",
    "environment_error": "环境错误",
    "hypothesis_error": "假设错误（跑了但没新信息）",
    "budget_exhausted": "预算耗尽",
    "boundary_denied": "被护栏/控制器拒绝",
}

#: 环境类错误（目标不可达、超时、DNS、TLS……而不是工具自身用法错）
_ENV_PATTERNS = re.compile(
    r"(connection\s+(refused|reset|aborted)|timed?\s*out|timeout|unreachable|no\s+route|"
    r"name\s+or\s+service\s+not\s+known|temporary\s+failure|ssl|certificate|拒绝连接|连接超时|不可达)",
    re.I,
)

#: 工具自身错误（参数写错、依赖缺失、不支持）
_TOOL_PATTERNS = re.compile(
    r"(参数非法|未知参数|缺少必填|取值必须|未安装|unknown\s+tool|not\s+found|not\s+installed|"
    r"命令不在白名单|unsupported|requires)",
    re.I,
)

#: 预算相关
_BUDGET_PATTERNS = re.compile(r"(预算|budget|步数上限|超时预算|预算用尽)", re.I)


def classify_failure(result: Any, *, new_information: bool = False) -> str:
    """把一次工具结果归类。优先级：边界拒绝 > 预算 > 工具 > 环境 > 假设（无新信息）。"""
    if result is None:
        return "ok"
    if getattr(result, "denied", False):
        return "boundary_denied"
    if getattr(result, "ok", False):
        return "ok"
    text = f"{getattr(result, 'error', '') or ''} {getattr(result, 'summary', '') or ''}"
    if _BUDGET_PATTERNS.search(text):
        return "budget_exhausted"
    if _TOOL_PATTERNS.search(text):
        return "tool_error"
    if _ENV_PATTERNS.search(text):
        return "environment_error"
    # 跑了但什么都没得到 → 假设层面没有支撑
    return "hypothesis_error" if not new_information else "tool_error"


def advice_for(kind: str, *, alternatives: list[str] | None = None, attempt: int = 0) -> str:
    """按失败类型给出下一步建议（文档里的四类处理）。"""
    if kind == "tool_error":
        return "校正参数或检查依赖（不要原样重试同一调用）"
    if kind == "environment_error":
        return "先确认目标服务/权限/网络是否可用（例如换端口重探、检查是否需要登录）"
    if kind == "budget_exhausted":
        return "缩小任务范围或请求人工决定是否追加预算"
    if kind == "boundary_denied":
        return "该动作被判为越界：换合规路径，或请人工确认授权后再继续"
    if kind == "hypothesis_error":
        if alternatives:
            return f"这条路线已无新信息，换等价工具试试：{', '.join(alternatives[:3])}"
        if attempt >= 2:
            return "这条假设连续无支撑：记录反证并降低同路线优先级，或换切入点"
        return "换参数或换切入点，避免在同一方法上打转"
    return ""


@dataclass
class LoopGuard:
    """重复行为识别：指纹 = (工具, 规范化参数, 结果摘要)。"""

    max_repeats: int = 3
    action_counter: Counter = field(default_factory=Counter)
    fingerprint_counter: Counter = field(default_factory=Counter)
    total_steps: int = 0
    redundant_steps: int = 0
    last_advice: str = ""
    escalation_step: int = 0

    # ------------------------------------------------------------------ 指纹
    @staticmethod
    def params_key(params: dict[str, Any] | None) -> str:
        try:
            return json.dumps(params or {}, sort_keys=True, ensure_ascii=False)[:300]
        except TypeError:
            return str(params)[:300]

    @staticmethod
    def result_digest(result: Any) -> str:
        """结果指纹：摘要 + 数据 + 原始输出前 4KB 的哈希（不看时间/耗时这类抖动字段）。"""
        parts = [
            str(getattr(result, "summary", "") or "")[:300],
            str(getattr(result, "error", "") or "")[:200],
            json.dumps(getattr(result, "data", None) or {}, sort_keys=True, ensure_ascii=False)[:2000],
            str(getattr(result, "raw", "") or "")[:4000],
        ]
        return hashlib.sha256("\n".join(parts).encode("utf-8", "replace")).hexdigest()[:16]

    # ------------------------------------------------------------------ 观察
    def observe(self, tool: str, params: dict[str, Any] | None, result: Any) -> str:
        """记录一次调用；若判定为"无新信息"，返回升级建议（否则返回空串）。"""
        self.total_steps += 1
        action = f"{tool}:{self.params_key(params)}"
        fingerprint = f"{action}:{self.result_digest(result)}"
        self.action_counter[action] += 1
        self.fingerprint_counter[fingerprint] += 1
        repeats = self.fingerprint_counter[fingerprint]
        if repeats < 2:
            return ""
        self.redundant_steps += 1
        self.escalation_step += 1
        self.last_advice = self._escalate(repeats, tool)
        return self.last_advice

    def _escalate(self, repeats: int, tool: str) -> str:
        """① 换参数 → ② 换等价工具 → ③ 换阶段 → ④ 请求人工。"""
        if repeats == 2:
            return f"{tool} 这次的结果与上次完全相同（无新信息）：换参数或换切入点"
        if repeats == 3:
            return f"{tool} 已连续 {repeats} 次得到相同结果：必须换工具或换阶段，不要继续同调用"
        return f"{tool} 已连续 {repeats} 次无新信息：停下来请求人工判断（当前路线可能整体走不通）"

    # ------------------------------------------------------------------ 统计
    @property
    def redundant_rate(self) -> float:
        if self.total_steps <= 0:
            return 0.0
        return round(self.redundant_steps / self.total_steps, 4)

    def needs_human(self) -> bool:
        return self.escalation_step >= self.max_repeats

    def tried_tools(self) -> set[str]:
        """已经试过的工具名（用于"换等价工具"时排除）。"""
        return {key.split(":", 1)[0] for key in self.action_counter}

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_steps": self.total_steps,
            "redundant_steps": self.redundant_steps,
            "redundant_action_rate": self.redundant_rate,
            "last_advice": self.last_advice,
        }


def alternatives_for(registry: Any, tool_name: str, *, tried: set[str] | None = None) -> list[str]:
    """同类别里还没试过的等价工具（用于"换工具"这一步）。"""
    if registry is None:
        return []
    spec = registry.get(tool_name)
    if spec is None:
        return []
    category = spec.spec.category
    tried = tried or set()
    candidates = [
        name
        for name in registry.names()
        if name != tool_name
        and name not in tried
        and (other := registry.get(name)) is not None
        and other.spec.category == category
    ]
    # 低等级优先（先试更保守的做法）
    order = {"detect": 0, "verify": 1, "exploit": 2}
    candidates.sort(key=lambda name: (order.get(registry.get(name).spec.action_level, 9), name))
    return candidates


__all__ = [
    "FAILURE_CN",
    "FAILURE_KINDS",
    "LoopGuard",
    "advice_for",
    "alternatives_for",
    "classify_failure",
]
