"""交互式控制台：人工可介入的全自动渗透。

    python3 -m trina console        # 或直接 `python3 -m trina`

三条交互路径
------------
1. **一条命令开打**：`play`（自动起 lab 靶机 → 跑 → 出报告）
2. **运行中实时介入**：`hint <文本>` 注入提示、`pause`/`resume`、`stop`、`status`
3. **审批闸门**：`--approve exploit`（默认）时，每个命令执行/爆破级动作都要人工 y/n 批准；
   超时未答复按拒绝处理（fail-closed）

命令都只是薄封装：能委托给 CLI 的（tools/guard/ranges/report/audit）直接复用同一实现，避免两套行为。
"""

from __future__ import annotations

import shlex
import sys
import threading
import time
from typing import Any, Callable

from .config import TrinaConfig
from .session import APPROVAL_MODES, Session

HELP_TEXT = """这是**对话式**控制台：直接说人话即可（例如「你好」「帮我看看 127.0.0.1:9099 有什么漏洞」「验证一下 /sqli 的注入」），
Trina 会回答你；需要动手时它会自己调用工具执行（照样过授权控制器、护栏与审计）。以 / 开头的输入一律当命令。

常用命令（也可不加 /）:
  /play [--target T] [--range NAME] [--goal G] [--max-steps N] [--approve MODE]
        一条命令开打：自动准备靶场（默认起本地 lab）→ 全自动渗透 → 出报告
  /run <target> [--goal G] [--max-steps N] [--approve MODE]
        对指定目标跑一轮完整任务（含阶段机与结论/覆盖统计）
  /status              当前状态；  /approve <off|exploit|verify|all> 调整审批模式
  /hint <文本>         给运行中的 Agent 注入提示（下一步生效）
  /pause /resume       暂停 / 继续；  /stop  停止当前任务
  /y /n                批准 / 拒绝当前挂起的动作审批
  /revoke <target_id>  撤销某注册目标的授权（P3 撤权演练）；/grant <target_id> 恢复
  /tools               工具清单；      /tool <name> --params '<json>'  单工具执行
  /guard check <target>                授权范围校验
  /ranges [list|status <name>]         靶场适配器状态
  /report [--state <f>]                由状态重建报告；  /review [--state <f>]  评价 Agent 复核
  /audit verify <logs/*.jsonl>         校验审计哈希链
  /help /quit                          帮助 / 退出
运行中可用: y / n · hint <文本>（或直接说人话＝介入提示）· status · pause · resume · stop"""

#: 已知命令名（用于区分"命令"与"对 Agent 说话"）
COMMAND_NAMES = {
    "help", "?", "play", "run", "status", "approve", "hint", "pause", "resume", "stop",
    "revoke", "grant", "y", "yes", "n", "no", "deny", "tools", "tool", "guard", "ranges",
    "report", "review", "audit", "lab", "quit", "exit", "q",
}


