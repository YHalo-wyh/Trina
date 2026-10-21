"""确定性假设调度：把「按优先级排序」从提示词变成真正影响动作选择。

为什么需要
----------
E4 实测与 `docs/TARGET_PROGRESS.md` 的缺口一致：台账把假设写进提示词，但**动作仍由模型自由选择**——
优先级只是"建议"，模型完全可能一直不去验证最高优先级的那条。本模块补上确定性的那一段：

1. 从台账里挑一条**可确定性验证**的最高优先级假设（必须绑定 `vuln_class` + `target`）；
2. 把它映射到注册检查项（`RegisteredTest.expect_finding` ↔ 假设的 `vuln_class`，用与评分同一套归一化）；
3. 生成调度指令进提示词（含 `test_id` 与目标），并要求模型要么执行、要么说明为什么换路；
4. 记录**遵从率**：模型是否按调度执行，进 `state.counters["scheduler_miss"]`、`state.scheduler` 与审计。

刻意保守的边界
--------------
* 调度只指定方向，**不直接执行工具**：执行仍经控制器与护栏（ADR-0003 单一收口）；
* 只调度"有验证对象"的假设（无 `vuln_class`/`target` 的描述性假设不参与，避免瞎指挥）；
* 同类检查项试过且该假设已有尝试过 → 不再重复绑定同一 `test_id`（与循环守卫同口径，避免打转），
  改为"自由验证"（让模型提高验证强度）；
* 连续未遵从 ≥ `escalate_after` 次时升级为硬提示，但**不阻断工具、不代模型编造证据**。

遵从判据（可测量、可复核）
--------------------------
动作有明确 ``test_id`` 时必须与调度一致；未解析出 ``test_id`` 时才按工具名回退匹配，
避免把“同工具的另一条检查项”统计成遵从；
没有绑定（既无 `test_id` 也无工具）时判定为"不可测量"（返回 ``None``，不计入遵从率分母）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..eval.metrics import normalize_vuln_class

#: 低于此优先级的假设不值得占用一次调度（默认与 E4 实验一致）
DEFAULT_MIN_PRIORITY = 0.15
#: 连续未遵从多少次后升级为硬提示
DEFAULT_ESCALATE_AFTER = 2


@dataclass
class ScheduleDecision:
    """一次调度决策：验证哪条假设、用哪个注册检查项。"""

    hypothesis_id: str
    statement: str
    vuln_class: str = ""
    target: str = ""
    parameter: str = ""
    test_id: str = ""
    tool: str = ""
    level: str = ""
    score: float = 0.0
    reason: str = ""
    required: bool = False
    #: 调度来源：hypothesis（台账假设）| goal（任务硬性要求，无假设时直接派检查项）
    source: str = "hypothesis"

    # ------------------------------------------------------------------ 视图
    def prompt_block(self) -> str:
        """进单步提示词的调度指令。"""
        lines = ["## 调度指令（确定性调度：本步优先验证最高优先级假设）"]
        if self.hypothesis_id:
            lines.append(f"- 待验证：{self.hypothesis_id}（优先级 {self.score:.2f}）：{self.statement}")
        else:
            lines.append(f"- 任务硬性要求：{self.statement}")
        lines.append(
            f"- 注册检查项：{self.test_id}（工具 {self.tool}，等级 {self.level or '-'}；"
            f"期望产出 {self.vuln_class} 类发现）→ 请在动作里带 \"test_id\": \"{self.test_id}\""
        )
        if self.required:
            lines.append(
                "- **本步必须执行上述检查项**：若确有理由换路，必须在 thought 里说明理由（未遵从会记入审计）"
            )
        else:
            lines.append("- 若该检查项不可用（工具缺失/目标不符），可改用仍能产出同类证据的动作，并在 thought 里说明理由。")
        if self.reason:
            lines.append(f"- 选它的理由：{self.reason}")
        return "\n".join(lines)

    def matches_action(self, *, test_id: str = "", tool: str = "") -> bool:
        """动作是否遵从调度（见模块 docstring 的判据）。"""
        if self.test_id and test_id:
            return test_id == self.test_id
        if self.tool and tool and tool == self.tool:
            return True
        return False

    @property
    def measurable(self) -> bool:
        """能绑到检查项的调度才算可测量（本模块只产出可测量的调度）。"""
        return bool(self.test_id or self.tool)

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["measurable"] = self.measurable
        return data


@dataclass
class HypothesisScheduler:
    """把台账优先级翻译成"这一步该验证什么"的确定性调度器。"""

    min_priority: float = DEFAULT_MIN_PRIORITY
    escalate_after: int = DEFAULT_ESCALATE_AFTER
    decisions: int = 0
    followed: int = 0
    missed: int = 0
    goal_directives: int = 0
    consecutive_misses: int = 0
    last: ScheduleDecision | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------ 度量
    @property
    def compliance_rate(self) -> float:
        denominator = self.followed + self.missed
        return round(self.followed / denominator, 4) if denominator else 0.0

    # ------------------------------------------------------------------ 决策
    def decide(
        self,
        ledger: Any,
        tests: list[Any] | None = None,
        *,
        executed_tests: set[str] | None = None,
        goal_classes: set[str] | None = None,
    ) -> ScheduleDecision | None:
        """挑一条最高优先级的**可绑定检查项**的假设；没有可调度的项时返回 None。

        只调度"能落到某个注册检查项"的假设：绑定不了的（`vuln_class` 不是发现类、该靶场没注册同类检查、
        同类检查都跑过）一律不调度——提示词里塞一条无法验收的指令只会烧 token（首轮冒烟实测：
        模型把 `vuln_class` 写成阶段名 `recon`，5 次调度全部无法测量）。

        **任务硬性要求优先**：`goal_classes`（来自 `GoalRequirements.classes`）里的漏洞类只要有一条
        可绑定的假设，就不再调度其它类——否则调度会与"未完成前不允许收敛"的任务要求打架
        （实测：目标要求验证 SQLi/LFI，调度却死盯信息泄露假设，预算烧光还没拿到 flag）。
        """
        tests = list(tests or [])
        executed = set(executed_tests or set())
        wanted_goal = {normalize_vuln_class(item) for item in (goal_classes or set()) if str(item).strip()}

        candidates: list[ScheduleDecision] = []
        for item in ledger.open_items():
            if not (item.vuln_class and item.target):
                continue  # 描述性假设不参与调度（没有验证对象，调度只会瞎指挥）
            score = item.priority(getattr(ledger, "risk_aversion", 1.0))
            if score < self.min_priority:
                continue
            test = self._bind(item, tests, executed)
            if test is None:
                continue  # 绑定不到检查项：没有确定性可说的东西
            candidates.append(
                ScheduleDecision(
                    hypothesis_id=item.id,
                    statement=item.statement,
                    vuln_class=item.vuln_class,
                    target=item.target,
                    parameter=item.parameter,
                    test_id=getattr(test, "test_id", ""),
                    tool=getattr(test, "tool", ""),
                    level=getattr(test, "level", ""),
                    score=score,
                    reason=(
                        f"最高优先级可绑定假设（{item.vuln_class}@{item.target}）→ 检查项 {getattr(test, 'test_id', '')}"
                        + ("（命中任务要求）" if wanted_goal and normalize_vuln_class(item.vuln_class) in wanted_goal else "")
                    ),
                    required=self.consecutive_misses >= self.escalate_after,
                )
            )
        if wanted_goal:
            on_goal = [item for item in candidates if normalize_vuln_class(item.vuln_class) in wanted_goal]
            if on_goal:
                candidates = on_goal
            else:
                # 任务要求里还没有对应假设 → 目标驱动调度：直接把该类的注册检查项派下去
                # （实测：目标要求 SQLi/LFI、台账里只有信息泄露假设时，按台账调度会与"未完成不允许收敛"打架）
                directive = self._goal_directive(wanted_goal, tests, executed)
                if directive is not None:
                    self.last = directive
                    self.decisions += 1
                    self.goal_directives += 1
                    return directive
        best = max(candidates, key=lambda item: item.score) if candidates else None
        self.last = best
        if best is not None:
            self.decisions += 1
        return best

    def _goal_directive(self, goal_classes: set[str], tests: list[Any], executed: set[str]) -> ScheduleDecision | None:
        """目标类还没跑过对应检查项时，直接给出确定性指令（无假设也能调度）。"""
        by_class: dict[str, Any] = {}
        for test in tests:
            if getattr(test, "test_id", "") in executed:
                continue
            expect = getattr(test, "expect_finding", "")
            if not expect:
                continue
            key = normalize_vuln_class(expect)
            if key in goal_classes and key not in by_class:
                by_class[key] = test
        for key in sorted(by_class):
            test = by_class[key]
            if getattr(test, "test_id", "") in executed:
                continue
            return ScheduleDecision(
                hypothesis_id="",
                statement=f"任务硬性要求：验证 {key}（台账里还没有对应假设）",
                vuln_class=key,
                test_id=getattr(test, "test_id", ""),
                tool=getattr(test, "tool", ""),
                level=getattr(test, "level", ""),
                score=1.0,
                reason=f"目标驱动调度：任务要求验证 {key}，绑定注册检查项 {getattr(test, 'test_id', '')}",
                required=self.consecutive_misses >= self.escalate_after,
                source="goal",
            )
        return None

    @staticmethod
    def _bind(item: Any, tests: list[Any], executed: set[str]) -> Any | None:
        """假设的 vuln_class → 注册检查项（expect_finding 归一化后匹配）。"""
        wanted = normalize_vuln_class(item.vuln_class)
        same_class = [
            test
            for test in tests
            if getattr(test, "expect_finding", "") and normalize_vuln_class(test.expect_finding) == wanted
        ]
        if not same_class:
            return None
        if getattr(item, "attempts", 0) > 0:
            # 这条假设已经试过一轮：优先挑没跑过的同类检查项；都跑过就不再重复绑定
            fresh = [test for test in same_class if getattr(test, "test_id", "") not in executed]
            return fresh[0] if fresh else None
        return same_class[0]

    # ------------------------------------------------------------------ 反馈
    def observe(self, decision: ScheduleDecision | None, *, test_id: str = "", tool: str = "") -> bool | None:
        """记录模型实际动作是否遵从调度；返回 True/False，无调度时返回 None。"""
        if decision is None:
            return None
        followed = decision.matches_action(test_id=test_id, tool=tool)
        if followed:
            self.followed += 1
            self.consecutive_misses = 0
        else:
            self.missed += 1
            self.consecutive_misses += 1
        self.history.append(
            {
                "hypothesis_id": decision.hypothesis_id,
                "expected": decision.test_id or decision.tool,
                "actual": test_id or tool,
                "followed": followed,
            }
        )
        if len(self.history) > 50:
            self.history = self.history[-50:]
        return followed

    def to_dict(self) -> dict[str, Any]:
        return {
            "decisions": self.decisions,
            "goal_directives": self.goal_directives,
            "followed": self.followed,
            "missed": self.missed,
            "compliance_rate": self.compliance_rate,
            "last": self.last.to_dict() if self.last is not None else None,
            "recent": self.history[-5:],
        }


__all__ = ["DEFAULT_ESCALATE_AFTER", "DEFAULT_MIN_PRIORITY", "HypothesisScheduler", "ScheduleDecision"]
