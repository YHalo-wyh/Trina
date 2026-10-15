"""授权策略：注册表 + 独立控制器（P3 要求：计划器提出，控制器批准）。

为什么需要它
------------
模型能做的只是**建议**：它不能自己扩大范围，也不能把自己的文字当作许可。
授权来自**独立注册表**与审批记录，判据在执行层每次动作前独立评估，而不是任务开始时读一次。

三道判据
--------
1. **目标注册**：只接受注册的 `target_id`；不在注册表里的目标一律拒绝（不给"任意 URL"开口子）。
2. **检查注册**：只接受注册的 `test_id`（绑定工具与参数模板）；未注册工具/动作拒绝。
3. **预算与停止合同**：检查数、逻辑预算、无新增证据即停；授权撤销后立即转为拒绝（减权而非绕路）。

注册表来源（人可读、可审查）
--------------------------
* `configs/ranges/*.yaml` 的 `targets:` 段 → 目标与地址白名单
* 同文件的 `tests:` 段 → 该靶场的注册检查项（test_id → 工具 + 参数）

用法：`mode: strict`（只认注册表，演示/评分推荐）或 `mode: guided`（注册表是提示，护栏仍兜底）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config import PROJECT_ROOT

#: 允许的策略模式
POLICY_MODES = ("free", "guided", "strict")


@dataclass(frozen=True)
class RegisteredTarget:
    target_id: str
    addresses: tuple[str, ...]
    notes: str = ""
    source: str = ""

    def matches(self, raw: str) -> bool:
        """端口感知的匹配：`http://127.0.0.1:9099` 不应把 `:8081` 也认成同一注册目标。"""
        query_host, query_port, _ = _split_address(raw)
        if not query_host:
            return False
        for address in self.addresses:
            addr_host, addr_port, _ = _split_address(address)
            if addr_host != query_host:
                continue
            if addr_port is None:
                # 注册项本身没写端口（整机级注册）：按主机匹配
                if query_port is None:
                    return True
                continue
            if query_port is not None and addr_port == query_port:
                return True
        return False


def _split_address(raw: str) -> tuple[str, int | None, str]:
    """归一化成 (host, port, path)；解析失败返回空 host。"""
    from .scope import parse_target

    text = str(raw or "").strip()
    if not text:
        return ("", None, "")
    try:
        info = parse_target(text)
    except ValueError:
        return ("", None, "")
    return (info.host.lower(), info.port, (info.path or "").lower())


@dataclass(frozen=True)
class RegisteredTest:
    test_id: str
    target_id: str
    tool: str
    level: str = "detect"
    params: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    expect_finding: str = ""
    source: str = ""


@dataclass
class Decision:
    """控制器的裁决（与 ScopeGuard 的 Decision 区分：这里是"授权"，那里是"边界"）。"""

    allowed: bool
    kind: str = "policy"  # target|test|budget|revoked|mode|ok
    reason: str = ""
    target_id: str = ""
    test_id: str = ""
    hint: str = ""

    def __bool__(self) -> bool:
        return self.allowed

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "kind": self.kind,
            "reason": self.reason,
            "target_id": self.target_id,
            "test_id": self.test_id,
            "hint": self.hint,
        }


class PolicyRegistry:
    """从靶场配置里装载「目标 + 注册检查项」。"""

    def __init__(self) -> None:
        self.targets: dict[str, RegisteredTarget] = {}
        self.tests: dict[str, RegisteredTest] = {}
        self.sources: list[str] = []

    # ------------------------------------------------------------------ 装载
    @classmethod
    def load(cls, config: Any = None, *, extra_dirs: list[str] | None = None) -> "PolicyRegistry":
        registry = cls()
        ranges_dir = PROJECT_ROOT / "configs" / "ranges"
        paths: list[Path] = []
        if ranges_dir.exists():
            paths.extend(sorted(ranges_dir.glob("*.yaml")))
        for raw in extra_dirs or []:
            directory = Path(raw)
            directory = directory if directory.is_absolute() else PROJECT_ROOT / directory
            if directory.exists():
                paths.extend(sorted(directory.glob("*.yaml")))
        for path in paths:
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception:  # noqa: BLE001 - 单个配置损坏不应让策略装载失败
                continue
            registry._absorb(data, path)
        return registry

    def _absorb(self, data: dict[str, Any], path: Path) -> None:
        name = str(data.get("name") or path.stem)
        source = f"{path.name}:{name}"
        for index, item in enumerate(data.get("targets") or []):
            if not isinstance(item, dict):
                continue
            target_id = str(item.get("id") or item.get("target_id") or f"{name}-target-{index + 1}")
            addresses = []
            for key in ("url", "base_url", "address", "target"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    addresses.append(value.strip())
            host = str(item.get("host") or "").strip()
            port = item.get("port")
            if host and port:
                # 有端口的注册项只登记 host:port，避免"同主机任意端口"被误认成已授权
                addresses.append(f"{host}:{port}")
            elif host:
                addresses.append(host)
            if not addresses:
                continue
            self.targets[target_id] = RegisteredTarget(
                target_id=target_id,
                addresses=tuple(dict.fromkeys(addresses)),
                notes=str(item.get("notes") or item.get("name") or ""),
                source=source,
            )
        for item in data.get("tests") or []:
            if not isinstance(item, dict):
                continue
            test_id = str(item.get("id") or item.get("test_id") or "").strip()
            tool = str(item.get("tool") or "").strip()
            if not test_id or not tool:
                continue
            self.tests[test_id] = RegisteredTest(
                test_id=test_id,
                target_id=str(item.get("target_id") or name),
                tool=tool,
                level=str(item.get("level") or item.get("action_level") or "detect"),
                params=dict(item.get("params") or {}),
                description=str(item.get("description") or ""),
                expect_finding=str(item.get("expect_finding") or ""),
                source=source,
            )
        if source not in self.sources:
            self.sources.append(source)

    # ------------------------------------------------------------------ 查询
    @property
    def empty(self) -> bool:
        return not self.targets and not self.tests

    def match_target(self, raw: str) -> RegisteredTarget | None:
        for target in self.targets.values():
            if target.matches(raw):
                return target
        return None

    def tests_for(self, target_id: str) -> list[RegisteredTest]:
        return [test for test in self.tests.values() if test.target_id == target_id]

    def test_tools(self, target_id: str) -> set[str]:
        return {test.tool for test in self.tests_for(target_id)}

    def prompt_block(self, target_id: str = "") -> str:
        """给大模型看的「已注册检查项」清单（模型只能从中提议）。"""
        tests = self.tests_for(target_id) if target_id else list(self.tests.values())
        if not tests:
            return "（当前靶场未注册检查项：控制器会按动作等级与范围护栏放行，但仍不接受未授权目标）"
        lines = ["注册检查项（建议优先在其中选择了：test_id → 工具）:"]
        for test in tests:
            suffix = f" — {test.description}" if test.description else ""
            lines.append(f"  - {test.test_id}: {test.tool}（等级 {test.level}）{suffix}")
        return "\n".join(lines)


@dataclass
class Budget:
    """预算与停止合同（P3：数字是试跑参数，不是安全标准）。"""

    max_checks: int = 40
    logic_seconds: int = 900
    stop_on_no_new_evidence: int = 4


class AuthorizationController:
    """独立控制器：在执行层逐动作批准，并维护预算、覆盖与撤销状态。"""

    def __init__(
        self,
        registry: PolicyRegistry | None = None,
        *,
        mode: str = "guided",
        budget: Budget | None = None,
        audit: Any | None = None,
        default_target_id: str = "",
    ):
        self.registry = registry or PolicyRegistry()
        self.mode = mode if mode in POLICY_MODES else "guided"
        self.budget = budget or Budget()
        self.audit = audit
        #: 本次运行的目标注册（动作里写裸主机 / 少写端口时继承它；仅限同一主机，且同样受撤权约束）
        self.default_target_id = default_target_id
        self.started_at = time.monotonic()
        self.checks_used = 0
        self.planned: list[str] = []
        self.executed: list[str] = []
        self.skipped: list[dict[str, str]] = []
        self.denied: list[dict[str, Any]] = []
        self.revoked: set[str] = set()
        self.unregistered: list[str] = []
        self.stale_steps = 0
        self.last_reason = ""

    # ------------------------------------------------------------------ 状态
    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def register_plan(self, test_ids: list[str]) -> None:
        for test_id in test_ids:
            if test_id not in self.planned:
                self.planned.append(test_id)

    def revoke(self, target_id: str, reason: str = "人工撤销授权") -> None:
        self.revoked.add(target_id)
        self._log("policy.revoke", decision="revoke", target_id=target_id, reason=reason)

    def grant(self, target_id: str) -> None:
        self.revoked.discard(target_id)

    def note_evidence(self, new_evidence: bool) -> bool:
        """返回是否应触发「无新增证据」停止。"""
        self.stale_steps = 0 if new_evidence else self.stale_steps + 1
        return self.stale_steps >= self.budget.stop_on_no_new_evidence

    def stop_reason(self) -> str:
        if self.elapsed >= self.budget.logic_seconds:
            return f"逻辑预算用尽（{self.budget.logic_seconds}s）"
        if self.checks_used >= self.budget.max_checks:
            return f"检查数达到上限（{self.budget.max_checks}）"
        if self.stale_steps >= self.budget.stop_on_no_new_evidence:
            return f"连续 {self.stale_steps} 步无新增证据"
        return ""

    # ------------------------------------------------------------------ 裁决
    def check(self, request: dict[str, Any]) -> Decision:
        """执行层逐动作裁决。`request`: tool/params/level/target/test_id(_id)。"""
        tool = str(request.get("tool") or "")
        level = str(request.get("level") or "detect")
        target_raw = str(request.get("target") or "")
        test_id = str(request.get("test_id") or "")
        target: RegisteredTarget | None = self.registry.match_target(target_raw) if target_raw else None
        if target is None and self.default_target_id:
            # 动作里写了裸主机/少写端口时，若与本次运行的目标同主机则继承其注册
            # （继承不等于放宽：仍然受撤权、预算与检查注册约束）
            fallback = self.registry.targets.get(self.default_target_id)
            action_host = _split_address(target_raw)[0]
            if fallback is not None and action_host and any(
                _split_address(address)[0] == action_host for address in fallback.addresses
            ):
                target = fallback
        target_id = target.target_id if target else ""

        # 1) 预算闸门
        if self.checks_used >= self.budget.max_checks:
            return self._deny("budget", f"检查数已达预算上限 {self.budget.max_checks}", target_id, test_id)
        if self.elapsed >= self.budget.logic_seconds:
            return self._deny("budget", f"逻辑预算已用尽（{self.budget.logic_seconds}s）", target_id, test_id)

        # 2) 授权撤销（减权，不绕路）
        if target_id and target_id in self.revoked:
            return self._deny(
                "revoked",
                f"目标 {target_id} 的授权已被撤销，禁止继续调度",
                target_id,
                test_id,
                hint="停止受影响任务并如实报告未覆盖项；新目标必须重新批准，不能改地址绕过",
            )

        # 3) 目标注册（strict / guided 模式）
        if self.mode in ("strict", "guided") and not self.registry.empty:
            if not target_raw:
                return self._deny("target", "动作未声明目标", target_id, test_id)
            if target is None:
                if self.mode == "strict":
                    return self._deny(
                        "target",
                        f"目标 {target_raw} 不在注册表中（strict 模式只接受注册的 target_id）",
                        target_id,
                        test_id,
                        hint="请只对注册靶场动作；新增目标需先写入 configs/ranges/*.yaml",
                    )
            elif not target_id:
                target_id = target.target_id

        # 4) 检查注册（strict 模式强制 test_id）
        if not test_id and self.mode == "guided" and not self.registry.empty:
            # guided 模式：按「工具 + 目标」反查注册检查项，让覆盖统计反映真实跑过哪些注册项；
            # 查不到的动作仍然放行（交给护栏），但会计入 unregistered 便于复核。
            for candidate in self.registry.tests_for(target_id or ""):
                if candidate.tool == tool:
                    test_id = candidate.test_id
                    level = candidate.level or level
                    break
            if not test_id:
                self.unregistered.append(tool)
        if self.mode == "strict" and not self.registry.empty:
            if not test_id:
                return self._deny(
                    "test",
                    f"未声明 test_id（strict 模式要求动作来自注册检查项）",
                    target_id,
                    test_id,
                    hint="在动作里给出注册的 test_id，例如 {\"test_id\": \"sqli_probe\", \"action\": \"sqli_test\"}",
                )
            test = self.registry.tests.get(test_id)
            if test is None:
                return self._deny(
                    "test", f"test_id {test_id} 未注册", target_id, test_id,
                    hint="只能使用注册的检查项；确需新增请先写入靶场配置",
                )
            if test.tool != tool:
                return self._deny(
                    "test",
                    f"test_id {test_id} 绑定的是 {test.tool}，与请求工具 {tool} 不一致",
                    target_id,
                    test_id,
                )
            if target_id and test.target_id not in ("", target_id):
                return self._deny(
                    "test", f"检查项 {test_id} 不属于目标 {target_id}", target_id, test_id
                )
            level = test.level or level

        self.checks_used += 1
        if test_id and test_id not in self.executed:
            self.executed.append(test_id)
        if self.checks_used == 1:
            self._log("policy.start", decision="allow", target_id=target_id, mode=self.mode)
        self._log(
            "policy.allow",
            decision="allow",
            tool=tool,
            level=level,
            target_id=target_id,
            test_id=test_id,
            checks_used=self.checks_used,
        )
        return Decision(True, kind="ok", reason="控制器批准", target_id=target_id, test_id=test_id)

    def note_skip(self, test_id: str, reason: str) -> None:
        if test_id:
            self.skipped.append({"test_id": test_id, "reason": reason})

    # ------------------------------------------------------------------ 覆盖
    def coverage(self) -> dict[str, Any]:
        """已测 / 未测 / 原因（停止时必须能回答这三个问题）。"""
        registered = [t for t in self.registry.tests.values()]
        executed = set(self.executed)
        untested = [
            {"test_id": t.test_id, "tool": t.tool, "reason": self._untested_reason(t)}
            for t in registered
            if t.test_id not in executed
        ]
        return {
            "mode": self.mode,
            "budget": {"max_checks": self.budget.max_checks, "logic_seconds": self.budget.logic_seconds},
            "checks_used": self.checks_used,
            "executed": sorted(executed),
            "unregistered": sorted(set(self.unregistered)),
            "untested": untested,
            "skipped": list(self.skipped),
            "denied": list(self.denied[-20:]),
            "revoked": sorted(self.revoked),
            "stop_reason": self.stop_reason(),
        }

    def _untested_reason(self, test: RegisteredTest) -> str:
        if test.target_id in self.revoked:
            return "授权被撤销"
        if self.checks_used >= self.budget.max_checks:
            return "预算用尽"
        if self.elapsed >= self.budget.logic_seconds:
            return "逻辑预算用尽"
        if self.stale_steps >= self.budget.stop_on_no_new_evidence:
            return "连续无新增证据，按停止合同提前结束"
        return "未调度（步数/优先级不足）"

    # ------------------------------------------------------------------ 内部
    def _deny(self, kind: str, reason: str, target_id: str = "", test_id: str = "", hint: str = "") -> Decision:
        decision = Decision(False, kind=kind, reason=reason, target_id=target_id, test_id=test_id, hint=hint)
        self.denied.append(decision.to_dict())
        self.last_reason = reason
        self._log("policy.deny", decision="deny", kind=kind, reason=reason, target_id=target_id, test_id=test_id)
        return decision

    def _log(self, event: str, **fields: Any) -> None:
        if self.audit is not None:
            try:
                self.audit.append(event, **fields)
            except Exception:  # noqa: BLE001 - 审计故障不影响裁决
                pass


__all__ = [
    "AuthorizationController",
    "Budget",
    "Decision",
    "POLICY_MODES",
    "PolicyRegistry",
    "RegisteredTarget",
    "RegisteredTest",
]
