"""受控 r2 stdio JSON 与 GDB/MI3 会话，不自行创建进程。"""
from __future__ import annotations

import json
import re
import time
from collections import OrderedDict

from ..scope import execution_budget
from ..tools.external import InferiorInput, ManagedPipe, binary_sandbox_argv, which
from .mi import parse_record
from .models import BinaryArtifact, BinaryError, address


class R2Backend:
    def __init__(self, ctx, artifact: BinaryArtifact, analysis: str, arch: str = "", bits: int = 0, base_va: str = ""):
        if not which("r2"):
            raise BinaryError("TOOL_MISSING", "未安装 radare2 (r2)")
        argv = [which("r2"), "-q0", "-N", "-e", "scr.color=0", "-e", "bin.cache=true"]
        if arch:
            if not re.fullmatch(r"[A-Za-z0-9_+-]{1,32}", arch):
                raise BinaryError("BAD_ARCH", "架构名称非法")
            argv += ["-a", arch]
        if bits:
            if bits not in {8, 16, 32, 64}:
                raise BinaryError("BAD_BITS", "不支持的位数")
            argv += ["-b", str(bits)]
        if base_va:
            argv += ["-B", hex(address(base_va))]
        argv.append(str(artifact.path))
        self.pipe = ManagedPipe(binary_sandbox_argv(ctx, argv), ctx, delimiter=b"\0", tool="radare2")
        self.cache = OrderedDict()
        self.raw = []
        try:
            self.pipe.receive(execution_budget(ctx.config.binary.analysis_timeout))
            self.version = self.command("?V", ctx.config.binary.query_timeout).strip()
            self.info = self.query("ij", ctx.config.binary.query_timeout)
            if not isinstance(self.info, dict) or not isinstance(self.info.get("bin"), dict):
                if not arch or not bits:
                    raise BinaryError("NEEDS_ARCH", "裸 BIN 请提供 arch、bits 和必要的 base_va")
            if analysis != "quick":
                self.command("aa" if analysis == "standard" else "aaa", ctx.config.binary.analysis_timeout)
            self.probes = {name: isinstance(self.query(name, ctx.config.binary.query_timeout), list) for name in ("iSj", "iij", "aflj", "iej")}
            self.imports_count = len(self.cache.get("iij", []))
        except BaseException:
            self.pipe.close()
            raise

    def command(self, command: str, timeout: float) -> str:
        self.pipe.send(command)
        reply = self.pipe.receive(execution_budget(timeout))
        self.raw.append({"command": command, "response": reply})
        return reply

    def query(self, command: str, timeout: float):
        if command in self.cache:
            return self.cache[command]
        reply = self.command(command, timeout)
        try:
            data = json.loads(reply)
        except ValueError as exc:
            raise BinaryError("R2_UNSUPPORTED", f"{command} 未返回合法 JSON") from exc
        self.cache[command] = data
        self.cache.move_to_end(command)
        while len(self.cache) > 16:
            self.cache.popitem(last=False)
        return data


