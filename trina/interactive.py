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

HELP_TEXT = """这是**对话式**控制台：直接说人话即可（例如「你好」「帮我看看 127.0.0.1:9099 有什么漏洞」「继续」），
Trina 会回答你；需要动手时它会自己调用工具执行（照样过授权控制器、护栏与审计）。以 / 开头的输入一律当命令。

会话与模型:
  /board                      工作台视图：任务状态、研究成果、假设台账、覆盖、成本一屏看
  /hypotheses                 假设台账（按优先级排序，含状态与置信度）
  /graph                      证据关系图（mermaid：结论→证据→检查项/假设）
  /model [list|<spec>]        看/换模型（如 /model deepseek/deepseek-chat，或 /model list 列出可选）
  /backend <omp|deepseek>     切换模型后端（omp 复用 omp 鉴权；deepseek 直连官方 API）
  /save                       把当前后端与模型持久化到 ~/.trina/env（下次启动仍是这个设置）
  /usage                      查看本次会话的模型调用次数/token/成本
  /history [n]                查看最近对话（含工具结果摘要）
  /new | /clear               开启新对话会话（清空上下文，审计另起一个文件）

任务与工具:
  /play [--target T] [--range NAME] [--goal G] [--max-steps N] [--approve MODE]
                              一条命令开打：自动准备靶场 → 全自动渗透 → 出报告
  /run <target> [--goal G] ... 对指定目标跑一轮完整任务（含阶段机与结论/覆盖）
  /status                     当前状态（空闲时显示上一轮结论与覆盖）
  /approve <off|exploit|verify|all>   调整审批闸门（默认 exploit）
  /tools   /tool <name> --params '<json>'   工具清单 / 单工具执行
  /guard check <target>       授权范围校验
  /ranges [list|status <name>]  靶场适配器状态
  /revoke <target_id> /grant <target_id>    撤销/恢复某注册目标的授权
  /report [--state <f>]       由状态重建报告；  /review [--state <f>]  评价 Agent 复核
  /audit verify <logs/*.jsonl>  校验审计哈希链
  /lab                        本地靶机（故意脆弱，仅回环）
  /help /quit                 帮助 / 退出

运行中可用: y / n 审批 · hint <文本>（或直接说人话＝介入提示）· status · pause · resume · stop"""

