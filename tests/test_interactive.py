"""人机协同测试：审批闸门、实时介入、暂停/停止、交互控制台、一条命令开打。

这些是「人工可介入」这条产品线的对外可感知行为：
没有它们，交互式 CLI 就只是把自动流程包一层壳子。
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from trina.brain.agent import PentestAgent
from trina.config import LLMConfig, TrinaConfig
from trina.llm.replay_backend import ReplayBackend
from trina.play import build_plan
from trina.session import Session, level_needs_approval
from trina.tools import build_registry
from trina.tools.registry import ParamSpec, Tool, ToolRegistry, ToolResult, ToolSpec


class _EchoTool(Tool):
    spec = ToolSpec(
        name="echo_probe",
        summary="无害探针",
        category="recon",
        action_level="verify",
        params={"target": ParamSpec("str", "目标", required=True)},
    )

    def run(self, ctx, target):
        return ToolResult(ok=True, summary=f"probe {target}", facts={"ports": [{"host": target, "port": 80}]})


class _BinaryEchoTool(Tool):
    spec = ToolSpec(name="binary_echo", summary="二进制会话手工入口夹具", category="binary",
                    action_level="detect", params={"value": ParamSpec("int", "测试值", required=True)})

    def run(self, ctx, value):
        return ToolResult(ok=True, summary=f"binary {value}", data={"value": value})


class TestApprovalModes(unittest.TestCase):
    def test_level_thresholds(self):
        self.assertFalse(level_needs_approval("off", "exploit"))
        self.assertTrue(level_needs_approval("exploit", "exploit"))
        self.assertFalse(level_needs_approval("exploit", "verify"))
        self.assertTrue(level_needs_approval("verify", "verify"))
        self.assertFalse(level_needs_approval("verify", "detect"))
        self.assertTrue(level_needs_approval("all", "detect"))
        self.assertTrue(level_needs_approval("exploit", "unknown-level"))

    def test_answer_releases_waiter(self):
        session = Session(approval_mode="all", approval_timeout=5)
        result: list[bool] = []

        def _waiter():
            result.append(session.request_approval({"tool": "x", "level": "detect", "params": {}}))

        thread = threading.Thread(target=_waiter)
        thread.start()
        for _ in range(50):  # 等 waiter 挂起
            if session.pending:
                break
            time.sleep(0.02)
        self.assertIsNotNone(session.pending)
        self.assertTrue(session.answer(False))
        thread.join(timeout=5)
        self.assertEqual(result, [False])
        self.assertEqual(session.decisions[-1]["tool"], "x")

    def test_timeout_is_fail_closed(self):
        session = Session(approval_mode="all", approval_timeout=0.2)
        allowed = session.request_approval({"tool": "danger", "level": "exploit", "params": {}})
        self.assertFalse(allowed, "无人值守时必须按拒绝处理")
        self.assertTrue(session.decisions[-1]["timeout"])

    def test_off_mode_never_waits(self):
        session = Session(approval_mode="off")
        self.assertTrue(session.request_approval({"tool": "x", "level": "exploit", "params": {}}))
        self.assertEqual(session.decisions, [])


class TestAgentHumanHooks(unittest.TestCase):
    """Agent 侧：审批拒绝 / 人工提示 / 停止信号必须真的生效。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = TrinaConfig.load().with_overrides(
            run__workspace=str(Path(self.tmp.name) / "runs"),
            audit__dir=str(Path(self.tmp.name) / "logs"),
            policy__mode="free",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _agent(self, script: list[dict], **kwargs) -> PentestAgent:
        script_path = Path(self.tmp.name) / "replay.json"
        script_path.write_text(json.dumps({"steps": script}, ensure_ascii=False), encoding="utf-8")
        backend = ReplayBackend(LLMConfig(backend="replay", replay_script=str(script_path)))
        return PentestAgent(self.cfg, registry=ToolRegistry([_EchoTool()]), backend=backend, **kwargs)

    def test_human_rejection_skips_action(self):
        seen: list[dict] = []
        agent = self._agent(
            [
                {"response": '{"thought": "探一下", "action": "echo_probe", "params": {"target": "127.0.0.1"}}'},
                {"response": '{"thought": "结束", "final": {"summary": "被拒后收尾"}}'},
            ],
            approval=lambda request: seen.append(request) or False,
        )
        state = agent.run("127.0.0.1")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["tool"], "echo_probe")
        self.assertIn("human", [item["kind"] for item in state.timeline])
        actions = [step.get("action") for step in state.steps]
        self.assertIn("echo_probe", actions)
        self.assertTrue(any(step.get("denied") for step in state.steps), "被人工拒绝的动作要记为 denied")
        # 拒绝后没有产生事实
        self.assertFalse(state.hosts.get("127.0.0.1") and state.hosts["127.0.0.1"].ports)

    def test_approval_allows_action(self):
        agent = self._agent(
            [
                {"response": '{"thought": "探一下", "action": "echo_probe", "params": {"target": "127.0.0.1"}}'},
                {"response": '{"thought": "结束", "final": {"summary": "已批准"}}'},
            ],
            approval=lambda request: True,
        )
        state = agent.run("127.0.0.1")
        self.assertIn(80, state.hosts["127.0.0.1"].ports)

    def test_steering_queue_reaches_agent(self):
        import queue

        steers: "queue.Queue[str]" = queue.Queue()
        steers.put("先看 /upload 的真实路径")
        agent = self._agent(
            [
                {"response": '{"thought": "探一下", "action": "echo_probe", "params": {"target": "127.0.0.1"}}'},
                {"response": '{"thought": "结束", "final": {"summary": "ok"}}'},
            ],
            steer_queue=steers,
        )
        state = agent.run("127.0.0.1")
        human_notes = [item["text"] for item in state.timeline if item["kind"] == "human"]
        self.assertTrue(any("先看 /upload" in note for note in human_notes), human_notes)

    def test_stop_event_halts_loop(self):
        stop = threading.Event()
        stop.set()
        agent = self._agent(
            [{"response": '{"thought": "不该执行", "action": "echo_probe", "params": {"target": "127.0.0.1"}}'}],
            stop_event=stop,
        )
        state = agent.run("127.0.0.1", max_steps=3)
        self.assertEqual(state.status, "stopped")
        self.assertEqual(state.counters.get("llm_calls", 0), 0, "停止信号应在调用模型前生效")