class Console:
    """对话式控制台：自然语言对话 + 命令两种入口，共用同一套护栏/控制器/审计。"""

    def __init__(
        self,
        config: TrinaConfig | None = None,
        *,
        input_fn: Callable[[str], str] | None = None,
        output: Callable[[str], None] | None = None,
        backend: Any = None,
        registry: Any = None,
        approval_mode: str = "exploit",
        autostart_lab: bool = True,
    ):
        self.config = config or TrinaConfig.load()
        self._input = input_fn or input
        self.output = output or (lambda line: print(line, flush=True))
        self.backend = backend
        self.registry = registry
        self.autostart_lab = autostart_lab
        self._lock = threading.RLock()
        self._last_state: Any = None
        self._plan: Any = None
        self._controller: Any = None
        self.chat: Any = None
        self.session = Session(approval_mode=approval_mode, emit=self._emit)
        self.exit_requested = False

    # ------------------------------------------------------------------ 输出
    def _emit(self, message: str, prefix: str = "[trina]") -> None:
        with self._lock:
            self.output(f"{prefix} {message}" if message else "")

    def _agent_log(self, message: str) -> None:
        self._emit(message, prefix="  ·")

    def _on_step(self, step: Any) -> None:
        mark = "拒" if step.denied else ("OK" if step.ok else "×")
        self._emit(f"[{step.index:02d}] {mark} {step.action} — {str(step.summary)[:110]}", prefix="[step]")

    # ------------------------------------------------------------------ 主循环
    def run(self) -> int:
        self._emit("Trina 交互控制台（**对话式**）：直接说人话即可，例如「你好」「帮我看看 127.0.0.1:9099 有什么漏洞」；")
        self._emit("需要动手时我会自己调用工具（照过授权控制器与护栏）。命令仍可用，见 /help。")
        self._banner()
        while not self.exit_requested:
            try:
                raw = self._input(self._prompt())
            except (EOFError, KeyboardInterrupt):
                self._emit("\n再见。")
                break
            except StopIteration:  # 测试注入的输入耗尽
                break
            if not self.handle(raw):
                break
        self._cleanup()
        return 0

    def _prompt(self) -> str:
        return "trina(run)> " if self.session.running else "trina> "

    def _banner(self) -> None:
        policy = self.config.policy
        self._emit(
            f"审批模式={self.session.approval_mode} · 策略={policy.mode} · "
            f"动作上限={self.config.scope.exploit_level} · 目标范围={', '.join(self.config.scope.authorized_cidrs)}"
        )

    # ------------------------------------------------------------------ 命令
    def handle(self, raw: str) -> bool:
        line = (raw or "").strip()
        if not line:
            self._show_status()
            return True
        if line in ("quit", "exit", "q"):
            if self.session.running:
                self._emit("任务仍在运行：先 stop，或再次输入 quit 强制退出。")
                if not getattr(self, "_quit_warned", False):
                    self._quit_warned = True
                    return True
            return False
        self._quit_warned = False

        # 以 / 开头的一律当命令；否则先按命令名匹配，匹配不到就交给对话
        forced = line.startswith("/")
        if forced:
            line = line[1:].strip()
        if not line:
            self.output(HELP_TEXT)
            return True
        try:
            argv = shlex.split(line)
        except ValueError:
            self._emit(f"命令解析失败，按对话处理: {line[:60]}")
            if forced:
                return True
            self._start_chat_turn(line)
            return True
        command, args = argv[0], argv[1:]

        if self.session.running:
            return self._handle_running(command, args, line, forced=forced)

        handler = {
            "help": lambda: self.output(HELP_TEXT),
            "?": lambda: self.output(HELP_TEXT),
            "play": lambda: self._cmd_play(args),
            "run": lambda: self._cmd_run(args),
            "status": self._show_status,
            "approve": lambda: self._cmd_approve(args),
            "hint": lambda: self.session.steer(" ".join(args)),
            "pause": lambda: self._emit("当前没有运行中的任务"),
            "resume": lambda: self._emit("当前没有运行中的任务"),
            "stop": lambda: self._emit("当前没有运行中的任务"),
            "revoke": lambda: self._cmd_revoke(args, True),
            "grant": lambda: self._cmd_revoke(args, False),
            "y": lambda: self._emit("当前没有待审批的动作"),
            "n": lambda: self._emit("当前没有待审批的动作"),
            "tools": lambda: self._delegate(["tools", *args]),
            "tool": lambda: self._delegate(["tool", *args]),
            "guard": lambda: self._delegate(["guard", *args]),
            "ranges": lambda: self._delegate(["ranges", *args]),
            "report": lambda: self._delegate(["report", *args]),
            "review": lambda: self._delegate(["review", *args]),
            "audit": lambda: self._delegate(["audit", *args]),
            "lab": lambda: self._delegate(["lab", *args]),
        }.get(command)
        if handler is None:
            if forced:
                self._emit(f"未知命令 {command!r}；输入 /help 查看可用命令（不加 / 时按对话处理）。")
                return True
            self._start_chat_turn(line)
            return True
        try:
            handler()
        except Exception as exc:  # noqa: BLE001 - 控制台不能因为单条命令崩掉
            self._emit(f"命令执行失败: {type(exc).__name__}: {exc}")
        return True

    def _handle_running(self, command: str, args: list[str], line: str, *, forced: bool = False) -> bool:
        if command in ("y", "yes", "approve") and (command != "approve" or not args or forced):
            if not self.session.answer(True):
                self._emit("当前没有待审批的动作")
            return True
        if command in ("n", "no", "deny"):
            if not self.session.answer(False):
                self._emit("当前没有待审批的动作")
            return True
        if command == "hint" or line.startswith("!"):
            text = " ".join(args) if command == "hint" else line[1:].strip()
            self.session.steer(text)
            return True
        if command == "pause":
            self.session.toggle_pause()
            return True
        if command == "resume":
            if self.session.pause_event.is_set():
                self.session.toggle_pause()
            else:
                self._emit("任务未处于暂停状态")
            return True
        if command == "stop":
            self.session.request_stop()
            return True
        if command == "status":
            self._show_status()
            return True
        if command == "quit":
            self._emit("任务运行中：先 stop 再退出。")
            return True
        if command in ("help", "?"):
            self.output(HELP_TEXT)
            return True
        if not forced and command not in COMMAND_NAMES:
            # 运行中说人话 = 人工介入提示（最自然的交互）；已知命令名仍按命令处理
            self.session.steer(line)
            return True
        self._emit("任务运行中，可用命令：y / n · hint <文本> · status · pause · resume · stop（也可直接说人话＝介入提示）")
        return True

    # ------------------------------------------------------------------ 子命令
    # ------------------------------------------------------------------ 对话
    def _ensure_chat(self) -> Any:
        if self.chat is None:
            from .brain.chat import ChatAgent

            self.chat = ChatAgent(
                self.config,
                registry=self.registry,
                backend=self.backend,
                approval=self.session.request_approval,
                steer_queue=self.session.steers,
                stop_event=self.session.stop_event,
                pause_event=self.session.pause_event,
                logger=self._agent_log,
                on_tool=self._on_tool_result,
            )
            self._emit(f"对话会话已建立（审计：{self.chat.audit.path.name}）")
        return self.chat

    def _on_tool_result(self, result: Any) -> None:
        """把工具产出的关键事实回显（发现/凭据/flag/路径），让用户看得见"它在干什么"。"""
        facts = result.facts or {}
        for finding in facts.get("findings", []) or []:
            sev = finding.get("severity", "?")
            self._emit(f"[发现] [{sev}] {finding.get('vuln_class')} {finding.get('url') or finding.get('target')}", prefix="[Trina]")
        for flag in facts.get("flags", []) or []:
            self._emit(f"[flag] {str(flag.get('value'))[:60]}", prefix="[Trina]")
        for cred in facts.get("credentials", []) or []:
            self._emit(f"[凭据] {cred.get('service')}@{cred.get('target')} 用户 {cred.get('username')}", prefix="[Trina]")

    def _start_chat_turn(self, text: str) -> None:
        if self.session.running:
            self._emit("有任务在跑：先 stop，或直接把这句话作为介入提示（无需命令前缀）")
            return
        chat = self._ensure_chat()
        self.session.reset_for_run()
        self.session.state = chat.state
        self.session.started_at = time.monotonic()

        def _worker() -> None:
            try:
                reply = chat.ask(text)
                self._emit("—" * 28, prefix="[Trina]")
                for line in (reply or "").splitlines() or ["(空回复)"]:
                    self._emit(line, prefix="[Trina]")
                self._emit(self._chat_digest(chat), prefix="[Trina]")
                self._last_state = chat.state
            except Exception as exc:  # noqa: BLE001 - 对话异常不能杀掉控制台
                self._emit(f"对话出错: {type(exc).__name__}: {exc}", prefix="[错误]")

        self.session.thread = threading.Thread(target=_worker, name="trina-chat", daemon=True)
        self.session.thread.start()

    def _chat_digest(self, chat: Any) -> str:
        snap = chat.snapshot()
        executed = "、".join(snap.get("executed") or []) or "—"
        return (
            f"会话 {snap['run_id']}｜轮次 {snap['turns']}｜目标 {snap['target'] or '未指定'}｜"
            f"发现 {snap['findings']}｜flag {snap['flags']}｜已用检查 {snap['checks_used']}（{executed}）"
        )

    def _show_status(self) -> None:
        self._emit(f"状态: {self.session.status_line()}")
        if not self.session.running and self._last_state is not None:
            state = self._last_state
            self._emit(
                f"上一轮: 结论={state.verdict or '-'} 状态={state.status} 步数={state.counters.get('steps', 0)} "
                f"漏洞={len(state.findings)} flag={len(state.flags)} 被拒={state.counters.get('denied', 0)}"
            )
            coverage = state.coverage or {}
            if coverage:
                self._emit(
                    f"覆盖: 已测 {len(coverage.get('executed') or [])} 项 · 未测 "
                    f"{len(coverage.get('untested') or [])} 项 · 停止原因 {coverage.get('stop_reason') or '正常收敛'}"
                )

    def _cmd_approve(self, args: list[str]) -> None:
        mode = (args[0] if args else "").strip().lower()
        if mode not in APPROVAL_MODES:
            self._emit(f"用法: approve {'|'.join(APPROVAL_MODES)}（当前 {self.session.approval_mode}）")
            return
        self.session.approval_mode = mode
        self._emit(f"审批模式已切换为 {mode}")

    def _cmd_revoke(self, args: list[str], revoke: bool) -> None:
        if not args:
            self._emit("用法: revoke <target_id> / grant <target_id>")
            return
        target_id = args[0]
        self._ensure_controller()
        if revoke:
            self._controller.revoke(target_id, reason="控制台人工撤销")
            self._emit(f"已撤销 {target_id} 的授权：后续对该目标的检查会被控制器拒绝（减权，不绕路）")
        else:
            self._controller.grant(target_id)
            self._emit(f"已恢复 {target_id} 的授权")

    def _ensure_controller(self) -> Any:
        if self._controller is None:
            from .policy import AuthorizationController, Budget, PolicyRegistry

            policy = self.config.policy
            self._controller = AuthorizationController(
                PolicyRegistry.load(self.config, extra_dirs=list(policy.registry_dirs)),
                mode=policy.mode,
                budget=Budget(policy.max_checks, policy.logic_seconds, policy.stop_on_no_new_evidence),
            )
        return self._controller

    def _cmd_play(self, args: list[str]) -> None:
        parsed = _parse_play_args(args)
        self._start_task(target=parsed.get("target"), range_name=parsed.get("range"), goal=parsed.get("goal", ""),
                         max_steps=parsed.get("max_steps"), approval=parsed.get("approve"))

    def _cmd_run(self, args: list[str]) -> None:
        if not args or args[0].startswith("-"):
            self._emit("用法: run <target> [--goal G] [--max-steps N] [--approve MODE]")
            return
        target = args[0]
        parsed = _parse_play_args(args[1:])
        self._start_task(target=target, range_name=None, goal=parsed.get("goal", ""),
                         max_steps=parsed.get("max_steps"), approval=parsed.get("approve"))

    def _delegate(self, argv: list[str]) -> None:
        """把无状态命令交给 CLI 同一实现（保证行为一致）。"""
        from . import cli

        code = cli.main(argv)
        if code not in (0, None):
            self._emit(f"（命令退出码 {code}）")

    # ------------------------------------------------------------------ 任务
    def _start_task(
        self,
        *,
        target: str | None,
        range_name: str | None,
        goal: str,
        max_steps: int | None,
        approval: str | None,
    ) -> None:
        if self.session.running:
            self._emit("已有任务在运行：先 stop 或等它结束")
            return
        if approval:
            if approval.lower() in APPROVAL_MODES:
                self.session.approval_mode = approval.lower()
            else:
                self._emit(f"未知审批模式 {approval}，沿用 {self.session.approval_mode}")
        self.session.reset_for_run()
        self._ensure_controller()
        self._controller.checks_used = 0
        self._controller.started_at = time.monotonic()
        try:
            from .play import build_plan
        except ImportError as exc:  # pragma: no cover
            self._emit(f"无法加载 play 模块: {exc}")
            return
        try:
            plan = build_plan(
                self.config,
                target=target,
                range_name=range_name,
                goal=goal,
                autostart_lab=self.autostart_lab,
                log=lambda msg: self._emit(msg, prefix="[靶场]"),
            )
        except Exception as exc:  # noqa: BLE001 - 靶场准备失败要给出可读原因
            self._emit(f"靶场准备失败: {type(exc).__name__}: {exc}")
            return
        self._plan = plan
        self.session.state = None
        self.session.started_at = time.monotonic()
        teardown_after = bool(plan.started_fixtures)

        def _worker() -> None:
            from .play import run_plan

            self._emit(
                f"开始任务：{plan.target}"
                + (f" · 目标要求：{goal}" if goal else "")
                + f" · 审批模式：{self.session.approval_mode}",
                prefix="[任务]",
            )
            try:
                state = run_plan(
                    self.config,
                    plan,
                    backend=self.backend,
                    registry=self.registry,
                    approval=self.session.request_approval,
                    steer_queue=self.session.steers,
                    stop_event=self.session.stop_event,
                    pause_event=self.session.pause_event,
                    controller=self._controller,
                    max_steps=max_steps,
                    log=self._agent_log,
                    on_step=self._on_step,
                )
                self._last_state = state
                self.session.state = state
                self._report_summary(state)
            except Exception as exc:  # noqa: BLE001 - 任务异常必须回显而不是杀掉控制台
                self._emit(f"任务异常: {type(exc).__name__}: {exc}", prefix="[错误]")
            finally:
                if teardown_after:
                    plan.teardown()
                    self._emit("已回收本次自动拉起的靶机", prefix="[靶场]")

        self.session.thread = threading.Thread(target=_worker, name="trina-task", daemon=True)
        self.session.thread.start()
        self._emit("任务已在后台运行：y/n 审批 · hint <文本> 介入 · status 查看 · stop 停止")

    def _report_summary(self, state: Any) -> None:
        self._emit("—" * 30, prefix="[任务]")
        self._emit(
            f"结论 {state.verdict or '-'} · 运行状态 {state.status} · 步数 {state.counters.get('steps', 0)} · "
            f"漏洞 {len(state.findings)} · flag {len(state.flags)} · 被拒 {state.counters.get('denied', 0)}",
            prefix="[任务]",
        )
        coverage = state.coverage or {}
        if coverage:
            self._emit(
                f"覆盖：已测 {len(coverage.get('executed') or [])} 项 / 未测 "
                f"{len(coverage.get('untested') or [])} 项；停止原因：{coverage.get('stop_reason') or '正常收敛'}",
                prefix="[任务]",
            )
        for finding in state.findings[:8]:
            self._emit(f"  [{finding.severity}] {finding.vuln_class} {finding.url or finding.target} ({finding.status})",
                       prefix="[发现]")
        self._emit(f"产物：{state.artifacts.get('workspace') or '-'}", prefix="[任务]")

    def _cleanup(self) -> None:
        if self.session.running:
            self.session.request_stop()
            assert self.session.thread is not None
            self.session.thread.join(timeout=5)
        if self._plan is not None and self._plan.started_fixtures:
            self._plan.teardown()


