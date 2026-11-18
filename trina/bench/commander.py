"""Bench Commander：在轮次边界观察进展，动态决定续做、加时或释放。

设计边界
--------
- **只做调度，不调用工具、不调用模型**：输入是已经发生的结构化事实（部分 Flag、
  事实增量、重复探索率、单题/全局预算），输出是一个有上界的动作。
- **先花完单题预算，再动用储备池**：避免一上来就给“看起来快出来了”的任务加时。
- **加时有硬上界**：`max_extensions`、`extension_seconds`、`min_remaining_seconds`
  和 `reserve_ratio` 共同约束，任何单题都不能吃掉整场预算。
- **无证据不续做**：没有部分 Flag、也没有新增事实时直接释放，避免沉没成本。

这个模块是确定性的。模型 Commander 如果将来接入，应实现同一 `review()` 契约，
并把每次判断写入审计；当前实现不产生额外模型调用。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

RETRY = "retry"
EXTEND = "extend"
RELEASE = "release"
CONTINUE = "continue"
ACTIONS = (RETRY, EXTEND, RELEASE, CONTINUE)


@dataclass(frozen=True)
class CommanderDecision:
    """一次轮次边界决策。"""

    action: str
    seconds: int = 0
    reason: str = ""

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"未知 Commander 动作: {self.action!r}")
        if type(self.seconds) is not int or self.seconds < 0:
            raise ValueError("Commander 加时秒数必须为非负整数")


@dataclass
class CommanderSettings:
    """动态决策的硬上界；所有字段都可被 CLI/环境变量覆盖。"""

    extension_seconds: int = 60
    max_extensions: int = 1
    reserve_ratio: float = 0.10
    min_remaining_seconds: int = 120
    max_redundant_rate: float = 0.80

    def validate(self) -> None:
        for name in ("extension_seconds", "max_extensions", "min_remaining_seconds"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"commander.{name} 必须为非负整数")
        for name in ("reserve_ratio", "max_redundant_rate"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"commander.{name} 必须为数值")
            if not 0 <= float(value) <= 1:
                raise ValueError(f"commander.{name} 必须在 0..1 之间")


class BenchCommander:
    """轮次边界的确定性调度器。"""

    def __init__(self, settings: CommanderSettings | None = None):
        self.settings = settings or CommanderSettings()
        self.settings.validate()

    def reserve(self, total_seconds: int) -> int:
        """全局储备池：按总预算比例与绝对下限取较大值。"""
        if total_seconds <= 0:
            return self.settings.min_remaining_seconds
        return max(self.settings.min_remaining_seconds,
                   int(total_seconds * self.settings.reserve_ratio))

    def can_extend(self, *, extensions: int, global_remaining: float, total_seconds: int) -> bool:
        """加时前必须同时满足次数、时长和全局储备约束。"""
        if extensions >= self.settings.max_extensions:
            return False
        if self.settings.extension_seconds <= 0:
            return False
        if not math.isfinite(global_remaining):
            return True
        return global_remaining - self.settings.extension_seconds >= self.reserve(total_seconds)

    def review(
        self,
        *,
        status: str,
        correct_flags: int,
        total_flags: int,
        progressed: bool,
        redundant_rate: float,
        attempt: int,
        extensions: int,
        task_remaining: float,
        global_remaining: float,
        total_seconds: int,
    ) -> CommanderDecision:
        """根据一轮结束时的结构化事实决定下一步。

        `task_remaining` 是单题预算的剩余墙钟；`global_remaining` 是整场剩余墙钟。
        返回 `retry` 时调用方继续用剩余单题预算；`extend` 时才从储备池加时。
        """
        del attempt  # 保留参数供未来模型 Commander 使用
        if total_flags > 0 and correct_flags >= total_flags:
            return CommanderDecision(RELEASE, reason="已拿全 flag")
        if status in {"blocked", "stopped"}:
            return CommanderDecision(RELEASE, reason=f"运行状态 {status}")

        partial = 0 < correct_flags < total_flags
        high_repeat = redundant_rate >= self.settings.max_redundant_rate
        if high_repeat and not partial:
            return CommanderDecision(RELEASE, reason=f"重复探索率 {redundant_rate:.0%}")
        if not partial and not progressed:
            return CommanderDecision(RELEASE, reason="本轮没有新增事实或部分 flag")

        # 先用完单题剩余预算，再考虑加时；避免首轮就动用储备池。
        if task_remaining > 1:
            reason = "已有部分正确 flag" if partial else "本轮有新增事实"
            return CommanderDecision(RETRY, reason=reason)
        if self.can_extend(extensions=extensions, global_remaining=global_remaining,
                           total_seconds=total_seconds):
            reason = "已有部分正确 flag" if partial else "有新增事实且仍有未验证方向"
            return CommanderDecision(EXTEND, seconds=self.settings.extension_seconds, reason=reason)
        return CommanderDecision(RELEASE, reason="单题预算用尽且没有可用加时储备")

    def review_mid_attempt(
        self,
        *,
        status: str,
        correct_flags: int,
        total_flags: int,
        progressed: bool,
        redundant_rate: float,
        extensions: int,
        task_remaining: float,
        global_remaining: float,
        total_seconds: int,
    ) -> CommanderDecision:
        """本轮执行中的检查点决策：继续、加时或停止。

        与轮次边界的 `review()` 不同，这里默认 `continue`：没有进展不代表
        模型已经卡死，只有重复探索率过高时才提前停止；单题软窗口快用完且有
        部分 Flag/新增事实时，从储备池加时。
        """
        if total_flags > 0 and correct_flags >= total_flags:
            return CommanderDecision(CONTINUE, reason="已拿全 flag")
        if status in {"blocked", "stopped"}:
            return CommanderDecision(RELEASE, reason=f"运行状态 {status}")
        partial = 0 < correct_flags < total_flags
        high_repeat = redundant_rate >= self.settings.max_redundant_rate
        if high_repeat and not partial and not progressed:
            return CommanderDecision(RELEASE, reason=f"重复探索率 {redundant_rate:.0%}")
        if (partial or progressed) and task_remaining <= 1 and self.can_extend(
            extensions=extensions, global_remaining=global_remaining, total_seconds=total_seconds,
        ):
            reason = "已有部分正确 flag" if partial else "检查点有新增事实"
            return CommanderDecision(EXTEND, seconds=self.settings.extension_seconds, reason=reason)
        return CommanderDecision(CONTINUE, reason="继续观察")


__all__ = [
    "ACTIONS",
    "BenchCommander",
    "CommanderDecision",
    "CommanderSettings",
    "CONTINUE",
    "EXTEND",
    "RELEASE",
    "RETRY",
]