class TestPlayPlan(unittest.TestCase):
    def test_plan_with_explicit_target(self):
        plan = build_plan(TrinaConfig.load(), target="127.0.0.1:9099", goal="g", autostart_lab=False, log=lambda _m: None)
        self.assertEqual(plan.target, "127.0.0.1:9099")
        self.assertFalse(plan.started_fixtures)

    def test_plan_autostarts_lab_and_reuses_existing(self):
        from trina.play import _port_open

        already_running = _port_open("127.0.0.1", 9099)
        first = build_plan(TrinaConfig.load(), autostart_lab=True, log=lambda _m: None)
        try:
            if already_running:
                self.assertFalse(first.started_fixtures, "已有靶机在跑时应复用而不是重复拉起")
            else:
                self.assertTrue(first.started_fixtures, "空环境下应自动拉起 lab")
            self.assertIn("127.0.0.1:9099", first.target)
            second = build_plan(TrinaConfig.load(), autostart_lab=True, log=lambda _m: None)
            self.assertFalse(second.started_fixtures, "第二次调用一定走复用分支")
        finally:
            first.teardown()

    def test_plan_requires_something_to_attack(self):
        with self.assertRaises(RuntimeError):
            build_plan(TrinaConfig.load(), autostart_lab=False, log=lambda _m: None)


