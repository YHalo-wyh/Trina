"""外部专业工具的调用封装（nmap / nuclei / sqlmap / hydra / ffuf / gobuster ...）。

原则
----
1. **外部二进制优先，内置引擎兜底**：实训 WSL 环境常常没有安装这些工具，
   因此每个能力都必须有纯 Python 实现，外部工具只是「加速器」。
2. **shell=False**：永远用 argv 列表调用，杜绝命令注入与通配符意外展开。
3. **统一执行**：`run_external()` 内先过 `guard.check_argv()`（full 直接放行，restricted 应用边界），再执行、再审计。
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from .. import __version__
from ..scope import ScopeViolation


def which(binary: str) -> str | None:
    return shutil.which(binary)


def have(binary: str) -> bool:
    return which(binary) is not None


def installed(*binaries: str) -> dict[str, str | None]:
    return {name: which(name) for name in binaries}


@dataclass
class ExternalRun:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    code: int | None = -1
    command: str = ""
    duration: float = 0.0
    error: str = ""
    binary: str = ""
    job_id: str = ""
    running: bool = False

    @property
    def output(self) -> str:
        return self.stdout or self.stderr

    def to_dict(self, limit: int = 3000) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "command": self.command,
            "duration": round(self.duration, 2),
            "stdout": self.stdout[:limit],
            "stderr": self.stderr[:600],
            "error": self.error,
            "job_id": self.job_id,
            "running": self.running,
        }


class BackgroundJobs:
    """每个 ToolContext 独立持有的有界作业表；后台线程只更新作业，不写事实。"""

    def __init__(self):
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._closed = False

    def start(self, argv: list[str], *, cwd: str, env: dict[str, str] | None, timeout: float, tool: str,
              owner: Any = None) -> str:
        import uuid
        from ..scope import ensure_execution_active

        with self._lock:
            ensure_execution_active()
            if self._closed:
                raise ValueError("本会话的后台作业表已关闭")
            if sum(job["status"] == "running" for job in self._jobs.values()) >= 4:
                raise ValueError("每个会话最多同时运行 4 个后台终端作业")
            for key in list(self._jobs):
                if len(self._jobs) < 24:
                    break
                if self._jobs[key]["status"] != "running":
                    del self._jobs[key]
            proc = subprocess.Popen(
                argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=os.name != "nt",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
            native_job = None
            try:
                native_job = _windows_background_job(proc) if os.name == "nt" else None
                ensure_execution_active()
            except Exception:
                if native_job is not None:
                    import ctypes
                    ctypes.windll.kernel32.CloseHandle(native_job)
                proc.kill()
                proc.communicate()
                raise
            job_id = "job-" + uuid.uuid4().hex[:10]
            job: dict[str, Any] = {
                "id": job_id, "tool": tool, "status": "running", "exit_code": None,
                "started": time.monotonic(), "elapsed_seconds": 0.0,
                "stdout": "", "stderr": "", "error": "", "process": proc,
                "done": threading.Event(),
                "native_job": native_job,
                "owner": owner,
            }
            self._jobs[job_id] = job

        def read(stream, key):
            import codecs
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            try:
                while chunk := stream.read1(1024):
                    with self._lock:
                        job[key] = (job[key] + decoder.decode(chunk))[-16_000:]
            finally:
                stream.close()

        readers = [threading.Thread(target=read, args=(proc.stdout, "stdout"), daemon=True),
                   threading.Thread(target=read, args=(proc.stderr, "stderr"), daemon=True)]
        for reader in readers:
            reader.start()

        def monitor():
            timed_out = False
            try:
                proc.wait(timeout=max(.01, timeout))
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_background(proc, native_job)
            finally:
                # 主程序正常退出也回收它留下的子进程及管道。
                _terminate_background(proc, native_job)
                if native_job is not None:
                    import ctypes
                    ctypes.windll.kernel32.CloseHandle(native_job)
                for reader in readers:
                    reader.join(timeout=.5)
                with self._lock:
                    job["exit_code"] = proc.poll()
                    job["elapsed_seconds"] = round(time.monotonic() - job["started"], 2)
                    if job["status"] == "running":
                        job["status"] = "timed_out" if timed_out else ("completed" if proc.returncode == 0 else "failed")
                        job["error"] = f"后台执行超时（{timeout:g}s）" if timed_out else ""
                    job["done"].set()
        threading.Thread(target=monitor, name=f"trina-{job_id}", daemon=True).start()
        return job_id

    def snapshot(self, job_id: str = "", *, tail: int = 400) -> list[dict[str, Any]]:
        with self._lock:
            if job_id and job_id not in self._jobs:
                raise ValueError("后台作业不存在或不属于当前会话")
            rows = [self._jobs[job_id]] if job_id else list(self._jobs.values())
            if not job_id:
                active = [job for job in rows if job["status"] == "running"]
                recent = [job for job in rows if job["status"] != "running"]
                rows = active + recent[-max(1, 8 - len(active)):]
            return [{
                "id": job["id"], "tool": job["tool"], "status": job["status"],
                "exit_code": job["exit_code"], "error": job["error"],
                "elapsed_seconds": round(time.monotonic() - job["started"], 2) if job["status"] == "running" else job["elapsed_seconds"],
                "stdout_tail": job["stdout"][-tail:], "stderr_tail": job["stderr"][-tail:],
            } for job in rows[-8:]]

    def wait(self, job_id: str, seconds: float) -> None:
        with self._lock:
            if job_id not in self._jobs:
                raise ValueError("后台作业不存在或不属于当前会话")
            done = self._jobs[job_id]["done"]
        done.wait(max(0, min(seconds, 10)))

    def stop(self, job_id: str) -> None:
        with self._lock:
            if job_id not in self._jobs:
                raise ValueError("后台作业不存在或不属于当前会话")
            job = self._jobs[job_id]
            if job["status"] != "running":
                return
            job["status"], job["error"] = "cancelled", "操作者或任务生命周期取消"
            proc = job["process"]
            native_job = job["native_job"]
        _terminate_background(proc, native_job)
        self.wait(job_id, 2)

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.cancel_running()

    def stop_owner(self, owner: Any) -> None:
        """工具失败或超时时，仅撤销该次执行启动的作业。"""
        with self._lock:
            ids = [key for key, job in self._jobs.items() if job["owner"] is owner and job["status"] == "running"]
        for job_id in ids:
            self.stop(job_id)

    def cancel_running(self) -> None:
        with self._lock:
            ids = [key for key, job in self._jobs.items() if job["status"] == "running"]
        for job_id in ids:
            self.stop(job_id)


def _terminate_background(proc: subprocess.Popen, native_job: Any = None) -> None:
    try:
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGKILL)
        elif native_job is not None:
            import ctypes
            ctypes.windll.kernel32.TerminateJobObject(native_job, 1)
        elif proc.poll() is None:
            proc.kill()
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _windows_background_job(proc: subprocess.Popen):
    """Windows Job Object 随任务关闭，包含子孙进程；不依赖 taskkill 或可见窗口。"""
    import ctypes
    from ctypes import wintypes as w

    class Limits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong), ("flags", w.DWORD),
                    ("min_working", ctypes.c_size_t), ("max_working", ctypes.c_size_t), ("active", w.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]
    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", Limits), ("io", ctypes.c_ulonglong * 6),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
    kernel = ctypes.windll.kernel32
    kernel.CreateJobObjectW.argtypes, kernel.CreateJobObjectW.restype = [ctypes.c_void_p, w.LPCWSTR], w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not job:
        raise OSError("无法创建后台进程 Job Object")
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(job, int(proc._handle)):
        kernel.CloseHandle(job)
        if proc.poll() is not None:
            return None
        raise OSError("无法绑定后台进程生命周期")
    return job


@dataclass
class McpCall:
    """一次 MCP stdio 工具调用结果。"""

    ok: bool
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    stderr: str = ""
    command: str = ""
    duration: float = 0.0


class ManagedPipe:
    """任务级双向管道，帧有界；进程组或 Job Object 统一回收子孙进程。"""

    def __init__(self, argv: list[str], ctx: Any, *, delimiter: bytes, tool: str):
        from ..scope import ensure_execution_active
        ensure_execution_active()
        decision = ctx.guard.check_argv(argv, tool=tool, required_level=ctx.effective_level)
        if decision.denied:
            raise ScopeViolation(decision)
        self.frames: queue.Queue = queue.Queue(maxsize=1024)
        self.error = ""
        self.stderr = bytearray()
        self._closed = False
        self._close_lock = threading.Lock()
        self.cleanups: list = []
        self._limit = ctx.config.binary.max_output_bytes
        self._queued = 0
        self._queue_lock = threading.Lock()
        self._stdout_done = threading.Event()
        env = _mcp_environment()
        # 不继承用户插件、预加载库、PYTHONPATH 或宿主配置。
        for key in ("PYTHONPATH", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
            env.pop(key, None)
        env.update({"HOME": str(ctx.workspace), "LC_ALL": "C", "R2_NOPLUGINS": "1"})
        self.proc = subprocess.Popen(argv, cwd=ctx.workspace, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=os.name != "nt",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
        self.native_job = None
        try:
            self.native_job = _windows_background_job(self.proc) if os.name == "nt" else None
            ensure_execution_active()
            ctx.audit.append("binary.process.start", backend=tool, pid=self.proc.pid, argv=argv)
        except BaseException:
            self.close()
            raise

        def read_frames():
            pending = bytearray()
            try:
                while chunk := self.proc.stdout.read1(4096):
                    pending.extend(chunk)
                    if len(pending) > self._limit:
                        raise ValueError("OUTPUT_LIMIT")
                    while delimiter in pending:
                        frame, _, rest = pending.partition(delimiter)
                        pending = bytearray(rest)
                        with self._queue_lock:
                            self._queued += len(frame)
                            if self._queued > self._limit:
                                raise ValueError("OUTPUT_LIMIT")
                        self.frames.put_nowait(bytes(frame))
            except (ValueError, queue.Full, OSError) as exc:
                self.error = f"OUTPUT_LIMIT: {exc}"
                self.close()
            finally:
                if pending and not self.error:
                    try:
                        self.frames.put_nowait(bytes(pending))
                        with self._queue_lock:
                            self._queued += len(pending)
                    except queue.Full:
                        self.error = "OUTPUT_LIMIT"
                self.proc.stdout.close()
                self._stdout_done.set()
        def read_errors():
            try:
                while chunk := self.proc.stderr.read1(1024):
                    self.stderr.extend(chunk)
                    del self.stderr[:-16000]
            finally:
                self.proc.stderr.close()
        self.readers = [threading.Thread(target=read_frames, daemon=True), threading.Thread(target=read_errors, daemon=True)]
        for thread in self.readers:
            thread.start()

    def send(self, command: str) -> None:
        from ..scope import ensure_execution_active
        ensure_execution_active()
        if self._closed or self.proc.poll() is not None:
            raise RuntimeError(self.error or "SESSION_LOST")
        self.proc.stdin.write(command.encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def receive(self, timeout: float) -> str:
        from ..scope import ensure_execution_active
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            ensure_execution_active()
            try:
                frame = self.frames.get(timeout=min(.05, max(.001, end - time.monotonic())))
                with self._queue_lock:
                    self._queued -= len(frame)
                return frame.decode("utf-8", "replace")
            except queue.Empty:
                if (self.proc.poll() is not None and self._stdout_done.is_set()) or self._closed:
                    detail = self.stderr.decode("utf-8", "replace").strip()[:500]
                    if "bwrap:" in detail:
                        raise RuntimeError("SANDBOX_UNAVAILABLE: " + detail)
                    raise RuntimeError(self.error or ("SESSION_LOST: " + detail if detail else "SESSION_LOST"))
        raise TimeoutError("COMMAND_TIMEOUT")

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            _terminate_background(self.proc, self.native_job)
            if self.native_job is not None:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(self.native_job)
            if self.proc.stdin:
                self.proc.stdin.close()
            for cleanup in self.cleanups:
                cleanup()


def binary_sandbox_argv(ctx: Any, argv: list[str], *, tty: str = "") -> list[str]:
    """二进制执行的 OS 边界；bwrap 不可用时失败，不静默降级。"""
    if ctx.config.binary.sandbox == "none":
        return argv
    if os.name == "nt" or not which("bwrap"):
        raise RuntimeError("SANDBOX_UNAVAILABLE: 需要 Linux bubblewrap，或显式设置 binary.sandbox=none")
    # 不挂载宿主 home、/proc 或其他 Worker；只读运行库与当前项目输入。
    command = [which("bwrap"), "--unshare-all", "--die-with-parent", "--new-session", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    from pathlib import Path
    for directory in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc"):
        if Path(directory).exists():
            command += ["--ro-bind", directory, directory]
    command += ["--ro-bind", str(Path.cwd().resolve()), str(Path.cwd().resolve()),
                "--tmpfs", str(ctx.config.workspace_dir.resolve()),
                "--bind", str(ctx.workspace.resolve()), str(ctx.workspace.resolve()),
                "--chdir", str(ctx.workspace.resolve())]
    if tty:
        command += ["--dev-bind", tty, "/dev/trina-input"]
    command += ["--", *argv]
    return command


class InferiorInput:
    """有界文本 stdin 经专属 PTY 提供，不启用 shell 或接管调试器 MI stdin。"""

    def __init__(self, path: str):
        import pty
        import termios
        from pathlib import Path
        data = Path(path).read_bytes()
        if len(data) > 65536 or any(byte < 32 and byte not in (9, 10) for byte in data) or any(len(line) > 2048 for line in data.split(b"\n")):
            raise ValueError("STDIN_LIMIT: 首版调试 stdin 为最多 64KiB 的文本，每行最多 2048B，不含控制字符")
        self.data, self.output = data, bytearray()
        self.master, self.slave = pty.openpty()
        self.path = os.ttyname(self.slave)
        self.closed = threading.Event()
        attrs = termios.tcgetattr(self.slave)
        attrs[0], attrs[1], attrs[3] = 0, 0, termios.ICANON
        attrs[6][termios.VEOF] = b"\x04"
        termios.tcsetattr(self.slave, termios.TCSANOW, attrs)
        os.set_blocking(self.master, False)
        def read():
            while not self.closed.wait(.01):
                try:
                    chunk = os.read(self.master, 1024)
                    self.output.extend(chunk)
                    del self.output[:-16000]
                except BlockingIOError:
                    pass
                except OSError:
                    return
        threading.Thread(target=read, daemon=True).start()

    def feed(self) -> None:
        import termios
        termios.tcflush(self.slave, termios.TCIFLUSH)
        self.output.clear()
        def write():
            remaining = self.data + b"\x04\x04"
            while remaining and not self.closed.wait(.01):
                try:
                    size = os.write(self.master, remaining[:1024])
                    remaining = remaining[size:]
                except BlockingIOError:
                    pass
                except OSError:
                    return
        threading.Thread(target=write, daemon=True).start()

    def close(self) -> None:
        if not self.closed.is_set():
            self.closed.set()
            os.close(self.master)
            os.close(self.slave)


def run_binary_external(argv: list[str], ctx: Any, *, input_data: str, timeout: float) -> ExternalRun:
    """候选执行复用受控管道及进程树清理，标准输入与输出同时排空。"""
    from ..scope import execution_budget
    started = time.monotonic()
    pipe = ManagedPipe(binary_sandbox_argv(ctx, argv), ctx, delimiter=b"\n", tool="binary_execute")
    def feed():
        try:
            pipe.proc.stdin.write(input_data.encode("utf-8"))
            pipe.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                pipe.proc.stdin.close()
            except (OSError, ValueError):
                pass
    threading.Thread(target=feed, daemon=True).start()
    output, error = [], ""
    end = time.monotonic() + execution_budget(timeout)
    try:
        while time.monotonic() < end:
            try:
                output.append(pipe.receive(max(.001, end - time.monotonic())))
                if sum(len(item) for item in output) > ctx.config.binary.max_output_bytes:
                    error = "OUTPUT_LIMIT"
                    break
            except RuntimeError as exc:
                if pipe.proc.poll() is None or pipe.error:
                    error = pipe.error or str(exc)
                break
        else:
            error = "EXECUTION_TIMEOUT"
    except TimeoutError:
        error = "EXECUTION_TIMEOUT"
    finally:
        pipe.close()
    code = pipe.proc.returncode
    return ExternalRun(ok=not error and code == 0, stdout="\n".join(output), stderr=pipe.stderr.decode("utf-8", "replace"),
        code=code, command=" ".join(argv), duration=time.monotonic() - started, error=error, binary=argv[0])


def run_external(
    argv: Sequence[str],
    ctx: Any,
    *,
    tool: str = "",
    target: str = "",
    required_level: str = "detect",
    timeout: float | None = None,
    input_data: str | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    audit: bool = True,
    background: bool = False,
) -> ExternalRun:
    """执行外部二进制。护栏拒绝时抛 `ScopeViolation`（由注册表统一转为拒绝结果）。"""
    argv = [str(a) for a in argv]
    binary = argv[0] if argv else ""
    resolved = which(binary)
    if resolved is None:
        return ExternalRun(ok=False, error=f"未安装外部工具 {binary}", binary=binary, command=" ".join(argv))
    argv = [resolved, *argv[1:]]

    decision = ctx.guard.check_argv(argv, tool=tool or binary, target=target, required_level=required_level)
    if decision.denied:
        raise ScopeViolation(decision)

    host = ""
    if target:
        try:
            from ..scope import parse_target

            host = parse_target(target).host
        except ValueError:
            host = target
    ctx.throttle(host)

    if background:
        jobs = ctx.extra.get("background_jobs")
        if not isinstance(jobs, BackgroundJobs):
            raise ValueError("当前 ToolContext 不支持后台作业")
        budget = min(float(timeout or ctx.config.run.tool_timeout), ctx.config.run.tool_timeout)
        deadline = ctx.extra.get("task_deadline")
        if deadline is not None:
            budget = min(budget, max(.01, deadline - time.monotonic()))
        job_id = jobs.start(argv, cwd=cwd or str(ctx.workspace), env=env if env is not None else _mcp_environment(),
                            timeout=budget, tool=tool or binary, owner=ctx.extra.get("_execution_owner"))
        try:
            ctx.audit.append("external.background.start", tool=tool or binary, job_id=job_id, timeout=budget, target=target)
        except Exception:
            jobs.stop(job_id)
            raise
        return ExternalRun(ok=True, code=None, command=" ".join(argv), binary=binary, job_id=job_id, running=True)

    budget = timeout or ctx.config.run.tool_timeout
    if ctx.remaining() is not None:
        budget = min(budget, max(.001, ctx.remaining()))

    from ..scope import execution_budget
    budget = execution_budget(budget)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            input=input_data,
            capture_output=True,
            text=True,
            timeout=budget,
            check=False,
            errors="replace",
            cwd=cwd,
            env=env,
        )
        result = ExternalRun(
            ok=proc.returncode == 0,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            code=proc.returncode,
            command=" ".join(argv),
            duration=time.monotonic() - started,
            binary=binary,
        )
    except subprocess.TimeoutExpired:
        result = ExternalRun(ok=False, error=f"超时 {budget:.0f}s", command=" ".join(argv), duration=time.monotonic() - started, binary=binary)
    except OSError as exc:
        result = ExternalRun(ok=False, error=str(exc), command=" ".join(argv), duration=time.monotonic() - started, binary=binary)

    if audit:
        digest = hashlib.sha256(result.stdout.encode("utf-8", "replace")).hexdigest()
        ctx.audit.append(
            "external.run",
            tool=tool or binary,
            decision="allow",
            command=result.command,
            code=result.code,
            target=target,
            duration=round(result.duration, 2),
            stdout_digest=digest,
            stdout_bytes=len(result.stdout),
        )
    return result


def run_mcp_stdio(
    argv: Sequence[str],
    tool_name: str,
    arguments: dict[str, Any],
    ctx: Any,
    *,
    target: str = "",
    required_level: str = "detect",
    timeout: float | None = None,
    cwd: str | None = None,
    protocol_version: str = "2025-11-25",
    env: dict[str, str] | None = None,
) -> McpCall:
    """启动一次可取消的 MCP stdio 会话并调用一个工具。

    这是 Capability Fabric 的最小传输边界：每次调用独立启动进程，stdout
    只接受逐行 JSON-RPC，stderr 只作诊断。进程在结果、错误或超时后都会回收，
    不让能力进程脱离 ToolRegistry 的执行生命周期。
    """
    argv = [str(item) for item in argv]
    if not argv:
        return McpCall(ok=False, error="MCP command 不能为空")
    resolved = which(argv[0])
    if resolved is None:
        return McpCall(ok=False, error=f"未安装 MCP 启动程序 {argv[0]}", command=" ".join(argv))
    argv = [resolved, *argv[1:]]
    decision = ctx.guard.check_argv(argv, tool=f"mcp:{tool_name}", target=target, required_level=required_level)
    if decision.denied:
        raise ScopeViolation(decision)

    budget = timeout or ctx.config.run.tool_timeout
    if ctx.remaining() is not None:
        budget = min(budget, max(.1, ctx.remaining()))
    from ..scope import execution_budget

    budget = execution_budget(budget)
    started = time.monotonic()
    stderr_parts: list[str] = []
    lines: "queue.Queue[str | None]" = queue.Queue()
    process_env = _mcp_environment(env)
    creation: dict[str, Any] = {}
    if os.name == "nt":
        creation["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        creation["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=cwd,
            env=process_env,
            **creation,
        )
    except OSError as exc:
        return McpCall(ok=False, error=str(exc), command=" ".join(argv), duration=time.monotonic() - started)

    def read_stdout() -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    def read_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_parts.append(line)
            if sum(map(len, stderr_parts)) > 12_000:
                del stderr_parts[:-20]

    stdout_thread = threading.Thread(target=read_stdout, name="trina-mcp-stdout", daemon=True)
    stderr_thread = threading.Thread(target=read_stderr, name="trina-mcp-stderr", daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    deadline = time.monotonic() + budget

    def send(message: dict[str, Any]) -> None:
        if proc.stdin is None or proc.poll() is not None:
            raise RuntimeError("MCP 进程已退出")
        proc.stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
        proc.stdin.flush()

    def receive(request_id: int) -> dict[str, Any]:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"MCP 调用超时（{budget:.1f}s）")
            try:
                line = lines.get(timeout=min(.2, remaining))
            except queue.Empty:
                if proc.poll() is not None:
                    raise RuntimeError(f"MCP 进程提前退出（code={proc.returncode}）")
                continue
            if line is None:
                raise RuntimeError(f"MCP stdout 已关闭（code={proc.poll()}）")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and message.get("id") == request_id:
                return message

    call_result: dict[str, Any] = {}
    error = ""
    try:
        send({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "trina", "version": __version__},
            },
        })
        initialized = receive(1)
        if initialized.get("error"):
            raise RuntimeError(f"MCP initialize 失败: {initialized['error']}")
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        send({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        })
        response = receive(2)
        if response.get("error"):
            raise RuntimeError(f"MCP tools/call 失败: {response['error']}")
        call_result = response.get("result") if isinstance(response.get("result"), dict) else {}
    except (RuntimeError, TimeoutError, BrokenPipeError, OSError) as exc:
        error = str(exc)
    finally:
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1.0)
        stdout_thread.join(timeout=1.0)
        stderr_thread.join(timeout=1.0)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()

    duration = time.monotonic() - started
    stderr = "".join(stderr_parts)[-4000:]
    ok = not error and not bool(call_result.get("isError"))
    if call_result.get("isError") and not error:
        error = _mcp_text(call_result) or "MCP 工具返回错误"
    digest = hashlib.sha256(json.dumps(call_result, ensure_ascii=False, sort_keys=True).encode("utf-8", "replace")).hexdigest()
    ctx.audit.append(
        "external.mcp",
        tool=tool_name,
        decision="allow",
        command=" ".join(argv),
        target=target,
        ok=ok,
        error=error[:300],
        duration=round(duration, 3),
        output_digest=digest,
    )
    return McpCall(
        ok=ok,
        result=call_result,
        error=error,
        stderr=stderr,
        command=" ".join(argv),
        duration=duration,
    )


def _mcp_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """只继承启动运行时所需环境，避免把宿主 API Key 交给第三方能力。"""
    allowed = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
        "HOME", "USERPROFILE", "LANG", "LC_ALL", "PYTHONPATH", "VIRTUAL_ENV",
        "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH",
    }
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    result["PYTHONUNBUFFERED"] = "1"
    for key, value in (extra or {}).items():
        if not key or "\x00" in key or "\x00" in str(value):
            raise ValueError("MCP 环境变量非法")
        result[str(key)] = str(value)
    return result


def _mcp_text(result: dict[str, Any]) -> str:
    parts = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n".join(part for part in parts if part)


__all__ = ["ExternalRun", "McpCall", "have", "installed", "run_external", "run_mcp_stdio", "which"]
