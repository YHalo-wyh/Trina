"""原生二进制工具的契约、解析与真实 ELF 测试；缺少依赖明确 skip。"""
from __future__ import annotations

import copy
import json
import os
import secrets
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from trina.audit import AuditLog
from trina.binary.mi import parse_record
from trina.binary.models import BinaryArtifact, BinaryError, address, page
from trina.brain.binary_prompt import role_for_registry
from trina.config import TrinaConfig
from trina.scope import ScopeGuard, ScopeViolation
from trina.state import RunState
from trina.tools import build_registry
from trina.tools.external import run_external
from trina.tools.registry import ToolContext
from trina.tools.schema import function_schema


class BinaryContractTests(unittest.TestCase):
    def test_real_registry_schemas_and_500k_default(self):
        registry = build_registry(["binary"])
        self.assertEqual(len(registry.names()), 10)
        for spec in registry.specs():
            native = function_schema(spec)
            self.assertEqual(native["function"]["name"], spec.name)
            self.assertEqual(native["function"]["parameters"]["additionalProperties"], False)
        self.assertEqual(TrinaConfig().llm.effective_context_window, 500000)
        self.assertEqual(TrinaConfig().with_overrides(llm__model="gpt-4.1").llm.effective_context_window, 1000000)

    def test_mi_nested_values_async_errors_and_precision(self):
        row = parse_record('31^done,stack=[frame={level="0",addr="0xffffffffffffffff",func="main"}],value="a\\n\\042b"')
        self.assertEqual(row["token"], "31")
        self.assertEqual(row["data"]["stack"][0]["frame"]["addr"], "0xffffffffffffffff")
        self.assertEqual(row["data"]["value"], 'a\n"b')
        self.assertEqual(parse_record('*stopped,reason="signal-received",signal-name="SIGSEGV"')["data"]["signal-name"], "SIGSEGV")
        self.assertEqual(parse_record('2^error,msg="cannot access memory"')["class"], "error")
        self.assertEqual(parse_record('@"hello\\n"')["text"], "hello\n")

    def test_address_injection_and_bounded_paging(self):
        self.assertEqual(address("0xffffffffffffffff"), 2**64 - 1)
        for value in ("0x10;!sh", "1|cat", "0x10 @ 4", "$(id)", "-1", "1>out", str(2**64)):
            with self.assertRaises(BinaryError):
                address(value)
        result = page([{"value": str(i)} for i in range(1000)], limit=7)
        self.assertEqual(result["next_cursor"], "7")
        self.assertEqual(len(page(list(range(1000)), cursor="7", limit=7)["items"]), 7)

    def test_address_ref_keeps_offset_static_and_runtime_spaces_separate(self):
        artifact = BinaryArtifact(Path("fixture"), "hash", {"file_format": "ELF", "bits": 64,
            "machine": 62, "pie": True, "segments": [{"offset": 0x1000, "vaddr": 0x2000,
            "filesz": 0x200, "memsz": 0x300}]})
        ref = artifact.address_ref(static_va=0x2080, load_bias=0x700000, module="fixture").to_dict()
        self.assertEqual(ref["file_offset"], "0x1080")
        self.assertEqual(ref["static_va"], "0x2080")
        self.assertEqual(ref["runtime_va"], "0x702080")
        self.assertEqual(ref["load_bias"], "0x700000")
        self.assertEqual(ref["pointer_bits"], 64)

    def test_binary_role_only_follows_fixed_binary_capability(self):
        role = role_for_registry(build_registry(["binary"]))
        self.assertIn("<gdb_tool_policy>", role)
        self.assertIn("仅在当前任务需要分析或调试二进制时适用", role)
        self.assertNotIn("# Binary Worker", role)
        self.assertNotIn("binary-role-v1", role_for_registry(build_registry(["web"])))
        self.assertIn("自定义", role_for_registry(build_registry(["web"]), "自定义"))

    def test_pie_load_bias_uses_segment_vaddr_and_mapping_offset(self):
        from types import SimpleNamespace
        from pathlib import PurePosixPath
        from trina.binary.backends import GDBBackend
        backend = GDBBackend.__new__(GDBBackend)
        backend.raw, backend.load_bias = [], None
        backend.artifact = SimpleNamespace(path=PurePosixPath("/fixture"), elf={"segments": [{"offset": 0, "vaddr": 0x200000}]})
        def mapping(_):
            backend.raw.append({"kind": "~", "text": "0x700000 0x701000 0x1000 0x0 r--p /fixture\n"})
        backend.command = mapping
        with patch("os.sysconf", return_value=4096, create=True):
            backend.mappings()
        self.assertEqual(backend.load_bias, 0x500000)

    def test_missing_r2_returns_machine_error_without_fake_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = TrinaConfig().with_overrides(run__workspace=str(root))
            ctx = ToolContext(config=cfg, guard=ScopeGuard(cfg.scope), audit=AuditLog.open(root / "logs", "missing"),
                state=RunState("missing", "", ""), http=None, workspace=root / "missing")
            path = ctx.workspace / "input.bin"
            path.write_bytes(b"not an executable")
            with patch("trina.binary.backends.which", return_value=None):
                result = build_registry(["binary"]).execute("binary_open", {"path": str(path)}, ctx)
            self.assertEqual(result.data["error_code"], "TOOL_MISSING")
            self.assertEqual(ctx.extra["binary_sessions"].snapshot(), [])


