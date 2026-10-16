"""假设台账（Hypothesis Ledger）：把"待验证的问题"变成结构化对象，按优先级排序。

为什么需要它
------------
普通 ReAct 只有"思考→调工具→看输出"，模型容易反复撞同一面墙，或者因为一段不可靠输出就推翻整个计划。
本模块把每个假设做成可追踪对象，并用显式优先级函数决定"下一步做哪个"：

    Priority(h) = P(h) × V(h) × I(h) / (C(h) + λ·R(h) + ε)

* P 可信程度（先验，启发式，不是统计校准概率）
* V 成功价值（拿到结论对任务有多重要）
* I 信息增益（无论成败都能学到多少）
* C 成本（时间/Token）、R 风险（越界/破坏性/暴露）
* λ 风险厌恶系数（默认 1.0，可由配置调）

状态完全由**确定性代码**依据工具结果更新（匹配的带证据验证 → confirmed；明确反证 → refuted），
模型只能提出假设与置信度建议，不能自行宣布"假设成立"。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urlsplit

#: 假设状态（与 findings 的三态解耦：假设是"待验证的问题"，漏洞是"已确认的事实"）
HYPO_STATUSES = ("open", "verifying", "confirmed", "refuted", "blocked")
HYPO_STATUS_CN = {
    "open": "待验证",
    "verifying": "验证中",
    "confirmed": "已确认",
    "refuted": "已否定",
    "blocked": "被护栏阻断",
}

#: 等级的粗粒度取值（文档建议：初期用规则与有限等级就够，别装作精确概率）
LEVEL_CHOICES = (0.2, 0.4, 0.6, 0.8)


def _clamp(value: Any, default: float = 0.5) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.05, number))


def _level(value: Any, default: float = 0.5) -> float:
    """把"高/中/低"或数字统一成有限等级，避免模型给出假精确的 0.8342。"""
    if isinstance(value, str):
        text = value.strip().lower()
        mapping = {
            "high": 0.8, "高": 0.8, "h": 0.8,
            "medium": 0.6, "mid": 0.6, "中": 0.6, "m": 0.6,
            "low": 0.4, "低": 0.4, "l": 0.4,
            "very low": 0.2, "极低": 0.2,
        }
        if text in mapping:
            return mapping[text]
        try:
            return _clamp(float(text), default)
        except ValueError:
            return default
    return _clamp(value, default)


@dataclass
class Hypothesis:
    """一条待验证的假设。"""

    id: str
    statement: str
    status: str = "open"
    confidence: float = 0.5      # P
    value: float = 0.6           # V
    info_gain: float = 0.6       # I
    cost: float = 0.4            # C
    risk: float = 0.2            # R
    next_step: str = ""
    vuln_class: str = ""         # 自动确认所需的结构化验证对象
    target: str = ""
    parameter: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    attempts: int = 0
    stale_attempts: int = 0      # 连续"无新信息"的尝试次数
    source: str = ""             # model | human | planner
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # ------------------------------------------------------------------ 计算
    def matches_finding(self, finding: Any) -> bool:
        """只把同漏洞类、同位置、同参数的结构化发现关联到本假设。"""
        if not self.vuln_class or not self.target:
            return False
        if self.vuln_class.strip().lower() != str(finding.vuln_class).strip().lower():
            return False
        if self.parameter and self.parameter != finding.parameter:
            return False

        def location(value: str) -> tuple[Any, ...]:
            parsed = urlsplit(value)
            if parsed.scheme and parsed.netloc:
                port = parsed.port or {"http": 80, "https": 443}.get(parsed.scheme)
                return (parsed.scheme.lower(), parsed.hostname, port, parsed.path or "/")
            return (value,)

        try:
            return location(self.target) == location(finding.url or finding.target)
        except ValueError:
            return False

    def priority(self, risk_aversion: float = 1.0) -> float:
        """Priority(h) = P×V×I / (C + λR + ε)。已收敛的假设优先级置 0。"""
        if self.status in ("confirmed", "refuted"):
            return 0.0
        epsilon = 0.05
        numerator = self.confidence * self.value * self.info_gain
        denominator = self.cost + risk_aversion * self.risk + epsilon
        return round(numerator / denominator, 4)

    def add_evidence(self, item: dict[str, Any]) -> None:
        entry = dict(item)
        entry.setdefault("ts", round(time.time(), 3))
        self.evidence.append(entry)
        self.updated_at = time.time()

    # ------------------------------------------------------------------ 视图
    def render(self) -> str:
        cn = HYPO_STATUS_CN.get(self.status, self.status)
        return (
            f"{self.id} [{cn}] P={self.confidence:.2f} V={self.value:.2f} I={self.info_gain:.2f} "
            f"C={self.cost:.2f} R={self.risk:.2f} 优先级={self.priority():.2f} — {self.statement}"
            + (f"（下一步：{self.next_step}）" if self.next_step else "")
        )

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["priority"] = self.priority()
        data["status_cn"] = HYPO_STATUS_CN.get(self.status, self.status)
        return data


class HypothesisLedger:
    """假设集合：新增、更新、按优先级排序、给模型看的摘要。"""

    def __init__(self, *, risk_aversion: float = 1.0, max_items: int = 40):
        self.items: dict[str, Hypothesis] = {}
        self.risk_aversion = risk_aversion
        self.max_items = max_items
        self._seq = 0

    # ------------------------------------------------------------------ 增
    def next_id(self) -> str:
        self._seq += 1
        return f"H-{self._seq:03d}"

    def add(
        self,
        statement: str,
        *,
        confidence: Any = 0.5,
        value: Any = 0.6,
        info_gain: Any = 0.6,
        cost: Any = 0.4,
        risk: Any = 0.2,
        next_step: str = "",
        source: str = "model",
        hypothesis_id: str = "",
        vuln_class: str = "",
        target: str = "",
        parameter: str = "",
    ) -> Hypothesis:
        text = (statement or "").strip()
        if not text:
            raise ValueError("假设内容不能为空")
        # 去重：与已有未收敛假设语义近似（前缀互相包含）则复用
        for existing in self.items.values():
            if existing.status not in ("open", "verifying"):
                continue
            if (existing.vuln_class, existing.target, existing.parameter) != (vuln_class, target, parameter):
                continue
            left, right = existing.statement[:24], text[:24]
            if left == right or left.startswith(right) or right.startswith(left):
                return existing
        if len(self.items) >= self.max_items:
            # 台账满时淘汰最低优先级的未收敛项
            worst = min(self.items.values(), key=lambda h: h.priority(self.risk_aversion))
            self.items.pop(worst.id, None)
        hid = hypothesis_id or self.next_id()
        item = Hypothesis(
            id=hid,
            statement=text[:400],
            confidence=_level(confidence, 0.5),
            value=_level(value, 0.6),
            info_gain=_level(info_gain, 0.6),
            cost=_level(cost, 0.4),
            risk=_level(risk, 0.2),
            next_step=str(next_step)[:200],
            vuln_class=vuln_class,
            target=target,
            parameter=parameter,
            source=source,
        )
        self.items[hid] = item
        return item

    def from_action(self, action: dict[str, Any]) -> Hypothesis | None:
        """模型可以在动作里附带假设：{"hypothesis": {"statement": …, "confidence": "high", …}}。"""
        raw = action.get("hypothesis")
        # 引用只能复用既有假设；未知编号不能覆盖或伪造台账记录。
        reference = action.get("hypothesis_id") or (raw.get("id") if isinstance(raw, dict) else "")
        if reference:
            return self.get(str(reference))
        if isinstance(raw, str) and raw.strip():
            return self.add(raw, source="model")
        if isinstance(raw, dict):
            statement = str(raw.get("statement") or raw.get("text") or "").strip()
            if not statement:
                return None
            return self.add(
                statement,
                confidence=raw.get("confidence", 0.5),
                value=raw.get("value", 0.6),
                info_gain=raw.get("info_gain", raw.get("information", 0.6)),
                cost=raw.get("cost", 0.4),
                risk=raw.get("risk", 0.2),
                next_step=str(raw.get("next_step") or ""),
                source="model",
                vuln_class=str(raw.get("vuln_class") or ""),
                target=str(raw.get("target") or raw.get("url") or ""),
                parameter=str(raw.get("parameter") or ""),
            )
        return None

    # ------------------------------------------------------------------ 改
    def record_tool_outcome(
        self, hypothesis_id: str, *, result: Any, state: Any, tool: str, new_information: bool,
    ) -> Hypothesis | None:
        """任务与对话模式共用的证据判定：验证对象匹配且本次产出带证据的确证。"""
        from .state import Finding

        hypothesis = self.get(hypothesis_id)
        if hypothesis is None:
            return None
        related: list[dict[str, Any]] = []
        confirmed = False
        for raw in (result.facts or {}).get("findings", []) or []:
            if not isinstance(raw, dict):
                continue
            data = {k: v for k, v in raw.items() if k in Finding.__dataclass_fields__}
            data.setdefault("id", "")
            data.setdefault("title", "")
            data.setdefault("vuln_class", "unknown")
            candidate = Finding(**data)
            if not hypothesis.matches_finding(candidate):
                continue
            saved = next((f for f in state.findings if f.dedup_key == candidate.dedup_key), None)
            if saved is not None:
                related.append({"kind": "tool", "tool": tool, "finding_id": saved.id})
                confirmed = confirmed or bool(
                    result.ok and not result.denied and candidate.evidence
                    and candidate.status in ("verified", "exploited")
                )
        return self.record_outcome(
            hypothesis_id, new_information=new_information, confirmed=confirmed,
            blocked=result.denied, note=str(result.summary or result.error)[:160], evidence=related,
        )

    def get(self, hypothesis_id: str) -> Hypothesis | None:
        return self.items.get(hypothesis_id)

    def record_outcome(
        self,
        hypothesis_id: str,
        *,
        new_information: bool,
        confirmed: bool = False,
        refuted: bool = False,
        blocked: bool = False,
        note: str = "",
        evidence: Iterable[dict[str, Any]] = (),
    ) -> Hypothesis | None:
        """按工具结果更新假设。**确定性规则**，不看模型的自述。"""
        item = self.items.get(hypothesis_id)
        if item is None:
            return None
        entries = list(evidence)
        item.attempts += 1
        item.updated_at = time.time()
        for entry in entries:
            item.add_evidence(entry)
        if note:
            item.notes.append(note[:200])
        # 已有确证不因后续超时或重复结果而消失；冲突证据保留供复核。
        if item.status in ("confirmed", "refuted"):
            return item
        if blocked:
            item.status = "blocked"
            item.confidence = max(0.05, item.confidence * 0.7)
            item.next_step = "换合规路径或请人工确认授权"
            return item
        if confirmed and entries:
            item.status = "confirmed"
            item.confidence = 0.9
            item.next_step = ""
            return item
        if refuted and entries:
            item.status = "refuted"
            item.next_step = ""
            return item
        if new_information:
            item.status = "verifying"
            item.confidence = min(0.95, item.confidence + 0.1)
            item.stale_attempts = 0
            return item
        # 无新信息只降低调度优先级，不能充当反证。
        item.stale_attempts += 1
        item.confidence = max(0.05, round(item.confidence * 0.85, 3))
        if item.stale_attempts >= 3:
            item.status = "open"
            item.next_step = "连续验证无新信息：换验证方法或请求人工判断，结论仍未解决"
            item.notes.append("连续验证无新信息，保留待验证状态")
        else:
            item.status = "verifying"
            item.cost = _clamp(item.cost + 0.1)
        return item

    # ------------------------------------------------------------------ 查
    def open_items(self) -> list[Hypothesis]:
        items = [h for h in self.items.values() if h.status in ("open", "verifying")]
        return sorted(items, key=lambda h: h.priority(self.risk_aversion), reverse=True)

    def confirmed(self) -> list[Hypothesis]:
        return [h for h in self.items.values() if h.status == "confirmed"]

    def refuted(self) -> list[Hypothesis]:
        return [h for h in self.items.values() if h.status == "refuted"]

    def blocked(self) -> list[Hypothesis]:
        return [h for h in self.items.values() if h.status == "blocked"]

    # ------------------------------------------------------------------ 摘要
    def prompt_block(self, limit: int = 5) -> str:
        """给模型的台账摘要：按优先级排序的待验证假设。"""
        open_items = self.open_items()
        if not open_items:
            return "（暂无待验证假设：可以先提出 1—3 个可验证的假设，再动手）"
        lines = ["待验证假设（按优先级排序；用 hypothesis_id 复用编号，hypothesis 字段新增假设）:"]
        for item in open_items[:limit]:
            lines.append(
                f"  - {item.id}（优先级 {item.priority(self.risk_aversion):.2f}）{item.statement}"
                + (f"｜下一步：{item.next_step}" if item.next_step else "")
            )
        if len(open_items) > limit:
            lines.append(f"  … 另有 {len(open_items) - limit} 条未列出")
        return "\n".join(lines)

    def summary(self) -> dict[str, Any]:
        return {
            "total": len(self.items),
            "open": len(self.open_items()),
            "confirmed": len(self.confirmed()),
            "refuted": len(self.refuted()),
            "blocked": len(self.blocked()),
        }

    def finalize(self) -> list[Hypothesis]:
        """收尾：把仍未收敛的假设标成"未解决"（不假装验证过，也不当作已否定）。

        返回被收尾的假设，供状态栏/报告说明"哪些问题这轮没答完"。
        """
        unfinished: list[Hypothesis] = []
        for item in self.items.values():
            if item.status not in ("open", "verifying"):
                continue
            item.status = "open"
            if not item.next_step:
                item.next_step = f"下一轮优先验证：{item.statement[:60]}"
            item.notes.append("本轮结束时仍未取得确凿证据（既未确认也未否定）")
            unfinished.append(item)
        return unfinished

    def to_dict(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in sorted(self.items.values(), key=lambda h: h.id)]


__all__ = [
    "HYPO_STATUSES",
    "HYPO_STATUS_CN",
    "Hypothesis",
    "HypothesisLedger",
    "LEVEL_CHOICES",
]
