"""基于工具事实跟踪阶段与停止条件。

阶段推进**由事实驱动**，不由模型自称：
    recon  有开放端口 → enum
    enum   有服务/路径/技术栈信息 → vuln
    vuln   有漏洞（或步数耗尽） → exploit
    exploit 有已验证/已利用的漏洞或 flag → post
    post   拿到 flag 或连续无新增 → report
每个阶段还有步数预算，避免在某一阶段原地打转。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..state import RunState
PHASES = ("recon", "enum", "vuln", "exploit", "post", "report")


@dataclass
class Planner:
    """跟踪阶段与阶段内进度。"""

    max_steps_per_phase: int = 6
    phase: str = "recon"
    phase_step: int = 0
    stagnant_steps: int = 0
    force_next: bool = False
    history: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ 判据
    @staticmethod
    def has_ports(state: RunState) -> bool:
        return any(host.ports for host in state.hosts.values())

    @staticmethod
    def has_attack_surface(state: RunState) -> bool:
        """枚举完成的判据：拿到了技术栈或路径这类「可攻击面」信息。

        注意不能只靠「有主机」就认为枚举完成——那会让 Agent 在没看清服务前就跳到漏洞验证。
        """
        return any(host.technologies or host.web_paths for host in state.hosts.values())

    @staticmethod
    def has_findings(state: RunState) -> bool:
        return bool(state.findings)

    @staticmethod
    def has_verified(state: RunState) -> bool:
        return any(f.status in ("verified", "exploited") for f in state.findings)

    def objective_met(self, state: RunState, phase: str | None = None) -> bool:
        phase = phase or self.phase
        if phase == "recon":
            return self.has_ports(state)
        if phase == "enum":
            return self.has_attack_surface(state)
        if phase == "vuln":
            # 优先要求「已验证」，但允许发现若干线索后前移，避免在无通用 PoC 的靶场上卡死
            return self.has_verified(state) or (self.has_findings(state) and self.phase_step >= 3)
        if phase == "exploit":
            return self.has_verified(state) or bool(state.flags)
        if phase == "post":
            return bool(state.flags) or bool(state.credentials)
        return True

    # ------------------------------------------------------------------ 推进
    def tick(self, state: RunState, *, new_facts: bool) -> tuple[str, str | None]:
        """推进一次。返回 (当前阶段, 阶段切换说明或 None)。"""
        self.phase_step += 1
        self.stagnant_steps = 0 if new_facts else self.stagnant_steps + 1
        note: str | None = None

        budget_hit = self.phase_step >= self.max_steps_per_phase
        if self.objective_met(state):
            note = f"{self.phase} 阶段目标达成"
        elif budget_hit:
            note = f"{self.phase} 阶段步数预算用尽"
        if self.force_next:
            note = note or f"{self.phase} 阶段被显式跳过（停滞）"
        if note:
            next_phase = self._next_phase(state)
            if next_phase and next_phase != self.phase:
                self.history.append(self.phase)
                self.phase = next_phase
                self.phase_step = 0
                self.stagnant_steps = 0
                self.force_next = False
                return self.phase, f"{note} → 进入 {next_phase}"
        self.force_next = False
        return self.phase, None

    def _next_phase(self, state: RunState) -> str | None:
        index = PHASES.index(self.phase)
        if index >= len(PHASES) - 1:
            return None
        # 未拿到漏洞就不该进入 exploit/post：允许跳级但保持基本因果
        if self.phase == "vuln" and not self.has_findings(state):
            return "exploit"  # 仍进入利用阶段：尝试用现有工具直接验证可利用性
        return PHASES[index + 1]

    def force_advance(self) -> None:
        self.force_next = True

    def should_finish(self, state: RunState) -> bool:
        """是否已无更多可做。"""
        if state.flags and self.phase in ("post", "report"):
            return True
        if self.phase == "report":
            return True
        if self.stagnant_steps >= max(3, self.max_steps_per_phase):
            return True
        return False