class TestChatAgent(unittest.TestCase):
    """对话式大脑：说人话→回答；需要动手→自己调工具（仍过护栏/控制器/审批）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = TrinaConfig.load().with_overrides(
            run__workspace=str(Path(self.tmp.name) / "runs"),
            audit__dir=str(Path(self.tmp.name) / "logs"),
            policy__mode="guided",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _chat(self, script: list[dict], **kwargs):
        from trina.brain.chat import ChatAgent

        path = Path(self.tmp.name) / "chat-replay.json"
        path.write_text(json.dumps({"steps": script}, ensure_ascii=False), encoding="utf-8")
        backend = ReplayBackend(LLMConfig(backend="replay", replay_script=str(path)))
        return ChatAgent(self.cfg, registry=ToolRegistry([_EchoTool()]), backend=backend, **kwargs)

    def test_plain_reply_does_not_call_tools(self):
        chat = self._chat([{"response": "你好，我是 Trina。告诉我目标就可以开始。"}])
        reply = chat.ask("你好")
        self.assertIn("Trina", reply)
        self.assertEqual(chat.state.counters.get("tool_calls", 0), 0)

    def test_chat_sends_security_system_and_contract_as_user_data(self):
        chat = self._chat([{"response": "收到。"}])
        original = chat.backend.complete_messages
        calls: list[list[dict[str, str]]] = []

        def capture(messages, **kwargs):
            calls.append([dict(item) for item in messages])
            return original(messages, **kwargs)

        chat.backend.complete_messages = capture  # type: ignore[method-assign]
        self.assertEqual(chat.ask("你好"), "收到。")
        self.assertIn("CTF / 攻防比赛", calls[0][0]["content"])
        self.assertIn("# Delivery", calls[0][0]["content"])
        self.assertIn("原生 tool calling", calls[0][1]["content"])
        self.assertIn("# 可用工具", calls[0][1]["content"])
        self.assertEqual(calls[0][-2]["content"], "你好")
        self.assertNotIn("[user]", "\n".join(item["content"] for item in calls[0]))

    def test_tool_action_then_reply(self):
        chat = self._chat(
            [
                {"response": '{"thought": "先探一下", "action": "echo_probe", "params": {"target": "127.0.0.1"}}'},
                {"response": "探完了：127.0.0.1 的 80 端口是开的。"},
            ]
        )
        reply = chat.ask("帮我看看 127.0.0.1")
        self.assertIn("80", reply)
        self.assertIn(80, chat.state.hosts["127.0.0.1"].ports)
        self.assertGreaterEqual(chat.state.counters.get("tool_calls", 0), 1)

    def test_dsml_tool_action_is_executed_instead_of_rendered(self):
        dsml = (
            '<|DSML|calls><|DSML|invoke name="echo_probe">'
            '<|DSML|parameter name="params" string="false">'
            '{"target":"127.0.0.1"}'
            '</|DSML|parameter></|DSML|invoke></|DSML|calls>'
        )
        chat = self._chat([
            {"response": dsml},
            {"response": "已完成检查。"},
        ])
        reply = chat.ask("检查本机")
        self.assertEqual(reply, "已完成检查。")
        self.assertEqual(chat.state.counters.get("tool_calls"), 1)
        self.assertNotIn("DSML", reply)

    def test_native_tool_calls_use_standard_api_and_keep_tool_pairs(self):
        chat = self._chat([
            {"native_only": True, "tool_calls": [
                {"id": "call_probe", "name": "echo_probe", "arguments": {"target": "127.0.0.1"}},
            ]},
            {"native_only": True, "tool_calls": [
                {"id": "call_finish", "name": "finish", "arguments": {"status": "success", "summary": "编码链路完成"}},
            ]},
        ])
        reply = chat.ask("使用标准工具调用检查本机")
        self.assertEqual(reply, "编码链路完成")
        self.assertTrue(chat.backend._tools_supplied)
        assistant_calls = [item for item in chat._messages if item.get("role") == "assistant" and item.get("tool_calls")]
        tool_results = [item for item in chat._messages if item.get("role") == "tool"]
        self.assertEqual({item["tool_calls"][0]["id"] for item in assistant_calls}, {"call_probe", "call_finish"})
        self.assertEqual({item["tool_call_id"] for item in tool_results}, {"call_probe", "call_finish"})
        self.assertIn(80, chat.state.hosts["127.0.0.1"].ports)

    def test_prose_plus_action_is_executed(self):
        """模型常写「一句解释 + JSON 动作」：必须识别动作并执行，而不是当成纯文本回答。"""
        chat = self._chat(
            [
                {
                    "response": '我先做只读侦察。\n'
                    '{"thought": "扫端口", "action": "echo_probe", "params": {"target": "127.0.0.1"}}\n'
                    '{"thought": "顺便指纹", "action": "echo_probe", "params": {"target": "127.0.0.1"}}'
                },
                {"response": "侦察完成：80 端口开放。"},
            ]
        )
        reply = chat.ask("看看 127.0.0.1")
        self.assertIn("80", reply)
        self.assertEqual(chat.state.counters.get("tool_calls", 0), 1, "只执行第一个动作")
        self.assertIn(80, chat.state.hosts["127.0.0.1"].ports)

    def test_controller_denial_is_surfaced_to_model_and_user(self):
        self.cfg.policy.mode = "strict"
        chat = self._chat(
            [
                {"response": '{"thought": "直接扫", "action": "echo_probe", "params": {"target": "127.0.0.1"}}'},
                {"response": "抱歉，控制器没批准这个动作，我换个注册检查项试试。"},
            ]
        )
        reply = chat.ask("扫一下 127.0.0.1")
        self.assertIn("控制器", reply)
        self.assertTrue(chat.state.boundary_events, "越界请求要留痕")
        self.assertEqual(chat.controller.coverage()["executed"], [], "未注册动作不应计入已测")

    def test_human_rejection_blocks_tool(self):
        chat = self._chat(
            [
                {"response": '{"thought": "探一下", "action": "echo_probe", "params": {"target": "127.0.0.1"}}'},
                {"response": "好的，我先不动手。"},
            ],
            approval=lambda request: False,
        )
        reply = chat.ask("探一下 127.0.0.1")
        self.assertIn("不动手", reply)
        self.assertFalse(chat.state.hosts.get("127.0.0.1") and chat.state.hosts["127.0.0.1"].ports)


class TestLauncherArgCompatibility(unittest.TestCase):
    """启动器会按后端注入 `--set llm.*`：每个被注入的子命令都必须能解析这些参数。

    这类 bug 只在"启动器 + 某个子命令"组合下出现（`trina tools` 曾因此直接报错），
    所以用一个不依赖网络的解析测试把它钉住。
    """

    def test_injected_overrides_parse_for_launcher_subcommands(self):
        """启动器在子命令与其余参数之间注入 --set（`trina <sub> --set … <rest>`），逐命令验证能解析。"""
        from trina.cli import build_parser

        injected = [
            "--set", "llm.backend=openai",
            "--set", "llm.model=deepseek-v4-flash",
            "--set", "llm.base_url=https://api.deepseek.com/v1",
            "--set", "llm.api_key_env=DEEPSEEK_API_KEY",
        ]
        cases = {
            "console": ["console", *injected],
            "play": ["play", *injected, "--target", "127.0.0.1"],
            "doctor": ["doctor", *injected],
            "run": ["run", *injected, "--target", "127.0.0.1"],
            "eval": ["eval", *injected, "run", "--suite", "web"],
            "review": ["review", *injected, "--state", "runs/x/state.json"],
            "report": ["report", *injected, "rebuild", "--state", "runs/x/state.json"],
            "guard": ["guard", *injected, "check", "127.0.0.1"],
            "tool": ["tool", *injected, "port_scan"],
            "ranges": ["ranges", *injected, "list"],
        }
        for sub, argv in cases.items():
            try:
                build_parser().parse_args(argv)
            except SystemExit as exc:  # argparse 报错会 SystemExit(2)
                self.fail(f"子命令 {sub} 无法解析启动器注入的参数（argv={argv}）: {exc}")

    def test_commands_without_config_args(self):
        """tools/audit/lab 走另一分支（不注入 --set），解析应正常。"""
        from trina.cli import build_parser

        for argv in (["tools"], ["tools", "--json"], ["audit", "verify", "logs/x.jsonl"], ["lab", "--http"]):
            try:
                build_parser().parse_args(argv)
            except SystemExit as exc:
                self.fail(f"{argv} 解析失败: {exc}")


class TestConsole(unittest.TestCase):
    """控制台命令分发（不需要真 LLM：只验证解析、状态与错误处理）。"""

    def setUp(self):
        provider_temp = tempfile.TemporaryDirectory()
        self.addCleanup(provider_temp.cleanup)
        env = patch.dict("os.environ", {
            "TRINA_PROVIDERS_FILE": str(Path(provider_temp.name) / "providers.json"),
            "TRINA_TASKS_FILE": str(Path(provider_temp.name) / "tasks.json"),
        })
        env.start()
        self.addCleanup(env.stop)

    def _console(self, lines: list[str], **kwargs):
        from trina.interactive import Console

        output: list[str] = []
        inputs = iter(lines)

        def _fake_input(_prompt: str) -> str:
            try:
                return next(inputs)
            except StopIteration:
                raise EOFError from None

        console = Console(
            TrinaConfig.load().with_overrides(policy__mode="free"),
            input_fn=_fake_input,
            output=output.append,
            autostart_lab=False,
            **kwargs,
        )
        return console, output

    def test_finding_rows_show_distinct_titles_and_hide_exact_duplicates(self):
        console, output = self._console([])
        finding = {
            "title": "缺少安全响应头 X-Frame-Options",
            "vuln_class": "security_misconfig_headers",
            "parameter": "X-Frame-Options",
            "url": "https://example.test/",
            "severity": "info",
        }
        console._on_tool_result(SimpleNamespace(facts={"findings": [finding, dict(finding)]}))
        blob = "\n".join(output)
        self.assertEqual(blob.count("缺少安全响应头 X-Frame-Options"), 1)
        self.assertIn("https://example.test/", blob)

    def test_model_failure_is_visible_once_and_console_returns_to_idle(self):
        from trina.llm import LLMResponse
        for error in ("HTTP 403: forbidden", "HTTP 429: insufficient_quota"):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                backend = ReplayBackend(LLMConfig(backend="replay"))
                backend.complete_messages = lambda *_args, **_kwargs: LLMResponse(ok=False, error=error)
                console, output = self._console([], backend=backend)
                console.config = console.config.with_overrides(run__workspace=str(Path(directory) / "runs"),
                                                               audit__dir=str(Path(directory) / "logs"))
                try:
                    console._start_chat_turn("你好")
                    console.session.thread.join(3)
                    self.assertFalse(console.session.running)
                    self.assertEqual(console.view.activity()[0], "idle")
                    self.assertEqual("\n".join(output).count(error), 1)
                    events = console.chat.events.replay(kinds=("model.error", "model.finished"))
                    self.assertEqual([event.kind for event in events], ["model.error", "model.finished"])
                finally:
                    console._cleanup()

    def test_context_budget_failure_is_visible_before_any_backend_request(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = ReplayBackend(LLMConfig(backend="replay"))
            console, output = self._console([], backend=backend)
            console.config = console.config.with_overrides(run__workspace=str(Path(directory) / "runs"),
                                                           audit__dir=str(Path(directory) / "logs"),
                                                           llm__context_window=3072, llm__max_tokens=1024)
            try:
                console._start_chat_turn("你好")
                console.session.thread.join(3)
                self.assertFalse(console.session.running)
                self.assertEqual(console.view.activity()[0], "idle")
                self.assertIn("上下文预算不足", "\n".join(output))
                self.assertEqual(backend.calls, 0)
            finally:
                console._cleanup()

    def test_status_and_help_and_quit(self):
        console, output = self._console(["help", "status", "approve verify", "quit"])
        self.assertEqual(console.run(), 0)
        blob = "\n".join(output)
        self.assertIn("会话与模型", blob)
        self.assertIn("/model", blob)
        self.assertIn("/play", blob)
        self.assertIn("审批模式已切换为 verify", blob)
        self.assertEqual(console.session.approval_mode, "verify")

    def test_default_console_trusts_run_without_persistent_approval_badge(self):
        console, _ = self._console([])
        self.assertEqual(console.session.approval_mode, "off")
        self.assertNotIn("审批", console._prompt_status())

    def test_manual_binary_tool_reuses_chat_context_without_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = TrinaConfig.load().with_overrides(run__workspace=str(Path(directory) / "runs"),
                                                     audit__dir=str(Path(directory) / "logs"), policy__mode="free")
            backend = ReplayBackend(LLMConfig(backend="replay"))
            output = []
            console = __import__("trina.interactive", fromlist=["Console"]).Console(
                cfg, output=output.append, backend=backend, registry=ToolRegistry([_BinaryEchoTool()]), autostart_lab=False)
            try:
                console._cmd_tool(["binary_echo", "--param", "value=7"])
                self.assertEqual(backend.calls, 0)
                self.assertEqual(console.chat.state.counters["tool_calls"], 1)
                self.assertTrue(any("binary_echo" in row["content"] for row in console.chat.history))
                self.assertTrue(any("binary 7" in row["content"] for row in console.chat.history))
            finally:
                console._cleanup()

    def test_free_text_goes_to_chat(self):
        """非命令的自然语言应进入对话，而不是报"未知命令"。"""
        import json as _json
        import tempfile as _tempfile
        from pathlib import Path as _Path

        from trina.brain.chat import ChatAgent
        from trina.config import LLMConfig as _LLMConfig
        from trina.llm.replay_backend import ReplayBackend as _Replay

        with _tempfile.TemporaryDirectory() as tmp:
            script = _Path(tmp) / "r.json"
            script.write_text(_json.dumps({"steps": [{"response": "你好！我是 Trina，说个目标我就开工。"}]}), encoding="utf-8")
            console, output = self._console(["你好", "quit"])
            console.chat = ChatAgent(
                console.config.with_overrides(
                    run__workspace=str(_Path(tmp) / "runs"), audit__dir=str(_Path(tmp) / "logs"), policy__mode="free"
                ),
                registry=ToolRegistry([_EchoTool()]),
                backend=_Replay(_LLMConfig(backend="replay", replay_script=str(script))),
                logger=console._agent_log,
            )
            console.run()
            console.session.thread.join(timeout=5) if console.session.thread else None
            blob = "\n".join(output)
            self.assertIn("你好！我是 Trina", blob)
            self.assertNotIn("[Trina] 输出", blob)
            self.assertNotIn("未知命令", blob)

    def test_unknown_slash_command_is_reported(self):
        console, output = self._console(["/nope", "quit"])
        console.run()
        self.assertTrue(any("未知命令" in line for line in output))

    def test_model_switch_and_removed_backend_command(self):
        console, output = self._console(["/model deepseek/deepseek-chat", "/backend omp", "/usage", "quit"])
        console.run()
        blob = "\n".join(output)
        self.assertIn("已切换模型", blob)
        self.assertIn("未知命令 'backend'", blob)
        self.assertIn("暂无可显示的用量数据", blob)
        self.assertNotEqual(console.config.llm.backend, "omp")
        self.assertIn("DeepSeek Chat", console._footer_context().model)

    def test_model_list_and_history_and_new(self):
        console, output = self._console(
            ["/model list", "/history", "/new", "/history", "quit"]
        )
        console.run()
        blob = "\n".join(output)
        self.assertTrue("可选模型" in blob or "没能列出" in blob)
        self.assertIn("还没有对话历史", blob)
        self.assertIn("已开启新对话会话", blob)
        self.assertEqual(console._footer_context().task, "新对话")

    def test_save_persists_backend_and_model(self):
        import os
        import tempfile as _tempfile
        from pathlib import Path as _Path

        with _tempfile.TemporaryDirectory() as tmp:
            env_file = _Path(tmp) / "env"
            env_file.write_text("# keep me\nexport DEEPSEEK_API_KEY=sk-test\n", encoding="utf-8")
            os.environ["TRINA_ENV_FILE"] = str(env_file)
            # 隔离本机供应商配置：否则默认配置会按 ~/.trina/providers.json 的 active 连接改写后端。
            os.environ["TRINA_PROVIDERS_FILE"] = str(_Path(tmp) / "providers.json")
            try:
                console, output = self._console(["/model deepseek-chat", "/save", "quit"])
                console.run()
                blob = "\n".join(output)
                self.assertIn("已保存到", blob)
                content = env_file.read_text(encoding="utf-8")
                from trina.providers import ProfileStore
                stored = ProfileStore()
                self.assertTrue(stored.active)
                self.assertEqual(stored.profiles[stored.active].model, console.config.llm.model)
                self.assertNotIn("export TRINA_MODEL=", content)
                self.assertIn("DEEPSEEK_API_KEY=sk-test", content, "既有内容不能被覆盖")
                self.assertNotIn("# keep me\n# keep me", content)
            finally:
                os.environ.pop("TRINA_ENV_FILE", None)
                os.environ.pop("TRINA_PROVIDERS_FILE", None)

    def test_usage_without_chat_still_reports_ledger(self):
        console, output = self._console(["/usage", "quit"])
        console.run()
        self.assertTrue(any("暂无可显示的用量数据" in line for line in output))

    def test_goal_persists_and_running_update_uses_typed_steer(self):
        from trina.session import GoalUpdate

        console, output = self._console([])
        console.handle("/goal set 验证登录流程")
        self.assertEqual(console.task_store.goal(), "验证登录流程")
        console.session.thread = SimpleNamespace(is_alive=lambda: True)
        console.handle("/goal set 追加检查会话失效")
        update = console.session.steers.get_nowait()
        self.assertIsInstance(update, GoalUpdate)
        self.assertEqual(str(update), "追加检查会话失效")
        self.assertIn("追加检查会话失效", "\n".join(output))

    def test_revoke_and_grant(self):
        console, output = self._console(["revoke lab-http", "grant lab-http", "quit"])
        console.run()
        blob = "\n".join(output)
        self.assertIn("已撤销 lab-http", blob)
        self.assertIn("已恢复 lab-http", blob)

    def test_scheduled_config_rebuilds_policy_controller(self):
        console, _output = self._console([])
        interactive = console._ensure_controller()
        interactive.revoke("lab-http")
        scheduled_config = console.config.with_overrides(policy__max_checks=7)

        scheduled = console._ensure_controller(scheduled_config)
        self.assertIsNot(scheduled, interactive)
        self.assertEqual(scheduled.budget.max_checks, 7)
        self.assertNotIn("lab-http", scheduled.revoked)

        restored = console._ensure_controller()
        self.assertIsNot(restored, scheduled)
        self.assertEqual(restored.budget.max_checks, console.config.policy.max_checks)

    def test_run_without_target_explains_usage(self):
        console, output = self._console(["run", "quit"])
        console.run()
        self.assertTrue(any("用法: run <target>" in line for line in output))

    def test_running_state_rejects_unrelated_commands(self):
        console, output = self._console([])
        console.session.thread = threading.Thread(target=lambda: time.sleep(0.5))
        console.session.thread.start()
        console.handle("tools")
        self.assertTrue(any("任务运行中" in line for line in output))
        console.session.thread.join(timeout=2)

    def test_running_supplement_preserves_quotes_and_sends_without_confirmation(self):
        from types import SimpleNamespace
        console, output = self._console([])
        console.session.thread = SimpleNamespace(is_alive=lambda: True)
        text = '补充：先看 /login 的 "redirect 参数\n再检查返回内容'
        self.assertTrue(console.handle(text))
        self.assertIn("补充 1 条待处理", console._prompt_status())
        self.assertEqual(console.session.steers.get_nowait(), text)
        self.assertTrue(any("已发送" in line for line in output))


if __name__ == "__main__":
    unittest.main()