@unittest.skipUnless(os.name == "posix" and all(shutil.which(name) for name in ("r2", "gdb", "gcc", "bwrap")), "真实 ELF 测试需要 Linux r2/GDB/gcc/bwrap")
class RealBinaryTests(unittest.TestCase):
    def setUp(self):
        parent = Path("runs").resolve()
        parent.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="binary-test-", dir=parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = TrinaConfig().with_overrides(run__workspace=str(self.root / "runs"), audit__dir=str(self.root / "logs"))
        self.registry = build_registry(["binary"])
        self.ctx = self.context("worker-a")
        self.addCleanup(self.ctx.extra["binary_sessions"].close)
        source = Path(__file__).parent / "fixtures" / "binary" / "challenge.c"
        self.source = self.ctx.workspace / "challenge.c"
        shutil.copyfile(source, self.source)
        # 不把候选输入交给求解路径；只把随机编码写入编译单元。
        encoded = bytes(ord(ch) ^ 0x5a for ch in secrets.token_hex(6))
        (self.ctx.workspace / "encoded.h").write_text("unsigned char encoded[] = {" + ",".join(str(x) for x in encoded) + "};\n")
        self.binary = self.ctx.workspace / "challenge"
        compiled = run_external(["gcc", "-g", "-O0", "-fPIE", "-pie", "-o", str(self.binary), str(self.source)], self.ctx)
        self.assertTrue(compiled.ok, compiled.stderr)

    def context(self, run_id):
        return ToolContext(config=self.config, guard=ScopeGuard(self.config.scope), audit=AuditLog.open(self.config.audit_dir, run_id),
            state=RunState(run_id, "local", "fixture"), http=None, workspace=self.config.workspace_dir / run_id, tools=self.registry)

    def call(self, name, **params):
        result = self.registry.execute(name, params, self.ctx)
        self.assertTrue(result.ok, result.to_observation())
        return result.data

    def test_real_static_dynamic_candidate_and_authoritative_local_verification(self):
        opened = self.call("binary_open", path=str(self.binary), analysis="standard")
        sid = opened["session_id"]
        self.assertTrue(opened["pie"])
        self.assertEqual(opened["bits"], 64)
        self.assertEqual(self.call("binary_open", path=str(self.binary), analysis="standard")["session_id"], sid)
        strings = self.call("binary_overview", session_id=sid, section="strings", query="accepted")
        self.assertTrue(strings["items"])
        string_va = strings["items"][0]["vaddr"]
        xrefs = self.call("binary_xrefs", session_id=sid, address=string_va)
        self.assertTrue(xrefs["items"])
        local = self.call("binary_function", session_id=sid, address=xrefs["items"][0]["from"])
        self.assertTrue(local["items"])
        functions = self.call("binary_functions", session_id=sid, query="sym.check")
        check_va = functions["items"][0]["offset"]
        dbg = self.call("debug_start", path=str(self.binary), argv=["incorrect"])
        did = dbg["session_id"]
        self.assertEqual(dbg["state"], "STOPPED")
        bp = self.call("debug_control", session_id=did, action="break", address="static_va:" + check_va)
        self.assertTrue(bp["breakpoint"].get("number"))
        stopped = self.call("debug_control", session_id=did, action="continue")
        self.assertEqual(stopped["reason"], "breakpoint-hit")
        regs = self.call("debug_inspect", session_id=did, view="registers")
        self.assertTrue(regs["items"])
        self.assertTrue(self.call("debug_inspect", session_id=did, view="frames")["stack"])
        memory = self.call("debug_inspect", session_id=did, view="memory", address="symbol:encoded", length=12)
        recovered = bytes.fromhex(memory["memory"][0]["contents"])
        candidate = ''.join(chr(value ^ 0x5a) for value in recovered)
        self.call("debug_control", session_id=did, action="stepi")
        self.call("debug_control", session_id=did, action="stop")
        validated = self.call("binary_execute", path=str(self.binary), argv=[candidate])
        self.assertEqual(validated["code"], 0)
        self.assertEqual(validated["stdout"].strip(), "accepted")
        self.assertFalse(validated["verified_solve"], "工具执行成功不能冒充平台判定")
        wrong = self.registry.execute("binary_execute", {"path": str(self.binary), "argv": ["incorrect"]}, self.ctx)
        self.assertFalse(wrong.ok)
        self.assertTrue(self.ctx.audit.verify()[0])

    def test_signal_timeout_and_process_cleanup(self):
        for mode, reason in (("--crash", "signal-received"), ("incorrect", "exited")):
            started = self.call("debug_start", path=str(self.binary), argv=[mode], break_at_entry=False)
            did = started["session_id"]
            result = self.call("debug_control", session_id=did, action="run")
            self.assertTrue(result["reason"].startswith(reason), result)
            if mode == "--crash":
                self.assertEqual(result["events"][0]["signal-name"], "SIGSEGV")
            self.call("debug_control", session_id=did, action="stop")
        self.config.binary.debug_timeout = .3
        opened = self.call("debug_start", path=str(self.binary), argv=["--loop"], break_at_entry=False)
        row = self.ctx.extra["binary_sessions"].sessions[opened["session_id"]]
        result = self.registry.execute("debug_control", {"session_id": opened["session_id"], "action": "run"}, self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.data["error_code"], "DEBUG_TIMEOUT")
        self.assertIsNotNone(row["backend"].pipe.proc.poll())

    def test_worker_isolation_expired_handle_mutation_and_injection(self):
        opened = self.call("binary_open", path=str(self.binary), analysis="standard")
        sid = opened["session_id"]
        other = self.context("worker-b")
        self.addCleanup(other.extra["binary_sessions"].close)
        result = self.registry.execute("binary_functions", {"session_id": sid}, other)
        self.assertFalse(result.ok)
        self.assertEqual(result.data["error_code"], "SESSION_EXPIRED")
        self.assertFalse(self.registry.execute("binary_open", {"path": str(self.binary)}, other).ok)
        for value in ("0x1;!id", "0x1|cat", "0x1>out"):
            result = self.registry.execute("binary_function", {"session_id": sid, "address": value}, self.ctx)
            self.assertEqual(result.data["error_code"], "BAD_ADDRESS")
        self.binary.write_bytes(self.binary.read_bytes() + b"changed")
        changed = self.registry.execute("binary_functions", {"session_id": sid}, self.ctx)
        self.assertEqual(changed.data["error_code"], "ARTIFACT_CHANGED")
        replacement = self.call("binary_open", path=str(self.binary))
        self.assertNotEqual(replacement["session_id"], sid)

    def test_debug_stdin_pty_no_shell_and_repeat_aslr(self):
        input_file = self.ctx.workspace / "input.txt"
        input_file.write_text("incorrect\n")
        original = input_file.read_bytes()
        bases = []
        for _ in range(2):
            started = self.call("debug_start", path=str(self.binary), stdin_file=str(input_file))
            sid = started["session_id"]
            mappings = self.call("debug_inspect", session_id=sid, view="mappings")
            self.assertIsNotNone(mappings["load_bias"])
            bases.append(mappings["load_bias"])
            stopped = self.call("debug_control", session_id=sid, action="continue")
            self.assertEqual(stopped["state"], "EXITED")
            backend = self.ctx.extra["binary_sessions"].sessions[sid]["backend"]
            time.sleep(.03)
            self.assertIn(b"rejected", backend.input.output)
            self.call("debug_control", session_id=sid, action="stop")
        self.assertNotEqual(bases[0], bases[1], "ASLR 启用的两次实际地址应变化")
        self.assertEqual(input_file.read_bytes(), original)

    def test_chat_resume_expires_saved_handles_and_does_not_restart_processes(self):
        from trina.brain.chat import ChatAgent
        from trina.llm import LLMResponse
        from trina.llm.replay_backend import ReplayBackend
        backend = ReplayBackend(self.config.llm)
        backend.complete_messages = lambda *_args, **_kwargs: LLMResponse(text="fixture")
        chat = ChatAgent(self.config, registry=self.registry, backend=backend)
        self.addCleanup(chat.close)
        target = chat.workspace / "challenge"
        shutil.copyfile(self.binary, target)
        target.chmod(0o755)
        chat.ask("保存本次静态分析")
        result = self.registry.execute("binary_open", {"path": str(target)}, chat.ctx)
        self.assertTrue(result.ok, result.error)
        sid = result.data["session_id"]
        process = chat.ctx.extra["binary_sessions"].sessions[sid]["backend"].pipe.proc
        chat.save()
        chat.close()
        self.assertIsNotNone(process.poll())
        restored = ChatAgent.resume(self.config, chat.run_id, registry=self.registry, backend=backend)
        self.addCleanup(restored.close)
        self.assertEqual(restored.ctx.extra["binary_sessions"].sessions, {})
        previous = restored.ctx.extra["binary_sessions"].snapshot()
        self.assertEqual(previous[0]["state"], "EXPIRED")
        expired = self.registry.execute("binary_functions", {"session_id": sid}, restored.ctx)
        self.assertEqual(expired.data["error_code"], "SESSION_EXPIRED")

    def test_capability_release_and_tool_timeout_cancel_real_processes(self):
        from trina.capabilities import CapabilityCatalog, CapabilityFabric, CapabilityRuntime
        fabric = CapabilityFabric(CapabilityCatalog.from_registry(self.registry), CapabilityRuntime(self.registry))
        lease = fabric.lease("worker-a", "binary", "binary", constraints={"selectors": ["category.binary"]})
        self.ctx.tools = lease.registry
        result = lease.registry.execute("binary_open", {"path": str(self.binary)}, self.ctx)
        self.assertTrue(result.ok, result.error)
        sid = result.data["session_id"]
        process = self.ctx.extra["binary_sessions"].sessions[sid]["backend"].pipe.proc
        fabric.release(lease.id)
        end = time.monotonic() + 2
        while process.poll() is None and time.monotonic() < end:
            time.sleep(.02)
        self.assertIsNotNone(process.poll())
        self.assertFalse(lease.registry.execute("binary_functions", {"session_id": sid}, self.ctx).ok)
        # 真实 r2 进程 + 单次调用等待的可控故障注入，验证晚到线程不能继续发送命令。
        self.ctx = self.context("timeout-worker")
        self.addCleanup(self.ctx.extra["binary_sessions"].close)
        target = self.ctx.workspace / "challenge"
        shutil.copyfile(self.binary, target)
        opened = self.call("binary_open", path=str(target))
        native = self.ctx.extra["binary_sessions"].sessions[opened["session_id"]]["backend"]
        original = native.query
        def slow(*args):
            time.sleep(.15)
            return original(*args)
        with patch.object(native, "query", side_effect=slow):
            result = self.registry.execute("binary_functions", {"session_id": opened["session_id"]}, self.ctx, timeout=.03)
            self.assertFalse(result.ok)
            time.sleep(.2)
        self.assertIsNotNone(native.pipe.proc.poll())

    def test_large_raw_output_is_archived_and_pagination_stays_visible(self):
        header = self.ctx.workspace / "encoded.h"
        header.write_text(header.read_text() + 'char long_text[]="' + 'A' * 12000 + '";\n')
        compiled = run_external(["gcc", "-g", "-O0", "-fPIE", "-pie", "-o", str(self.binary), str(self.source)], self.ctx)
        self.assertTrue(compiled.ok)
        opened = self.call("binary_open", path=str(self.binary), analysis="standard")
        result = self.registry.execute("binary_overview", {"session_id": opened["session_id"], "section": "strings", "query": "AAAAA"}, self.ctx)
        self.assertTrue(result.ok, result.error)
        self.assertIn("next_cursor", result.to_observation())
        self.assertGreater(Path(result.data["evidence_ref"]).stat().st_size, 10000)
        self.assertLess(len(result.to_observation()), 3000)

    def test_two_workers_hold_distinct_debuggers_and_idle_expiration(self):
        from concurrent.futures import ThreadPoolExecutor
        other = self.context("parallel-worker")
        self.addCleanup(other.extra["binary_sessions"].close)
        target = other.workspace / "challenge"
        shutil.copyfile(self.binary, target)
        target.chmod(0o755)
        with ThreadPoolExecutor(max_workers=2) as executor:
            left = executor.submit(self.registry.execute, "debug_start", {"path": str(self.binary), "argv": ["incorrect"]}, self.ctx)
            right = executor.submit(self.registry.execute, "debug_start", {"path": str(target), "argv": ["incorrect"]}, other)
            first, second = left.result(), right.result()
        self.assertTrue(first.ok, first.error)
        self.assertTrue(second.ok, second.error)
        a, b = first.data["session_id"], second.data["session_id"]
        self.assertNotEqual(a, b)
        pa = self.ctx.extra["binary_sessions"].sessions[a]["backend"].pipe.proc
        pb = other.extra["binary_sessions"].sessions[b]["backend"].pipe.proc
        self.assertNotEqual(pa.pid, pb.pid)
        self.assertFalse(self.registry.execute("debug_inspect", {"session_id": a, "view": "registers"}, other).ok)
        self.config.binary.idle_timeout = .1
        end = time.monotonic() + 2
        while (pa.poll() is None or pb.poll() is None) and time.monotonic() < end:
            time.sleep(.02)
        self.assertIsNotNone(pa.poll())
        self.assertIsNotNone(pb.poll())
        result = self.registry.execute("debug_inspect", {"session_id": a, "view": "registers"}, self.ctx)
        self.assertEqual(result.data["error_code"], "SESSION_EXPIRED")

    def test_agent_json_native_and_prompt_layers_execute_real_tools(self):
        from tests.test_tool_protocol import _ScriptedBackend
        from trina.brain.agent import PentestAgent
        target = self.root / "input-challenge"
        shutil.copyfile(self.binary, target)
        for layered in (False, True):
            for protocol in ("json", "native"):
                cfg = self.config.with_overrides(llm__prompt_layers=layered, llm__tool_protocol=protocol)
                arguments = {"path": str(target), "analysis": "quick"}
                if protocol == "native":
                    script = [{"tool": ("binary_open", arguments)}, {"tool": ("finish", {"status": "failed", "summary": "仅验证工具调用"})}]
                else:
                    script = [{"text": json.dumps({"action": "binary_open", "params": arguments})}, {"text": '{"final":{"status":"failed","summary":"仅验证工具调用"}}'}]
                backend = _ScriptedBackend(cfg.llm, script)
                agent = PentestAgent(cfg, registry=self.registry, backend=backend)
                state = agent.run(str(target), goal="读取格式", max_steps=3, max_seconds=15, run_id=f"binary-{layered}-{protocol}")
                self.assertIn("binary-role-v1", backend.requests[0]["messages"][0]["content"])
                self.assertTrue(any(row.get("tool") == "binary_open" and row.get("ok") for row in state.steps))
                self.assertTrue(all(not row["live"] for row in agent._ctx.extra["binary_sessions"].snapshot()))
                if protocol == "native":
                    self.assertIn("binary_open", {row["function"]["name"] for row in backend.requests[0]["tools"]})

    def test_swarm_local_attachment_leases_binary_tools_to_generic_worker(self):
        from tests.test_tool_protocol import _ScriptedBackend
        from trina.swarm import SwarmRuntime, TaskSpec
        target = self.root / "swarm-input"
        shutil.copyfile(self.binary, target)
        backends = []
        def factory(cfg):
            backend = _ScriptedBackend(cfg, [{"tool": ("binary_open", {"path": str(target)})},
                                            {"tool": ("finish", {"status": "failed", "summary": "仅格式观测"})}])
            backends.append(backend)
            return backend
        cfg = self.config.with_overrides(llm__tool_protocol="native", report__output_dir=str(self.root / "reports"))
        runtime = SwarmRuntime(cfg, [TaskSpec("binary", str(target), "读取附件格式", max_steps=3, max_seconds=15)],
                               max_workers=1, backend_factory=factory, capability_dirs=[])
        runtime.run()
        record = runtime.records["binary"]
        self.assertIn("tool.debug_start", record.capability_ids)
        self.assertIn("tool.binary_functions", record.capability_ids)
        self.assertIn("binary-role-v1", backends[0].requests[0]["messages"][0]["content"])
        leases = [row for row in runtime.audit.iter_records(runtime.audit.path) if row.get("event") == "capability.lease"]
        self.assertTrue(leases)
        self.assertFalse(runtime.fabric.get_lease(leases[-1]["lease"]).active)
        self.assertFalse(runtime.pool.workers[0].busy)


if __name__ == "__main__":
    unittest.main()