class GDBBackend:
    def __init__(self, ctx, artifact: BinaryArtifact, argv: list[str], stdin_file: str = "", break_at_entry: bool = True):
        if not which("gdb"):
            raise BinaryError("TOOL_MISSING", "未安装 GDB")
        if not artifact.elf:
            raise BinaryError("UNSUPPORTED_TARGET", "首版原生动态调试仅支持本机 ELF")
        import platform
        allowed = {"x86_64": {3, 62}, "aarch64": {40, 183}, "i686": {3}}
        if artifact.elf.get("machine") not in allowed.get(platform.machine(), set()):
            raise BinaryError("UNSUPPORTED_ARCH", "未配置跨架构 QEMU/gdbstub，不运行非本机 ISA")
        self.artifact, self.state, self.pid = artifact, "CREATED", None
        self.token = self.event_id = 0
        self.reason, self.pc, self.thread, self.inferior = "", "", "", ""
        self.events, self.raw, self.breakpoints = [], [], []
        self.output = ""
        self.timeout = ctx.config.binary.debug_timeout
        self.load_bias = None
        self.mapping_rows = []
        self.input = None
        if any(any(ch.isspace() or ch in "\\\"'" for ch in arg) for arg in argv):
            raise BinaryError("UNSUPPORTED_ARGV", "当前 GDB 无 shell 模式只接受不含空白、引号或反斜线的参数；复杂输入请用 stdin_file 或 binary_execute")
        command = [which("gdb"), "--quiet", "--nx", "--nh", "--interpreter=mi3",
                   "-iex", "set auto-load off", "-iex", "set debuginfod enabled off",
                   "-iex", "set startup-with-shell off"]
        if stdin_file:
            self.input = InferiorInput(stdin_file)
        try:
            self.pipe = ManagedPipe(binary_sandbox_argv(ctx, command, tty=self.input.path if self.input else ""), ctx, delimiter=b"\n", tool="gdb")
        except BaseException:
            if self.input:
                self.input.close()
            raise
        if self.input:
            self.pipe.cleanups.append(self.input.close)
        try:
            self.command('-gdb-set mi-async on')
            self.command('-gdb-set disable-randomization off')
            self.command('-gdb-set startup-with-shell off')
            self.command('-gdb-set follow-fork-mode parent')
            self.command('-gdb-set detach-on-fork off')
            self.command('-file-exec-and-symbols ' + json.dumps(str(artifact.path), ensure_ascii=False))
            if argv:
                self.command('-exec-arguments ' + ' '.join(argv))
            if stdin_file:
                tty = "/dev/trina-input" if ctx.config.binary.sandbox == "bwrap" else self.input.path
                self.command('-inferior-tty-set ' + json.dumps(tty))
                self.input.feed()
            self.state = "LOADED"
            if break_at_entry:
                before = self.event_id
                self.command('-interpreter-exec console "starti"')
                self.wait_stop(before)
        except BaseException:
            self.pipe.close()
            raise

    def record(self, line: str) -> dict:
        item = parse_record(line)
        self.raw.append(item)
        if item["kind"] == "*" and item.get("class") == "running":
            self.state = "RUNNING"
        if item["kind"] == "=" and item.get("class") == "thread-group-started":
            if self.inferior and self.inferior != item["data"].get("id"):
                raise BinaryError("MULTI_INFERIOR_UNSUPPORTED", "首版不控制多个 inferior；已停止并回收本任务进程")
            self.pid = item["data"].get("pid")
            self.inferior = item["data"].get("id", "")
        if item["kind"] == "*" and item.get("class") == "stopped":
            self.event_id += 1
            data = item["data"]
            self.reason = data.get("reason", "stopped")
            self.pc = data.get("frame", {}).get("addr", "")
            self.thread = data.get("thread-id", "")
            self.state = "EXITED" if self.reason.startswith("exited") else "STOPPED"
            self.events.append({"event_id": self.event_id, **data})
            self.events[:] = self.events[-16:]
        if item["kind"] in {"@", "output"}:
            self.output = (self.output + item.get("text", ""))[-16000:]
        return item

    def command(self, command: str) -> dict:
        self.token += 1
        token = str(self.token)
        self.pipe.send(token + command)
        end = time.monotonic() + execution_budget(self.timeout)
        while time.monotonic() < end:
            row = self.record(self.pipe.receive(max(.001, end - time.monotonic())))
            if row["kind"] == "^" and row.get("token") == token:
                if row["class"] == "error":
                    raise BinaryError("GDB_ERROR", row["data"].get("msg", "MI error"))
                return row["data"]
        raise TimeoutError("MI_RESPONSE_TIMEOUT")

    def wait_stop(self, before: int, timeout: float | None = None) -> None:
        end = time.monotonic() + execution_budget(timeout or self.timeout)
        while self.event_id <= before:
            if time.monotonic() >= end:
                raise TimeoutError("DEBUG_TIMEOUT")
            self.record(self.pipe.receive(max(.001, end - time.monotonic())))

    def mappings(self) -> list[dict]:
        start = len(self.raw)
        self.command('-interpreter-exec console "info proc mappings"')
        text = ''.join(row.get("text", "") for row in self.raw[start:] if row["kind"] == "~")
        rows = []
        for line in text.splitlines():
            match = re.match(r"\s*(0x[0-9a-f]+)\s+(0x[0-9a-f]+)\s+(0x[0-9a-f]+)\s+(0x[0-9a-f]+)\s+(.*)", line)
            if match:
                first, end, size, offset, rest = match.groups()
                name = rest.split()[-1] if rest else ""
                rows.append({"start": first, "end": end, "offset": offset, "module": name})
        for row in rows:
            if row["module"] != str(self.artifact.path):
                continue
            for segment in self.artifact.elf.get("segments", []):
                import os
                page_size = os.sysconf("SC_PAGE_SIZE")
                if address(row["offset"]) == segment["offset"] // page_size * page_size:
                    self.load_bias = address(row["start"]) - segment["vaddr"] // page_size * page_size
                    break
            if self.load_bias is not None:
                break
        self.mapping_rows = rows
        return rows

    def location(self, raw: str) -> str:
        if raw.startswith("symbol:"):
            symbol = raw[7:]
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_:.$]{0,127}", symbol):
                raise BinaryError("BAD_ADDRESS", "符号名称含命令或表达式")
            return symbol
        space, _, value = raw.partition(":")
        if space not in {"static_va", "runtime_va"} or not value:
            raise BinaryError("BAD_ADDRESS", "动态地址必须注明 static_va:、runtime_va: 或 symbol:")
        number = address(value)
        if space == "static_va" and self.artifact.elf.get("pie"):
            if self.state != "STOPPED":
                raise BinaryError("NOT_STOPPED", "PIE 静态地址转换需要运行后暂停")
            self.mappings()
            if self.load_bias is None:
                raise BinaryError("ADDRESS_UNRESOLVED", "未确认主模块 PT_LOAD 与运行映射")
            number += self.load_bias
        return "*" + hex(number)

    def snapshot(self) -> dict:
        return {"state": self.state, "reason": self.reason, "pc": self.pc, "pid": self.pid,
                "thread_id": self.thread, "inferior": self.inferior, "event_id": self.event_id,
                "load_bias": hex(self.load_bias) if self.load_bias is not None else None,
                "pending": self.state == "RUNNING", "events": self.events[-1:]}
