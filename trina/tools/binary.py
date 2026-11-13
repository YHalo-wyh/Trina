"""语义化二进制工具；实际执行由注册表、任务会话与受控管道共同承载。"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid

from .registry import ParamSpec as P, Tool, ToolResult, ToolSpec
from .external import run_binary_external
from ..binary.models import BinaryError, address, page


def optional(kind: str, description: str, default=None, enum=None) -> P:
    return P(kind, description, required=False, default=default, enum=enum)


PAGING = {"query": optional("str", "结果文字过滤", ""), "limit": optional("int", "返回项数，最多配置上限", 10),
          "cursor": optional("str", "上次返回的 next_cursor", "")}
SID = {"session_id": P("str", "当前任务会话句柄")}


def normalized(value):
    if isinstance(value, list):
        return [normalized(row) for row in value]
    if isinstance(value, dict):
        return {key: hex(item) if key in {"offset", "addr", "vaddr", "paddr", "jump", "fail", "from", "to"} and type(item) is int and item >= 0
                else normalized(item) for key, item in value.items()}
    return value


class BinaryTool(Tool):
    def __init__(self, name: str, summary: str, params: dict, level: str = "detect"):
        self.spec = ToolSpec(name=name, summary=summary, category="binary", action_level=level,
            params=params, notes="缺少原生依赖时返回 TOOL_MISSING，不模拟分析或调试成功")

    def run(self, ctx, **params) -> ToolResult:
        started = time.monotonic()
        pool = ctx.extra["binary_sessions"]
        try:
            data, row = self.invoke(ctx, pool, **params)
            event = "binary-" + uuid.uuid4().hex[:12]
            raw = json.dumps({"tool": self.spec.name, "data": data,
                              "protocol_records": row.get("records", row["backend"].raw) if row else []}, ensure_ascii=False)
            path = ctx.save_artifact(event + ".json", raw)
            evidence = {"kind": "binary", "source": row["kind"] if row else self.spec.name,
                        "artifact_sha256": row["artifact"].sha256 if row else data.get("artifact_sha256", ""),
                        "tool_event_id": event, "raw_ref": str(path), "sha256": hashlib.sha256(raw.encode()).hexdigest()}
            ctx.audit.append("binary.observation", tool=self.spec.name, event_id=event,
                raw_ref=str(path), raw_bytes=len(raw.encode()), duration=time.monotonic() - started,
                commands_executed=len(row.get("records", row["backend"].raw)) if row else 1)
            # 返回观察保留分页游标与证据索引；完整 native 结果在工件中。
            visible = {"evidence_ref": str(path), **{key: value for key, value in data.items() if key != "items"}}
            for key in ("stdout", "stderr"):
                if len(visible.get(key, "")) > 1200:
                    visible[key] = visible[key][:1200]
                    visible["output_truncated"] = True
            if "items" in data:
                visible["items"] = data["items"]
            ok = data.get("ok", True)
            return ToolResult(ok=ok, summary=f"{self.spec.name}: {data.get('state', 'OK')}", data=visible,
                              error=(data.get("error") or f"程序退出码 {data.get('code')}") if not ok else "",
                              evidence=[evidence], raw=raw, duration=time.monotonic() - started)
        except BinaryError as exc:
            return self.failure(ctx, exc.code, str(exc), params)
        except (TimeoutError, RuntimeError, OSError) as exc:
            code = "ANALYSIS_TIMEOUT" if isinstance(exc, TimeoutError) and self.spec.name == "binary_open" else (
                   "DEBUG_TIMEOUT" if isinstance(exc, TimeoutError) else str(exc).split(":", 1)[0])
            return self.failure(ctx, code, str(exc), params)

    def failure(self, ctx, code: str, detail: str, params: dict) -> ToolResult:
        """失败证据也保存原生响应，不以错误摘要替代真实状态。"""
        row = ctx.extra["binary_sessions"].sessions.get(params.get("session_id", ""))
        event = "binary-error-" + uuid.uuid4().hex[:12]
        payload = {"error_code": code, "detail": detail, "protocol_records": row["backend"].raw if row else []}
        if row:
            payload["stderr"] = row["backend"].pipe.stderr.decode("utf-8", "replace")
        raw = json.dumps(payload, ensure_ascii=False)
        path = ctx.save_artifact(event + ".json", raw)
        ctx.audit.append("binary.failure", tool=self.spec.name, event_id=event, error_code=code, detail=detail[:300], raw_ref=str(path))
        return ToolResult(ok=False, error=detail, summary=f"{code}: {detail}", data={"error_code": code, "evidence_ref": str(path)},
                          raw=raw, evidence=[{"kind": "binary_error", "raw_ref": str(path), "tool_event_id": event}])

    def invoke(self, ctx, pool, **p) -> tuple[dict, dict | None]:
        name = self.spec.name
        if name in {"binary_open", "debug_start"}:
            options = {key: value for key, value in p.items() if key != "path"}
            if name == "debug_start":
                args = options.get("argv", [])
                if (any(not isinstance(item, str) or "\0" in item or "\n" in item for item in args)
                        or len(args) > 128 or sum(len(item.encode()) for item in args) > 65536):
                    raise BinaryError("BAD_ARGV", "参数必须是有界字符串列表")
                if options.get("stdin_file"):
                    options["stdin_file"] = str(pool.artifact(ctx, options["stdin_file"]).path)
            sid, row, reused = pool.open(ctx, "r2" if name == "binary_open" else "gdb", p["path"], **options)
            result = self.metadata(sid, row)
            if name == "binary_open":
                info = row["backend"].info.get("bin", {})
                result.update({"file_format": info.get("class", info.get("format", "raw")), "arch": info.get("arch"),
                    "bits": info.get("bits"), "endian": info.get("endian"), "r2_version": row["backend"].version,
                    "probes": row["backend"].probes, "reused": reused,
                    "imports_count": row["backend"].imports_count})
                entries = row["backend"].cache.get("iej", [])
                if entries and "entry" not in result:
                    result["entry"] = hex(entries[0].get("vaddr", entries[0].get("paddr", 0)))
            return result, {**row, "records": [] if reused else list(row["backend"].raw)}
        if name == "binary_session":
            pool.check(ctx)
            if p["action"] == "list":
                return {"items": pool.snapshot()}, None
            with pool.use(ctx, p.get("session_id", "")) as row:
                row["backend"].pipe.close()
                return {"state": "CLOSED", "session_id": p["session_id"]}, row
        if name == "binary_execute":
            artifact = pool.artifact(ctx, p["path"])
            args = p.get("argv", [])
            if (any(not isinstance(item, str) or "\0" in item for item in args)
                    or len(args) > 128 or sum(len(item.encode()) for item in args) > 65536):
                raise BinaryError("BAD_ARGV", "执行参数非法")
            stdin = p.get("stdin", "")
            if len(stdin.encode()) > 65536:
                raise BinaryError("INPUT_LIMIT", "标准输入最多 64 KiB")
            if p["timeout"] <= 0:
                raise BinaryError("BAD_TIMEOUT", "运行时限必须为正数")
            result = run_binary_external([str(artifact.path), *args], ctx,
                timeout=min(p["timeout"], ctx.config.binary.debug_timeout), input_data=stdin)
            data = {**result.to_dict(limit=ctx.config.binary.max_output_bytes), "artifact_sha256": artifact.sha256, "verified_solve": False,
                    "state": "EXITED" if result.ok else "FAILED"}
            # 正常退出仅证明进程执行；不添加 flags 或 success 事实。
            return data, None
        kind = "gdb" if name.startswith("debug_") else "r2"
        with pool.use(ctx, p["session_id"], kind) as row:
            backend = row["backend"]
            if kind == "r2":
                timeout = ctx.config.binary.query_timeout
                if name == "binary_overview":
                    command = {"summary": "ij", "sections": "iSj", "imports": "iij", "strings": "izj"}[p["section"]]
                elif name == "binary_functions":
                    command = "aflj"
                else:
                    number = address(p["address"])
                    if name == "binary_xrefs":
                        command = ("axtj" if p["direction"] == "to" else "axfj") + " @ " + hex(number)
                    else:
                        command = ("agfj" if p["view"] == "blocks" else "pdfj") + " @ " + hex(number)
                result = backend.query(command, timeout)
                if isinstance(result, dict) and "ops" in result:
                    result = result["ops"] if p.get("view") != "summary" else [{key: value for key, value in result.items() if key != "ops"}]
                if p.get("view") == "blocks" and isinstance(result, list):
                    result = [block for function in result for block in function.get("blocks", [])]
                rows = result if isinstance(result, list) else [result]
                result = self.paged(rows, p, ctx)
            elif name == "debug_control":
                result = self.control(backend, p)
            else:
                result = self.inspect(backend, p, ctx)
            return {**self.metadata(p["session_id"], row), **result}, {**row, "records": list(backend.raw)}

    @staticmethod
    def metadata(sid, row) -> dict:
        artifact, backend = row["artifact"], row["backend"]
        data = {"session_id": sid, "artifact_sha256": artifact.sha256, "path": str(artifact.path),
                "address_space": "static_va" if row["kind"] == "r2" else "runtime_va", **{key: value for key, value in artifact.elf.items() if key != "segments"}}
        entry = artifact.elf.get("entry")
        if entry:
            data["entry_ref"] = artifact.address_ref(static_va=address(entry),
                load_bias=getattr(backend, "load_bias", None), module=str(artifact.path)).to_dict()
        if row["kind"] == "gdb":
            if "entry" in data:
                data["static_entry"] = data.pop("entry")
            data.update(backend.snapshot())
            if backend.pc:
                data["pc_ref"] = artifact.address_ref(runtime_va=address(backend.pc),
                    load_bias=backend.load_bias, module=str(artifact.path)).to_dict()
        return data

    @staticmethod
    def paged(rows, p, ctx) -> dict:
        # 在通用 observation 截断之前给出可继续的游标，大项的原件仍在协议工件。
        result = page(normalized(rows), p.get("query", ""), p.get("limit", 10), p.get("cursor", ""), ctx.config.binary.max_query_items)
        shown, used = [], 0
        for item in result["items"]:
            text = json.dumps(item, ensure_ascii=False)
            if shown and used + len(text) > 1400:
                break
            if len(text) > 1400:
                item = {"preview": text[:1000], "detail_in_evidence": True}
            shown.append(item)
            used += min(len(text), 1000)
        result["items"] = shown
        end = address(p.get("cursor") or "0") + len(shown)
        result.update(next_cursor=str(end) if end < result["total"] else None, truncated=end < result["total"])
        return result

    @staticmethod
    def control(backend, p) -> dict:
        action = p["action"]
        if action == "stop":
            backend.pipe.close()
            backend.state = "CLOSED"
            return backend.snapshot()
        if action == "break":
            where = backend.location(p.get("address", ""))
            result = backend.command('-break-insert ' + json.dumps(where))
            item = result.get("bkpt", {})
            backend.breakpoints.append(item)
            return {"breakpoint": item}
        if action == "delete_break":
            bid = p.get("breakpoint_id", "")
            if not re.fullmatch(r"\d+(?:\.\d+)?", bid):
                raise BinaryError("BAD_BREAKPOINT", "断点 ID 非法")
            backend.command('-break-delete ' + bid)
            backend.breakpoints[:] = [row for row in backend.breakpoints if row.get("number") != bid]
            return {}
        count = p.get("count", 1)
        if not 1 <= count <= 32:
            raise BinaryError("BAD_COUNT", "单次最多执行 32 条指令")
        if action in {"continue", "stepi", "nexti"} and backend.state != "STOPPED":
            raise BinaryError("NOT_STOPPED", "操作需要已停止的 inferior")
        if action == "interrupt" and backend.state != "RUNNING":
            raise BinaryError("NOT_RUNNING", "当前没有运行中的 inferior")
        command = {"run": "-exec-run", "continue": "-exec-continue", "stepi": "-exec-step-instruction",
                   "nexti": "-exec-next-instruction", "interrupt": "-exec-interrupt --all"}[action]
        for _ in range(count if action in {"stepi", "nexti"} else 1):
            if action == "run" and backend.input:
                backend.input.feed()
            before = backend.event_id
            backend.command(command)
            backend.wait_stop(before)
            if backend.state == "EXITED":
                break
        return backend.snapshot()

    @staticmethod
    def inspect(backend, p, ctx) -> dict:
        if backend.state != "STOPPED":
            raise BinaryError("NOT_STOPPED", "先等待或中断程序到 STOPPED 再观察")
        view, length = p["view"], p["length"]
        if not 1 <= length <= ctx.config.binary.max_memory_bytes:
            raise BinaryError("MEMORY_LIMIT", "读取长度超过限制")
        if view == "mappings":
            items = backend.mappings()
            for row in items:
                row["start_ref"] = backend.artifact.address_ref(runtime_va=address(row["start"]),
                    load_bias=backend.load_bias if row["module"] == str(backend.artifact.path) else None,
                    module=row["module"]).to_dict()
            return {"items": items, "load_bias": hex(backend.load_bias) if backend.load_bias is not None else None}
        if view == "registers":
            names = backend.command('-data-list-register-names').get("register-names", [])
            wanted = p.get("registers", []) or [name for name in names if name in {"rax", "rbx", "rcx", "rdx", "rdi", "rsi", "rbp", "rsp", "rip", "eflags", "pc", "sp", "r0", "r1", "r2", "r3"}]
            if any(name not in names for name in wanted) or len(wanted) > 32:
                raise BinaryError("BAD_REGISTERS", "寄存器不存在或请求过多")
            values = backend.command('-data-list-register-values x ' + ' '.join(str(names.index(name)) for name in wanted))
            return {"items": [{"register": names[int(row["number"])], "value": row["value"]} for row in values.get("register-values", [])]}
        if view == "frames":
            return backend.command('-stack-list-frames 0 15')
        if view == "stack":
            number = backend.command('-data-evaluate-expression "$sp"')["value"].split()[0]
        else:
            number = backend.location(p.get("address", ""))
            number = number[1:] if number.startswith("*") else number
        if view in {"memory", "stack"}:
            return backend.command('-data-read-memory-bytes ' + json.dumps(number) + ' ' + str(length))
        start = address(number)
        return backend.command(f'-data-disassemble -s {hex(start)} -e {hex(start + length)} -- 0')


TOOLS = [
    BinaryTool("binary_open", "只读打开二进制并复用本任务 r2 分析会话", {"path": P("str", "当前项目或任务内文件"),
        "analysis": optional("str", "按需提高分析深度", "quick", ["quick", "standard", "deep"]),
        "arch": optional("str", "裸 BIN 的架构", ""), "bits": optional("int", "裸 BIN 的位数", 0), "base_va": optional("str", "裸 BIN 加载地址", "")}),
    BinaryTool("binary_overview", "分页查询摘要、节区、导入或字符串", {**SID, **PAGING, "section": optional("str", "摘要类型", "summary", ["summary", "sections", "imports", "strings"])}),
    BinaryTool("binary_functions", "过滤并分页查看函数索引", {**SID, **PAGING}),
    BinaryTool("binary_function", "查看指定静态地址的局部指令或基本块", {**SID, **PAGING, "address": P("str", "静态 VA 整数"),
        "view": optional("str", "局部视图", "disasm", ["disasm", "blocks", "ops", "summary"])}),
    BinaryTool("binary_xrefs", "查询静态地址的传入或传出引用", {**SID, **PAGING, "address": P("str", "静态 VA 整数"), "direction": optional("str", "引用方向", "to", ["to", "from"])}),
    BinaryTool("debug_start", "在受控沙箱启动本机 ELF 的 GDB/MI3 会话，默认保留 ASLR 并停在入口", {"path": P("str", "ELF 路径"),
        "argv": optional("list", "被调试程序参数", []), "stdin_file": optional("str", "标准输入文件（不支持时明确报错）", ""), "break_at_entry": optional("bool", "启动后停在第一条指令", True)}, "exploit"),
    BinaryTool("debug_control", "断点、运行、单步或结束；等待真实 stopped/exited 事件", {**SID,
        "action": P("str", "控制操作", enum=["break", "delete_break", "run", "continue", "stepi", "nexti", "interrupt", "stop"]),
        "address": optional("str", "symbol:名称 / static_va:0x... / runtime_va:0x...", ""),
        "breakpoint_id": optional("str", "已有断点 ID", ""), "count": optional("int", "单步条数", 1)}, "exploit"),
    BinaryTool("debug_inspect", "读取停止状态下的寄存器、内存、栈、帧、汇编或模块映射", {**SID,
        "view": P("str", "观察内容", enum=["registers", "memory", "stack", "frames", "disasm", "mappings"]),
        "address": optional("str", "注明地址空间的地址或符号", ""), "length": optional("int", "内存字节数", 64), "registers": optional("list", "寄存器名称", [])}, "exploit"),
    BinaryTool("binary_session", "查看或关闭本任务二进制会话", {"action": optional("str", "管理动作", "list", ["list", "close"]), "session_id": optional("str", "要关闭的句柄", "")}),
    BinaryTool("binary_execute", "真实运行候选输入并保留退出状态；任务成功另由权威验证器裁定", {"path": P("str", "程序路径"),
        "argv": optional("list", "候选输入参数", []), "stdin": optional("str", "候选标准输入", ""), "timeout": optional("float", "最长执行秒数", 10)}, "exploit"),
]