#: 已知命令名（用于区分"命令"与"对 Agent 说话"）
COMMAND_NAMES = {
    "help", "?", "play", "run", "status", "approve", "hint", "pause", "resume", "stop",
    "revoke", "grant", "y", "yes", "n", "no", "deny", "tools", "tool", "guard", "ranges",
    "report", "review", "audit", "lab", "quit", "exit", "q",
    "model", "backend", "history", "new", "usage", "save", "clear",
    "board", "hypotheses", "graph",
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
        #: 注入的后端（测试用）或按当前会话配置构建的缓存
        self._backend_obj = backend
        #: 测试可注入的后端工厂（形如 make_backend(llm_config) -> LLMBackend）
        self._backend_factory = None
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
                if self.session.running and self.session.thread is not None:
                    # 输入结束时不要立刻掐掉正在进行的对话/任务：等它收尾（最多 20s），避免丢掉最后一轮结果
                    self._emit("输入已结束：等待当前对话/任务收尾（最多 20 秒）…")
                    self.session.thread.join(timeout=20)
                self._emit("\n再见。")
                break
            except StopIteration:  # 测试注入的输入耗尽
                break
            if not self.handle(raw):
                break
        self._cleanup()
        return 0

    def _prompt(self) -> str:
        """提示符带当前模型（omp 风格的状态提示）。"""
        short = (self.config.llm.model or "?").split("/")[-1]
        return f"trina[{short}](run)> " if self.session.running else f"trina[{short}]> "

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

        if command in ("quit", "exit", "q"):
            self._emit("\n再见。")
            self.running = False
            self.exit_requested = True
            return True

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
            "model": lambda: self._cmd_model(args),
            "backend": lambda: self._cmd_backend(args),
            "history": lambda: self._cmd_history(args),
            "new": self._cmd_new,
            "clear": self._cmd_new,
            "usage": self._cmd_usage,
            "save": self._cmd_save,
            "board": self._cmd_board,
            "hypotheses": self._cmd_hypotheses,
            "graph": self._cmd_graph,
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

    # ------------------------------------------------------------------ 工作台视图
    def _current_state(self) -> Any:
        """当前可展示的状态：运行中的 → 本会话最近一轮 → 磁盘上最近一次运行。"""
        if self.session.state is not None:
            return self.session.state
        if self._last_state is not None:
            return self._last_state
        if self.chat is not None and self.chat.state is not None:
            return self.chat.state
        # 会话里还没有状态：读磁盘上最近一次运行，让 /board /graph 开箱可用
        try:
            from .board import latest_state_file, load_run_state

            config = getattr(self, "config", None)
            workspace = getattr(config, "workspace_dir", None) or "runs"
            path = latest_state_file(workspace)
            if path is not None:
                state = load_run_state(path)
                if state is not None:
                    self.output(f"（已载入最近一次运行 {getattr(state, 'run_id', '') or path.parent.name}；用 /play 开始新一轮）")
                    return state
        except Exception:  # noqa: BLE001 - 载入失败不影响后续命令
            return None
        return None

    def _cmd_board(self) -> None:
        from .board import render_board

        state = self._current_state()
        if state is None:
            self._emit("还没有可展示的内容：先 /play 跑一轮，或直接说句话让我开工")
            return
        hypotheses = self.chat.ledger.to_dict() if self.chat is not None else None
        self.output(render_board(state, hypotheses=hypotheses))

    def _cmd_hypotheses(self) -> None:
        if self.chat is not None:
            ledger = self.chat.ledger
            items = ledger.to_dict()
            summary = ledger.summary()
        else:
            state = self._current_state()
            items = (getattr(state, "hypotheses", None) or []) if state is not None else []
            summary = {
                "total": len(items),
                "open": len([h for h in items if h.get("status") in ("open", "verifying")]),
                "confirmed": len([h for h in items if h.get("status") == "confirmed"]),
                "refuted": len([h for h in items if h.get("status") == "refuted"]),
                "blocked": len([h for h in items if h.get("status") == "blocked"]),
            }
        if not items:
            self._emit("假设台账为空（模型可以在动作里带 hypothesis 字段提出假设，我会按优先级排序）")
            return
        self._emit(
            f"假设台账：共 {summary['total']} 条（待验证 {summary['open']}、已确认 {summary['confirmed']}、"
            f"已否定 {summary['refuted']}、被阻断 {summary['blocked']}）"
        )
        ordered = sorted(items, key=lambda h: h.get("priority", 0), reverse=True)
        for item in ordered[:15]:
            self.output(
                f"    {item.get('id')} [{item.get('status_cn', item.get('status'))}] "
                f"优先级={item.get('priority')} P={item.get('confidence')} — {str(item.get('statement', ''))[:70]}"
            )

    def _cmd_graph(self) -> None:
        """证据关系图（mermaid）：结论 → 证据 → 检查项/假设 可反向追溯。"""
        state = self._current_state()
        if state is None:
            self._emit("还没有可绘制的证据：先 /play 跑一轮，或说句话让我开工")
            return
        from .evidence_graph import build as build_graph

        registry = self._controller.registry if self._controller is not None else None
        try:
            graph = build_graph(state, registry=registry, hypotheses=getattr(state, "hypotheses", None))
        except Exception as exc:  # noqa: BLE001
            self._emit(f"绘图失败：{type(exc).__name__}: {exc}")
            return
        self._emit(graph.summary())
        self.output(graph.to_mermaid())

    # ------------------------------------------------------------------ 会话/模型
    def _backend(self) -> Any:
        """按当前（会话级）配置取后端；切换模型/后端后会被重建。"""
        if self._backend_obj is None:
            from .llm import make_backend

            self._backend_obj = self._backend_factory(self.config.llm) if self._backend_factory else make_backend(self.config.llm)
        return self._backend_obj

    def _reload_backend(self) -> None:
        self._backend_obj = None
        if self.chat is not None:  # 保留对话历史，只换大脑后端
            from .llm import make_backend

            self.chat.backend = make_backend(self.config.llm)

    def _cmd_model(self, args: list[str]) -> None:
        llm = self.config.llm
        if not args or args[0] in ("list", "ls", "?"):
            self._emit(f"当前后端 {llm.backend} · 模型 {llm.model}" + (f" · base_url {llm.base_url}" if llm.backend == "openai" else ""))
            from .models import list_models

            entries = list_models(limit=25)
            if not entries:
                self._emit("（没能列出可选模型：可手动 /model provider/model）")
                return
            self._emit("可选模型（/model <spec> 切换；加 /save 持久化）:")
            for entry in entries:
                marker = " ←当前" if entry.spec.endswith(llm.model) else ""
                self.output(f"    {entry.render()}{marker}")
            return
        spec = args[0]
        from .models import normalize_spec

        llm.model = normalize_spec(spec, llm.backend)
        self._reload_backend()
        self._emit(f"已切换模型（本会话）：{llm.model}（后端 {llm.backend}）—— 想长期生效用 /save")

    def _cmd_backend(self, args: list[str]) -> None:
        llm = self.config.llm
        if not args:
            self._emit(f"当前后端：{llm.backend}（可选：omp | deepseek）")
            return
        name = args[0].strip().lower()
        from .models import preset_for

        if name == "omp":
            llm.backend = "omp"
            if "/" not in llm.model:
                llm.model = f"deepseek/{llm.model}"
            self._reload_backend()
            self._emit(f"已切到 omp 后端（复用 omp 鉴权）：模型 {llm.model}")
            return
        preset = preset_for(name)
        if preset is None:
            self._emit(f"未知后端 {name}（可选 omp | deepseek）")
            return
        import os

        llm.backend = "openai"
        llm.base_url = str(preset["base_url"])
        llm.api_key_env = str(preset.get("api_key_env") or llm.api_key_env)
        models = list(preset.get("models") or [])
        if llm.model not in models:
            llm.model = str(models[0]) if models else llm.model
        self._reload_backend()
        has_key = bool(os.environ.get(llm.api_key_env))
        self._emit(
            f"已切到 {name} 官方 API：模型 {llm.model} · base_url {llm.base_url}"
            + ("" if has_key else f"（注意：环境变量 {llm.api_key_env} 未设置，调用会失败；见 scripts/env.example）")
        )

    def _cmd_history(self, args: list[str]) -> None:
        if self.chat is None or not self.chat.history:
            self._emit("还没有对话历史（先说句话，或 /play 跑一轮任务）")
            return
        limit = 8
        if args:
            try:
                limit = max(1, int(args[0]))
            except ValueError:
                pass
        items = self.chat.history[-limit:]
        self._emit(f"最近 {len(items)} 条对话（共 {len(self.chat.history)} 条）：")
        for item in items:
            role = {"user": "你", "assistant": "Trina"}.get(item.get("role", ""), item.get("role", "?"))
            text = str(item.get("content", "")).replace("\n", " ")
            if text.startswith("[工具结果]"):
                role = "工具"
                text = text[len("[工具结果]"):].strip()
            self.output(f"    {role}: {text[:220]}")

    def _cmd_new(self) -> None:
        if self.session.running:
            self._emit("有任务在跑：先 /stop 再开新会话")
            return
        self.chat = None
        self._emit("已开启新对话会话（历史与审计都重新开始；上一次的审计文件仍在 logs/ 下）")

    def _cmd_usage(self) -> None:
        backend = self._backend()
        ledger = getattr(backend, "ledger", None)
        if ledger is not None:
            data = ledger.to_dict()
            self._emit(
                f"模型调用 {data.get('calls', 0)} 次 · 输入 {data.get('input_tokens', 0)} tokens · "
                f"输出 {data.get('output_tokens', 0)} tokens · 缓存读 {data.get('cache_read_tokens', 0)} · "
                f"成本 ${data.get('cost_usd', 0)}"
            )
        else:
            self._emit("当前后端未统计用量")
        if self.chat is not None:
            self._emit(self._chat_digest(self.chat))
        if self._last_state is not None:
            self._emit(f"上一轮任务：结论={self._last_state.verdict or '-'} 状态={self._last_state.status}")

    def _cmd_save(self) -> None:
        """把当前后端/模型持久化到 ~/.trina/env（仓库外，0600）——下次启动仍是这个设置。"""
        import os
        from pathlib import Path as _Path

        path = _Path(os.environ.get("TRINA_ENV_FILE") or (_Path.home() / ".trina" / "env"))
        keep: list[str] = []
        if path.exists():
            keep = [
                line
                for line in path.read_text(encoding="utf-8").splitlines()
                if not line.startswith(("export TRINA_API=", "export TRINA_MODEL="))
            ]
        keep.append(f"export TRINA_API={self.config.llm.backend}")
        keep.append(f"export TRINA_MODEL={self.config.llm.model}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(keep).strip() + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self._emit(f"已保存到 {path}：后端 {self.config.llm.backend} · 模型 {self.config.llm.model}（下次启动生效）")

    # ------------------------------------------------------------------ 子命令
    # ------------------------------------------------------------------ 对话
    def _ensure_chat(self) -> Any:
        if self.chat is None:
            from .brain.chat import ChatAgent

            self.chat = ChatAgent(
                self.config,
                registry=self.registry,
                backend=self._backend(),
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
        hypotheses = snap.get("hypotheses") or {}
        return (
            f"会话 {snap['run_id']}｜轮次 {snap['turns']}｜目标 {snap['target'] or '未指定'}｜"
            f"发现 {snap['findings']}｜flag {snap['flags']}｜已用检查 {snap['checks_used']}（{executed}）｜"
            f"假设 待验证 {hypotheses.get('open', 0)}/已确认 {hypotheses.get('confirmed', 0)}/已否定 {hypotheses.get('refuted', 0)}"
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
                    backend=self._backend(),
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