def _parse_play_args(args: list[str]) -> dict[str, Any]:
    """极简参数解析（play/run 通用）：--target/--range/--goal/--max-steps/--approve。

    `--goal` 会把后续非 `--` 开头的词全部吃进去：控制台里用户习惯不加引号（`play --goal 拿到 flag`）。
    """
    parsed: dict[str, Any] = {}
    index = 0
    value_flags = {"--target": "target", "--range": "range", "--approve": "approve"}
    while index < len(args):
        token = args[index]
        if token == "--goal":
            words: list[str] = []
            index += 1
            while index < len(args) and not args[index].startswith("--"):
                words.append(args[index])
                index += 1
            parsed["goal"] = " ".join(words)
            continue
        if token in value_flags:
            parsed[value_flags[token]] = args[index + 1] if index + 1 < len(args) else ""
            index += 2
            continue
        if token == "--max-steps":
            try:
                parsed["max_steps"] = int(args[index + 1])
            except (IndexError, ValueError):
                pass
            index += 2
            continue
        index += 1
    return parsed


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：`python3 -m trina console`。"""
    import argparse

    parser = argparse.ArgumentParser(prog="trina console", description="Trina 交互控制台（人工可介入）")
    parser.add_argument("--approve", choices=APPROVAL_MODES, default="exploit",
                        help="审批模式：off 全自动 / exploit 只审高危 / verify / all（默认 exploit）")
    parser.add_argument("--config", help="配置文件路径")
    parser.add_argument("--set", action="append", metavar="section.field=value", help="临时覆盖配置，可重复")
    parser.add_argument("--no-lab-autostart", action="store_true", help="play 时不自动起本地 lab 靶机")
    args = parser.parse_args(argv)

    from .cli import _literal  # 复用 CLI 的字面量解析

    overrides = {}
    for item in args.set or []:
        key, _, value = item.partition("=")
        overrides[key] = _literal(value)
    config = TrinaConfig.load(args.config, overrides)
    console = Console(config, approval_mode=args.approve, autostart_lab=not args.no_lab_autostart)
    return console.run()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
