"""「直接开打」：一条命令把靶场准备好并跑完，产出报告。

    python3 -m trina play                      # 自动起本地 lab 靶机并开打
    python3 -m trina play --range dvwa         # 用靶场适配器（prepare → primary_target）
    python3 -m trina play --target 127.0.0.1:9099 --goal "验证 RCE 并读 flag"

设计：`build_plan()` 只负责「把靶场准备好并给出目标」，`run_plan()` 只负责「按人机协同参数跑一轮」。
控制台与 CLI 共用这两个函数，避免两套路径。靶机的启停遵循「谁起来谁负责回收」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .config import TrinaConfig

DEFAULT_LAB_HTTP_PORT = 9099
DEFAULT_LAB_SSH_PORT = 2222


@dataclass
class PlayPlan:
    """一次「开打」的准备工作结果。"""

    target: str
    goal: str = ""
    range_name: str = ""
    label: str = ""
    adapter: Any = None
    fixtures: list[tuple[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def started_fixtures(self) -> bool:
        return bool(self.fixtures)

    def teardown(self) -> None:
        for kind, server in self.fixtures:
            try:
                if kind == "http":
                    server.shutdown()
                    server.server_close()
                else:
                    server.stop()
            except Exception:  # noqa: BLE001 - 回收失败不影响结论
                pass
        self.fixtures.clear()
        if self.adapter is not None:
            try:
                self.adapter.teardown()
            except Exception:  # noqa: BLE001
                pass


def _port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    import socket

    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def build_plan(
    config: TrinaConfig,
    *,
    target: str | None = None,
    range_name: str | None = None,
    goal: str = "",
    autostart_lab: bool = True,
    log: Callable[[str], None] = print,
) -> PlayPlan:
    """准备靶场：优先用适配器，其次用给定目标，都没有就起本地 lab。"""
    if range_name:
        from .adapters import load_adapter

        adapter = load_adapter(config, range_name)
        info = adapter.prepare()
        resolved = target or adapter.primary_target()
        plan = PlayPlan(
            target=resolved or "",
            goal=goal,
            range_name=adapter.name,
            label=f"靶场 {adapter.name}（{adapter.kind}）",
            adapter=adapter,
        )
        notes = getattr(info, "notes", "") or adapter.status().get("detail", "")
        if notes:
            plan.notes.append(str(notes))
        if not plan.target:
            raise RuntimeError(f"靶场 {adapter.name} 未提供可用目标，请用 --target 显式指定")
        log(f"[靶场] {plan.label} → {plan.target}")
        if notes:
            log(f"[靶场] {notes}")
        return plan

    if target:
        return PlayPlan(target=target, goal=goal, label="指定目标")

    if not autostart_lab:
        raise RuntimeError("未指定 --target/--range，且已关闭自动起靶机")

    plan = PlayPlan(target=f"http://127.0.0.1:{DEFAULT_LAB_HTTP_PORT}", goal=goal, label="本地 lab 靶机（内置模拟器）")
    from .lab.http_target import create_server

    if _port_open("127.0.0.1", DEFAULT_LAB_HTTP_PORT):
        plan.notes.append(f"复用已在运行的 lab HTTP 靶机（127.0.0.1:{DEFAULT_LAB_HTTP_PORT}）")
    else:
        server, _thread = create_server(port=DEFAULT_LAB_HTTP_PORT)
        plan.fixtures.append(("http", server))
        plan.notes.append(f"已启动 lab HTTP 靶机 http://127.0.0.1:{DEFAULT_LAB_HTTP_PORT}")
    try:
        from .lab.ssh_target import create_ssh_server

        if _port_open("127.0.0.1", DEFAULT_LAB_SSH_PORT):
            plan.notes.append(f"复用已在运行的 lab SSH 靶机（127.0.0.1:{DEFAULT_LAB_SSH_PORT}）")
        else:
            ssh_server, _sthread = create_ssh_server(port=DEFAULT_LAB_SSH_PORT)
            plan.fixtures.append(("ssh", ssh_server))
            plan.notes.append(f"已启动 lab SSH 靶机 ssh://127.0.0.1:{DEFAULT_LAB_SSH_PORT}")
    except Exception as exc:  # noqa: BLE001 - SSH 靶机可选
        plan.notes.append(f"SSH 靶机未启动（{type(exc).__name__}: {exc}）")
    for note in plan.notes:
        log(f"[靶机] {note}")
    return plan


def run_plan(
    config: TrinaConfig,
    plan: PlayPlan,
    *,
    backend: Any = None,
    registry: Any = None,
    approval: Callable[[dict[str, Any]], bool] | None = None,
    steer_queue: Any = None,
    stop_event: Any = None,
    pause_event: Any = None,
    controller: Any = None,
    max_steps: int | None = None,
    max_seconds: int | None = None,
    extra_rules: str = "",
    write_report: bool = True,
    log: Callable[[str], None] = print,
    on_step: Callable[[Any], None] | None = None,
) -> Any:
    """按计划跑一轮，返回 `RunState`（可选写报告）。"""
    from .brain import PentestAgent
    from .llm import make_backend
    from .tools import LOAD_ERRORS, build_registry

    registry = registry or build_registry()
    for module, error in LOAD_ERRORS.items():
        log(f"[警告] 工具模块 {module} 未加载: {error}")
    backend = backend or make_backend(config.llm)
    agent = PentestAgent(
        config,
        registry=registry,
        backend=backend,
        logger=log,
        on_step=on_step,
        approval=approval,
        steer_queue=steer_queue,
        stop_event=stop_event,
        pause_event=pause_event,
        controller=controller,
    )
    state = agent.run(
        plan.target,
        goal=plan.goal,
        max_steps=max_steps,
        max_seconds=max_seconds,
        extra_rules=extra_rules,
    )
    if write_report:
        try:
            from .report import build_report

            report = build_report(state, config, meta={"range": plan.range_name, "goal": plan.goal, "play": plan.label})
            for path in report.save():
                log(f"[报告] {path}")
        except ImportError as exc:  # pragma: no cover - 报告模块缺失时降级
            log(f"[警告] 报告模块不可用（{exc}），跳过报告生成")
    return state


__all__ = ["DEFAULT_LAB_HTTP_PORT", "DEFAULT_LAB_SSH_PORT", "PlayPlan", "build_plan", "run_plan"]
